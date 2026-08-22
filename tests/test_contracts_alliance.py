"""Corporation contracts: token choice and honest labelling.

Measured against real data (alliance The Initiative., 2026-08-22):
/corporations/{id}/contracts/ returns far more than its own docs promise - it also
returns every contract assigned to the corporation's ALLIANCE, issued by other
member corps. Two corps of the same alliance returned the same 2912 contracts, and
the character endpoint returned none of them. `availability` is NOT the marker
(ESI reports "personal" for all of them); `assignee_id == alliance_id` is.

Consequences these tests pin down:
  1. the row must be labelled by who the contract is assigned to, not by the
     corporation whose endpoint happened to answer;
  2. the corporation token must be one that actually carries the contracts scope,
     or a whole corporation silently disappears behind a 403.
"""
from __future__ import annotations

import jwt
import pytest

CORP = 98000001
ALLIANCE = 99000001
SCOPE = "esi-contracts.read_corporation_contracts.v1"
CHAR_A, CHAR_B = 900000001, 900000002


def _jwt(scopes: list[str]) -> str:
    return jwt.encode({"sub": "CHARACTER:EVE:1", "name": "T", "scp": scopes},
                      "unused-secret", algorithm="HS256")


# ── token_has_scope ───────────────────────────────────────────────────────────

def test_token_has_scope_reads_the_scp_claim():
    from app.auth.esi_oauth import token_has_scope
    assert token_has_scope(_jwt([SCOPE, "esi-wallet.read_character_wallet.v1"]), SCOPE)
    assert not token_has_scope(_jwt(["esi-wallet.read_character_wallet.v1"]), SCOPE)
    # A single scope may arrive as a bare string rather than a list.
    assert token_has_scope(_jwt(SCOPE), SCOPE)


def test_token_has_scope_never_raises_on_junk():
    from app.auth.esi_oauth import token_has_scope
    assert not token_has_scope(None, SCOPE)
    assert not token_has_scope("", SCOPE)
    assert not token_has_scope("not-a-jwt", SCOPE)
    assert not token_has_scope(_jwt([]), SCOPE)


# ── the page ──────────────────────────────────────────────────────────────────

@pytest.fixture
def two_chars_one_corp(app_module):
    """CHAR_A has no contracts scope, CHAR_B does - both in the same corporation.

    CHAR_A is the older character, so it is the one the page used to pick.
    """
    conn = app_module.get_conn()
    before = {r[0]: r[1] for r in conn.execute(
        "SELECT character_id, access_token FROM characters").fetchall()}
    conn.execute("UPDATE characters SET access_token=?, corporation_id=? WHERE character_id=?",
                 (_jwt(["esi-wallet.read_character_wallet.v1"]), CORP, CHAR_A))
    conn.execute("UPDATE characters SET access_token=?, corporation_id=? WHERE character_id=?",
                 (_jwt([SCOPE]), CORP, CHAR_B))
    conn.commit()
    conn.close()
    yield
    conn = app_module.get_conn()
    for cid, tok in before.items():
        conn.execute("UPDATE characters SET access_token=? WHERE character_id=?", (tok, cid))
    conn.commit()
    conn.close()


ALLIANCE_CONTRACT = {
    "contract_id": 234230831, "type": "item_exchange", "status": "outstanding",
    "availability": "personal",          # ESI really says this for alliance contracts
    "issuer_id": 2113770487, "issuer_corporation_id": 98465001,
    "assignee_id": ALLIANCE, "acceptor_id": 0, "for_corporation": False,
    "price": 1_300_000_000.0, "title": "i.Shadow Fleet DPS",
    "date_issued": "2026-08-20T04:24:58Z", "date_expired": "2026-09-19T04:24:58Z",
    "start_location_id": 60003760, "volume": 1.0,
}


def _corp_page(client, app_module, fetch, party_names=None):
    real = (app_module.contracts_api.fetch_corp_contracts, app_module._resolve_party_names)
    app_module.contracts_api.fetch_corp_contracts = fetch

    async def _names(ids):
        base = {ALLIANCE: "The Initiative.", CORP: "Test Corp",
                2113770487: "Some Pilot", 98465001: "Other Corp"}
        base.update(party_names or {})
        return {i: base.get(i, str(i)) for i in ids}

    app_module._resolve_party_names = _names
    try:
        return client.get("/contracts?char=all&scope=corp").text
    finally:
        (app_module.contracts_api.fetch_corp_contracts,
         app_module._resolve_party_names) = real


def test_alliance_contract_is_labelled_by_its_assignee_not_the_queried_corp(
        client, app_module, two_chars_one_corp):
    """The reported bug: alliance-wide contracts showed up under one of my corps."""
    async def _fetch(cl, corp_id, token):
        return [dict(ALLIANCE_CONTRACT)], None

    html = _corp_page(client, app_module, _fetch)
    assert "Assigned to" in html            # the column says what it means
    assert "The Initiative." in html        # the alliance the contract is for
    assert "Test Corp" not in html          # never the corp whose endpoint answered


def test_a_character_without_the_scope_does_not_hide_the_corporation(
        client, app_module, two_chars_one_corp):
    """The older character has no contracts scope; the corp must still be read."""
    used: list[str] = []

    async def _fetch(cl, corp_id, token):
        from app.auth.esi_oauth import token_has_scope
        used.append("ok" if token_has_scope(token, SCOPE) else "403")
        if not token_has_scope(token, SCOPE):
            return None, "This character lacks the corporation role to read contracts (Accountant)."
        return [dict(ALLIANCE_CONTRACT)], None

    html = _corp_page(client, app_module, _fetch)
    # The capable token is tried FIRST, so no 403 is spent at all.
    assert used == ["ok"], used
    assert "The Initiative." in html
    assert "No character with corporation contract access" not in html


def test_a_corp_nobody_can_read_is_named_instead_of_silently_dropped(
        client, app_module, two_chars_one_corp):
    async def _fetch(cl, corp_id, token):
        return None, "403"

    html = _corp_page(client, app_module, _fetch)
    assert "No character with corporation contract access for: Test Corp" in html
    assert "Re-add" in html


def test_single_character_without_the_scope_gets_the_real_reason(client, app_module,
                                                                two_chars_one_corp):
    """A guaranteed 403 is not worth spending, and "Accountant role" would mislead."""
    async def _fetch(cl, corp_id, token):
        raise AssertionError("must not call ESI with a token that cannot work")

    real = app_module.contracts_api.fetch_corp_contracts
    app_module.contracts_api.fetch_corp_contracts = _fetch
    try:
        html = client.get(f"/contracts?char={CHAR_A}&scope=corp").text
    finally:
        app_module.contracts_api.fetch_corp_contracts = real
    assert "added before the app asked for corporation contract access" in html
    assert "Accountant" not in html


# ── status buckets, row cap, and the hooks the client-side filter needs ────────

def _rows(status_raw, n=1, **kw):
    out = []
    for i in range(n):
        r = {"contract_id": 1000 + i, "status_raw": status_raw, "type_raw": "item_exchange",
             "type": "Item Exchange", "status": status_raw.title(), "title": f"t{i}",
             "price": 1.0, "reward": 0.0, "collateral": 0.0, "volume": 0.0,
             "issuer": "P", "start": "Jita", "end": "", "party_label": "",
             "date_expired": "2026-09-19T10:00:00Z", "issuer_id": 1, "issuer_corp_id": 2}
        r.update(kw)
        out.append(r)
    return out


def test_open_is_the_default_and_means_outstanding_plus_in_progress(app_module):
    rows = (_rows("outstanding", 2) + _rows("in_progress") + _rows("finished", 3)
            + _rows("deleted", 4))
    kept, total, trunc = app_module._apply_contract_view(rows, "open", ())
    assert {r["status_raw"] for r in kept} == {"outstanding", "in_progress"}
    assert total == 3 and not trunc


def test_the_finished_bucket_covers_both_finished_flavours(app_module):
    rows = (_rows("finished") + _rows("finished_issuer") + _rows("finished_contractor")
            + _rows("outstanding") + _rows("failed"))
    kept, total, _ = app_module._apply_contract_view(rows, "finished", ())
    assert total == 3
    assert {r["status_raw"] for r in kept} == {
        "finished", "finished_issuer", "finished_contractor"}


def test_any_keeps_every_status(app_module):
    rows = _rows("outstanding") + _rows("deleted") + _rows("reversed")
    kept, total, _ = app_module._apply_contract_view(rows, "any", ())
    assert total == 3 and len(kept) == 3


def test_the_row_cap_reports_that_it_truncated(app_module):
    rows = _rows("outstanding", app_module._CONTRACT_ROW_CAP + 25)
    kept, total, trunc = app_module._apply_contract_view(rows, "any", ())
    assert len(kept) == app_module._CONTRACT_ROW_CAP
    assert total == app_module._CONTRACT_ROW_CAP + 25 and trunc


def test_my_own_contracts_are_marked_by_character_or_corporation(app_module):
    rows = (_rows("outstanding", 1, issuer_id=900000001, issuer_corp_id=5)
            + _rows("outstanding", 1, issuer_id=42, issuer_corp_id=98000001)
            + _rows("outstanding", 1, issuer_id=42, issuer_corp_id=5))
    kept, _, _ = app_module._apply_contract_view(rows, "any", (900000001, 98000001))
    assert [r["is_own"] for r in kept] == [True, True, False]


def test_rendered_rows_carry_what_the_client_filter_reads(client, app_module):
    """The filter and the sorter run in the browser off these data-* attributes."""
    api = app_module.contracts_api
    real = (api.fetch_character_contracts, app_module._resolve_party_names)

    async def _fetch(cl, cid, tok):
        return [{"contract_id": 7001, "type": "courier", "status": "outstanding",
                 "title": "Jita run", "price": 0, "reward": 25_000_000,
                 "collateral": 900_000_000, "volume": 330_000,
                 "issuer_id": 900000001, "issuer_corporation_id": 98000001,
                 "date_issued": "2026-08-20T10:00:00Z",
                 "date_expired": "2026-09-19T10:00:00Z",
                 "start_location_id": 60003760, "end_location_id": 60003760}]

    async def _names(ids):
        return {i: "Someone" for i in ids}

    api.fetch_character_contracts = _fetch
    app_module._resolve_party_names = _names
    try:
        html = client.get("/contracts?char=900000001&scope=personal").text
    finally:
        (api.fetch_character_contracts, app_module._resolve_party_names) = real

    for attr in ('data-search=', 'data-type="courier"', 'data-typelabel="Courier"',
                 'data-status="outstanding"', 'data-price=', 'data-collateral=',
                 'data-expires=', 'data-own="1"'):
        assert attr in html, attr
    # Numbers are compared as numbers: whether ESI hands us an int or a float only
    # changes the rendered text, not what the filter parses out of it.
    import re as _re
    for key, want in (("reward", 25_000_000.0), ("volume", 330_000.0)):
        m = _re.search(rf'data-{key}="([^"]*)"', html)
        assert m and float(m.group(1)) == want, (key, m and m.group(1))
    # The columns the in-game window lets you sort by.
    for col in ("typelabel:text", "price:num", "reward:num", "collateral:num",
                "expires:text", "statuslabel:text", "location:text"):
        assert f'data-sort="{col}"' in html, col
    assert 'id="contract-filters"' in html and 'id="contract-table"' in html
