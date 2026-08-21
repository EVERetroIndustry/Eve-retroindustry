"""Custom corporation division names (wallet tabs + hangar divisions).

A corporation can rename each of its 7 wallet and 7 hangar divisions, and ESI
returns those names from /corporations/{id}/divisions/ - but ONLY for divisions
that are not using the default name, and only to a Director. So the interesting
part is not the happy path, it is what we fall back to.
"""
from __future__ import annotations

import asyncio
import time as _time

import pytest


# ── fake ESI ──────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class _Client:
    """Minimal stand-in for the httpx client the fetcher is handed."""

    def __init__(self, resp):
        self._resp = resp
        self.calls = 0

    async def get(self, url, **kw):
        self.calls += 1
        return self._resp


def _fetch(resp):
    from app.character import wallet as w
    return asyncio.run(w.fetch_corp_divisions(_Client(resp), 98000001, "tok"))


# ── the fetcher ───────────────────────────────────────────────────────────────

def test_only_renamed_divisions_come_back():
    """ESI omits divisions still on their default name - that is not an error."""
    data, err = _fetch(_Resp(200, {
        "wallet": [{"division": 1, "name": "Master Wallet"},
                   {"division": 3, "name": "Buyback"}],
        "hangar": [{"division": 2, "name": "Ore"}],
    }))
    assert err is None
    assert data["wallet"] == {1: "Master Wallet", 3: "Buyback"}
    assert data["hangar"] == {2: "Ore"}


def test_blank_and_malformed_entries_are_ignored():
    """A blank name must not blank out a division label."""
    data, err = _fetch(_Resp(200, {
        "wallet": [{"division": 2, "name": ""}, {"division": 4, "name": "   "},
                   {"division": 5}, {"name": "no division"},
                   {"division": 6, "name": " Trimmed "}],
        "hangar": [],
    }))
    assert err is None
    assert data["wallet"] == {6: "Trimmed"}
    assert data["hangar"] == {}


def test_missing_keys_do_not_raise():
    data, err = _fetch(_Resp(200, {}))
    assert err is None and data == {"wallet": {}, "hangar": {}}


def test_403_without_the_director_role_says_so():
    _, err = _fetch(_Resp(403, {"error": "Character does not have required role(s)"}))
    assert "Director" in err and "role" in err


def test_403_without_the_scope_asks_for_a_re_add():
    """Characters authorized before the scope existed get a plain 403."""
    _, err = _fetch(_Resp(403, {"error": "Forbidden"}))
    assert "Re-add" in err


def test_other_http_and_transport_errors_are_reported():
    _, err = _fetch(_Resp(500, None))
    assert "500" in err

    from app.character import wallet as w

    class _Boom:
        async def get(self, *a, **k):
            raise RuntimeError("connection reset")

    _, err = asyncio.run(w.fetch_corp_divisions(_Boom(), 1, "tok"))
    assert "connection reset" in err


# ── label merging ─────────────────────────────────────────────────────────────

def test_wallet_labels_keep_defaults_for_unnamed_divisions(app_module):
    labels = app_module._wallet_division_labels({3: "Buyback"})
    assert labels[3] == "Buyback"
    assert labels[1] == "Master Wallet" and labels[7] == "7th Wallet"
    assert sorted(labels) == [1, 2, 3, 4, 5, 6, 7]


def test_hangar_labels_only_override_the_division_flags(app_module):
    labels = app_module._corp_hangar_labels({2: "Ore", 9: "not a division"})
    assert labels["CorpSAG2"] == "Ore"
    assert labels["CorpSAG1"] == "Division 1"      # untouched default
    # Neither of these is a numbered division and must never be renamed.
    assert labels["Hangar"] == "Hangar"
    assert labels["CorpDeliveries"] == "Deliveries"
    assert "CorpSAG9" not in labels
    # The shared constant must not be mutated by the merge.
    assert app_module._CORP_DIV_LABEL["CorpSAG2"] == "Division 2"


# ── cache behaviour ───────────────────────────────────────────────────────────

CORP = 98000001


@pytest.fixture
def clean_div_cache(app_module):
    def _clear():
        conn = app_module.get_conn()
        app_module._ensure_corp_division_cache(conn)
        conn.execute("DELETE FROM corp_division_cache")
        conn.commit()
        conn.close()
    _clear()
    yield _clear
    _clear()


def _names(app_module, **stub):
    """Run _corp_division_names with fetch_corp_divisions replaced."""
    real = app_module.wallet_api.fetch_corp_divisions
    app_module.wallet_api.fetch_corp_divisions = stub["fn"]
    conn = app_module.get_conn()
    try:
        return asyncio.run(app_module._corp_division_names(
            conn, stub.get("corp", CORP), stub.get("token", "tok")))
    finally:
        app_module.wallet_api.fetch_corp_divisions = real
        conn.close()


def test_names_are_cached_so_every_page_view_is_not_an_esi_call(app_module, clean_div_cache):
    hits = []

    async def _ok(client, corp_id, token):
        hits.append(corp_id)
        return {"wallet": {2: "Buyback"}, "hangar": {5: "Ore"}}, None

    first = _names(app_module, fn=_ok)
    assert first["wallet"] == {2: "Buyback"} and first["hangar"] == {5: "Ore"}
    assert len(hits) == 1

    async def _never(client, corp_id, token):
        raise AssertionError("second call must be served from the cache")

    second = _names(app_module, fn=_never)
    assert second["wallet"] == {2: "Buyback"} and second["hangar"] == {5: "Ore"}
    assert second["error"] is None


def test_a_failed_refresh_keeps_the_last_known_names(app_module, clean_div_cache):
    """A stale real name beats falling back to "3rd Wallet"."""
    async def _ok(client, corp_id, token):
        return {"wallet": {2: "Buyback"}, "hangar": {}}, None

    _names(app_module, fn=_ok)
    # Age the cache past the TTL so the next call really tries ESI.
    conn = app_module.get_conn()
    conn.execute("UPDATE corp_division_cache SET cached_at=? WHERE corporation_id=?",
                 (_time.time() - app_module._CORP_DIVISION_TTL - 60, CORP))
    conn.commit()
    conn.close()

    async def _fail(client, corp_id, token):
        return None, "ESI returned HTTP 503."

    out = _names(app_module, fn=_fail)
    assert out["wallet"] == {2: "Buyback"}
    # Nothing to tell the user - they can still see the real names.
    assert out["error"] is None


def test_the_error_is_only_surfaced_when_there_is_nothing_cached(app_module, clean_div_cache):
    async def _fail(client, corp_id, token):
        return None, "Only a Director can read custom division names."

    out = _names(app_module, fn=_fail)
    assert out["wallet"] == {} and out["hangar"] == {}
    assert "Director" in out["error"]


def test_no_token_means_no_esi_call(app_module, clean_div_cache):
    async def _never(client, corp_id, token):
        raise AssertionError("must not call ESI without a token")

    out = _names(app_module, fn=_never, token=None)
    assert out == {"wallet": {}, "hangar": {}, "error": None}


# ── end to end ────────────────────────────────────────────────────────────────

def test_corp_wallet_page_shows_the_custom_division_name(client, app_module, clean_div_cache):
    api = app_module.wallet_api
    real = (api.fetch_corp_wallets, api.fetch_corp_divisions, api.fetch_corp_journal,
            api.fetch_corp_transactions, app_module._resolve_party_names)

    async def _wallets(cl, corp_id, token):
        return [{"division": 1, "balance": 1.0}, {"division": 3, "balance": 2.0}], None

    async def _divs(cl, corp_id, token):
        return {"wallet": {3: "Buyback Bank"}, "hangar": {}}, None

    async def _journal(cl, corp_id, div, token, limit=0):
        return []

    async def _txns(cl, corp_id, div, token):
        return []

    async def _party(ids):
        return {i: "Test Corp" for i in ids}

    api.fetch_corp_wallets = _wallets
    api.fetch_corp_divisions = _divs
    api.fetch_corp_journal = _journal
    api.fetch_corp_transactions = _txns
    app_module._resolve_party_names = _party
    try:
        html = client.get("/wallet?char=900000001&scope=corp&division=3").text
    finally:
        (api.fetch_corp_wallets, api.fetch_corp_divisions, api.fetch_corp_journal,
         api.fetch_corp_transactions, app_module._resolve_party_names) = real

    assert "Buyback Bank" in html          # the selector card and the balance header
    assert "3rd Wallet" not in html        # the default it replaced is gone
    assert "Master Wallet" in html         # division 1 was not renamed


def test_corp_hangar_division_shows_the_custom_name(client, app_module, clean_div_cache):
    """The same cached call renames the Assets hangar divisions."""
    from app.character.assets import CharAsset
    TRIT = 34
    corp_assets = [CharAsset(item_id=770001, type_id=TRIT, location_id=60003760,
                             location_flag="CorpSAG2", quantity=100,
                             is_singleton=False, is_blueprint_copy=False)]

    async def _corp(cl, char_id, token, conn, force_refresh=False):
        return CORP, corp_assets

    async def _divs(cl, corp_id, token):
        return {"wallet": {}, "hangar": {2: "Ore Buyback"}}, None

    real = (app_module.fetch_corp_assets, app_module.wallet_api.fetch_corp_divisions)
    app_module.fetch_corp_assets = _corp
    app_module.wallet_api.fetch_corp_divisions = _divs
    try:
        html = client.get("/assets?view=900000001").text
    finally:
        (app_module.fetch_corp_assets, app_module.wallet_api.fetch_corp_divisions) = real

    assert "Ore Buyback" in html
    assert "Division 2" not in html


def test_a_failure_is_cached_so_a_403_is_not_repeated_on_every_page_view(
        app_module, clean_div_cache):
    """A 4xx costs 5 tokens in the ESI bucket and a slot in the error budget."""
    calls = []

    async def _fail(client, corp_id, token):
        calls.append(corp_id)
        return None, "Only a Director can read custom division names."

    first = _names(app_module, fn=_fail)
    assert "Director" in first["error"] and len(calls) == 1

    async def _never(client, corp_id, token):
        raise AssertionError("the failure must be remembered, not retried")

    second = _names(app_module, fn=_never)
    assert "Director" in second["error"]


def test_a_cached_failure_is_retried_once_its_short_ttl_expires(app_module, clean_div_cache):
    """Re-adding the character must start working without a long wait."""
    async def _fail(client, corp_id, token):
        return None, "Re-add this character..."

    _names(app_module, fn=_fail)
    conn = app_module.get_conn()
    conn.execute("UPDATE corp_division_cache SET cached_at=? WHERE corporation_id=?",
                 (_time.time() - app_module._CORP_DIVISION_ERROR_TTL - 60, CORP))
    conn.commit()
    conn.close()

    async def _ok(client, corp_id, token):
        return {"wallet": {4: "Ship Fund"}, "hangar": {}}, None

    out = _names(app_module, fn=_ok)
    assert out["wallet"] == {4: "Ship Fund"} and out["error"] is None
    # The error TTL is much shorter than the success TTL - that is the point.
    assert app_module._CORP_DIVISION_ERROR_TTL < app_module._CORP_DIVISION_TTL


def test_re_adding_a_character_clears_only_the_failed_rows(app_module, clean_div_cache):
    conn = app_module.get_conn()
    app_module._save_corp_divisions(conn, CORP, {"wallet": {2: "Buyback"}, "hangar": {}}, None)
    app_module._save_corp_divisions(conn, 98000002, {"wallet": {}, "hangar": {}}, "403")
    app_module._clear_failed_corp_divisions(conn)
    left = [r[0] for r in conn.execute(
        "SELECT corporation_id FROM corp_division_cache").fetchall()]
    conn.close()
    assert left == [CORP]
