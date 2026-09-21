"""What actually sold on alliance contracts, and when.

Prices shows units traded on the market over seven days. There is no equivalent
for contracts anywhere in ESI, but for ALLIANCE contracts the raw material is
there: `/corporations/{id}/contracts/` returns finished ones alongside
outstanding, and every finished one carries `date_completed` (measured: 487 of
487). Public contracts have no such thing - ESI lists only what is still on
offer, so a sale is simply a row that vanished, and nothing distinguishes it
from a contract the issuer withdrew.

Measured across five corporations at the time this was written: 437 contracts
completed in seven days moving 999 billion ISK, 1139 in thirty moving 2.33
trillion.

The history is kept here rather than read from `alliance_contracts`, because
that table is REPLACED on every re-listing and ESI's own window is 29 days - so
anything older would quietly disappear. Stored, it grows for as long as the app
is used, exactly like the wallet journal.

Quantities, not ISK, are reported per item: a bundle sells for one price covering
everything in it, so the ISK cannot honestly be split across forty types, while
the units can. That also matches what the Prices column already means.
"""
from __future__ import annotations

import sqlite3
import time

from app.character.income import _parse_ts


def ensure_sales_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS contract_sales (
            contract_id  INTEGER PRIMARY KEY,
            alliance_id  INTEGER,
            completed_ts REAL NOT NULL,
            type         TEXT,
            price        REAL,
            volume       REAL,
            issuer_id    INTEGER,
            acceptor_id  INTEGER,
            items_read   INTEGER DEFAULT 0,
            location_id  INTEGER,
            location     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_cs_ts ON contract_sales(completed_ts);
        CREATE TABLE IF NOT EXISTS contract_sale_items (
            contract_id INTEGER NOT NULL,
            type_id     INTEGER NOT NULL,
            is_bpc      INTEGER NOT NULL DEFAULT 0,
            quantity    INTEGER NOT NULL,
            PRIMARY KEY (contract_id, type_id, is_bpc)
        );
        CREATE INDEX IF NOT EXISTS idx_csi_type ON contract_sale_items(type_id);
    """)
    # price_series falls back to the location name cache for a sale recorded
    # before the name was known, and on a fresh install that table may not exist
    # yet - so make sure of it here rather than letting the query raise.
    try:
        from app.web.location_resolver import ensure_location_name_table
        ensure_location_name_table(conn)
    except Exception:
        pass

    # Where the trade happened. Added to an existing database rather than only to
    # new ones, and kept here rather than looked up later: alliance_contracts is
    # replaced on every re-listing, so the name would vanish with it.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(contract_sales)")}
    for col, decl in (("location_id", "INTEGER"), ("location", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE contract_sales ADD COLUMN {col} {decl}")
    conn.commit()


def record_sales(conn: sqlite3.Connection, alliance_id: int, contracts: list[dict],
                 loc_names: dict | None = None) -> int:
    """Note every contract that has completed. Returns how many were new.

    IGNORE, not REPLACE: a completed contract never changes again, and re-reading
    the same thirty days must not rewrite history.
    """
    ensure_sales_tables(conn)
    rows = []
    for c in contracts or []:
        if c.get("status") != "finished":
            continue
        ts = _parse_ts(c.get("date_completed")) or _parse_ts(c.get("date_accepted"))
        if not ts or not c.get("contract_id"):
            continue
        loc = c.get("start_location_id") or 0
        rows.append((int(c["contract_id"]), alliance_id, ts, c.get("type") or "",
                     float(c.get("price") or 0.0), float(c.get("volume") or 0.0),
                     c.get("issuer_id") or 0, c.get("acceptor_id") or 0,
                     loc, (loc_names or {}).get(loc, "")))
    if not rows:
        return 0
    before = conn.execute("SELECT COUNT(*) FROM contract_sales").fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO contract_sales (contract_id, alliance_id, completed_ts,"
        " type, price, volume, issuer_id, acceptor_id, location_id, location)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM contract_sales").fetchone()[0] - before


def harvest_items(conn: sqlite3.Connection) -> int:
    """Copy the contents of sold contracts somewhere they will survive.

    `alliance_contract_items` is pruned whenever a contract drops out of ESI's
    thirty-day window, which would take the sales history with it. Contents never
    change, so one copy is enough and it is made as soon as they are read.
    """
    ensure_sales_tables(conn)
    try:
        rows = conn.execute("""
            SELECT i.contract_id, i.type_id, COALESCE(i.is_bpc, 0), SUM(i.quantity)
            FROM alliance_contract_items i
            JOIN contract_sales s ON s.contract_id = i.contract_id
            WHERE s.items_read = 0 AND i.is_included = 1
            GROUP BY i.contract_id, i.type_id, COALESCE(i.is_bpc, 0)""").fetchall()
    except sqlite3.OperationalError:
        return 0
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO contract_sale_items (contract_id, type_id, is_bpc, quantity)"
        " VALUES (?,?,?,?)", rows)
    done = {r[0] for r in rows}
    conn.executemany("UPDATE contract_sales SET items_read = 1 WHERE contract_id = ?",
                     [(cid,) for cid in done])
    conn.commit()
    return len(done)


def backfill_locations(conn: sqlite3.Connection) -> int:
    """Fill in where a sale happened for rows recorded before the column existed.

    Only works while the contract is still in ESI's window and therefore still in
    `alliance_contracts`; older ones keep an empty location rather than a guess.
    """
    ensure_sales_tables(conn)
    try:
        cur = conn.execute("""
            UPDATE contract_sales SET
                location_id = (SELECT c.start_location_id FROM alliance_contracts c
                                WHERE c.contract_id = contract_sales.contract_id),
                location    = (SELECT c.start_name FROM alliance_contracts c
                                WHERE c.contract_id = contract_sales.contract_id)
            WHERE COALESCE(location_id, 0) = 0
              AND EXISTS (SELECT 1 FROM alliance_contracts c
                           WHERE c.contract_id = contract_sales.contract_id)""")
    except sqlite3.OperationalError:
        return 0
    conn.commit()
    return cur.rowcount or 0


def sold_by_type(conn: sqlite3.Connection, days: float, type_ids=None,
                 limit: int = 200) -> list[dict]:
    """Units of each type that changed hands on completed contracts."""
    ensure_sales_tables(conn)
    since = time.time() - days * 86400.0
    where = ["s.completed_ts >= ?"]
    args: list = [since]
    if type_ids:
        ids = [int(t) for t in type_ids]
        where.append("i.type_id IN (%s)" % ",".join("?" * len(ids)))
        args += ids
    sql = (f"SELECT i.type_id, SUM(i.quantity), COUNT(DISTINCT i.contract_id)"
           f" FROM contract_sale_items i JOIN contract_sales s"
           f" ON s.contract_id = i.contract_id WHERE {' AND '.join(where)}"
           f" GROUP BY i.type_id ORDER BY 3 DESC, 2 DESC LIMIT ?")
    return [{"type_id": t, "quantity": q, "contracts": n}
            for t, q, n in conn.execute(sql, (*args, limit))]


def sold_quantities(conn: sqlite3.Connection, type_ids, days: float) -> dict[int, dict]:
    """{type_id: {quantity, contracts}} - for putting a number beside an item."""
    return {r["type_id"]: r for r in sold_by_type(conn, days, type_ids, limit=10000)}


def price_series(conn: sqlite3.Connection, type_id: int, days: float = 90.0) -> list[dict]:
    """Prices actually paid for this item on completed contracts, newest last.

    SINGLE-item contracts only, and that is not a shortcut: a bundle sells for one
    price covering everything in it, so dividing it by the quantity of one line
    would invent a number. It is the same rule the contract appraisal uses to
    decide what a contract says about a type.

    This is the figure the market cannot give for a capital. Measured: Jita sell
    quoted a Thanatos at 2.70b while five contracts sold one for 1.80b to 2.16b.
    """
    ensure_sales_tables(conn)
    since = time.time() - days * 86400.0
    rows = conn.execute("""
        SELECT s.completed_ts, s.price, i.quantity, s.contract_id,
               COALESCE(NULLIF(s.location, ''), l.name, ''), s.location_id
        FROM contract_sales s JOIN contract_sale_items i ON i.contract_id = s.contract_id
        LEFT JOIN location_name_cache l ON l.location_id = s.location_id
        WHERE i.type_id = ? AND s.completed_ts >= ? AND s.price > 0 AND i.quantity > 0
          AND 1 = (SELECT COUNT(*) FROM contract_sale_items x
                    WHERE x.contract_id = s.contract_id)
        ORDER BY s.completed_ts""", (int(type_id), since)).fetchall()
    return [{"t": int(ts), "unit": price / qty, "qty": qty, "price": price,
             "contract_id": cid,
             # A name if we have one anywhere, the id if not - never a blank cell
             # pretending the trade happened nowhere.
             "location": loc or (f"#{lid}" if lid else "")}
            for ts, price, qty, cid, loc, lid in rows]


def sales_summary(conn: sqlite3.Connection, days: float) -> dict:
    """Totals plus, honestly, how much of the window we can actually speak for.

    `items_read` matters: a sale whose contents were never fetched still counts
    as ISK but contributes no units, so a quantity is only as complete as that
    figure says.
    """
    ensure_sales_tables(conn)
    since = time.time() - days * 86400.0
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(price),0), COALESCE(SUM(volume),0),"
        "       COALESCE(SUM(items_read),0)"
        " FROM contract_sales WHERE completed_ts >= ?", (since,)).fetchone()
    oldest = conn.execute("SELECT MIN(completed_ts) FROM contract_sales").fetchone()[0]
    return {"days": days, "contracts": row[0], "isk": row[1], "volume": row[2],
            "with_items": row[3], "oldest": oldest}
