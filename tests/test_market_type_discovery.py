"""The type catalogue is learned from the market, not only from the static data.

Reported twice, the second time with a name: Extended 'Radiance' Cerebral
Accelerator was missing when Jita prices refreshed. It is not a bug in the
refresh - the item was never in the catalogue to refresh. The app's idea of
"everything tradeable in EVE" came solely from the bundled SDE, and CCP
publishes that on its own schedule, so event boosters and accelerators trade
for weeks before they appear in it.

Measured on a live Jita order book: 19 281 type_ids on offer and **97 of them
entirely unknown** to the SDE - including that accelerator (96544, 41 orders,
cheapest sell 69.79M) and the Akoman attack battlecruiser from the Crimson
Harvest event (95741, a 1.8B buy order).

The order book is the authority on what trades, so the refresh now learns from
it. Discovery is free: those pages are downloaded anyway, and /universe/types/
carries no rate-limit group, so a genuinely new id costs one call, once.
"""
import asyncio
import sqlite3
import time

import pytest

from app.web import prices_helper as ph

ACCEL = 96544
ACCEL_NAME = "Extended 'Radiance' Cerebral Accelerator"


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p


class _Client:
    """Stands in for ESI. Records which type ids were actually asked about."""

    def __init__(self, table, asked):
        self.table, self.asked = table, asked

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        tid = int(url.rstrip("/").rsplit("/", 1)[-1])
        self.asked.append(tid)
        payload = self.table.get(tid)
        return _Resp(payload, 200 if payload else 404)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE sde_types (type_id INTEGER PRIMARY KEY, name TEXT,
                 group_id INTEGER, published INTEGER, market_group_id INTEGER,
                 volume REAL, packaged_volume REAL)""")
    c.execute("INSERT INTO sde_types VALUES (34,'Tritanium',18,1,1857,0.01,0.01)")
    c.commit()
    ph.ensure_discovered_types_table(c)
    yield c
    c.close()


def _stub(monkeypatch, table, asked):
    monkeypatch.setattr(ph, "esi_client", lambda *a, **k: _Client(table, asked))


ESI_TABLE = {
    ACCEL: {"name": ACCEL_NAME, "group_id": 303, "market_group_id": 2487,
            "published": True, "volume": 1.0, "packaged_volume": 1.0},
}


def test_a_traded_type_the_static_data_lacks_is_learned(conn, monkeypatch):
    asked = []
    _stub(monkeypatch, ESI_TABLE, asked)

    added = asyncio.run(ph.discover_market_types(conn, [34, ACCEL]))

    assert added == [ACCEL]
    assert asked == [ACCEL], "only the unknown id should cost a call"
    row = conn.execute(
        "SELECT name, group_id, published, market_group_id FROM sde_types WHERE type_id=?",
        (ACCEL,)).fetchone()
    assert row == (ACCEL_NAME, 303, 1, 2487)


def test_a_learned_type_passes_the_tradeable_filter(conn, monkeypatch):
    """published + a market group come from ESI, so the existing filter lets it
    through on its own - the catalogue was the gap, not the filter."""
    _stub(monkeypatch, ESI_TABLE, [])
    asyncio.run(ph.discover_market_types(conn, [ACCEL]))

    ids = {r[0] for r in conn.execute(
        "SELECT type_id FROM sde_types WHERE published=1 AND market_group_id IS NOT NULL")}
    assert ACCEL in ids


def test_nothing_is_asked_when_the_static_data_already_knows_everything(conn, monkeypatch):
    asked = []
    _stub(monkeypatch, ESI_TABLE, asked)
    assert asyncio.run(ph.discover_market_types(conn, [34])) == []
    assert asked == []


def test_a_type_esi_will_not_describe_is_skipped_not_invented(conn, monkeypatch):
    """404, or a reply with no name: record nothing rather than a blank row."""
    _stub(monkeypatch, {ACCEL: {"name": "   ", "published": True}}, [])
    assert asyncio.run(ph.discover_market_types(conn, [ACCEL, 999999])) == []
    assert conn.execute("SELECT COUNT(*) FROM discovered_types").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sde_types WHERE type_id=?", (ACCEL,)).fetchone()[0] == 0


# ── surviving a static-data update ───────────────────────────────────────────

def test_an_sde_update_does_not_throw_the_discoveries_away(conn, monkeypatch):
    """_refresh_sde_from_bundle DROPs sde_types and rebuilds it from the bundle.
    Without re-applying, every SDE update would silently lose the learned types
    and the item would vanish again until the next full price refresh."""
    _stub(monkeypatch, ESI_TABLE, [])
    asyncio.run(ph.discover_market_types(conn, [ACCEL]))

    conn.execute("DELETE FROM sde_types WHERE type_id=?", (ACCEL,))   # the new bundle still lacks it
    conn.commit()
    assert ph._apply_discovered_types(conn) == 1
    assert conn.execute("SELECT name FROM sde_types WHERE type_id=?", (ACCEL,)).fetchone()[0] == ACCEL_NAME


def test_official_data_supersedes_the_guess(conn, monkeypatch):
    """Once the SDE carries the type, the discovery row goes - CCP's row is the
    better one, and two sources for the same id is how they drift apart."""
    _stub(monkeypatch, ESI_TABLE, [])
    asyncio.run(ph.discover_market_types(conn, [ACCEL]))
    # The bundle now has it, with CCP's own name.
    conn.execute("UPDATE sde_types SET name='Official name' WHERE type_id=?", (ACCEL,))
    conn.commit()

    ph._apply_discovered_types(conn)

    assert conn.execute("SELECT COUNT(*) FROM discovered_types WHERE type_id=?",
                        (ACCEL,)).fetchone()[0] == 0
    assert conn.execute("SELECT name FROM sde_types WHERE type_id=?",
                        (ACCEL,)).fetchone()[0] == "Official name"
    assert conn.execute("SELECT COUNT(*) FROM sde_types WHERE type_id=?",
                        (ACCEL,)).fetchone()[0] == 1, "must not duplicate the row"


def test_the_sde_refresh_puts_the_discoveries_back(app_module, monkeypatch):
    """The unit test above proves _apply_discovered_types restores rows; this
    proves _refresh_sde_from_bundle actually calls it. One missing line there
    and every SDE update quietly loses the learned types again."""
    m = app_module
    called = []
    monkeypatch.setattr(m, "_apply_discovered_types", lambda c: called.append(c) or 0)

    conn = m.get_conn()
    try:
        before = conn.execute("SELECT COUNT(*) FROM sde_types").fetchone()[0]
        # Make the user copy one row short so the refresh has a reason to run.
        # It rebuilds the table from the bundle, so this heals itself.
        victim = conn.execute("SELECT type_id FROM sde_types ORDER BY type_id DESC LIMIT 1").fetchone()[0]
        conn.execute("DELETE FROM sde_types WHERE type_id=?", (victim,))
        conn.commit()

        m._refresh_sde_from_bundle(conn)

        assert called, "_refresh_sde_from_bundle must re-apply the discovered types"
        assert conn.execute("SELECT COUNT(*) FROM sde_types").fetchone()[0] == before
    finally:
        conn.close()
