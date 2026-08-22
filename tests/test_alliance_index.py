"""Alliance contract index + the in-game style filters.

The index exists because alliance contracts are only reachable through the
corporation endpoint, one call per contract for the contents, inside a rate limit
of 300 calls / 15 min PER CHARACTER (group corp-contract, 2 tokens per 2xx). So the
things worth pinning down are: only alliance-assigned contracts are kept, the
listing costs one call per corporation, the item calls are spread over every capable
character, a run stops at its budget and the next one resumes, and every filter
means what its label says.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.character.contracts import ItemFetch as _IF
from app.web import contracts_helper as ch

ALLIANCE = 99000001
CORP_A, CORP_B = 98000001, 98000002


def _contract(cid, **kw):
    base = {
        "contract_id": cid, "type": "item_exchange", "status": "outstanding",
        "availability": "personal", "assignee_id": ALLIANCE,
        "issuer_id": 2100000000 + cid, "issuer_corporation_id": 98765432,
        "acceptor_id": 0, "for_corporation": False,
        "price": 1_000_000.0, "reward": 0.0, "collateral": 0.0, "buyout": 0.0,
        "volume": 100.0, "title": f"contract {cid}",
        "date_issued": "2026-08-20T10:00:00Z", "date_expired": "2026-09-19T10:00:00Z",
        "days_to_complete": 0, "start_location_id": 60003760, "end_location_id": 0,
    }
    base.update(kw)
    return base


@pytest.fixture
def conn(app_module):
    c = app_module.get_conn()
    ch.ensure_alliance_contract_tables(c)
    c.execute("DELETE FROM alliance_contracts")
    c.execute("DELETE FROM alliance_contract_items")
    c.execute("DELETE FROM alliance_contract_meta")
    c.commit()
    yield c
    c.execute("DELETE FROM alliance_contracts")
    c.execute("DELETE FROM alliance_contract_items")
    c.execute("DELETE FROM alliance_contract_meta")
    c.commit()
    c.close()


def _run_index(conn, sources, listings, items=None, item_calls=None, list_calls=None):
    """Drive stream_alliance_index with stubbed ESI, return the last SSE payload."""
    import json as _json

    async def _list(client, corp_id, token):
        if list_calls is not None:
            list_calls.append(corp_id)
        return list(listings.get(corp_id, [])), None

    async def _items(client, corp_id, contract_id, token):
        if item_calls is not None:
            item_calls.append((corp_id, contract_id, token))
        # The fetcher answers WHY, not just what: ok / gone / throttled / limited.
        return _IF(list((items or {}).get(contract_id, [])), "ok")

    async def _parties(ids):
        return {i: f"party {i}" for i in ids}

    async def _locations(ids):
        return {i: "Jita IV - Moon 4" for i in ids}

    real = (ch.contracts_api.fetch_corp_contracts, ch.contracts_api.fetch_corp_contract_items)
    ch.contracts_api.fetch_corp_contracts = _list
    ch.contracts_api.fetch_corp_contract_items = _items
    try:
        async def _drive():
            last = {}
            async for chunk in ch.stream_alliance_index(
                    conn, ALLIANCE, sources, _parties, _locations):
                last = _json.loads(chunk[len("data: "):])
            return last
        return asyncio.run(_drive())
    finally:
        (ch.contracts_api.fetch_corp_contracts,
         ch.contracts_api.fetch_corp_contract_items) = real


# ── indexing ──────────────────────────────────────────────────────────────────

def test_only_alliance_assigned_contracts_are_kept(conn):
    """The corp endpoint mixes in the corporation's own and personal contracts."""
    listings = {CORP_A: [
        _contract(1),
        _contract(2, assignee_id=CORP_A),          # assigned to the corporation
        _contract(3, assignee_id=2124225246),      # assigned to a character
        _contract(4),
    ]}
    last = _run_index(conn, [(CORP_A, "tokA")], listings)
    assert last["contract_count"] == 2
    kept = sorted(r[0] for r in conn.execute(
        "SELECT contract_id FROM alliance_contracts").fetchall())
    assert kept == [1, 4]


def test_the_same_contract_from_two_corps_is_stored_once(conn):
    """Every corp of the alliance returns the identical alliance contracts."""
    listings = {CORP_A: [_contract(1), _contract(2)],
                CORP_B: [_contract(1), _contract(2), _contract(3)]}
    last = _run_index(conn, [(CORP_A, "tokA"), (CORP_B, "tokB")], listings)
    assert last["contract_count"] == 3


def test_listing_is_one_call_per_corporation_not_per_token(conn):
    """Several characters of one corp must not mean several listing calls."""
    calls: list = []
    _run_index(conn, [(CORP_A, "t1"), (CORP_A, "t2"), (CORP_B, "t3")],
               {CORP_A: [_contract(1)], CORP_B: [_contract(1)]}, list_calls=calls)
    assert calls == [CORP_A, CORP_B]


def test_item_calls_are_spread_over_every_capable_character(conn):
    """The rate limit bucket is per character, so each token is extra allowance."""
    listings = {CORP_A: [_contract(i) for i in range(1, 7)]}
    calls: list = []
    _run_index(conn, [(CORP_A, "t1"), (CORP_A, "t2"), (CORP_B, "t3")], listings,
               items={i: [{"type_id": 34, "quantity": 1}] for i in range(1, 7)},
               item_calls=calls)
    assert len(calls) == 6
    # round robin: every token pulled its share
    assert sorted({c[2] for c in calls}) == ["t1", "t2", "t3"]


def test_a_run_stops_at_its_budget_and_the_next_one_resumes(conn, monkeypatch):
    monkeypatch.setattr(ch, "_ITEM_CALLS_PER_TOKEN", 2)
    listings = {CORP_A: [_contract(i) for i in range(1, 6)]}
    items = {i: [{"type_id": 34, "quantity": i}] for i in range(1, 6)}
    calls: list = []
    last = _run_index(conn, [(CORP_A, "t1")], listings, items=items, item_calls=calls)
    assert len(calls) == 2 and last["items_left"] == 3

    calls2: list = []
    last2 = _run_index(conn, [(CORP_A, "t1")], listings, items=items, item_calls=calls2)
    # Contents never change: the two already fetched are not asked for again.
    assert len(calls2) == 2 and last2["items_left"] == 1
    assert {c[1] for c in calls} & {c[1] for c in calls2} == set()


def test_outstanding_contracts_get_their_contents_first(conn, monkeypatch):
    monkeypatch.setattr(ch, "_ITEM_CALLS_PER_TOKEN", 1)
    listings = {CORP_A: [_contract(1, status="finished"),
                         _contract(2, status="deleted"),
                         _contract(3, status="outstanding")]}
    calls: list = []
    _run_index(conn, [(CORP_A, "t1")], listings,
               items={i: [{"type_id": 34, "quantity": 1}] for i in (1, 2, 3)},
               item_calls=calls)
    assert [c[1] for c in calls] == [3]


def test_courier_contracts_are_not_asked_for_contents(conn):
    listings = {CORP_A: [_contract(1, type="courier"), _contract(2, type="loan")]}
    calls: list = []
    _run_index(conn, [(CORP_A, "t1")], listings, items={}, item_calls=calls)
    assert calls == []


def test_a_blueprint_copy_is_recognised_from_raw_quantity(conn):
    """This endpoint has no is_blueprint_copy field: -2 means copy, -1 an original."""
    listings = {CORP_A: [_contract(1)]}
    items = {1: [{"type_id": 12005, "quantity": 1, "raw_quantity": -2},
                 {"type_id": 12006, "quantity": 1, "raw_quantity": -1},
                 {"type_id": 34, "quantity": 5}]}
    _run_index(conn, [(CORP_A, "t1")], listings, items=items)
    flags = dict(conn.execute(
        "SELECT type_id, is_bpc FROM alliance_contract_items WHERE contract_id=1").fetchall())
    assert flags == {12005: 1, 12006: 0, 34: 0}


def test_names_are_stored_so_filtering_needs_no_esi(conn):
    _run_index(conn, [(CORP_A, "t1")], {CORP_A: [_contract(1)]})
    row = conn.execute("SELECT issuer_name, issuer_corp_name, start_name"
                       " FROM alliance_contracts WHERE contract_id=1").fetchone()
    assert row[0].startswith("party ") and row[1].startswith("party ")
    assert row[2] == "Jita IV - Moon 4"


def test_contracts_that_left_the_window_lose_their_items(conn):
    """ESI only lists 30 days, so the item table must not grow forever."""
    _run_index(conn, [(CORP_A, "t1")], {CORP_A: [_contract(1), _contract(2)]},
               items={1: [{"type_id": 34, "quantity": 1}],
                      2: [{"type_id": 35, "quantity": 1}]})
    assert conn.execute("SELECT COUNT(*) FROM alliance_contract_items").fetchone()[0] == 2
    _run_index(conn, [(CORP_A, "t1")], {CORP_A: [_contract(2)]})
    left = [r[0] for r in conn.execute(
        "SELECT DISTINCT contract_id FROM alliance_contract_items").fetchall()]
    assert left == [2]


# ── filters ───────────────────────────────────────────────────────────────────

@pytest.fixture
def indexed(conn):
    """A small spread of contracts with contents, to filter over."""
    listings = {CORP_A: [
        _contract(1, price=5_000_000, title="Hulk fit", volume=1000,
                  status="outstanding"),
        _contract(2, price=2_000_000_000, title="capital parts", volume=50_000),
        _contract(3, type="courier", reward=25_000_000, collateral=900_000_000,
                  volume=330_000, title="Jita to C-N4OD", price=0),
        _contract(4, status="finished", price=1_000, title="old deal"),
        _contract(5, type="auction", price=0, buyout=700_000_000, title="ship auction"),
    ]}
    items = {
        1: [{"type_id": 22544, "quantity": 1},                       # Hulk
            {"type_id": 34, "quantity": 1000}],                       # Tritanium
        2: [{"type_id": 34, "quantity": 5_000_000}],
        5: [{"type_id": 645, "quantity": 1}],                         # Dominix
    }
    _run_index(conn, [(CORP_A, "t1")], listings, items=items)
    # A location and issuer we can filter by, independent of the stub resolver.
    conn.execute("UPDATE alliance_contracts SET start_name='C-N4OD - Fountain of Life',"
                 " issuer_name='Harald Bechtersgard', issuer_corp_name='TEMPLAR.'"
                 " WHERE contract_id IN (1,3)")
    conn.commit()
    return conn


def _search(conn, **kw):
    rows, total = ch.search_alliance_contracts(conn, ALLIANCE, **kw)
    return sorted(r["contract_id"] for r in rows), total


def test_status_defaults_to_outstanding(indexed):
    ids, total = _search(indexed)
    assert 4 not in ids and total == 4
    ids_any, total_any = _search(indexed, status="any")
    assert total_any == 5 and 4 in ids_any


def test_item_filter_matches_contract_contents(indexed):
    assert _search(indexed, item="Hulk")[0] == [1]
    assert _search(indexed, item="tritanium")[0] == [1, 2]
    assert _search(indexed, item="no-such-item")[0] == []


def test_exact_item_filter_does_not_match_substrings(indexed):
    # "Hulk" is a substring of other type names, the exact switch pins it down.
    assert _search(indexed, item="Hulk", exact_item=True)[0] == [1]
    assert _search(indexed, item="Hul", exact_item=True)[0] == []
    assert _search(indexed, item="Hul")[0] == [1]


def test_contract_type_and_price_range(indexed):
    assert _search(indexed, ctype="courier")[0] == [3]
    assert _search(indexed, ctype="auction")[0] == [5]
    assert _search(indexed, max_price=10_000_000)[0] == [1, 3, 5]
    assert _search(indexed, min_price=1_000_000_000)[0] == [2]
    assert _search(indexed, min_price=1_000_000, max_price=10_000_000)[0] == [1]


def test_courier_specific_filters(indexed):
    assert _search(indexed, min_reward=20_000_000)[0] == [3]
    assert _search(indexed, max_collateral=1_000_000)[0] == [1, 2, 5]
    # contract 5 keeps the default 100 m3, contract 1 has 1000
    assert _search(indexed, max_volume=2_000)[0] == [1, 5]
    assert _search(indexed, max_volume=500)[0] == [5]


def test_location_issuer_and_title(indexed):
    assert _search(indexed, location="C-N4OD")[0] == [1, 3]
    assert _search(indexed, issuer="Harald")[0] == [1, 3]
    assert _search(indexed, issuer="TEMPLAR")[0] == [1, 3]      # corp name too
    assert _search(indexed, title="auction")[0] == [5]


def test_expires_within_days(indexed):
    soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 2 * 86400))
    indexed.execute("UPDATE alliance_contracts SET date_expired=? WHERE contract_id=2",
                    (soon,))
    indexed.commit()
    assert _search(indexed, expires_days=3)[0] == [2]
    assert 2 in _search(indexed, expires_days=60)[0]


def test_hide_own_drops_my_characters_and_corporations(indexed):
    mine = 2100000001          # issuer_id of contract 1 (_contract() derives it)
    ids, _ = _search(indexed, hide_own=True, own_ids=(mine,))
    assert 1 not in ids
    ids2, _ = _search(indexed, hide_own=True, own_ids=(98765432,))   # issuer corp
    assert ids2 == []


def test_sorting_and_the_row_limit(indexed):
    rows, total = ch.search_alliance_contracts(indexed, ALLIANCE, status="any",
                                               sort="price_hi")
    assert rows[0]["contract_id"] == 2
    rows, total = ch.search_alliance_contracts(indexed, ALLIANCE, status="any",
                                               sort="price", limit=2)
    # The total still reports every match, not just the page.
    assert len(rows) == 2 and total == 5


def test_items_are_grouped_like_the_in_game_window(conn):
    """ESI returns one row per stack, so a fit can repeat the same charge 49 times."""
    listings = {CORP_A: [_contract(1)]}
    items = {1: [{"type_id": 23079, "quantity": 1} for _ in range(5)]
                + [{"type_id": 34, "quantity": 10, "is_included": False}]}
    _run_index(conn, [(CORP_A, "t1")], listings, items=items)
    got = ch.get_alliance_contract_items(conn, 1)
    assert len(got) == 2
    by_type = {g["type_id"]: g for g in got}
    assert by_type[23079]["quantity"] == 5 and by_type[23079]["included"]
    assert by_type[34]["included"] is False        # asked for, not offered


def test_index_status_counts_what_the_page_shows(indexed):
    st = ch.get_alliance_index_status(indexed, ALLIANCE)
    assert st["contract_count"] == 5
    assert st["outstanding"] == 4
    assert st["with_items"] == 3                   # contracts 1, 2 and 5
    assert st["indexed_at"] > 0
    assert ch.get_alliance_index_status(indexed, 99999999) is None


# ── the page ──────────────────────────────────────────────────────────────────

def _alliance_page(client, app_module, url, *, alliance_map=None, scoped=True):
    """Render /contracts/alliance with corp->alliance and tokens stubbed."""
    import jwt
    scope = app_module.CORP_CONTRACT_SCOPE
    tok = jwt.encode({"sub": "CHARACTER:EVE:1", "scp": [scope] if scoped else []},
                     "unused-secret", algorithm="HS256")

    async def _alliances(conn, corp_ids):
        if alliance_map is not None:
            return dict(alliance_map)
        return {c: ALLIANCE for c in corp_ids}

    async def _names(ids):
        return {i: ("The Initiative." if i == ALLIANCE else f"Corp {i}") for i in ids}

    real = (app_module._corp_alliance_ids, app_module._resolve_party_names,
            app_module._get_valid_token_for)
    app_module._corp_alliance_ids = _alliances
    app_module._resolve_party_names = _names
    app_module._get_valid_token_for = lambda conn, cid: tok
    try:
        return client.get(url).text
    finally:
        (app_module._corp_alliance_ids, app_module._resolve_party_names,
         app_module._get_valid_token_for) = real


def test_page_shows_the_alliance_and_the_index_state(client, app_module, indexed):
    html = _alliance_page(client, app_module, f"/contracts/alliance?alliance={ALLIANCE}")
    assert "The Initiative." in html
    assert "Index alliance contracts" not in html    # it IS indexed
    assert "Hulk fit" in html                        # a row from the fixture


def test_page_filters_come_from_the_query_string(client, app_module, indexed):
    html = _alliance_page(client, app_module,
                          f"/contracts/alliance?alliance={ALLIANCE}&item=Hulk")
    assert "Hulk fit" in html and "capital parts" not in html
    html = _alliance_page(client, app_module,
                          f"/contracts/alliance?alliance={ALLIANCE}&ctype=courier")
    assert "Jita to C-N4OD" in html and "Hulk fit" not in html
    # A junk number must not 500 the page.
    html = _alliance_page(client, app_module,
                          f"/contracts/alliance?alliance={ALLIANCE}&max_price=abc")
    assert "Error loading alliance contracts" not in html


def test_a_corporation_without_a_capable_character_is_reported(client, app_module, conn):
    html = _alliance_page(client, app_module, "/contracts/alliance", scoped=False)
    assert "No character with corporation contract access" in html
    assert "Re-added" in html or "re-added" in html


def test_corporations_outside_an_alliance_are_not_offered(client, app_module, conn):
    html = _alliance_page(client, app_module, "/contracts/alliance", alliance_map={})
    assert "None of your corporations is in an alliance" in html


def test_search_box_matches_title_issuer_corp_and_location(indexed):
    """The Alliance tab has the same one-box search as the other tabs."""
    assert _search(indexed, q="Hulk")[0] == [1]              # title
    assert _search(indexed, q="Harald")[0] == [1, 3]         # issuer name
    assert _search(indexed, q="TEMPLAR")[0] == [1, 3]        # issuer corporation
    assert _search(indexed, q="C-N4OD")[0] == [1, 3]         # location
    assert _search(indexed, q="nothing-like-this")[0] == []


def test_an_indexed_alliance_stays_browsable_without_a_usable_token(conn, indexed):
    """The rows are local; only Refresh needs ESI."""
    assert ch.indexed_alliances(indexed) == [ALLIANCE]


def test_alliance_page_uses_the_same_filter_bar_as_the_other_tabs(client, app_module, indexed):
    html = _alliance_page(client, app_module, f"/contracts/alliance?alliance={ALLIANCE}")
    for field in ('id="cf-item"', 'id="cf-exact"', 'id="cf-q"', 'id="cf-type"',
                  'id="cf-status"', 'id="cf-minp"', 'id="cf-maxp"', 'id="cf-minr"',
                  'id="cf-maxc"', 'id="cf-maxv"', 'id="cf-days"', 'id="cf-loc"',
                  'id="cf-issuer"', 'id="cf-title"', 'id="cf-sort"', 'id="cf-own"'):
        assert field in html, field
    # Here the bar submits: the filter is SQL over the whole index.
    assert 'data-mode="server"' in html
    assert 'name="q"' in html and 'name="item"' in html


def test_alliance_filters_survive_a_page_without_any_readable_corporation(
        client, app_module, indexed):
    """Tokens expire; the indexed rows must still be filterable."""
    async def _no_alliances(conn, corp_ids):
        return {}

    real = app_module._corp_alliance_ids
    app_module._corp_alliance_ids = _no_alliances
    try:
        html = client.get(f"/contracts/alliance?alliance={ALLIANCE}&q=Hulk").text
    finally:
        app_module._corp_alliance_ids = real
    assert "Hulk fit" in html and "capital parts" not in html


def test_a_failed_item_fetch_is_not_recorded_as_an_empty_contract(conn, monkeypatch):
    """Rate limit refusals used to look like "this contract has no contents"."""
    monkeypatch.setattr(ch, "_ITEM_FAIL_LIMIT", 3)
    listings = {CORP_A: [_contract(i) for i in range(1, 21)]}
    calls: list = []

    async def _list(client, corp_id, token):
        return list(listings[CORP_A]), None

    async def _fail(client, corp_id, contract_id, token):
        calls.append(contract_id)
        return _IF(None, "limited")      # the ESI token bucket is empty

    async def _parties(ids):
        return {i: "x" for i in ids}

    async def _locations(ids):
        return {i: "y" for i in ids}

    import asyncio as _a
    import json as _j
    real = (ch.contracts_api.fetch_corp_contracts, ch.contracts_api.fetch_corp_contract_items)
    ch.contracts_api.fetch_corp_contracts = _list
    ch.contracts_api.fetch_corp_contract_items = _fail
    try:
        async def _drive():
            last = {}
            async for chunk in ch.stream_alliance_index(conn, ALLIANCE, [(CORP_A, "t1")],
                                                        _parties, _locations):
                last = _j.loads(chunk[len("data: "):])
            return last
        last = _a.run(_drive())
    finally:
        (ch.contracts_api.fetch_corp_contracts,
         ch.contracts_api.fetch_corp_contract_items) = real

    # It gave up instead of spending the whole budget on refusals...
    assert last["rate_limited"] is True
    assert len(calls) < 20
    # ...nothing was stored as "no contents"...
    assert conn.execute("SELECT COUNT(*) FROM alliance_contract_items").fetchone()[0] == 0
    # ...and the contracts still count as needing contents next time.
    assert last["items_left"] >= 20 - len(calls)
    # The listing itself was saved before the contents phase, so the run is not a loss.
    assert conn.execute("SELECT COUNT(*) FROM alliance_contracts").fetchone()[0] == 20


def test_the_listing_is_saved_before_the_slow_contents_phase(conn):
    """An interrupted run must leave the contracts behind, not nothing."""
    import json as _j
    seen_listed_before_items = []

    async def _list(client, corp_id, token):
        return [_contract(1), _contract(2)], None

    async def _items(client, corp_id, contract_id, token):
        # By the time contents are fetched, the rows must already be in the database.
        seen_listed_before_items.append(
            conn.execute("SELECT COUNT(*) FROM alliance_contracts").fetchone()[0])
        return _IF([{"type_id": 34, "quantity": 1}], "ok")

    async def _parties(ids):
        return {i: "x" for i in ids}

    async def _locations(ids):
        return {i: "y" for i in ids}

    import asyncio as _a
    real = (ch.contracts_api.fetch_corp_contracts, ch.contracts_api.fetch_corp_contract_items)
    ch.contracts_api.fetch_corp_contracts = _list
    ch.contracts_api.fetch_corp_contract_items = _items
    try:
        async def _drive():
            async for _ in ch.stream_alliance_index(conn, ALLIANCE, [(CORP_A, "t1")],
                                                    _parties, _locations):
                pass
        _a.run(_drive())
    finally:
        (ch.contracts_api.fetch_corp_contracts,
         ch.contracts_api.fetch_corp_contract_items) = real
    assert seen_listed_before_items and min(seen_listed_before_items) == 2


def test_the_server_side_filter_is_a_real_form_that_submits_somewhere_real(
        client, app_module, indexed):
    """A dynamically assembled tag got its quotes escaped, so Search hit a 404.

    The rendered markup has to contain a form whose action is the page itself, with
    honest quotes - not `action=&#34;/contracts/alliance&#34;`, which the browser
    resolves to /%22/contracts/alliance%22.
    """
    import re
    html = _alliance_page(client, app_module, f"/contracts/alliance?alliance={ALLIANCE}")
    m = re.search(r'<form[^>]*id="contract-filters"[^>]*>', html)
    assert m, "the filter bar must render as a form in server mode"
    tag = m.group(0)
    assert 'action="/contracts/alliance"' in tag, tag
    assert 'method="get"' in tag, tag
    assert "&#34;" not in tag and "&quot;" not in tag, tag
    # And the hidden alliance id travels with it, or Search would lose the alliance.
    assert f'name="alliance" value="{ALLIANCE}"' in html


def test_searching_for_something_that_is_not_there_is_an_empty_result_not_an_error(
        client, app_module, indexed):
    html = _alliance_page(client, app_module,
                          f"/contracts/alliance?alliance={ALLIANCE}&item=nycx")
    assert "No contracts match this filter" in html
    assert "Not Found" not in html and "Internal Server Error" not in html


# ── keeping the index filled without anyone clicking ──────────────────────────

def test_contents_are_only_fetched_for_contracts_you_could_still_accept(conn):
    """This is what makes it one run instead of three: on a real alliance the
    finished and deleted contracts outnumber the open ones five to one, and their
    contents answer no question anybody asks."""
    listings = {CORP_A: [_contract(1, status="outstanding"),
                         _contract(2, status="finished"),
                         _contract(3, status="deleted"),
                         _contract(4, status="in_progress")]}
    calls: list = []
    _run_index(conn, [(CORP_A, "t1")], listings,
               items={i: [{"type_id": 34, "quantity": 1}] for i in range(1, 5)},
               item_calls=calls)
    assert [c[1] for c in calls] == [1]
    # And the same rule decides what is still considered missing.
    assert ch.contracts_missing_items(conn, ALLIANCE) == []


def test_the_filler_lists_then_fetches_and_reports_being_rate_limited(app_module, conn,
                                                                      monkeypatch):
    import asyncio
    contracts = [_contract(i) for i in range(1, 6)]

    async def _list(client, corp_id, token):
        return list(contracts), None

    calls = []

    async def _items(client, corp_id, contract_id, token):
        calls.append(contract_id)
        return (_IF(None, "limited") if len(calls) > 2
                else _IF([{"type_id": 34, "quantity": 1}], "ok"))

    monkeypatch.setattr(ch, "_ITEM_FAIL_LIMIT", 1)
    monkeypatch.setattr(ch.contracts_api, "fetch_corp_contracts", _list)
    monkeypatch.setattr(ch.contracts_api, "fetch_corp_contract_items", _items)

    async def _names(ids):
        return {i: "x" for i in ids}

    monkeypatch.setattr(app_module, "_resolve_party_names", _names)
    monkeypatch.setattr(app_module, "resolve_station_names_bulk",
                        lambda ids, token=None, conn=None: _names(ids))

    res = asyncio.run(app_module._alliance_fill_pass(conn, ALLIANCE, [(CORP_A, "t1")]))
    assert res["rate_limited"] is True
    state = app_module.alliance_fill_state(ALLIANCE)
    assert state["phase"] == "waiting"
    # A countdown the page can show, and it does not lie about what is left.
    assert 0 < state["retry_in"] <= app_module._ALLIANCE_WAIT
    assert state["missing"] >= 1
    # Whatever was fetched before the refusal is stored.
    assert conn.execute("SELECT COUNT(DISTINCT contract_id)"
                        " FROM alliance_contract_items").fetchone()[0] >= 1


def test_fill_status_endpoint_reports_progress_across_page_loads(client, app_module,
                                                                indexed):
    """The state lives on the server, so clicking around does not lose it."""
    app_module._ALLIANCE_FILL[ALLIANCE] = {
        "phase": "waiting", "done": 40, "total": 100,
        "retry_at": __import__("time").time() + 300, "missing": 60,
    }
    try:
        d = client.get(f"/api/contracts/alliance/fill-status?alliance_id={ALLIANCE}").json()
    finally:
        app_module._ALLIANCE_FILL.pop(ALLIANCE, None)
    assert d["phase"] == "waiting" and d["done"] == 40
    assert 250 < d["retry_in"] <= 300
    assert d["contract_count"] == 5          # from the indexed fixture


def test_fill_status_is_harmless_for_an_alliance_nobody_indexed(client, app_module, conn):
    d = client.get("/api/contracts/alliance/fill-status?alliance_id=42").json()
    assert d["contract_count"] == 0 and d["missing"] == 0
    assert d["phase"] == "starting"


# ── feeding the Plan page's contract price ────────────────────────────────────

def test_best_alliance_price_prefers_a_contract_holding_only_that_item(conn):
    """Same rule as the public browser: a bundle price also covers other items."""
    listings = {CORP_A: [
        _contract(1, price=100_000_000),      # 10 units of the product -> 10M/unit
        _contract(2, price=60_000_000),       # 1 unit + something else (bundle)
        _contract(3, price=5_000_000, status="finished"),   # cannot be accepted
    ]}
    items = {1: [{"type_id": 22544, "quantity": 10}],
             2: [{"type_id": 22544, "quantity": 1}, {"type_id": 34, "quantity": 500}],
             3: [{"type_id": 22544, "quantity": 1}]}
    _run_index(conn, [(CORP_A, "t1")], listings, items=items)
    best = ch.best_alliance_contract_price(conn, [ALLIANCE], 22544)
    assert best["contract_id"] == 1 and best["price"] == 10_000_000
    assert best["is_bundle"] is False
    assert best["single_count"] == 1 and best["bundle_count"] == 1
    # Nothing for a product nobody offers, and nothing without an alliance.
    assert ch.best_alliance_contract_price(conn, [ALLIANCE], 645) is None
    assert ch.best_alliance_contract_price(conn, [], 22544) is None


def test_best_alliance_price_falls_back_to_a_bundle_and_says_so(conn):
    listings = {CORP_A: [_contract(1, price=60_000_000)]}
    items = {1: [{"type_id": 22544, "quantity": 2}, {"type_id": 34, "quantity": 500}]}
    _run_index(conn, [(CORP_A, "t1")], listings, items=items)
    best = ch.best_alliance_contract_price(conn, [ALLIANCE], 22544)
    assert best["is_bundle"] is True and best["price"] == 30_000_000


def test_plan_contract_price_can_read_the_alliance_index(client, app_module, conn):
    """The Plan page could only use public contracts; alliance is now a real source."""
    listings = {CORP_A: [_contract(1, price=50_000_000)]}
    _run_index(conn, [(CORP_A, "t1")], listings,
               items={1: [{"type_id": 22544, "quantity": 5}]})
    d = client.get("/api/plan/contract-price?location_id=60003760&type_id=22544"
                   "&source=alliance").json()
    assert d["ok"] and d["price"] == 10_000_000
    assert d["source"] == "alliance"
    # A product nobody offers is a clean "no", not an error about public indexes.
    d = client.get("/api/plan/contract-price?location_id=60003760&type_id=645"
                   "&source=alliance").json()
    assert not d["ok"] and "alliance" in d["error"]


def test_cheapest_source_picks_the_cheaper_of_public_and_alliance(client, app_module, conn):
    listings = {CORP_A: [_contract(1, price=50_000_000)]}          # 10M/unit
    _run_index(conn, [(CORP_A, "t1")], listings,
               items={1: [{"type_id": 22544, "quantity": 5}]})

    def _public(price):
        async def _region(conn_, loc, token=None):
            return 10000002
        return _region, {"price": price, "is_bundle": False, "contract_id": 99,
                         "single_count": 1, "bundle_count": 0}

    real = (app_module.get_region_for_location, app_module.contracts_helper.get_index_status,
            app_module.contracts_helper.best_contract_price)
    try:
        region, pub = _public(4_000_000)
        app_module.get_region_for_location = region
        app_module.contracts_helper.get_index_status = lambda c, r: {"indexed_at": 1.0}
        app_module.contracts_helper.best_contract_price = lambda c, r, t: dict(pub)
        d = client.get("/api/plan/contract-price?location_id=60003760&type_id=22544"
                       "&source=both").json()
        assert d["ok"] and d["price"] == 4_000_000 and d["source"] == "public"

        app_module.contracts_helper.best_contract_price = lambda c, r, t: {
            "price": 25_000_000, "is_bundle": False, "contract_id": 99,
            "single_count": 1, "bundle_count": 0}
        d = client.get("/api/plan/contract-price?location_id=60003760&type_id=22544"
                       "&source=both").json()
        assert d["ok"] and d["price"] == 10_000_000 and d["source"] == "alliance"
    finally:
        (app_module.get_region_for_location, app_module.contracts_helper.get_index_status,
         app_module.contracts_helper.best_contract_price) = real


# ── telling the three refusals apart ──────────────────────────────────────────

def test_the_game_servers_stop_spamming_is_slept_off_not_treated_as_a_dead_end(
        conn, monkeypatch):
    """520 ConStopSpamming is the GAME server saying "not so fast", for seconds.

    It used to be counted with everything else, so 25 of them aborted the run and
    the page announced a 15-minute ESI rate limit that was not happening.
    """
    import asyncio
    # No sleep patching: the code floors the wait at one second and the three
    # contracts wait concurrently, so the test costs about that.
    listings = {CORP_A: [_contract(i) for i in range(1, 4)]}
    attempts: list = []

    async def _list(client, corp_id, token):
        return list(listings[CORP_A]), None

    async def _items(client, corp_id, contract_id, token):
        attempts.append(contract_id)
        # Throttled once per contract, then fine.
        if attempts.count(contract_id) == 1:
            return _IF(None, "throttled", 3.0)
        return _IF([{"type_id": 34, "quantity": 1}], "ok")

    monkeypatch.setattr(ch.contracts_api, "fetch_corp_contracts", _list)
    monkeypatch.setattr(ch.contracts_api, "fetch_corp_contract_items", _items)

    async def _names(ids):
        return {i: "x" for i in ids}

    async def _drive():
        async for _ in ch.stream_alliance_index(conn, ALLIANCE, [(CORP_A, "t1")],
                                                _names, _names):
            pass
    asyncio.run(_drive())
    # Every contract was retried and stored; nothing was reported as rate limited.
    assert conn.execute("SELECT COUNT(DISTINCT contract_id)"
                        " FROM alliance_contract_items").fetchone()[0] == 3
    assert len(attempts) == 6


def test_a_contract_that_no_longer_exists_is_not_asked_for_again(conn, monkeypatch):
    """404 means accepted, expired or deleted - retrying it forever is a treadmill."""
    import asyncio
    listings = {CORP_A: [_contract(1), _contract(2)]}
    calls: list = []

    async def _list(client, corp_id, token):
        return list(listings[CORP_A]), None

    async def _items(client, corp_id, contract_id, token):
        calls.append(contract_id)
        if contract_id == 1:
            return _IF(None, "gone")
        return _IF([{"type_id": 34, "quantity": 1}], "ok")

    monkeypatch.setattr(ch.contracts_api, "fetch_corp_contracts", _list)
    monkeypatch.setattr(ch.contracts_api, "fetch_corp_contract_items", _items)

    async def _names(ids):
        return {i: "x" for i in ids}

    async def _drive():
        async for _ in ch.stream_alliance_index(conn, ALLIANCE, [(CORP_A, "t1")],
                                                _names, _names):
            pass
    asyncio.run(_drive())
    assert sorted(calls) == [1, 2]
    # Not pending any more...
    assert ch.contracts_missing_items(conn, ALLIANCE) == []
    # ...and it does not drag the "covers X of Y" denominator down forever.
    total, covered = ch.alliance_item_coverage(conn, ALLIANCE)
    assert (total, covered) == (1, 1)
    # A second run does not ask about it again.
    calls.clear()
    asyncio.run(_drive())
    assert calls == []


# ── what a contract's contents are worth ──────────────────────────────────────

def _price(conn, type_id, sell=None, buy=None, adjusted=None):
    conn.execute("CREATE TABLE IF NOT EXISTS market_price_cache (type_id INTEGER PRIMARY KEY,"
                 " sell_price REAL, buy_price REAL, volume REAL, cached_at REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS adjusted_price_cache (type_id INTEGER PRIMARY KEY,"
                 " adjusted REAL NOT NULL, cached_at INTEGER)")
    if sell is not None or buy is not None:
        conn.execute("INSERT OR REPLACE INTO market_price_cache (type_id, sell_price, buy_price,"
                     " cached_at) VALUES (?,?,?,?)", (type_id, sell, buy, time.time()))
    if adjusted is not None:
        conn.execute("INSERT OR REPLACE INTO adjusted_price_cache (type_id, adjusted, cached_at)"
                     " VALUES (?,?,?)", (type_id, adjusted, int(time.time())))
    conn.commit()


def test_value_comes_from_jita_then_contracts_then_ccps_estimate(conn):
    """A titan has no Jita sell order - its price lives on a contract."""
    TRIT, TITAN, ODD, NOTHING = 34, 671, 645, 12005
    _price(conn, TRIT, sell=5.0, buy=4.0)
    _price(conn, ODD, adjusted=137_000_000)          # index value only
    _price(conn, NOTHING, adjusted=0)                # CCP has no number either
    # A single-item contract elsewhere in the alliance sells the titan.
    _run_index(conn, [(CORP_A, "t1")],
               {CORP_A: [_contract(500, price=80_000_000_000)]},
               items={500: [{"type_id": TITAN, "quantity": 1}]})

    items = [{"type_id": TRIT, "quantity": 1000, "name": "Tritanium", "included": True},
             {"type_id": TITAN, "quantity": 1, "name": "Erebus", "included": True},
             {"type_id": ODD, "quantity": 1, "name": "Deadspace mod", "included": True},
             {"type_id": NOTHING, "quantity": 1, "name": "Molok", "included": True}]
    a = ch.appraise_items(conn, items)
    assert [i["price_source"] for i in items] == ["jita", "contract", "estimate", None]
    assert a["counts"] == {"jita": 1, "contract": 1, "estimate": 1}
    assert a["value"] == 5_000 + 80_000_000_000 + 137_000_000
    assert a["unpriced"] == 1 and a["unpriced_names"] == ["Molok"]


def test_a_contract_is_never_valued_from_itself(conn):
    """Otherwise it would always compare exactly equal to its own price."""
    TITAN = 671
    _run_index(conn, [(CORP_A, "t1")],
               {CORP_A: [_contract(500, price=80_000_000_000)]},
               items={500: [{"type_id": TITAN, "quantity": 1}]})
    items = [{"type_id": TITAN, "quantity": 1, "name": "Erebus", "included": True}]
    # Appraising some other contract may use it...
    assert ch.appraise_items(conn, list(items))["value"] == 80_000_000_000
    # ...but appraising contract 500 itself may not.
    a = ch.appraise_items(conn, items, contract_id=500)
    assert a["value"] == 0 and a["unpriced"] == 1


def test_items_the_contract_asks_for_are_a_cost_not_a_gain(conn):
    TRIT = 34
    _price(conn, TRIT, sell=10.0)
    items = [{"type_id": TRIT, "quantity": 100, "name": "Tritanium", "included": True},
             {"type_id": TRIT, "quantity": 40, "name": "Tritanium", "included": False}]
    a = ch.appraise_items(conn, items)
    assert a["value"] == 1000 and a["asked"] == 400
    assert a["net"] == 600


def test_a_bundle_contract_never_sets_a_unit_price(conn):
    """In a bundle the price covers everything, so it says nothing about the unit."""
    TITAN, TRIT = 671, 34
    _run_index(conn, [(CORP_A, "t1")],
               {CORP_A: [_contract(501, price=80_000_000_000)]},
               items={501: [{"type_id": TITAN, "quantity": 1},
                            {"type_id": TRIT, "quantity": 1}]})
    assert ch.contract_unit_prices(conn, [TITAN]) == {}


# ── public contracts across all of New Eden ───────────────────────────────────

def _pub(conn, cid, region=10000002, price=1_000_000, volume=0.0, system=None,
         type_="item_exchange", items=((34, 1),)):
    ch.ensure_public_contract_tables(conn)
    conn.execute(
        "INSERT OR REPLACE INTO public_contracts (contract_id, region_id, type, price,"
        " reward, collateral, buyout, volume, date_expired, title, start_location_id,"
        " end_location_id, issuer_id, system_id) VALUES (?,?,?,?,0,0,0,?,?,?,?,0,1,?)",
        (cid, region, type_, price, volume, "2026-09-19T00:00:00Z", f"c{cid}", 60000000 + cid,
         system))
    for tid, qty in items:
        conn.execute("INSERT INTO public_contract_items (contract_id, type_id, quantity,"
                     " is_included) VALUES (?,?,?,1)", (cid, tid, qty))
    conn.commit()


def _sov(conn, system_id, alliance_id=None, faction_id=None):
    ch.ensure_public_contract_tables(conn)
    conn.execute("INSERT OR REPLACE INTO sov_map_cache (system_id, alliance_id, faction_id,"
                 " cached_at) VALUES (?,?,?,?)", (system_id, alliance_id, faction_id,
                                                  time.time()))
    conn.commit()


@pytest.fixture
def clean_public(app_module):
    conn = app_module.get_conn()
    ch.ensure_public_contract_tables(conn)
    for t in ("public_contracts", "public_contract_items", "public_contract_meta",
              "public_contract_items_absent", "sov_map_cache", "station_system_cache"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    yield conn
    for t in ("public_contracts", "public_contract_items", "public_contract_meta",
              "public_contract_items_absent", "sov_map_cache", "station_system_cache"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()


def test_a_price_is_not_taken_from_a_system_a_player_alliance_holds(clean_public):
    """In sov space the market is usually shut to outsiders, so a price there says
    what one group charges its own - measured: 150 of 531 single-item contracts sat
    in sov systems or places we cannot even verify."""
    TITAN = 671
    _sov(clean_public, 30000142, faction_id=500006)      # Jita: NPC
    _sov(clean_public, 30004600, alliance_id=1900696668)  # someone's staging
    _pub(clean_public, 1, price=200_000_000_000, system=30004600, items=((TITAN, 1),))
    _pub(clean_public, 2, price=150_000_000_000, system=30000142, items=((TITAN, 1),))
    _pub(clean_public, 3, price=90_000_000_000, system=None, items=((TITAN, 1),))
    prices = ch.contract_unit_prices(clean_public, [TITAN])
    # The 90b one is cheapest but its location cannot be verified, and the 200b one
    # is in sov space: the NPC-space price wins.
    assert prices[TITAN] == (150_000_000_000.0, "public")


def test_npc_null_and_unclaimed_space_are_perfectly_good_references(clean_public):
    TITAN = 671
    _sov(clean_public, 30000157, faction_id=500010)       # Venal: NPC null
    _pub(clean_public, 1, price=120_000_000_000, system=30000157, items=((TITAN, 1),))
    assert ch.contract_unit_prices(clean_public, [TITAN])[TITAN][0] == 120_000_000_000.0
    # A system nobody holds at all (no sov row) is fine too.
    _pub(clean_public, 2, price=110_000_000_000, system=30002000, items=((TITAN, 1),))
    assert ch.contract_unit_prices(clean_public, [TITAN])[TITAN][0] == 110_000_000_000.0


def test_public_search_spans_every_region_unless_one_is_named(clean_public):
    """Listing all of known space costs 106 requests, so a region is a filter now."""
    _pub(clean_public, 1, region=10000002)
    _pub(clean_public, 2, region=10000043)
    assert len(ch.search_public_contracts(clean_public)) == 2
    only = ch.search_public_contracts(clean_public, 10000043)
    assert [c["contract_id"] for c in only] == [2]
    assert only[0]["region_id"] == 10000043


def test_contents_are_fetched_biggest_volume_first(clean_public, monkeypatch):
    """Capitals are the biggest contracts there are, so volume order is what makes
    a capital's price available in seconds instead of after a full index."""
    import asyncio
    for cid, vol in ((1, 5.0), (2, 1_300_000.0), (3, 100.0), (4, 62_000_000.0)):
        _pub(clean_public, cid, volume=vol, items=())
    order = []

    class _Resp:
        status_code = 200
        def json(self): return [{"type_id": 34, "quantity": 1}]

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, **kw):
            order.append(int(url.rstrip("/").rsplit("/", 1)[-1]))
            return _Resp()

    monkeypatch.setattr(ch, "esi_client", lambda **kw: _Client())
    res = asyncio.run(ch.fill_public_items(clean_public, budget=2))
    assert order == [4, 2]                      # 62M m3 then 1.3M m3
    assert res["fetched"] == 2 and res["remaining"] == 2


def test_relisting_a_region_keeps_the_contents_already_fetched(clean_public):
    """Contents are immutable and expensive; a listing refresh must not throw them
    away, only drop what fell out of the listing entirely."""
    _pub(clean_public, 1, region=10000002, items=((34, 5),))
    _pub(clean_public, 2, region=10000002, items=((35, 5),))
    keep = [{"contract_id": 1, "type": "item_exchange", "price": 1.0, "volume": 0,
             "start_location_id": 60000001, "date_expired": "", "title": ""}]
    ch._store_public_listing(clean_public, 10000002, keep, {})
    left = {r[0] for r in clean_public.execute(
        "SELECT DISTINCT contract_id FROM public_contract_items").fetchall()}
    assert left == {1}


# ── the Plan page for hulls the market does not carry ─────────────────────────

def test_a_hull_with_no_market_price_gets_one_from_contracts(app_module, conn):
    """A Wyvern is never on a market order, so the plan had no revenue at all - and
    the whole profit section disappeared with it."""
    from types import SimpleNamespace
    row = conn.execute("SELECT type_id FROM sde_types WHERE name='Wyvern'").fetchone()
    assert row, "the bundled SDE must know the hull"
    tid = row[0]
    _run_index(conn, [(CORP_A, "t1")],
               {CORP_A: [_contract(700, price=42_500_000_000)]},
               items={700: [{"type_id": tid, "quantity": 1}]})

    plan = SimpleNamespace(blueprint=None, materials=[], product_type_id=tid,
                           quantity=10, mode="full", location_id=60003760,
                           can_manufacture=True, total_missing_types=0,
                           opt_total_cost=0.0, opt_naive_cost=0.0)
    # The market knows no price for it: `prices` is deliberately empty.
    out = app_module._plan_to_dict(plan, {}, "Wyvern", conn=conn)
    assert out["sell_price"] == 42_500_000_000
    assert out["sell_price_source"] == "contract"
    assert out["revenue"] == 425_000_000_000

    # A product the market does price keeps saying where the number came from.
    out2 = app_module._plan_to_dict(plan, {tid: (5.0, 4.0)}, "Wyvern", conn=conn)
    assert out2["sell_price"] == 5.0 and out2["sell_price_source"] == "jita"


def test_the_profit_table_is_not_hidden_when_there_is_no_price(app_module):
    """It is computed in the browser and exists precisely so a price can be fetched
    into it, so gating it on already having one is backwards."""
    import pathlib as _pl
    import re as _re
    tpl = _pl.Path("app/web/templates/plan.html").read_text()
    i = tpl.index("Profit by sell price source")
    guard = _re.search(r"\{% if [^%]*%\}", tpl[i:i + 600]).group(0)
    assert "result.materials" in guard, guard
    assert "revenue" not in guard, guard
