"""ESI's Expires and ETag, and remembered refusals (app/esi/http_cache.py).

Nothing may be lost: the caller must get exactly what ESI would have given,
only with fewer requests and fewer tokens spent.
"""
from __future__ import annotations

import asyncio
import base64
import email.utils
import json
import time

import httpx
import pytest

from app.esi import client as esi
from app.esi import http_cache

WALLET = "https://esi.evetech.net/latest/characters/900000001/wallet/journal/?page=1"
CORP = "https://esi.evetech.net/latest/corporations/98000001/wallets/"
HISTORY = "https://esi.evetech.net/latest/markets/10000002/history/?type_id=34"


def _jwt(char_id: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": f"CHARACTER:EVE:{char_id}"}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def _http_date(offset: float) -> str:
    return email.utils.formatdate(time.time() + offset, usegmt=True)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("EVE_APP_DIR", str(tmp_path))
    monkeypatch.delenv("EVE_NO_HTTP_CACHE", raising=False)
    monkeypatch.setattr(http_cache, "_CACHE", None)
    esi._ERROR_LIMIT._pause_until = 0.0
    esi._TOKEN_LIMIT._pause_until = {}
    return tmp_path


class _ESI:
    """Scripted network behind the governed transport."""

    def __init__(self, monkeypatch):
        self.answers: list[httpx.Response] = []
        self.sent: list[dict] = []

        async def fake(_self, request):
            self.sent.append({k.lower(): v for k, v in request.headers.items()})
            return self.answers.pop(0)
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake)

    def get(self, url, token=None, **kw):
        async def run():
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            async with esi.esi_client() as c:
                return await c.get(url, headers=headers, **kw)
        return asyncio.run(run())


def _ok(body, etag='"a"', fresh=300, **extra):
    h = {"etag": etag, "expires": _http_date(fresh), "date": _http_date(0),
         "x-pages": "3", **extra}
    return httpx.Response(200, headers=h, content=json.dumps(body).encode())


def test_before_expires_the_stored_answer_is_used_without_a_request(store, monkeypatch):
    net = _ESI(monkeypatch)
    net.answers = [_ok([1, 2])]
    first = net.get(WALLET, _jwt(900000001))
    again = net.get(WALLET, _jwt(900000001))
    assert len(net.sent) == 1
    assert again.json() == first.json() == [1, 2]
    assert again.headers["x-pages"] == "3"          # paging still works


def test_after_expires_a_304_hands_back_the_stored_body(store, monkeypatch):
    net = _ESI(monkeypatch)
    net.answers = [_ok([1, 2], fresh=0),
                   httpx.Response(304, headers={"etag": '"a"', "expires": _http_date(60),
                                                "date": _http_date(0)})]
    net.get(WALLET, _jwt(900000001))
    r = net.get(WALLET, _jwt(900000001))
    assert net.sent[1]["if-none-match"] == '"a"'
    assert r.status_code == 200 and r.json() == [1, 2]
    net.get(WALLET, _jwt(900000001))                 # fresh again after the 304
    assert len(net.sent) == 2


def test_a_changed_answer_replaces_the_stored_one(store, monkeypatch):
    net = _ESI(monkeypatch)
    net.answers = [_ok([1], fresh=0), _ok([1, 9], etag='"b"')]
    net.get(WALLET, _jwt(900000001))
    assert net.get(WALLET, _jwt(900000001)).json() == [1, 9]
    assert net.get(WALLET, _jwt(900000001)).json() == [1, 9]
    assert len(net.sent) == 2


def test_each_character_has_its_own_answer(store, monkeypatch):
    net = _ESI(monkeypatch)
    net.answers = [_ok({"a": 1}), _ok({"b": 2})]
    assert net.get(CORP, _jwt(1)).json() == {"a": 1}
    assert net.get(CORP, _jwt(2)).json() == {"b": 2}
    assert len(net.sent) == 2


def test_a_refusal_is_remembered_until_a_sync_or_login(store, monkeypatch):
    net = _ESI(monkeypatch)
    refused = httpx.Response(403, json={"error": "Character does not have required role(s)"})
    net.answers = [refused, _ok({"ok": 1})]
    assert net.get(CORP, _jwt(1)).status_code == 403
    assert net.get(CORP, _jwt(1)).status_code == 403       # no second error spent
    assert len(net.sent) == 1
    http_cache.forget_refusals(1)
    assert net.get(CORP, _jwt(1)).json() == {"ok": 1}


def test_an_expired_token_is_not_a_refusal(store, monkeypatch):
    net = _ESI(monkeypatch)
    net.answers = [httpx.Response(403, json={"error": "token is expired"}), _ok({"ok": 1})]
    assert net.get(CORP, _jwt(1)).status_code == 403
    assert net.get(CORP, _jwt(1)).json() == {"ok": 1}


def test_market_history_is_left_to_its_own_store_unless_asked(store, monkeypatch):
    net = _ESI(monkeypatch)
    net.answers = [_ok([1]), _ok([1]), _ok([2])]
    net.get(HISTORY)
    net.get(HISTORY)
    assert len(net.sent) == 2
    net.get(HISTORY, extensions={"esi_cache": True})
    assert net.get(HISTORY, extensions={"esi_cache": True}).json() == [2]
    assert len(net.sent) == 3


def test_errors_are_counted_per_route(store, monkeypatch):
    net = _ESI(monkeypatch)
    esi._ERRORS.clear()
    net.answers = [httpx.Response(404, json={"error": "nope"})]
    net.get("https://esi.evetech.net/latest/universe/structures/1000000000001/", _jwt(1))
    assert esi.esi_stats()["errors"] == [
        {"route": "/latest/universe/structures/{}/", "status": 404, "count": 1}]


def test_without_an_app_dir_nothing_is_stored(tmp_path, monkeypatch):
    monkeypatch.delenv("EVE_APP_DIR", raising=False)
    monkeypatch.delenv("EVE_NO_HTTP_CACHE", raising=False)
    net = _ESI(monkeypatch)
    net.answers = [_ok([1]), _ok([1])]
    net.get(WALLET, _jwt(1))
    net.get(WALLET, _jwt(1))
    assert len(net.sent) == 2


def test_a_type_that_is_not_there_is_not_asked_again_the_same_day(store, monkeypatch):
    net = _ESI(monkeypatch)
    net.answers = [httpx.Response(404, json={"error": "Type not found!"})]
    url = "https://esi.evetech.net/latest/universe/types/999999999/"
    assert net.get(url).status_code == 404
    assert net.get(url).status_code == 404
    assert len(net.sent) == 1


def test_other_not_founds_are_not_remembered(store, monkeypatch):
    net = _ESI(monkeypatch)
    net.answers = [httpx.Response(404, json={"error": "x"}), _ok({"ok": 1})]
    assert net.get(WALLET, _jwt(1)).status_code == 404
    assert net.get(WALLET, _jwt(1)).json() == {"ok": 1}


def test_a_whole_order_book_page_is_not_kept(store, monkeypatch):
    net = _ESI(monkeypatch)
    net.answers = [_ok([1]), _ok([1]), _ok([2]), _ok([3])]
    book = "https://esi.evetech.net/latest/markets/10000002/orders/?order_type=all&page=1"
    one = "https://esi.evetech.net/latest/markets/10000002/orders/?type_id=34"
    net.get(book)
    net.get(book)
    assert len(net.sent) == 2
    net.get(one)
    assert net.get(one).json() == [2]
    assert len(net.sent) == 3


def test_the_store_is_private(store, monkeypatch):
    import os
    import stat
    net = _ESI(monkeypatch)
    net.answers = [_ok([1])]
    net.get(WALLET, _jwt(1))
    mode = stat.S_IMODE(os.stat(store / "esi_http_cache.db").st_mode)
    assert mode & 0o077 == 0


def test_a_found_route_is_not_kept_but_no_route_is(store, monkeypatch):
    # A found route's jump count has its own store, which forgets counts when
    # the gate network changes; a copy kept here could bring an old one back.
    net = _ESI(monkeypatch)
    net.answers = [_ok([1, 2, 3]), _ok([1, 3]), httpx.Response(404, json={"error": "No route found"})]
    route = "https://esi.evetech.net/latest/route/30000001/30000003/"
    assert net.get(route).json() == [1, 2, 3]
    assert net.get(route).json() == [1, 3]
    assert len(net.sent) == 2
    nowhere = "https://esi.evetech.net/latest/route/30000001/31000005/"
    assert net.get(nowhere).status_code == 404
    assert net.get(nowhere).status_code == 404
    assert len(net.sent) == 3
