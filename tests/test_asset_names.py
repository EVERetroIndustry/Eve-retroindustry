"""Custom names of ships and containers survive an ESI hiccup, and the page
says when they are not fresh."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

URL = "https://esi.evetech.net/latest/characters/1/assets/names/"


def _esi(monkeypatch, m, answers):
    """answers: list of (status, body) served in order, the last one repeated."""
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        status, body = answers[min(len(calls), len(answers)) - 1]
        return httpx.Response(status, json=body)
    monkeypatch.setattr(m, "esi_client",
                        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def no_wait(_s):
        return None
    monkeypatch.setattr(m.asyncio, "sleep", no_wait)
    return calls


@pytest.fixture
def m(app_module):
    conn = app_module.get_conn()
    app_module._ensure_asset_name_cache(conn)
    conn.execute("DELETE FROM asset_name_cache")
    conn.commit()
    conn.close()
    return app_module


def test_a_timeout_is_retried_once(m, monkeypatch):
    calls = _esi(monkeypatch, m, [(504, {"error": "Timeout contacting tranquility"}),
                                  (200, [{"item_id": 5, "name": "Blue Thunder"}])])
    problems = []
    assert asyncio.run(m._fetch_asset_names(URL, "tok", [5], problems)) == {5: "Blue Thunder"}
    assert len(calls) == 2 and problems == []


def test_when_esi_keeps_failing_the_last_names_are_used_and_said(m, monkeypatch):
    _esi(monkeypatch, m, [(200, [{"item_id": 5, "name": "Blue Thunder"}])])
    asyncio.run(m._fetch_asset_names(URL, "tok", [5, 6]))
    _esi(monkeypatch, m, [(504, {"error": "Timeout contacting tranquility"})])
    problems = []
    got = asyncio.run(m._fetch_asset_names(URL, "tok", [5, 6], problems))
    assert got == {5: "Blue Thunder"}, "6 was never named: it shows its type"
    assert problems == [2]


def test_a_refusal_is_not_retried(m, monkeypatch):
    calls = _esi(monkeypatch, m, [(403, {"error": "forbidden"})])
    problems = []
    asyncio.run(m._fetch_asset_names(URL, "tok", [5], problems))
    assert len(calls) == 1 and problems == [1]


def test_more_than_a_thousand_ids_go_in_batches(m, monkeypatch):
    calls = _esi(monkeypatch, m, [(200, [])])
    asyncio.run(m._fetch_asset_names(URL, "tok", list(range(1, 2502))))
    assert [len(c) for c in calls] == [1000, 1000, 501]


def test_the_assets_page_says_names_are_not_fresh(client, m, monkeypatch):
    async def fake(char_id, token, container_ids, assets_raw, problems=None):
        if problems is not None:
            problems.append(3)
        return {}
    monkeypatch.setattr(m, "_resolve_container_names", fake)
    # Any container makes the page ask for names.
    conn = m.get_conn()
    raw = conn.execute("SELECT data_json FROM char_assets_cache WHERE character_id=900000001"
                       ).fetchone()[0]
    items = json.loads(raw)
    items.append({"item_id": 777001, "type_id": 641, "quantity": 1, "location_id": 60003760,
                  "location_flag": "Hangar", "is_singleton": True})
    items.append({"item_id": 777002, "type_id": 34, "quantity": 5, "location_id": 777001,
                  "location_flag": "Cargo", "is_singleton": False})
    conn.execute("UPDATE char_assets_cache SET data_json=? WHERE character_id=900000001",
                 (json.dumps(items),))
    conn.commit()
    try:
        html = client.get("/assets").text
        assert "ESI did not answer when asked for custom names (3 items)" in html
    finally:
        conn.execute("UPDATE char_assets_cache SET data_json=? WHERE character_id=900000001",
                     (raw,))
        conn.commit()
        conn.close()
