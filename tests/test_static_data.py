"""Static data after ESI dropped the /universe routes (compatibility date 2026-11-19).

ESI no longer serves types, groups, systems, constellations, stations, planets
or regions. The app reads them from the bundled SDE and, for anything newer
than the bundle, from CCP's SDE API (sde.evetech.net). These tests pin:

  * the order: bundle first (no request at all), SDE API second;
  * the translation of the SDE API's records (camelCase, names per language)
    into the ESI shape every caller reads;
  * that a failure is logged instead of passing for "does not exist";
  * that nothing in the app calls a removed route again.

The record payloads below are trimmed copies of real SDE API answers
(release 20261119T110708Z).
"""
from __future__ import annotations

import ast
import asyncio
import re
import sqlite3
from pathlib import Path

import pytest

from app.esi import static_data as sd

REPO = Path(__file__).resolve().parent.parent

JITA, JITA_44 = 30000142, 60003760
STARKMAN, STARKMAN_PRIME = 30002960, 40187877

RECORDS = {
    "types/587": {"typeID": 587, "groupID": 25, "marketGroupID": 64, "published": True,
                  "name": {"de": "Rifter", "en": "Rifter", "zh": "裂谷级"},
                  "volume": 27289, "packagedVolume": 2500, "portionSize": 1},
    "types/34": {"typeID": 34, "groupID": 18, "marketGroupID": 1857, "published": True,
                 "name": {"en": "Tritanium"}, "volume": 0.01, "portionSize": 1},
    "groups/65": {"groupID": 65, "categoryID": 7, "published": True,
                  "name": {"de": "Stasis Web", "en": "Stasis Web"}},
    "mapSolarSystems/39999999": {"solarSystemID": 39999999, "constellationID": 29999999,
                                 "regionID": 19999999, "securityStatus": 0.4321,
                                 "name": {"en": "Newsystem"}},
    "npcStations/69999999": {"stationID": 69999999, "solarSystemID": JITA,
                             "operationID": 14, "ownerID": 1000035},
    "mapPlanets/40009077": {"planetID": 40009077, "celestialIndex": 1,
                            "solarSystemID": JITA, "typeID": 2016},
    "mapPlanets/40009082": {"planetID": 40009082, "celestialIndex": 4,
                            "solarSystemID": JITA, "typeID": 13},
    f"mapPlanets/{STARKMAN_PRIME}": {
        "planetID": STARKMAN_PRIME, "celestialIndex": 2, "solarSystemID": STARKMAN,
        "uniqueName": {"de": "Starkman Prime", "en": "Starkman Prime"}},
}


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Net:
    """The SDE API and ESI /universe/names, recording every request."""

    def __init__(self, records=RECORDS, fail=(), names=None):
        self.records, self.fail, self.names = records, set(fail), names or {}
        self.calls: list[str] = []

    async def get(self, url, **kw):
        self.calls.append(url)
        assert url.startswith(sd.SDE_API + "/"), url
        assert kw.get("follow_redirects"), "latest/ is a redirect to the release"
        key = url[len(sd.SDE_API) + 1:].removesuffix(".json")
        if key in self.fail:
            return _Resp(503, {})
        rec = self.records.get(key)
        return _Resp(200, rec) if rec else _Resp(404, {"error": "Not found"})

    async def post(self, url, json=None, **kw):
        self.calls.append(url)
        assert url == sd.ESI_NAMES
        if any(i not in self.names for i in json):
            return _Resp(404, {"error": "Ensure all IDs are valid before resolving"})
        return _Resp(200, [{"id": i, "name": self.names[i], "category": "station"} for i in json])


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def bundle():
    """The shipped sde_base.db, read only."""
    c = sqlite3.connect(f"file:{REPO / 'sde_base.db'}?mode=ro", uri=True)
    yield c
    c.close()


# ── types and groups ─────────────────────────────────────────────────────────

def test_a_type_record_reads_like_the_esi_answer_did():
    net = _Net()
    assert _run(sd.fetch_type(net, 587)) == {
        "type_id": 587, "name": "Rifter", "group_id": 25, "market_group_id": 64,
        "published": True, "volume": 27289, "packaged_volume": 2500}
    assert net.calls == [f"{sd.SDE_API}/types/587.json"]


def test_without_a_packaged_volume_the_volume_is_it():
    # ESI answered packaged_volume = volume for anything not repackable. (A type
    # with no volume at all, e.g. 88133, stays None - as in the bundled SDE.)
    assert _run(sd.fetch_type(_Net(), 34))["packaged_volume"] == 0.01


def test_a_missing_type_is_none_and_a_failed_one_is_said_out_loud(capsys):
    assert _run(sd.fetch_type(_Net(), 999999999)) is None
    assert "[static-data]" not in capsys.readouterr().out      # 404 is an answer
    assert _run(sd.fetch_type(_Net(fail={"types/587"}), 587)) is None
    assert "[static-data] types/587 failed" in capsys.readouterr().out


def test_a_group_record():
    assert _run(sd.fetch_group(_Net(), 65)) == {
        "group_id": 65, "name": "Stasis Web", "category_id": 7, "published": True}


# ── systems ──────────────────────────────────────────────────────────────────

def test_a_bundled_system_costs_no_request(bundle):
    net = _Net()
    s = _run(sd.fetch_system(net, JITA, bundle))
    assert s["name"] == "Jita" and s["region_id"] == 10000002
    assert s["constellation_id"] == 20000020
    assert s["security_status"] == pytest.approx(0.945913)    # ESI: 0.94591313...
    assert net.calls == []


def test_a_system_newer_than_the_bundle_is_one_request(bundle):
    """ESI needed system -> constellation -> region; the record has the region."""
    net = _Net()
    s = _run(sd.fetch_system(net, 39999999, bundle))
    assert s == {"system_id": 39999999, "name": "Newsystem", "security_status": 0.4321,
                 "constellation_id": 29999999, "region_id": 19999999}
    assert len(net.calls) == 1


# ── stations ─────────────────────────────────────────────────────────────────

def test_a_bundled_station_costs_no_request(bundle):
    net = _Net()
    st = _run(sd.fetch_station(net, JITA_44, bundle))
    assert st["name"] == "Jita IV - Moon 4 - Caldari Navy Assembly Plant"
    assert (st["system_id"], st["region_id"]) == (JITA, 10000002)
    assert net.calls == []


def test_a_new_station_gets_its_system_from_the_sde_and_its_name_from_esi(bundle):
    """The SDE API's station record has no name; /universe/names (kept on ESI) does."""
    net = _Net(names={69999999: "Jita IX - Brand New Station"})
    st = _run(sd.fetch_station(net, 69999999, bundle))
    assert st == {"station_id": 69999999, "name": "Jita IX - Brand New Station",
                  "system_id": JITA, "region_id": 10000002}


def test_a_name_esi_cannot_resolve_does_not_lose_the_system(bundle):
    st = _run(sd.fetch_station(_Net(names={}), 69999999, bundle))
    assert st["name"] is None and st["system_id"] == JITA


def test_names_are_retried_one_by_one_when_a_batch_has_a_bad_id():
    net = _Net(names={1: "One"})
    assert _run(sd._esi_names(net, [1, 2])) == {1: "One"}


# ── planets and regions ─────────────────────────────────────────────────────

@pytest.mark.parametrize("pid, name", [(40009077, "Jita I"), (40009082, "Jita IV"),
                                       (STARKMAN_PRIME, "Starkman Prime")])
def test_planet_names_match_what_esi_called_them(bundle, pid, name):
    """Checked live against ESI /universe/planets for 49 planets: the name is
    '<system> <roman celestialIndex>' unless the planet has its own."""
    assert _run(sd.fetch_planet_name(_Net(), pid, bundle)) == name


@pytest.mark.parametrize("n, roman", [(1, "I"), (4, "IV"), (9, "IX"), (14, "XIV"),
                                      (19, "XIX"), (40, "XL")])
def test_roman_numerals(n, roman):
    assert sd._roman(n) == roman


def test_regions_come_from_the_bundle_without_wormhole_space(bundle):
    regions = sd.all_regions(bundle)
    ids = {r for r, _ in regions}
    assert 10000002 in ids and all(r < 11000000 for r in ids)
    assert [n for _, n in regions] == sorted(n for _, n in regions)
    assert len(regions) > 60


# ── a type learned at runtime survives an SDE update ────────────────────────

def test_a_type_name_learned_at_runtime_survives_a_bundle_refresh(monkeypatch):
    """type_resolver used to write the name into sde_types only, which a bundle
    refresh replaces wholesale - every SDE update forgot it."""
    from app.db import type_resolver
    from app.web import prices_helper as ph
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sde_types (type_id INTEGER PRIMARY KEY, name TEXT,"
                 " group_id INTEGER, published INTEGER, market_group_id INTEGER,"
                 " volume REAL, packaged_volume REAL)")
    assert _run(type_resolver.resolve_name(conn, 587, _Net())) == "Rifter"
    conn.execute("DELETE FROM sde_types")                  # what a bundle refresh does
    ph._apply_discovered_types(conn, prune=True)
    assert type_resolver.resolve_name_sync(conn, 587) == "Rifter"


# ── nothing calls a removed route ────────────────────────────────────────────

# ESI changelog, compatibility date 2026-11-19: every one of these is_breaking.
REMOVED = ("ancestries", "asteroid_belts", "bloodlines", "categories", "constellations",
           "factions", "graphics", "groups", "moons", "planets", "races", "regions",
           "schematics", "stargates", "stars", "stations", "systems", "types")
_REMOVED_RE = re.compile(r"/universe/(%s)(/|$|\?)" % "|".join(REMOVED))


def _code_strings(path: Path):
    """String literals (f-string parts included) that are not docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant):
            docs.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docs:
            yield node.lineno, node.value


def test_no_code_calls_a_route_esi_removed():
    """/latest/ still answers these even with the new compatibility date, so a
    forgotten call would keep working until CCP retires /latest/ - and then
    fail silently behind an `except`. Comments and docstrings may name them."""
    bad = []
    files = [*REPO.joinpath("app").rglob("*.py"), REPO / "launcher.py",
             *REPO.joinpath("scripts").rglob("*.py")]
    for f in files:
        for line, text in _code_strings(f):
            if _REMOVED_RE.search(text):
                bad.append(f"{f.relative_to(REPO)}:{line}: {text.strip()[:80]}")
    assert not bad, "calls to removed ESI routes:\n" + "\n".join(bad)


def test_the_guard_would_catch_one():
    assert _REMOVED_RE.search("https://esi.evetech.net/latest/universe/types/34/")
    assert _REMOVED_RE.search("/universe/systems/")
    assert _REMOVED_RE.search("/universe/regions")
    assert not _REMOVED_RE.search("/universe/structures/")
    assert not _REMOVED_RE.search("/universe/names/")
    assert not _REMOVED_RE.search("/universe/system_jumps/")


def test_a_record_the_sde_does_not_have_is_not_asked_again_at_once():
    net = _Net()
    assert _run(sd.fetch_type(net, 999999999)) is None
    assert _run(sd.fetch_type(net, 999999999)) is None
    assert len(net.calls) == 1
    # a failure is not remembered: the next look tries again
    net = _Net(fail={"types/587"})
    _run(sd.fetch_type(net, 587))
    _run(sd.fetch_type(net, 587))
    assert len(net.calls) == 2


def test_no_code_uses_the_legacy_latest_prefix():
    """Under /latest/ ESI kept answering routes the compatibility date has
    dropped (the static /universe ones, GET /route) - and GET /route there
    still routed over the gate network from before Fractured Skies. Every call
    goes to the root, where the date we send decides."""
    bad = []
    files = [*REPO.joinpath("app").rglob("*.py"), REPO / "launcher.py",
             *REPO.joinpath("scripts").rglob("*.py")]
    for f in files:
        for line, text in _code_strings(f):
            if "esi.evetech.net/latest" in text or re.match(r"^/latest/", text):
                bad.append(f"{f.relative_to(REPO)}:{line}: {text.strip()[:80]}")
    assert not bad, "\n".join(bad)


def test_the_compatibility_date_is_the_one_without_the_static_routes():
    from app.esi.client import ESI_COMPAT_DATE
    assert ESI_COMPAT_DATE >= "2026-11-19"
