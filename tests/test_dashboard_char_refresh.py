"""The Refresh icon on a character card refreshes THAT character.

It used to refresh the live data of every character at once, while sitting on
one card, and it never touched the portrait (cached for 30 days on disk and 30
more in the browser) or the corporation (only learned at login or asset sync).
"""
from __future__ import annotations

import os
import time

import pytest

ALPHA, BETA = 900000001, 900000002


@pytest.fixture
def fresh_dash(app_module):
    from app.web.page_cache import drop_cached

    def _clear():
        conn = app_module.get_conn()
        try:
            drop_cached(conn, app_module._DASH_LIVE_KIND)
            drop_cached(conn, app_module._CORP_CHECK_KIND)
        finally:
            conn.close()
        app_module._DASH_FLIGHT[0] = None
        app_module._DASH_CHAR_FLIGHT.clear()

    _clear()
    yield
    _clear()


def test_the_card_icon_asks_esi_about_its_own_character_only(client, app_module,
                                                             fresh_dash, monkeypatch):
    client.get("/api/dashboard/live")            # the full set the card merges into

    asked = []
    orig = app_module.fetch_location

    async def _loc(c, cid, tok):
        asked.append(cid)
        return await orig(c, cid, tok)
    monkeypatch.setattr(app_module, "fetch_location", _loc)

    d = client.get(f"/api/dashboard/live?char={BETA}").json()
    assert asked == [BETA]
    assert d.get("age_s") == 0
    # Both cards are still there, and the totals still cover both characters.
    assert set(d["chars"]) == {str(ALPHA), str(BETA)}
    assert d["agg_wallet_str"] and d["chars"][str(ALPHA)]["wallet_str"]


def test_without_a_stored_set_it_falls_back_to_the_full_refresh(client, fresh_dash):
    d = client.get(f"/api/dashboard/live?char={ALPHA}").json()
    assert d.get("logged_in") is True
    assert set(d["chars"]) == {str(ALPHA), str(BETA)}


def test_a_changed_corporation_is_picked_up(client, app_module, fresh_dash, monkeypatch):
    client.get("/api/dashboard/live")

    async def _corp(c, cid):
        return 98000777 if cid == ALPHA else None
    monkeypatch.setattr(app_module, "_fetch_character_corp", _corp)

    conn = app_module.get_conn()
    try:
        client.get(f"/api/dashboard/live?char={ALPHA}")
        assert app_module.get_character_row(conn, ALPHA)["corporation_id"] == 98000777
        # unknown is not "no corporation": Beta keeps what it had
        assert app_module.get_character_row(conn, BETA)["corporation_id"] == 98000001
    finally:
        app_module.update_corporation_id(conn, ALPHA, 98000001)
        conn.close()


def test_the_full_refresh_checks_corporations_only_now_and_then(app_module, monkeypatch):
    import asyncio
    from app.web.page_cache import drop_cached

    asked = []

    async def _corp(c, cid):
        asked.append(cid)
        return 98000001
    monkeypatch.setattr(app_module, "_fetch_character_corp", _corp)

    conn = app_module.get_conn()
    try:
        drop_cached(conn, app_module._CORP_CHECK_KIND)
        rows = {ALPHA: {"corporation_id": 98000001}}
        asyncio.run(app_module._refresh_corp_ids(conn, rows, [ALPHA], force=False))
        asyncio.run(app_module._refresh_corp_ids(conn, rows, [ALPHA], force=False))
        assert asked == [ALPHA]
        asyncio.run(app_module._refresh_corp_ids(conn, rows, [ALPHA], force=True))
        assert asked == [ALPHA, ALPHA]
    finally:
        drop_cached(conn, app_module._CORP_CHECK_KIND)
        conn.close()


def test_merge_redoes_the_totals_and_refuses_an_old_payload(app_module):
    card = lambda w, v: {"wallet_isk": w, "worth_isk": v, "wallet_str": "x"}
    stored = {"logged_in": True, "stale": True, "age_s": 99,
              "chars": {"1": card(100.0, 300.0), "2": card(50.0, 60.0)}}
    out = app_module._dash_merge_char(stored, 2, card(10.0, 20.0))
    assert out["agg_wallet_str"] == app_module._isk0(110.0)
    assert out["agg_value_str"] == app_module._isk0(320.0)
    assert "stale" not in out and "age_s" not in out

    old = {"logged_in": True, "chars": {"1": {"wallet_str": "x"}}}
    assert app_module._dash_merge_char(old, 2, card(1.0, 1.0)) is None
    assert app_module._dash_merge_char(None, 2, card(1.0, 1.0)) is None


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


@pytest.fixture
def image_server(app_module, monkeypatch):
    asked = []

    class _R:
        status_code = 200
        content = JPEG

    class _Client:
        async def get(self, url, **kw):
            asked.append(url)
            return _R()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(app_module, "esi_client", lambda *a, **k: _Client())
    return asked


def test_portrait_refresh_skips_the_ttl_and_replaces_the_old_copy(client, app_module,
                                                                  image_server):
    eid = 900000555
    variant = "characters-portrait"
    os.makedirs(app_module._ICON_DIR, exist_ok=True)
    old = app_module._icon_base(eid, 64, variant) + ".png"
    with open(old, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    # a fresh-enough copy is older than the once-a-minute guard
    t = time.time() - 120
    os.utime(old, (t, t))

    r = client.get(f"/portrait/characters/{eid}?size=64")
    assert r.status_code == 200 and image_server == []
    assert r.headers["cache-control"] == f"public, max-age={app_module._PORTRAIT_BROWSER_TTL}"

    r = client.get(f"/portrait/characters/{eid}?size=64&refresh=1")
    assert r.status_code == 200 and len(image_server) == 1
    assert not os.path.exists(old), "the png would keep winning over the new jpg"
    assert app_module._cached_icon(eid, 64, variant)[0].endswith(".jpg")

    # a second click right away is served from disk
    client.get(f"/portrait/characters/{eid}?size=64&refresh=1")
    assert len(image_server) == 1
