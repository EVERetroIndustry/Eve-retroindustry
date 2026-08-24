"""The public-contract contents reader has to finish.

Reported as a spinner that never stopped: "reading contents for item search -
47,718/47,722" for a day. Three contracts were the whole story. ESI answers them
with HTTP 200 and content-length 0, an empty body is not valid JSON, so the
fetch raised, returned, and recorded nothing at all - leaving them to be tried
again on every pass, for ever.

An answer of "this contract has no readable contents" is a real answer. Anything
that is not written down comes back.
"""
from __future__ import annotations

import asyncio
import sqlite3
import unittest.mock as mock

from app.web import contracts_helper as CH


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    CH.ensure_public_contract_tables(conn)
    for cid in (1, 2, 3, 4):
        conn.execute(
            "INSERT INTO public_contracts (contract_id, type, price, volume, region_id)"
            " VALUES (?,?,?,?,?)", (cid, "item_exchange", 1_000_000.0, 100.0, 10000002))
    conn.commit()
    return conn


class _Resp:
    def __init__(self, status, payload=None, raises=False):
        self.status_code = status
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


def _run(conn, responses: dict[int, _Resp]):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kw):
            cid = int(url.rstrip("/").split("/")[-1])
            return responses[cid]

    with mock.patch.object(CH, "esi_client", lambda **kw: Client()):
        return asyncio.run(CH.fill_public_items(conn, budget=100))


def test_an_empty_200_is_recorded_not_retried():
    """The reported bug: content-length 0 raised on .json() and left no trace."""
    conn = _conn()
    res = _run(conn, {
        1: _Resp(200, [{"type_id": 34, "quantity": 1}]),
        2: _Resp(200, raises=True),          # empty body
        3: _Resp(200, []),                   # valid but empty
        4: _Resp(200, [{"quantity": 1}]),    # no type_id to index
    })
    assert res["remaining"] == 0, "nothing may be left over, or the pass repeats for ever"
    assert res["fetched"] == 1
    assert res["gone"] == 3

    absent = {r[0] for r in conn.execute(
        "SELECT contract_id FROM public_contract_items_absent")}
    assert absent == {2, 3, 4}


def test_a_second_pass_asks_about_nothing():
    conn = _conn()
    _run(conn, {1: _Resp(200, [{"type_id": 34, "quantity": 1}]),
                2: _Resp(200, raises=True),
                3: _Resp(200, []),
                4: _Resp(200, [{"quantity": 1}])})
    asked: list[int] = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kw):
            asked.append(int(url.rstrip("/").split("/")[-1]))
            return _Resp(200, [])

    with mock.patch.object(CH, "esi_client", lambda **kw: Client()):
        res = asyncio.run(CH.fill_public_items(conn, budget=100))
    assert asked == [], "a settled contract must not be asked about again"
    assert res == {"fetched": 0, "gone": 0, "remaining": 0}


def test_a_transient_error_is_retried_not_written_off():
    """A 500 is not an answer - it has to come round again, unlike an empty 200."""
    conn = _conn()
    res = _run(conn, {1: _Resp(500), 2: _Resp(500), 3: _Resp(500), 4: _Resp(500)})
    assert res["remaining"] == 4
    assert conn.execute(
        "SELECT COUNT(*) FROM public_contract_items_absent").fetchone()[0] == 0


def test_a_404_is_still_recorded():
    conn = _conn()
    res = _run(conn, {1: _Resp(404), 2: _Resp(403), 3: _Resp(200, []),
                      4: _Resp(200, [{"type_id": 34, "quantity": 2}])})
    assert res["remaining"] == 0 and res["gone"] == 3


def test_settled_contracts_do_not_count_as_outstanding():
    """The count behind the spinner: a contract with no readable contents is
    done, not pending, or the progress line never reaches its total."""
    conn = _conn()
    _run(conn, {1: _Resp(200, [{"type_id": 34, "quantity": 1}]),
                2: _Resp(200, raises=True),
                3: _Resp(200, []),
                4: _Resp(200, [{"quantity": 1}])})
    st = CH.public_index_status(conn)
    assert st["priced"] == 4
    assert st["with_items"] == 1
    assert st["absent"] == 3
    assert st["priced"] - st["with_items"] - st["absent"] == 0
