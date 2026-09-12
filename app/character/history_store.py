"""The records that let net worth be reconstructed backwards.

Net worth is wallet plus the value of what is owned, and only the wallet half
can be walked back from the journal. The other half needs to know what the pile
CONTAINED in the past, and the only honest way to get there is to know what
entered and left it. ESI records three of those flows and nothing else:

  transactions   what was bought and sold on the market, in kind. Reaches about
                 30 days (measured: 226 rows, 2026-08-12 to 09-12).
  industry jobs  what was manufactured or reacted, so a product can be turned
                 back into the materials it came from. About 90 days (measured:
                 211 jobs, 2026-06-17 to 08-30, 202 of them delivered).
  mining ledger  what came out of the ground, per day and type. About 90 days.

All three are stored here rather than read live, for the same reason the journal
is: ESI's window is short and fixed, ours grows for as long as the app is used.
Everything else that moves items - loot, salvage, PI extraction, contracts,
reprocessing, ships lost - ESI does not record at all, which is why the estimate
built on this carries a band rather than pretending to be exact.

No new scope is needed for any of it: character jobs, the mining ledger and the
wallet (transactions share its scope) were all already being asked for.
"""
from __future__ import annotations

import sqlite3
import time

from app.character.income import _parse_ts

# ESI caches transactions and jobs for an hour and the mining ledger longer;
# asking again sooner cannot return anything new.
TRANSACTIONS_MAX_AGE = 3600.0
JOBS_MAX_AGE = 3600.0
MINING_MAX_AGE = 6 * 3600.0

ESI_BASE = "https://esi.evetech.net/latest"


def ensure_history_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wallet_transactions (
            character_id   INTEGER NOT NULL,
            transaction_id INTEGER NOT NULL,
            date_ts        REAL    NOT NULL,
            type_id        INTEGER NOT NULL,
            quantity       INTEGER NOT NULL,
            unit_price     REAL,
            is_buy         INTEGER,
            PRIMARY KEY (character_id, transaction_id)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_tx_ts"
                 " ON wallet_transactions(date_ts)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mining_ledger (
            character_id    INTEGER NOT NULL,
            day             TEXT    NOT NULL,
            solar_system_id INTEGER NOT NULL,
            type_id         INTEGER NOT NULL,
            quantity        INTEGER NOT NULL,
            PRIMARY KEY (character_id, day, solar_system_id, type_id)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS industry_jobs_done (
            character_id      INTEGER NOT NULL,
            job_id            INTEGER NOT NULL,
            activity_id       INTEGER,
            blueprint_id      INTEGER,
            blueprint_type_id INTEGER,
            product_type_id   INTEGER,
            runs              INTEGER,
            successful_runs   INTEGER,
            end_ts            REAL,
            status            TEXT,
            PRIMARY KEY (character_id, job_id)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_done_ts"
                 " ON industry_jobs_done(end_ts)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS history_meta (
            character_id INTEGER NOT NULL,
            kind         TEXT    NOT NULL,
            fetched_at   REAL,
            rows         INTEGER,
            PRIMARY KEY (character_id, kind)
        )""")
    conn.commit()


def _stale(conn: sqlite3.Connection, char_id: int, kind: str, max_age: float) -> bool:
    ensure_history_tables(conn)
    row = conn.execute("SELECT fetched_at FROM history_meta WHERE character_id=? AND kind=?",
                       (char_id, kind)).fetchone()
    return not row or not row[0] or (time.time() - row[0]) > max_age


def _mark(conn: sqlite3.Connection, char_id: int, kind: str, rows: int) -> None:
    conn.execute("INSERT OR REPLACE INTO history_meta (character_id, kind, fetched_at, rows)"
                 " VALUES (?,?,?,?)", (char_id, kind, time.time(), rows))
    conn.commit()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


# ── transactions ─────────────────────────────────────────────────────────────

def store_transactions(conn: sqlite3.Connection, char_id: int, rows: list[dict]) -> int:
    ensure_history_tables(conn)
    out = []
    for t in rows or []:
        tid, ts = t.get("transaction_id"), _parse_ts(t.get("date"))
        if tid is None or ts is None or not t.get("type_id"):
            continue
        out.append((char_id, int(tid), ts, int(t["type_id"]), int(t.get("quantity") or 0),
                    float(t.get("unit_price") or 0.0), 1 if t.get("is_buy") else 0))
    before = conn.execute("SELECT COUNT(*) FROM wallet_transactions WHERE character_id=?",
                          (char_id,)).fetchone()[0]
    if out:
        conn.executemany(
            "INSERT OR IGNORE INTO wallet_transactions (character_id, transaction_id,"
            " date_ts, type_id, quantity, unit_price, is_buy) VALUES (?,?,?,?,?,?,?)", out)
    after = conn.execute("SELECT COUNT(*) FROM wallet_transactions WHERE character_id=?",
                         (char_id,)).fetchone()[0]
    _mark(conn, char_id, "transactions", after)
    return after - before


async def refresh_transactions(client, conn: sqlite3.Connection, char_id: int,
                               token: str) -> int:
    if not token or not _stale(conn, char_id, "transactions", TRANSACTIONS_MAX_AGE):
        return 0
    r = await client.get(f"{ESI_BASE}/characters/{char_id}/wallet/transactions/",
                         headers=_auth(token), timeout=20)
    if r.status_code != 200:
        return 0
    return store_transactions(conn, char_id, r.json())


# ── mining ───────────────────────────────────────────────────────────────────

def store_mining(conn: sqlite3.Connection, char_id: int, rows: list[dict]) -> int:
    ensure_history_tables(conn)
    out = [(char_id, str(m["date"])[:10], int(m.get("solar_system_id") or 0),
            int(m["type_id"]), int(m.get("quantity") or 0))
           for m in rows or [] if m.get("date") and m.get("type_id")]
    before = conn.execute("SELECT COUNT(*) FROM mining_ledger WHERE character_id=?",
                          (char_id,)).fetchone()[0]
    if out:
        # REPLACE, not IGNORE: a day still in progress is re-read with a bigger
        # quantity, and the newer number is the right one.
        conn.executemany(
            "INSERT OR REPLACE INTO mining_ledger (character_id, day, solar_system_id,"
            " type_id, quantity) VALUES (?,?,?,?,?)", out)
    after = conn.execute("SELECT COUNT(*) FROM mining_ledger WHERE character_id=?",
                         (char_id,)).fetchone()[0]
    _mark(conn, char_id, "mining", after)
    return after - before


async def refresh_mining(client, conn: sqlite3.Connection, char_id: int, token: str) -> int:
    if not token or not _stale(conn, char_id, "mining", MINING_MAX_AGE):
        return 0
    r = await client.get(f"{ESI_BASE}/characters/{char_id}/mining/",
                         headers=_auth(token), timeout=20)
    if r.status_code != 200:
        return 0
    return store_mining(conn, char_id, r.json())


# ── industry jobs ────────────────────────────────────────────────────────────

def store_jobs(conn: sqlite3.Connection, char_id: int, rows: list[dict]) -> int:
    ensure_history_tables(conn)
    out = []
    for j in rows or []:
        jid = j.get("job_id")
        if jid is None:
            continue
        end = _parse_ts(j.get("completed_date") or j.get("end_date"))
        out.append((char_id, int(jid), j.get("activity_id"), j.get("blueprint_id"),
                    j.get("blueprint_type_id"), j.get("product_type_id"),
                    j.get("runs"), j.get("successful_runs"), end, j.get("status")))
    before = conn.execute("SELECT COUNT(*) FROM industry_jobs_done WHERE character_id=?",
                          (char_id,)).fetchone()[0]
    if out:
        # REPLACE: a job seen while active is seen again once delivered, and the
        # delivered row is the one that matters.
        conn.executemany(
            "INSERT OR REPLACE INTO industry_jobs_done (character_id, job_id, activity_id,"
            " blueprint_id, blueprint_type_id, product_type_id, runs, successful_runs,"
            " end_ts, status) VALUES (?,?,?,?,?,?,?,?,?,?)", out)
    after = conn.execute("SELECT COUNT(*) FROM industry_jobs_done WHERE character_id=?",
                         (char_id,)).fetchone()[0]
    _mark(conn, char_id, "jobs", after)
    return after - before


async def refresh_jobs(client, conn: sqlite3.Connection, char_id: int, token: str) -> int:
    if not token or not _stale(conn, char_id, "jobs", JOBS_MAX_AGE):
        return 0
    r = await client.get(f"{ESI_BASE}/characters/{char_id}/industry/jobs/",
                         headers=_auth(token), params={"include_completed": "true"},
                         timeout=20)
    if r.status_code != 200:
        return 0
    return store_jobs(conn, char_id, r.json())


async def refresh_all(client, conn: sqlite3.Connection, char_id: int, token: str) -> dict:
    """Top up everything the estimate reads. Failures are per source: a mining
    ledger that will not answer must not cost us the transactions."""
    out = {}
    for name, fn in (("transactions", refresh_transactions),
                     ("mining", refresh_mining),
                     ("jobs", refresh_jobs)):
        try:
            out[name] = await fn(client, conn, char_id, token)
        except Exception:
            out[name] = 0
    return out


def history_reach(conn: sqlite3.Connection) -> dict:
    """Oldest record of each kind, so the window can say where each correction
    stops applying rather than implying it covers the whole chart."""
    ensure_history_tables(conn)
    tx = conn.execute("SELECT MIN(date_ts), COUNT(*) FROM wallet_transactions").fetchone()
    jb = conn.execute("SELECT MIN(end_ts), COUNT(*) FROM industry_jobs_done"
                      " WHERE status='delivered'").fetchone()
    mn = conn.execute("SELECT MIN(day), COUNT(*) FROM mining_ledger").fetchone()
    return {
        "transactions": {"oldest": tx[0], "rows": tx[1] or 0},
        "jobs":         {"oldest": jb[0], "rows": jb[1] or 0},
        "mining":       {"oldest_day": mn[0], "rows": mn[1] or 0},
    }
