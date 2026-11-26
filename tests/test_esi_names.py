"""POST /universe/names refuses some valid ids, and one refused id fails the batch.

Measured 2026-11-26 on the issuers of 15 378 public contracts: 2 batches of 16
came back 404 {"error": "Ensure all IDs are valid before resolving."}, because
of 2113215724 "Andy Bach" and 2115852767 "NotAlvin" - two active characters
that /characters/{id} and /universe/ids know. Every caller dropped the whole
batch, so one such issuer turned a full Contracts page of issuers into numbers.
"""
from __future__ import annotations

import asyncio
import ast
import sqlite3
import time
from pathlib import Path

import pytest

from app.esi import names

REPO = Path(__file__).resolve().parent.parent
ANDY, ALVIN = 2113215724, 2115852767


@pytest.fixture(autouse=True)
def _fresh_memory():
    names._refused.clear()
    yield
    names._refused.clear()


class _Resp:
    def __init__(self, status, payload, errors_left=100):
        self.status_code, self._p = status, payload
        self.headers = {"x-esi-error-limit-remain": str(errors_left)}

    def json(self):
        return self._p


class _Esi:
    """ESI as measured: /universe/names answers for the batch or not at all and
    refuses `refused`; /characters/{id} still names them. Each 404 costs one
    from the error limit."""

    def __init__(self, known, refused=(), errors_left=100, names_down=False):
        self.known, self.refused = dict(known), set(refused)
        self.errors_left, self.names_down = errors_left, names_down
        self.posts: list[list[int]] = []
        self.gets: list[str] = []

    async def post(self, url, json=None, **kw):
        assert url == names.NAMES_URL
        self.posts.append(list(json))
        if self.names_down:
            return _Resp(503, {"error": "down"})
        if any(i in self.refused or i not in self.known for i in json):
            self.errors_left -= 1
            return _Resp(404, {"error": "Ensure all IDs are valid before resolving."},
                         self.errors_left)
        return _Resp(200, [{"id": i, "name": self.known[i][0], "category": self.known[i][1]}
                           for i in json], self.errors_left)

    async def get(self, url, **kw):
        self.gets.append(url)
        pid = int(url.rstrip("/").rsplit("/", 1)[-1])
        kind = url.split("/")[-3]
        if pid in self.known and self.known[pid][1] + "s" == kind:
            return _Resp(200, {"name": self.known[pid][0]})
        self.errors_left -= 1
        return _Resp(404, {"error": "not found"}, self.errors_left)


def _issuers(n=999):
    return {2120000000 + i: (f"Pilot {i}", "character") for i in range(n)}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


def test_one_refused_character_does_not_blank_the_page(conn):
    known = {**_issuers(), ANDY: ("Andy Bach", "character")}
    esi = _Esi(known, refused={ANDY})
    got = _run(names.resolve_names(esi, known, conn))
    assert got == {pid: v[0] for pid, v in known.items()}
    assert esi.gets == [f"{names.ESI}/characters/{ANDY}/"], "named by its own route"
    assert 100 - esi.errors_left <= 12, \
        "splitting 1000 ids down to one costs about ten errors, not a thousand"


def test_the_next_page_asks_nothing(conn):
    known = {**_issuers(50), ANDY: ("Andy Bach", "character")}
    _run(names.resolve_names(_Esi(known, refused={ANDY}), known, conn))
    again = _Esi(known, refused={ANDY})
    assert _run(names.resolve_names(again, known, conn))[ANDY] == "Andy Bach"
    assert again.posts == [] and again.gets == []


def test_a_refused_id_is_not_sent_to_universe_names_again(conn):
    known = {**_issuers(10), ANDY: ("Andy Bach", "character"), ALVIN: ("NotAlvin", "character")}
    _run(names.resolve_names(_Esi(known, refused={ANDY, ALVIN}), known, conn))
    conn.execute("DELETE FROM party_name_cache")        # even with nothing cached
    again = _Esi(known, refused={ANDY, ALVIN})
    got = _run(names.resolve_names(again, [ANDY, ALVIN, 2120000001], conn))
    assert got == {ANDY: "Andy Bach", ALVIN: "NotAlvin", 2120000001: "Pilot 1"}
    assert again.posts == [[2120000001]]


def test_categories_come_back_for_alliance_contracts(conn):
    known = {99007969: ("THE RUSSIA", "alliance"), 98529693: ("Russian Federation Co", "corporation")}
    cats: dict[int, str] = {}
    _run(names.resolve_names(_Esi(known), known, conn, cats))
    assert cats == {99007969: "alliance", 98529693: "corporation"}
    cats2: dict[int, str] = {}
    _run(names.resolve_names(_Esi({}), known, conn, cats2))     # from the cache this time
    assert cats2 == cats


def test_an_old_name_beats_a_number_when_esi_is_down(conn):
    known = {2120000001: ("Pilot 1", "character")}
    _run(names.resolve_names(_Esi(known), known, conn))
    conn.execute("UPDATE party_name_cache SET cached_at = ?", (time.time() - 30 * 86400,))
    down = _Esi(known, names_down=True)
    assert _run(names.resolve_names(down, known, conn)) == {2120000001: "Pilot 1"}
    assert len(down.posts) == 1, "a 503 is not split: it is not about the ids"


def test_a_week_old_name_is_asked_again(conn):
    """Characters can be renamed; a cached name is trusted for a week."""
    _run(names.resolve_names(_Esi({2120000001: ("Old Name", "character")}), [2120000001], conn))
    conn.execute("UPDATE party_name_cache SET cached_at = ?", (time.time() - 8 * 86400,))
    esi = _Esi({2120000001: ("New Name", "character")})
    assert _run(names.resolve_names(esi, [2120000001], conn)) == {2120000001: "New Name"}


def test_splitting_stops_while_the_error_limit_is_low(conn):
    known = {**_issuers(), ANDY: ("Andy Bach", "character")}
    esi = _Esi(known, refused={ANDY}, errors_left=names._KEEP_ERRORS)
    _run(names.resolve_names(esi, known, conn))
    assert len(esi.posts) == 1, "no splitting on the last of the error budget"


def test_a_refused_station_is_not_looked_up_as_a_character(conn):
    """Stations and systems have no ESI route of their own any more."""
    esi = _Esi({}, refused={60003760})
    assert _run(names.resolve_names(esi, [60003760], conn)) == {}
    assert esi.gets == []


def test_structures_and_junk_are_not_sent():
    esi = _Esi({})
    assert _run(names.resolve_names(esi, [0, None, 1_035_466_617_946], None)) == {}
    assert esi.posts == []


def test_works_without_a_database():
    known = {2120000001: ("Pilot 1", "character")}
    assert _run(names.resolve_names(_Esi(known), known, None)) == {2120000001: "Pilot 1"}


def test_nobody_posts_to_universe_names_directly():
    """Every caller goes through app.esi.names; a bare POST is how the whole-batch
    failure came back four times."""
    bad = []
    for f in (REPO / "app").rglob("*.py"):
        if f.name == "names.py" and f.parent.name == "esi":
            continue
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "post" \
                    and node.args and any(k in ast.unparse(node.args[0])
                                      for k in ("universe/names", "NAMES_URL", "ESI_NAMES")):
                bad.append(f"{f.relative_to(REPO)}:{node.lineno}")
    assert not bad, bad
