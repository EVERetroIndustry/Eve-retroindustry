"""Mining read from the game client's log files: what is read, what is never
read, where each cycle happened, and that a second pass reads only what is new."""
from __future__ import annotations

import os
import sqlite3

import pytest

from app.character import client_logs as L

CID = 900000001
OTHER = 900000002


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE characters (character_id INTEGER, character_name TEXT)")
    conn.executemany("INSERT INTO characters VALUES (?,?)",
                     [(CID, "Test Pilot Alpha"), (OTHER, "Test Pilot Beta")])
    conn.execute("CREATE TABLE sde_systems (system_id INTEGER, name TEXT)")
    conn.executemany("INSERT INTO sde_systems VALUES (?,?)",
                     [(30000001, "AAA-1"), (30000002, "BBB-2")])
    return conn


def _game(lines, listener="Test Pilot Alpha") -> str:
    head = ("------------------------------------------------------------\r\n"
            "  Gamelog\r\n"
            f"  Listener: {listener}\r\n"
            "  Session Started: 2026.10.21 07:00:00\r\n"
            "------------------------------------------------------------\r\n")
    return head + "".join(l + "\r\n" for l in lines)


def _mined(t, n, what, crit=False):
    pre = "Critical mining success! You mined an additional" if crit else "You mined"
    return (f"[ 2026.10.21 {t} ] (mining) <color=0x77ffffff>{pre} <font size=12>"
            f"<color=#ff8dc169>{n}<color=0x77ffffff><font size=10> units of "
            f"<color=0xffffffff><font size=12>{what}")


def _local(entries) -> bytes:
    text = ("﻿\r\n\r\n        ---------------------------------------------------------------\r\n"
            "          Channel Name:    Local\r\n          Listener:        Test Pilot Alpha\r\n"
            "        ---------------------------------------------------------------\r\n\r\n")
    for t, who, msg in entries:
        text += f"﻿[ 2026.10.21 {t} ] {who} > {msg}\r\n"
    return text.encode("utf-16-le")


@pytest.fixture
def logs(tmp_path):
    d = tmp_path / "logs"
    (d / "Gamelogs" / "old").mkdir(parents=True)
    (d / "Chatlogs").mkdir()
    (d / "Chatlogs" / f"Local_20261021_070000_{CID}.txt").write_bytes(_local([
        ("07:00:01", "EVE System", "Channel changed to Local : AAA-1"),
        ("07:05:00", "Some Pilot", "Channel changed to Local : BBB-2 o7"),   # chat, not a system
        ("09:00:00", "EVE System", "Channel changed to Local : BBB-2"),
    ]))
    (d / "Gamelogs" / f"20261021_070000_{CID}.txt").write_text(_game([
        _mined("06:59:00", 5, "Veldspar"),                  # before any known system
        _mined("07:10:00", 1234, "Kylixium"),
        _mined("07:10:00", 100, "Kylixium", crit=True),
        "[ 2026.10.21 07:10:00 ] (mining) Additional <color=#ffff4b>40<color=0x77ffffff> units depleted from asteroid as residue",
        "[ 2026.10.21 07:20:00 ] (notify) Strip Miner II deactivates as it finds the resource it was harvesting a pale shadow of its former glory.",
        _mined("08:00:00", 10, "Kylixium"),                 # 40 min later: a new session
        _mined("09:10:00", 7, "Blue Ice"),                  # after moving to BBB-2
        "[ 2026.10.21 09:20:00 ] (None) Jumping from BBB-2 to AAA-1",
        _mined("09:25:00", 3, "Veldspar"),
    ]), encoding="utf-8")
    return str(d)


def test_cycles_are_placed_in_the_system_and_grouped(logs):
    conn = _db()
    res = L.ingest(conn, logs)
    assert res["mining"] == 8
    rows = conn.execute("SELECT system_id, cycles, units, residue, depleted, types_json"
                        " FROM client_mining_sessions ORDER BY start_ts").fetchall()
    assert rows == [
        (30000001, 2, 1334, 40, 1, '{"Kylixium": 1334}'),
        (30000001, 1, 10, 0, 0, '{"Kylixium": 10}'),
        (30000002, 1, 7, 0, 0, '{"Blue Ice": 7}'),
        (30000001, 1, 3, 0, 0, '{"Veldspar": 3}'),          # placed by the gate jump
    ]
    # The cycle before any known system is not guessed into one.
    assert sum(r[2] for r in rows) == 1354


def test_nothing_anyone_wrote_in_chat_is_stored(logs):
    conn = _db()
    L.ingest(conn, logs)
    names = [r[0] for r in conn.execute(
        "SELECT system_name FROM client_log_location ORDER BY ts")]
    assert names == ["AAA-1", "BBB-2", "AAA-1"], "only EVE System lines and gate jumps"


def test_a_second_pass_reads_only_new_lines(logs):
    conn = _db()
    L.ingest(conn, logs)
    assert L.ingest(conn, logs)["files"] == 0
    path = os.path.join(logs, "Gamelogs", f"20261021_070000_{CID}.txt")
    with open(path, "a", encoding="utf-8") as f:
        f.write(_mined("09:30:00", 50, "Veldspar") + "\r\n")
        f.write(_mined("09:31:00", 60, "Veldspar"))          # still being written
    res = L.ingest(conn, logs)
    assert res["mining"] == 1, "a half-written line waits for the next pass"
    with open(path, "a", encoding="utf-8") as f:
        f.write("\r\n")
    assert L.ingest(conn, logs)["mining"] == 1
    assert conn.execute("SELECT COUNT(*) FROM client_log_mining").fetchone()[0] == 10
    last = conn.execute("SELECT units, types_json FROM client_mining_sessions"
                        " ORDER BY start_ts DESC LIMIT 1").fetchone()
    assert last == (113, '{"Veldspar": 113}')


def test_an_old_file_names_its_character_in_the_header(logs):
    conn = _db()
    (open(os.path.join(logs, "Gamelogs", "old", "20260301_120000.txt"), "w")
     .write(_game([_mined("07:30:00", 9, "Scordite")], listener="Test Pilot Beta")))
    (open(os.path.join(logs, "Gamelogs", "old", "20260301_110000.txt"), "w")
     .write(_game([_mined("07:30:00", 9, "Scordite")], listener="Someone Else")))
    L.ingest(conn, logs)
    chars = {r[0] for r in conn.execute("SELECT character_id FROM client_log_mining")}
    assert chars == {CID, OTHER}, "a character that is not added is not read"


def test_a_fleet_shows_as_one_session(logs):
    conn = _db()
    L.ingest(conn, logs)
    conn.execute("INSERT INTO client_mining_sessions VALUES"
                 " (?, 30000001, ?, ?, 3, 300, 0, 2, '{\"Kylixium\": 300}')",
                 (OTHER, 1792566600.0, 1792567200.0))     # 07:10-07:20 the same day
    got = L.sessions_in(conn, 30000001)
    fleet = next(g for g in got if "Test Pilot Beta" in g["characters"])
    assert fleet["characters"] == ["Test Pilot Alpha", "Test Pilot Beta"]
    assert fleet["units"] == 1634 and fleet["types"] == {"Kylixium": 1634}


def test_a_chosen_folder_must_be_an_eve_logs_folder(client, app_module, tmp_path, monkeypatch):
    from app.auth import token_store
    monkeypatch.setattr(token_store, "CONFIG_PATH", str(tmp_path / "config.json"))
    assert client.post("/api/sov/logs-dir", json={"path": str(tmp_path)}).status_code == 400
    good = tmp_path / "EVE" / "logs"
    (good / "Gamelogs").mkdir(parents=True)
    monkeypatch.setattr(app_module.bg, "auto_update_enabled", lambda: False)
    assert client.post("/api/sov/logs-dir", json={"path": str(good)}).json()["dirs"] == [str(good)]
    assert L.logs_dirs() == [str(good)]
