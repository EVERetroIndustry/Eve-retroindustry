"""A type nobody has priced yet must get its price from the search page.

Reported after the Cradle of War patch: "new items are in the game and not in
the app". Two separate causes, and only the first is about static data:

  1. the item does not exist in the user's SDE copy until the app ships a new
     `sde_base.db` - that is a release, not a bug;
  2. once it does exist, `/api/prices/search` finds it, inserts an empty row and
     kicks off `_bg_fetch_prices` - which called a name that does not exist and
     therefore never wrote a price. The row stayed blank until a FULL price
     refresh happened to sweep it up.

Measured on the live market at the time: the YC128 Shifting-Spacetime Crate had
16 Jita orders, cheapest sell 1.5b - a price the app could not show.
"""
import asyncio


def test_background_fetch_writes_a_price(app_module, monkeypatch):
    m = app_module
    captured = {}

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    async def _fake_bulk(client, conn, type_ids, force=False):
        captured["type_ids"] = list(type_ids)
        captured["force"] = force
        conn.execute(
            "INSERT OR REPLACE INTO market_price_cache "
            "(type_id, sell_price, buy_price, cached_at) VALUES (?,?,?,?)",
            (type_ids[0], 1_500_000_000.0, 625_600_000.0, 1.0),
        )
        conn.commit()

    monkeypatch.setattr(m, "esi_client", lambda *a, **k: _Client())
    monkeypatch.setattr("app.market.prices.fetch_jita_prices_bulk", _fake_bulk)

    asyncio.run(m._bg_fetch_prices([97314]))

    assert captured.get("type_ids") == [97314], "the fetcher was never reached"
    assert captured.get("force") is True
    conn = m.get_conn()
    try:
        row = conn.execute(
            "SELECT sell_price FROM market_price_cache WHERE type_id=?", (97314,)
        ).fetchone()
    finally:
        conn.close()
    assert row and row[0] == 1_500_000_000.0


def test_background_fetch_reports_failure_instead_of_swallowing_it(app_module, monkeypatch, capsys):
    """The bug hid for so long because the failure printed nothing. A fetch that
    quietly does nothing looks exactly like an item with no orders."""
    m = app_module

    def _boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(m, "esi_client", _boom)
    asyncio.run(m._bg_fetch_prices([97314]))          # must not propagate
    out = capsys.readouterr().out
    assert "kaboom" in out and "[prices]" in out, out
