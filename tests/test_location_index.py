"""Every kind of space in New Eden has to be findable in the custom-station box.

This exists because a gap here is invisible: ESI's removed /search/ endpoint
answered 404, which the app read as "no such place", and NPC nullsec looked
uncovered with nothing in the logs to say why. The index now ships with the app,
so coverage is testable offline - and these cases are the ones a person would
actually type.

A system with no NPC station (1DQ1-A, a wormhole, Niarja) is not a gap: there is
genuinely nothing there but player structures, which are covered separately by
the public-structure cache and by the character's own docking access.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

# (typed query, space type, a name that must be among the results)
FINDABLE = [
    ("Jita", "highsec", "Jita IV - Moon 4 - Caldari Navy Assembly Plant"),
    ("Amarr", "highsec", "Amarr VIII (Oris) - Emperor Family Academy"),
    ("Tama", "lowsec", None),
    ("Rancer", "lowsec", None),
    ("G-G78S", "NPC nullsec (Curse)", None),
    ("0T-LIB", "NPC nullsec (Stain)", None),
    ("ZV-72W", "NPC nullsec (Syndicate)", None),
    ("Vale", "NPC nullsec (Venal)", None),
    ("PR-8CA", "sov nullsec (Delve)", "PR-8CA III - Blood Raiders Logistic Support"),
    ("Skarkon", "Pochven", None),
    ("Kaunokka", "Pochven", None),
    # Partial names: the old ESI search did these with strict=false, and its
    # replacement cannot. If these fail, partial matching has silently gone away.
    ("PR-8", "partial system name", "PR-8CA III - Blood Raiders Logistic Support"),
    ("G-G7", "partial system name", None),
    ("Blood Raiders Logistic", "partial station name", None),
]


def _stations_for(conn: sqlite3.Connection, q: str) -> list[str]:
    """The offline half of the suggester: station names, plus stations in any
    system whose name matches."""
    like = f"%{q.lower()}%"
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sde_stations WHERE lower(name) LIKE ?", (like,))]
    for (sys_id,) in conn.execute(
            "SELECT system_id FROM sde_systems WHERE lower(name) LIKE ?", (like,)):
        names += [r[0] for r in conn.execute(
            "SELECT name FROM sde_stations WHERE system_id=?", (sys_id,))]
    return names


@pytest.mark.parametrize("query,space,expect", FINDABLE,
                         ids=[f"{q}-{s}".replace(" ", "_") for q, s, _ in FINDABLE])
def test_every_kind_of_space_is_findable(app_module, query, space, expect):
    conn = app_module.get_conn()
    try:
        names = _stations_for(conn, query)
        assert names, f"{space}: nothing found for {query!r}"
        if expect:
            assert expect in names
    finally:
        conn.close()


def test_the_index_covers_all_of_new_eden(app_module):
    """Counts, so a truncated or partial import is caught rather than looking
    like a search problem later."""
    conn = app_module.get_conn()
    try:
        stations = conn.execute("SELECT COUNT(*) FROM sde_stations").fetchone()[0]
        systems = conn.execute("SELECT COUNT(*) FROM sde_systems").fetchone()[0]
        no_region = conn.execute(
            "SELECT COUNT(*) FROM sde_systems WHERE region_id IS NULL").fetchone()[0]
        regions_with_stations = conn.execute(
            "SELECT COUNT(DISTINCT region_id) FROM sde_stations").fetchone()[0]
        wormholes = conn.execute(
            "SELECT COUNT(*) FROM sde_systems WHERE region_id >= 11000000").fetchone()[0]
        pochven = conn.execute(
            "SELECT COUNT(*) FROM sde_stations WHERE region_id = 10000070").fetchone()[0]
    finally:
        conn.close()

    assert stations > 5000, stations
    assert systems > 8000, systems
    assert no_region == 0, f"{no_region} systems without a region"
    assert regions_with_stations >= 39, regions_with_stations
    assert wormholes > 2500, wormholes          # w-space is in the index too
    assert pochven > 0, "Pochven has no stations in the index"


def test_a_system_with_no_npc_station_is_not_a_gap(app_module):
    """1DQ1-A and wormhole systems really have nothing but player structures -
    worth pinning so a future reader does not 'fix' it by inventing rows."""
    conn = app_module.get_conn()
    try:
        # Raravoss is in here deliberately: it looked like a search bug when a
        # live run found it, but that came from a structure already in the name
        # cache - the system itself has no NPC station.
        for name in ("1DQ1-A", "Niarja", "Raravoss"):
            row = conn.execute("SELECT system_id FROM sde_systems WHERE name=?",
                               (name,)).fetchone()
            assert row, f"{name} missing from the system index"
            n = conn.execute("SELECT COUNT(*) FROM sde_stations WHERE system_id=?",
                             (row[0],)).fetchone()[0]
            assert n == 0
    finally:
        conn.close()


def test_region_comes_from_the_sde_without_esi(app_module):
    """A station's region is what its prices are fetched against; taking it from
    the SDE saves two ESI calls per load and works with no network."""
    from app.web.location_resolver import _region_from_sde
    conn = app_module.get_conn()
    try:
        assert _region_from_sde(conn, 60003760, None) == 10000002        # Jita 4-4
        assert _region_from_sde(conn, 60014946, None) == 10000060        # PR-8CA, Delve
        # A structure is not in the SDE, but its system is
        row = conn.execute("SELECT system_id FROM sde_systems WHERE name='1DQ1-A'").fetchone()
        assert _region_from_sde(conn, 1_040_000_000_001, row[0]) == 10000060
    finally:
        conn.close()


def test_public_structures_are_due_when_never_fetched(app_module):
    from app.web.location_resolver import public_structures_stale, ensure_public_structure_meta
    conn = app_module.get_conn()
    try:
        ensure_public_structure_meta(conn)
        conn.execute("DELETE FROM public_structure_meta")
        conn.commit()
        assert public_structures_stale(conn) is True
        conn.execute("INSERT OR REPLACE INTO public_structure_meta VALUES (1, ?, 5)",
                     (time.time(),))
        conn.commit()
        assert public_structures_stale(conn) is False
        conn.execute("INSERT OR REPLACE INTO public_structure_meta VALUES (1, ?, 5)",
                     (time.time() - 25 * 3600,))
        conn.commit()
        assert public_structures_stale(conn) is True
    finally:
        conn.close()


class _R:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p


def test_public_structures_are_cached_with_system_region_and_market_flag(app_module):
    """Player structures are not in the SDE, so the only way to offer one the
    character has never docked at is ESI's public list. Region comes from the
    local system index, which is what makes the result priceable."""
    import asyncio
    from app.web import location_resolver as LR

    conn = app_module.get_conn()
    try:
        sys_id = conn.execute(
            "SELECT system_id FROM sde_systems WHERE name='1DQ1-A'").fetchone()[0]

        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *e): return False

            async def get(self, url, **kw):
                if url.endswith("/universe/structures/"):
                    if (kw.get("params") or {}).get("filter") == "market":
                        return _R([1001])
                    return _R([1001, 1002])
                if url.endswith("/universe/structures/1001/"):
                    return _R({"name": "1DQ1-A - Test Keepstar",
                               "solar_system_id": sys_id})
                if url.endswith("/universe/structures/1002/"):
                    return _R({}, status=403)      # ACL changed under us
                return _R({}, status=404)

        import unittest.mock as mock
        with mock.patch.object(LR, "esi_client", lambda **kw: Client()):
            res = asyncio.run(LR.refresh_public_structures(conn, "token"))

        assert res == {"named": 1, "total": 2}     # the 403 is skipped, not fatal
        row = conn.execute(
            "SELECT name, solar_system_id, region_id, has_market"
            " FROM location_name_cache WHERE location_id=1001").fetchone()
        assert row[0] == "1DQ1-A - Test Keepstar"
        assert row[1] == sys_id
        assert row[2] == 10000060                  # Delve, from the SDE
        assert row[3] == 1
        assert not LR.public_structures_stale(conn)
    finally:
        conn.close()
