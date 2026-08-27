"""Income across every character: what came in, from what, over any window.

ESI keeps roughly the last 30 days of the wallet journal and nothing older
(measured: one character, 319 entries spanning 2026-07-28 to 2026-08-27, a
single page, cached for an hour). So a question like "what did the whole account
earn in the last 18 hours" can be answered live, but "how did this month compare
with the last" cannot - unless the entries are kept. They are small: ~11 entries
a character a day, so twelve characters produce ~130 rows a day, a few MB a
year.

Keeping them also means every window is a local query. The journal is topped up
whenever something asks for a summary and a character's copy is older than ESI's
own cache, which is why no sync step is needed: opening the dashboard is enough
to keep the history growing.

`journal_id` is stable and unique per entry, so re-reading the same 30 days is
idempotent and a re-sync can never double-count.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

from app.character.wallet import fetch_journal

# ESI caches the journal for an hour (measured: last-modified 10:59:42,
# expires 11:59:42), so asking again sooner cannot return anything new.
JOURNAL_MAX_AGE = 3600.0

# The ref_types worth offering as one click, in the order they matter to someone
# who ratted for an evening. Everything else is still stored and still counted
# under "other" - this list only drives the quick filters.
INCOME_GROUPS: list[tuple[str, str, tuple[str, ...]]] = [
    ("bounty", "Bounty prizes", ("bounty_prizes", "bounty_prize")),
    ("ess", "ESS payouts", ("ess_escrow_transfer",)),
    ("goals", "Daily goals", ("daily_goal_payouts",)),
    ("agents", "Agents and missions",
     ("agent_mission_reward", "agent_mission_time_bonus_reward", "agent_mission_collateral_refunded")),
    ("market", "Market sales", ("market_transaction", "market_escrow", "market_provider_tax")),
    ("industry", "Industry", ("industry_job_tax", "manufacturing", "reaction",
                              "researching_technology", "copying")),
    ("contracts", "Contracts", ("contract_price", "contract_reward",
                                "contract_collateral", "contract_brokers_fee")),
    ("insurance", "Insurance", ("insurance",)),
    ("corp", "Corporation", ("corporate_reward_payout", "corp_account_withdrawal",
                             "player_donation")),
]


def ensure_income_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wallet_journal (
            character_id INTEGER NOT NULL,
            journal_id   INTEGER NOT NULL,
            date_ts      REAL    NOT NULL,
            ref_type     TEXT,
            amount       REAL,
            description  TEXT,
            PRIMARY KEY (character_id, journal_id)
        )""")
    # The window queries are always "since a timestamp", so that is the index.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_journal_ts"
                 " ON wallet_journal(date_ts)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wallet_journal_meta (
            character_id INTEGER PRIMARY KEY,
            fetched_at   REAL,
            entries      INTEGER
        )""")
    conn.commit()


def _parse_ts(value) -> float | None:
    """ESI dates are ISO-8601 UTC ('2026-08-27T09:21:31Z')."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def journal_is_stale(conn: sqlite3.Connection, char_id: int,
                     max_age: float = JOURNAL_MAX_AGE) -> bool:
    ensure_income_tables(conn)
    row = conn.execute("SELECT fetched_at FROM wallet_journal_meta WHERE character_id=?",
                       (char_id,)).fetchone()
    return not row or not row[0] or (time.time() - row[0]) > max_age


def store_journal(conn: sqlite3.Connection, char_id: int, entries: list[dict]) -> int:
    """Insert what is new and leave the rest alone. Returns rows added."""
    ensure_income_tables(conn)
    rows = []
    for e in entries or []:
        jid = e.get("id")
        ts = _parse_ts(e.get("date"))
        if jid is None or ts is None:
            continue
        rows.append((char_id, int(jid), ts, e.get("ref_type"),
                     float(e.get("amount") or 0.0), e.get("description")))
    before = conn.execute("SELECT COUNT(*) FROM wallet_journal WHERE character_id=?",
                          (char_id,)).fetchone()[0]
    if rows:
        conn.executemany(
            "INSERT OR IGNORE INTO wallet_journal"
            " (character_id, journal_id, date_ts, ref_type, amount, description)"
            " VALUES (?,?,?,?,?,?)", rows)
    after = conn.execute("SELECT COUNT(*) FROM wallet_journal WHERE character_id=?",
                         (char_id,)).fetchone()[0]
    conn.execute("INSERT OR REPLACE INTO wallet_journal_meta"
                 " (character_id, fetched_at, entries) VALUES (?,?,?)",
                 (char_id, time.time(), after))
    conn.commit()
    return after - before


async def refresh_journal(client, conn: sqlite3.Connection, char_id: int,
                          token: str, max_age: float = JOURNAL_MAX_AGE) -> int:
    """Top up one character's stored journal. No-op inside ESI's cache window."""
    if not token or not journal_is_stale(conn, char_id, max_age):
        return 0
    entries = await fetch_journal(client, char_id, token)
    return store_journal(conn, char_id, entries)


def _window_start(hours: float) -> float:
    return time.time() - hours * 3600.0


_INTERNAL_SQL = (
    "journal_id IN (SELECT journal_id FROM wallet_journal"
    "               GROUP BY journal_id HAVING COUNT(DISTINCT character_id) > 1)")


def income_summary(conn: sqlite3.Connection, hours: float,
                   char_ids: list[int] | None = None,
                   ref_types: list[str] | None = None,
                   positive_only: bool = True,
                   exclude_internal: bool = True) -> dict:
    """Totals for a rolling window, broken down by character and by ref_type.

    A rolling window on purpose: "the last 18 hours" needs no decision about
    which timezone a day starts in, and EVE's own day boundary (UTC) is not the
    one a player thinks in anyway.

    positive_only is what makes the number an INCOME figure: the journal holds
    both sides, and a day of buying modules would otherwise read as a loss
    against a night of ratting.

    exclude_internal drops money moved BETWEEN the account's own characters.
    One transfer is two journal entries sharing an id - minus for the sender,
    plus for the receiver - so counting the plus side reads as income when
    nothing entered the account. Measured on a real account: 3.08 billion of
    `player_donation` was exactly this. It is detected from the data rather than
    from ref_type, because the same ref_type from an outsider IS income: an id
    present for two of OUR characters means both ends are ours.
    """
    ensure_income_tables(conn)
    since = _window_start(hours)
    where = ["date_ts >= ?"]
    args: list = [since]
    # `is not None`, not truthiness: an EMPTY list means "nothing selected" and
    # must match nothing, while None means "no filter". An empty list is falsy,
    # so a truthiness test silently turned "none ticked" into "all of them".
    if char_ids is not None:
        if not char_ids:
            where.append("0")
        else:
            where.append("character_id IN (%s)" % ",".join("?" * len(char_ids)))
            args += list(char_ids)
    if ref_types is not None:
        if not ref_types:
            where.append("0")
        else:
            where.append("ref_type IN (%s)" % ",".join("?" * len(ref_types)))
            args += list(ref_types)
    if positive_only:
        where.append("amount > 0")
    if exclude_internal:
        where.append("NOT " + _INTERNAL_SQL)
    clause = " AND ".join(where)

    total = conn.execute(
        f"SELECT COALESCE(SUM(amount), 0), COUNT(*) FROM wallet_journal WHERE {clause}",
        args).fetchone()
    by_char = conn.execute(
        f"SELECT character_id, COALESCE(SUM(amount), 0), COUNT(*) FROM wallet_journal"
        f" WHERE {clause} GROUP BY character_id ORDER BY 2 DESC", args).fetchall()
    by_type = conn.execute(
        f"SELECT ref_type, COALESCE(SUM(amount), 0), COUNT(*) FROM wallet_journal"
        f" WHERE {clause} GROUP BY ref_type ORDER BY 2 DESC", args).fetchall()

    # Bounty and ESS together, whatever the filter is: it is the question that
    # started this, and it is the one the tile answers at a glance.
    rat_types = tuple(t for _k, _l, ts in INCOME_GROUPS[:2] for t in ts)
    rat = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM wallet_journal"
        " WHERE date_ts >= ? AND amount > 0 AND NOT " + _INTERNAL_SQL
        + " AND ref_type IN (%s)"
        % ",".join("?" * len(rat_types)), [since, *rat_types]).fetchone()[0]

    internal = conn.execute(
        "SELECT COALESCE(SUM(amount), 0), COUNT(*) FROM wallet_journal"
        " WHERE date_ts >= ? AND amount > 0 AND " + _INTERNAL_SQL, [since]).fetchone()

    return {
        "hours": hours,
        "since": since,
        "internal_excluded": internal[0] if exclude_internal else 0.0,
        "internal_entries": internal[1] if exclude_internal else 0,
        "total": total[0],
        "entries": total[1],
        "bounty_ess": rat,
        "by_character": [{"character_id": c, "total": t, "entries": n}
                         for c, t, n in by_char],
        "by_ref_type": [{"ref_type": r or "unknown", "total": t, "entries": n}
                        for r, t, n in by_type],
    }


def stored_range(conn: sqlite3.Connection) -> dict:
    """How far back the stored history actually goes, so the UI can say so
    rather than implying it can answer for any period."""
    ensure_income_tables(conn)
    row = conn.execute(
        "SELECT MIN(date_ts), MAX(date_ts), COUNT(*) FROM wallet_journal").fetchone()
    return {"oldest": row[0], "newest": row[1], "entries": row[2] or 0}
