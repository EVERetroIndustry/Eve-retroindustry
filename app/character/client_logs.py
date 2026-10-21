"""Mining read from the game client's own log files.

ESI's mining ledger says what a character mined per DAY and system. The client
writes far more to disk, and nothing else has it:

* Gamelogs (one file per client session, plain text): every mining cycle with
  the time to the second, units and type - "You mined 1 234 units of
  Kylixium", critical successes, residue lost, and an asteroid running out
  ("... deactivates as it finds the resource it was harvesting a pale shadow
  of its former glory"). Gate jumps appear as "Jumping from A to B".
* Chatlogs, Local channel (UTF-16): "Channel changed to Local : <system>" each
  time the character's system changes - which is what places a cycle in a
  system. ONLY those system lines are read; nothing anyone wrote in chat is.

What the logs do not say: that an anomaly is used up or when it returns. A
depleted asteroid is one rock, not a site. So this reports when, where and
what was mined, and claims nothing about respawns.

Files are read incrementally (byte offset per file), so the first pass over
months of logs costs seconds and later passes only the new lines.
"""
from __future__ import annotations

import calendar
import glob
import json
import os
import re
import sqlite3
import time

_LINE = re.compile(r"^\[ (\d{4})\.(\d\d)\.(\d\d) (\d\d):(\d\d):(\d\d) \] \(([^)]*)\) (.*)$")
_TAGS = re.compile(r"<[^>]*>")
_NUM = r"(\d[\d ,. ]*)"
_MINED = re.compile(r"^(?:Critical mining success! )?You mined (?:an additional )?" + _NUM
                    + r" units? of (.+?)\s*$")
_RESIDUE = re.compile(r"^Additional " + _NUM + r" units? depleted from (?:asteroid|.+?) as residue")
_DEPLETED = re.compile(r"deactivates as it finds the resource it was harvesting a pale shadow")
_JUMP = re.compile(r"^Jumping from (.+?) to (.+?)\s*$")
_LOCAL = re.compile(r"^\[ (\d{4})\.(\d\d)\.(\d\d) (\d\d):(\d\d):(\d\d) \] EVE System > "
                    r"Channel changed to Local : (.+?)\s*$")
_LISTENER = re.compile(r"^\s*Listener:\s*(.+?)\s*$")
_FILE_CHAR = re.compile(r"^(?:Local_)?\d{8}_\d{6}_(\d+)\.txt$")

SESSION_GAP = 30 * 60        # cycles further apart than this start a new session


def _ts(m) -> float:
    y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
    return float(calendar.timegm((y, mo, d, h, mi, s)))     # EVE time is UTC


def _int(text: str) -> int:
    return int(re.sub(r"[^\d]", "", text) or 0)


def candidate_dirs() -> list[str]:
    """Where the client keeps its logs: Windows, Wine/Proton prefixes, macOS."""
    home = os.path.expanduser("~")
    pats = [
        os.path.join(home, "Documents", "EVE", "logs"),
        os.path.join(home, "OneDrive", "Documents", "EVE", "logs"),
        os.path.join(home, "OneDrive", "*", "EVE", "logs"),
        os.path.join(home, "Games", "*", "drive_c", "users", "*", "Documents", "EVE", "logs"),
        os.path.join(home, ".wine", "drive_c", "users", "*", "Documents", "EVE", "logs"),
        os.path.join(home, ".steam", "steam", "steamapps", "compatdata", "8500", "pfx",
                     "drive_c", "users", "steamuser", "Documents", "EVE", "logs"),
        os.path.join(home, ".local", "share", "Steam", "steamapps", "compatdata", "8500", "pfx",
                     "drive_c", "users", "steamuser", "Documents", "EVE", "logs"),
    ]
    out = []
    for p in pats:
        for d in sorted(glob.glob(p)):
            if os.path.isdir(os.path.join(d, "Gamelogs")) and d not in out:
                out.append(d)
    return out


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS client_log_files (
            path         TEXT PRIMARY KEY,
            size         INTEGER,
            offset       INTEGER,
            character_id INTEGER
        );
        -- Where a character was from `ts` on, by system name as the client wrote it.
        CREATE TABLE IF NOT EXISTS client_log_location (
            character_id INTEGER,
            ts           REAL,
            system_name  TEXT,
            PRIMARY KEY (character_id, ts, system_name)
        );
        -- kind: mined | residue | depleted. One row per log line.
        CREATE TABLE IF NOT EXISTS client_log_mining (
            character_id INTEGER,
            ts           REAL,
            kind         TEXT,
            type_name    TEXT,
            units        INTEGER,
            path         TEXT,
            line_no      INTEGER,
            PRIMARY KEY (path, line_no)
        );
        CREATE INDEX IF NOT EXISTS idx_clm_char_ts ON client_log_mining(character_id, ts);
        CREATE TABLE IF NOT EXISTS client_mining_sessions (
            character_id INTEGER,
            system_id    INTEGER,
            start_ts     REAL,
            end_ts       REAL,
            cycles       INTEGER,
            units        INTEGER,
            residue      INTEGER,
            depleted     INTEGER,
            types_json   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_cms_system ON client_mining_sessions(system_id, end_ts);
    """)
    conn.commit()


def _char_ids(conn) -> dict[str, int]:
    return {n: cid for cid, n in conn.execute(
        "SELECT character_id, character_name FROM characters")}


def _read_new(path: str, offset: int, encoding: str) -> tuple[list[str], int, int]:
    """Lines added since `offset`, and the new offset. A partly written last
    line is left for the next pass. Returns the line number of the first one."""
    with open(path, "rb") as f:
        head = f.read(offset) if offset else b""
        data = f.read()
    if encoding == "utf-16":
        # Whole UTF-16 code units only; the BOM is consumed with the head.
        cut = data.rfind("\n".encode("utf-16-le"))
        if cut < 0:
            return [], offset, 0
        if cut % 2:
            cut -= 1
        chunk = data[:cut + 2]
        text = chunk.decode("utf-16-le", errors="replace").lstrip("﻿")
        start_line = head.decode("utf-16-le", errors="replace").count("\n") if head else 0
    else:
        cut = data.rfind(b"\n")
        if cut < 0:
            return [], offset, 0
        chunk = data[:cut + 1]
        text = chunk.decode("utf-8", errors="replace")
        start_line = head.count(b"\n")
    return text.splitlines(), offset + len(chunk), start_line


def _file_character(conn, path: str, names: dict[str, int], encoding: str) -> int | None:
    m = _FILE_CHAR.search(os.path.basename(path))
    if m:
        return int(m.group(1))
    # Older client versions named the file by time only; the header says who.
    with open(path, "rb") as f:
        head = f.read(2048)
    text = head.decode("utf-16-le" if encoding == "utf-16" else "utf-8", errors="replace")
    for line in text.splitlines()[:12]:
        lm = _LISTENER.match(line.replace("﻿", ""))
        if lm:
            return names.get(lm.group(1))
    return None


def ingest(conn: sqlite3.Connection, logs_dir: str, report=None) -> dict:
    """Read what was added to the log files since the last pass."""
    ensure_tables(conn)
    report = report or (lambda **kw: None)
    names = _char_ids(conn)
    files = [(p, "utf-8") for p in glob.glob(os.path.join(logs_dir, "Gamelogs", "*.txt"))]
    files += [(p, "utf-8") for p in glob.glob(os.path.join(logs_dir, "Gamelogs", "old", "*.txt"))]
    files += [(p, "utf-16") for p in glob.glob(os.path.join(logs_dir, "Chatlogs", "Local_*.txt"))]
    files += [(p, "utf-16") for p in glob.glob(os.path.join(logs_dir, "Chatlogs", "old", "Local_*.txt"))]
    known = {p: (s, o, c) for p, s, o, c in conn.execute(
        "SELECT path, size, offset, character_id FROM client_log_files")}
    added = {"files": 0, "mining": 0, "locations": 0}
    todo = []
    for path, enc in files:
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        prev = known.get(path)
        if prev and prev[0] == size:
            continue
        offset = prev[1] if prev and size >= prev[1] else 0
        todo.append((path, enc, size, offset, prev[2] if prev else None))
    for i, (path, enc, size, offset, cid) in enumerate(todo):
        if i % 50 == 0:
            report(pct=int(90 * i / max(1, len(todo))), detail=f"{i} / {len(todo)} log files")
        if cid is None:
            cid = _file_character(conn, path, names, enc)
        try:
            lines, new_offset, first = _read_new(path, offset, enc)
        except OSError:
            continue
        if cid:
            if enc == "utf-16":
                rows = []
                for line in lines:
                    m = _LOCAL.match(line.lstrip("﻿"))
                    if m:
                        rows.append((cid, _ts(m), m.group(7)))
                conn.executemany("INSERT OR IGNORE INTO client_log_location VALUES (?,?,?)", rows)
                added["locations"] += len(rows)
            else:
                mrows, lrows = [], []
                for n, line in enumerate(lines, start=first):
                    m = _LINE.match(line)
                    if not m:
                        continue
                    cat, msg = m.group(7), _TAGS.sub("", m.group(8))
                    if cat == "mining":
                        mm = _MINED.match(msg)
                        if mm:
                            mrows.append((cid, _ts(m), "mined", mm.group(2), _int(mm.group(1)), path, n))
                            continue
                        mr = _RESIDUE.match(msg)
                        if mr:
                            mrows.append((cid, _ts(m), "residue", None, _int(mr.group(1)), path, n))
                    elif cat == "notify" and _DEPLETED.search(msg):
                        mrows.append((cid, _ts(m), "depleted", None, 0, path, n))
                    elif cat == "None":
                        jm = _JUMP.match(msg)
                        if jm:
                            lrows.append((cid, _ts(m), jm.group(2)))
                conn.executemany("INSERT OR IGNORE INTO client_log_mining VALUES (?,?,?,?,?,?,?)",
                                 mrows)
                conn.executemany("INSERT OR IGNORE INTO client_log_location VALUES (?,?,?)", lrows)
                added["mining"] += len(mrows)
                added["locations"] += len(lrows)
        conn.execute("INSERT OR REPLACE INTO client_log_files VALUES (?,?,?,?)",
                     (path, size, new_offset, cid))
        added["files"] += 1
    conn.commit()
    report(pct=95, detail="sessions")
    added["sessions"] = rebuild_sessions(conn)
    return added


def rebuild_sessions(conn: sqlite3.Connection) -> int:
    """Group mining lines into sessions per character and system.

    A line belongs to the system the character was last seen in - by Local
    or by a gate jump - before it. A line with no known system before it is
    left out rather than guessed into one.
    """
    ensure_tables(conn)
    sys_id = {n: s for s, n in conn.execute("SELECT system_id, name FROM sde_systems") if n}
    locs: dict[int, list[tuple[float, int | None]]] = {}
    for cid, ts, name in conn.execute(
            "SELECT character_id, ts, system_name FROM client_log_location ORDER BY character_id, ts"):
        locs.setdefault(cid, []).append((ts, sys_id.get(name)))
    sessions = []
    cur = None
    import bisect
    for cid, ts, kind, tname, units in conn.execute(
            "SELECT character_id, ts, kind, type_name, units FROM client_log_mining"
            " ORDER BY character_id, ts"):
        tl = locs.get(cid) or []
        i = bisect.bisect_right(tl, (ts, float("inf"))) - 1
        system = tl[i][1] if i >= 0 else None
        if system is None:
            continue
        if (cur is None or cur["character_id"] != cid or cur["system_id"] != system
                or ts - cur["end_ts"] > SESSION_GAP):
            cur = {"character_id": cid, "system_id": system, "start_ts": ts, "end_ts": ts,
                   "cycles": 0, "units": 0, "residue": 0, "depleted": 0, "types": {}}
            sessions.append(cur)
        cur["end_ts"] = ts
        if kind == "mined":
            cur["cycles"] += 1
            cur["units"] += units
            cur["types"][tname] = cur["types"].get(tname, 0) + units
        elif kind == "residue":
            cur["residue"] += units
        elif kind == "depleted":
            cur["depleted"] += 1
    conn.execute("DELETE FROM client_mining_sessions")
    conn.executemany(
        "INSERT INTO client_mining_sessions VALUES (?,?,?,?,?,?,?,?,?)",
        [(s["character_id"], s["system_id"], s["start_ts"], s["end_ts"], s["cycles"],
          s["units"], s["residue"], s["depleted"], json.dumps(s["types"]))
         for s in sessions if s["cycles"]])
    conn.commit()
    return sum(1 for s in sessions if s["cycles"])


def last_mined(conn: sqlite3.Connection) -> dict[int, float]:
    """system_id -> when the last logged mining there ended."""
    try:
        return dict(conn.execute(
            "SELECT system_id, MAX(end_ts) FROM client_mining_sessions GROUP BY system_id"))
    except sqlite3.OperationalError:
        return {}


def sessions_in(conn: sqlite3.Connection, system_id: int, limit: int = 15) -> list[dict]:
    """Recent mining in one system, sessions of all characters merged when they
    overlap (a fleet mines together)."""
    try:
        rows = conn.execute(
            "SELECT s.character_id, c.character_name, s.start_ts, s.end_ts, s.cycles, s.units,"
            "       s.residue, s.depleted, s.types_json"
            "  FROM client_mining_sessions s"
            "  LEFT JOIN characters c ON c.character_id = s.character_id"
            " WHERE s.system_id=? ORDER BY s.start_ts", (system_id,)).fetchall()
    except sqlite3.OperationalError:
        return []
    merged: list[dict] = []
    for cid, cname, a, b, cyc, units, res, dep, types in rows:
        if merged and a <= merged[-1]["end_ts"] + SESSION_GAP:
            g = merged[-1]
            g["end_ts"] = max(g["end_ts"], b)
        else:
            g = {"start_ts": a, "end_ts": b, "characters": [], "cycles": 0, "units": 0,
                 "residue": 0, "depleted": 0, "types": {}}
            merged.append(g)
        if (cname or str(cid)) not in g["characters"]:
            g["characters"].append(cname or str(cid))
        g["cycles"] += cyc
        g["units"] += units
        g["residue"] += res
        g["depleted"] += dep
        for k, v in json.loads(types or "{}").items():
            g["types"][k] = g["types"].get(k, 0) + v
    return list(reversed(merged))[:limit]


CONFIG_KEY = "client_logs_dir"


def logs_dirs() -> list[str]:
    """The folder set by the user, else every one found in the usual places."""
    from app.auth.token_store import _load_json
    chosen = (_load_json().get(CONFIG_KEY) or "").strip()
    if chosen:
        return [chosen] if os.path.isdir(os.path.join(chosen, "Gamelogs")) else []
    return candidate_dirs()


def set_logs_dir(path: str) -> bool:
    """Remember a folder (empty = search the usual places). False if it is not
    an EVE logs folder."""
    from app.auth.token_store import _load_json, _save_json
    path = (path or "").strip()
    if path and not os.path.isdir(os.path.join(path, "Gamelogs")):
        return False
    data = _load_json()
    data[CONFIG_KEY] = path
    _save_json(data)
    return True
