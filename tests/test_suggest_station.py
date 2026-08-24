"""Station suggestions in the Prices custom-station box.

CCP removed the public /search/ endpoint - it 404s on every version for every
query - and that quietly cost this box two of its four sources: NPC stations by
name, and the system-name lookup everything else keys off. The visible symptom
was that a nullsec system typed in full (PR-8CA) found nothing, even though it
has an NPC station. These tests pin the replacement in place.
"""
from __future__ import annotations

import pytest

PR_8CA_SYSTEM = 30004711
PR_8CA_STATION = 60014946
PR_8CA_NAME = "PR-8CA III - Blood Raiders Logistic Support"


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class _FakeClient:
    """Records every ESI call so a test can assert on which endpoint was used."""

    def __init__(self, calls):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kw):
        self.calls.append(("POST", url))
        if "/universe/ids/" in url:
            return _Resp({"systems": [{"id": PR_8CA_SYSTEM, "name": "PR-8CA"}]})
        return _Resp({})

    async def get(self, url, **kw):
        self.calls.append(("GET", url))
        if "/universe/systems/" in url:
            return _Resp({"stations": [PR_8CA_STATION]})
        return _Resp({})


@pytest.fixture
def stub_esi(app_module, monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(app_module, "esi_client", lambda **kw: _FakeClient(calls))

    async def _names(ids, token=None, conn=None):
        return {sid: PR_8CA_NAME for sid in ids}

    monkeypatch.setattr(app_module, "resolve_station_names_bulk", _names)
    monkeypatch.setattr(app_module, "locations_in_system", lambda conn, sid: [])
    return calls


def test_a_system_name_finds_the_npc_station_in_it(client, stub_esi):
    """The reported bug: PR-8CA has an NPC station, and typing the system name
    has to reach it."""
    r = client.get("/api/suggest-station?q=PR-8CA")
    assert r.status_code == 200
    found = {e["location_id"] for e in r.json()["other"]}
    assert PR_8CA_STATION in found


def test_the_removed_search_endpoint_is_not_called(client, stub_esi):
    """A regression guard, not a style check: /search/ answers 404 for everything
    now, and the failure is silent - the result is simply empty."""
    client.get("/api/suggest-station?q=PR-8CA")
    urls = [u for _, u in stub_esi]
    assert not any(u.rstrip("/").endswith("/latest/search") for u in urls)


def test_a_name_the_local_index_knows_costs_no_request(client, stub_esi):
    """The SDE ships with the app, so a station or system it already knows must
    not cost a round trip - only the structure search, which needs ESI."""
    r = client.get("/api/suggest-station?q=PR-8CA")
    assert r.status_code == 200
    assert any(e["location_id"] == PR_8CA_STATION
               for e in r.json()["other"] + r.json()["owned"])
    assert not any("/universe/ids/" in u for _, u in stub_esi)


def test_a_name_the_local_index_misses_still_asks_esi(client, stub_esi):
    """The bundled SDE can be a patch behind, so a name it does not know is
    exactly the case /universe/ids/ still covers."""
    client.get("/api/suggest-station?q=Some Brand New Station")
    assert any("/universe/ids/" in u for _, u in stub_esi)


def test_a_name_matching_nothing_is_not_an_error(client, app_module, monkeypatch):
    """/universe/ids/ answers an unmatched name with an empty object and a 200,
    so there is no error case to mistake for one."""
    class _Empty(_FakeClient):
        async def post(self, url, **kw):
            self.calls.append(("POST", url))
            return _Resp({})

    monkeypatch.setattr(app_module, "esi_client", lambda **kw: _Empty([]))
    r = client.get("/api/suggest-station?q=zzqq-no-such-place")
    assert r.status_code == 200
    assert r.json()["other"] == [] and r.json()["owned"] == []


def test_a_short_query_asks_esi_nothing(client, stub_esi):
    r = client.get("/api/suggest-station?q=P")
    assert r.status_code == 200
    assert stub_esi == []
