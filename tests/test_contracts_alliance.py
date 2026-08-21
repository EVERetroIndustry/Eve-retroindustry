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
