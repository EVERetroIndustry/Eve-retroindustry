"""Assets tree: ship-hull folding, container labels and container-aware search.

Covers three defects reported together for assembled ships:
  * a searched-for ship showed as a bare hull with nothing to expand,
  * its value excluded the hull (the fold had silently died in v0.8.60 when the
    hangar bucket key gained an is_copy element and the folds kept rebuilding
    the old two-element key),
  * the custom name replaced the ship type, so you could not see or search for
    what hull it actually was.
"""
from __future__ import annotations

MEGATHRON, ANTIMATTER, TRIT = 641, 238, 34   # Megathron, Antimatter Charge L, Tritanium
OWNER = 900000001


def _hangar_row(type_id, name, qty, price, owner=OWNER, is_copy=False):
    return {
        "type_id": type_id, "name": name, "quantity": qty,
        "is_blueprint_copy": is_copy, "character_id": owner, "character_name": "Pilot",
        "unit_price": price, "total_value": (None if is_copy else price * qty),
    }


def _node():
    """One station: two Megathrons in the hangar, one of them assembled with a fit.

    Keyed exactly like assets_page keys it — (type_id, owner_id, is_copy).
    """
    ship_container_id = 555001
    return {
        "hangar": {
            (MEGATHRON, OWNER, False): _hangar_row(MEGATHRON, "Megathron", 2, 100_000_000.0),
            (TRIT, OWNER, False): _hangar_row(TRIT, "Tritanium", 1000, 5.0),
        },
        "containers": {
            ship_container_id: {
                (ANTIMATTER, OWNER, False): _hangar_row(
                    ANTIMATTER, "Antimatter Charge L", 400, 100.0),
            },
        },
    }, ship_container_id


# ── container / ship labels ───────────────────────────────────────────────────

def test_label_keeps_both_custom_name_and_type(app_module):
    f = app_module._container_display_name
    assert f("Blue Thunder", "Megathron", 1) == "Blue Thunder (Megathron)"
    # No custom name → the type is the label.
    assert f("", "Megathron", 1) == "Megathron"
    # ESI sends the literal string "None" for an unnamed item.
    assert f("None", "Megathron", 1) == "Megathron"
    assert f("none", "Megathron", 1) == "Megathron"
    # Don't repeat a type the pilot already put in the name.
    assert f("My Megathron", "Megathron", 1) == "My Megathron"
    assert f("Blue Thunder", "", 1) == "Blue Thunder"
    assert f("", "", 777) == "Container 777"


# ── hull folding ──────────────────────────────────────────────────────────────

def test_hull_is_folded_into_its_ship_container(app_module):
    node, ship_cid = _node()
    app_module._fold_ship_hulls(node, {ship_cid: MEGATHRON}, {ship_cid: (OWNER, "Pilot")})

    hull = node["containers"][ship_cid][("_hull", ship_cid)]
    assert hull["type_id"] == MEGATHRON and hull["quantity"] == 1
    assert hull["total_value"] == 100_000_000.0
    # The assembled one left the hangar; the other Megathron stays.
    hangar_row = node["hangar"][(MEGATHRON, OWNER, False)]
    assert hangar_row["quantity"] == 1
    assert hangar_row["total_value"] == 100_000_000.0     # re-totalled, not stale

    # Ship total = hull + fit/cargo, which is the number the user wants.
    assert sum(i["total_value"] for i in node["containers"][ship_cid].values()) == \
        100_000_000.0 + 400 * 100.0


def test_last_hull_leaves_no_empty_hangar_row(app_module):
    node, ship_cid = _node()
    node["hangar"][(MEGATHRON, OWNER, False)] = _hangar_row(
        MEGATHRON, "Megathron", 1, 100_000_000.0)
    app_module._fold_ship_hulls(node, {ship_cid: MEGATHRON}, {ship_cid: (OWNER, "Pilot")})
    assert (MEGATHRON, OWNER, False) not in node["hangar"]


def test_fold_survives_a_changed_bucket_key(app_module):
    """The regression guard: the fold must not depend on the key's shape.

    v0.8.60 added is_copy to the key and the fold, which rebuilt (type_id,
    owner_id), stopped matching without any error — hulls were never folded for
    ten minor versions. Match on the row's fields instead.
    """
    node, ship_cid = _node()
    # Re-key the hangar with an extra element the fold knows nothing about.
    node["hangar"] = {(k[0], k[1], k[2], "extra"): v for k, v in node["hangar"].items()}
    app_module._fold_ship_hulls(node, {ship_cid: MEGATHRON}, {ship_cid: (OWNER, "Pilot")})
    assert ("_hull", ship_cid) in node["containers"][ship_cid]


def test_fold_never_consumes_a_blueprint_copy(app_module):
    node, ship_cid = _node()
    node["hangar"] = {
        (MEGATHRON, OWNER, True): _hangar_row(
            MEGATHRON, "Megathron Blueprint", 1, None, is_copy=True),
    }
    app_module._fold_ship_hulls(node, {ship_cid: MEGATHRON}, {ship_cid: (OWNER, "Pilot")})
    assert ("_hull", ship_cid) not in node["containers"][ship_cid]
    assert node["hangar"][(MEGATHRON, OWNER, True)]["quantity"] == 1


def test_fold_matches_the_right_owner(app_module):
    node, ship_cid = _node()
    other = 900000002
    node["hangar"] = {
        (MEGATHRON, other, False): _hangar_row(MEGATHRON, "Megathron", 1, 1.0, owner=other),
    }
    app_module._fold_ship_hulls(node, {ship_cid: MEGATHRON}, {ship_cid: (OWNER, "Pilot")})
    assert ("_hull", ship_cid) not in node["containers"][ship_cid]   # not this pilot's ship
    assert node["hangar"][(MEGATHRON, other, False)]["quantity"] == 1


def test_corp_fold_without_an_owner_map(app_module):
    node, ship_cid = _node()
    for row in node["hangar"].values():
        row.pop("character_id", None)
    app_module._fold_ship_hulls(node, {ship_cid: MEGATHRON})
    hull = node["containers"][ship_cid][("_hull", ship_cid)]
    assert hull["quantity"] == 1
    assert "character_id" not in hull


# ── container-aware search ────────────────────────────────────────────────────

def _folded():
    node, ship_cid = _node()
    from app.web import main as m
    m._fold_ship_hulls(node, {ship_cid: MEGATHRON}, {ship_cid: (OWNER, "Pilot")})
    return node, ship_cid, {ship_cid: "Blue Thunder (Megathron)"}


def test_searching_a_ship_type_keeps_its_whole_fit(app_module):
    """The reported bug: this used to leave a hull row and nothing to expand."""
    node, ship_cid, labels = _folded()
    app_module._prune_by_search(node, labels, "megathron")
    assert ship_cid in node["containers"]
    items = node["containers"][ship_cid]
    assert ("_hull", ship_cid) in items                     # hull kept
    assert (ANTIMATTER, OWNER, False) in items              # and the fit/cargo
    assert node["hangar"]                                    # the spare hull matches too


def test_searching_a_custom_ship_name_keeps_its_whole_fit(app_module):
    node, ship_cid, labels = _folded()
    app_module._prune_by_search(node, labels, "blue thunder")
    assert node["containers"][ship_cid].keys() == _folded()[0]["containers"][ship_cid].keys()
    assert not node["hangar"]                                # nothing else matches


def test_searching_cargo_keeps_the_container_expandable(app_module):
    node, ship_cid, labels = _folded()
    app_module._prune_by_search(node, labels, "antimatter")
    assert list(node["containers"][ship_cid]) == [(ANTIMATTER, OWNER, False)]
    assert not node["hangar"]


def test_search_dropping_everything_leaves_nothing(app_module):
    node, ship_cid, labels = _folded()
    app_module._prune_by_search(node, labels, "zzz-no-such-item")
    assert node["hangar"] == {} and node["containers"] == {}


def test_empty_search_is_a_no_op(app_module):
    node, ship_cid, labels = _folded()
    before_h, before_c = dict(node["hangar"]), dict(node["containers"])
    app_module._prune_by_search(node, labels, "")
    app_module._prune_by_search(node, labels, "   ")
    assert node["hangar"] == before_h and node["containers"] == before_c


# ── end to end through the real /assets route ─────────────────────────────────

import json as _json
import time as _time

import pytest


@pytest.fixture
def assembled_ship(app_module):
    """Give char 2 one assembled, fitted Megathron plus a spare packaged one.

    Restores the character's cached assets afterwards so the shared session DB
    is left exactly as the other tests expect it.
    """
    CHAR = 900000002
    SHIP_ITEM = 555001
    conn = app_module.get_conn()
    row = conn.execute("SELECT data_json, cached_at FROM char_assets_cache"
                       " WHERE character_id=?", (CHAR,)).fetchone()
    original = (row[0], row[1]) if row else None
    now = _time.time()
    assets = [
        # two hulls in the hangar; one of them is the assembled ship below
        {"item_id": 500001, "type_id": MEGATHRON, "quantity": 2,
         "location_id": 60003760, "location_flag": "Hangar", "is_singleton": False},
        {"item_id": SHIP_ITEM, "type_id": MEGATHRON, "quantity": 1,
         "location_id": 60003760, "location_flag": "Hangar", "is_singleton": True},
        # ...its cargo
        {"item_id": 500003, "type_id": ANTIMATTER, "quantity": 400,
         "location_id": SHIP_ITEM, "location_flag": "Cargo", "is_singleton": False},
        # something unrelated at the same station
        {"item_id": 500004, "type_id": TRIT, "quantity": 1000,
         "location_id": 60003760, "location_flag": "Hangar", "is_singleton": False},
    ]
    # char_assets_cache has no unique constraint on character_id, so INSERT OR
    # REPLACE would just append a second row and _load_cache's fetchone() would
    # keep returning the old one. Delete first, exactly like _save_cache does.
    conn.execute("DELETE FROM char_assets_cache WHERE character_id=?", (CHAR,))
    conn.execute("INSERT INTO char_assets_cache (character_id, data_json, cached_at)"
                 " VALUES (?,?,?)", (CHAR, _json.dumps(assets), now))
    for tid, price in ((MEGATHRON, 100_000_000.0), (ANTIMATTER, 100.0), (TRIT, 5.0)):
        conn.execute("INSERT OR REPLACE INTO market_price_cache"
                     " (type_id, sell_price, buy_price, cached_at) VALUES (?,?,?,?)",
                     (tid, price, price * 0.9, now))
    conn.commit()
    conn.close()

    # Keep the test hermetic: the real resolver POSTs to ESI /assets/names/.
    real = app_module._resolve_container_names

    async def _fake(char_id, token, container_ids, assets_raw):
        return {cid: (app_module._container_display_name("Blue Thunder", "Megathron", cid),
                      60003760) for cid in container_ids}

    app_module._resolve_container_names = _fake
    yield CHAR, SHIP_ITEM
    app_module._resolve_container_names = real
    conn = app_module.get_conn()
    conn.execute("DELETE FROM char_assets_cache WHERE character_id=?", (CHAR,))
    if original:
        conn.execute("INSERT INTO char_assets_cache"
                     " (character_id, data_json, cached_at) VALUES (?,?,?)",
                     (CHAR, original[0], original[1]))
    conn.commit()
    conn.close()


def _text(client, url: str) -> str:
    """Page HTML with non-breaking spaces normalised.

    The ISK filters join thousands with U+00A0 so numbers never wrap; asserting
    on that invisible character in test source is a trap.
    """
    return client.get(url).text.replace("\u00a0", " ")

def test_route_shows_ship_with_type_and_folded_hull(client, assembled_ship):
    char, ship_item = assembled_ship
    html = _text(client, f"/assets?view={char}")
    assert "Blue Thunder (Megathron)" in html      # custom name AND the hull type
    # hull + cargo = 100M + 40k, formatted with the app's space thousands separator
    assert "100 040 000" in html


def test_route_search_by_ship_type_keeps_the_fit(client, assembled_ship):
    """The reported bug, end to end: searching a hull used to leave nothing to open."""
    char, ship_item = assembled_ship
    html = _text(client, f"/assets?view={char}&search=megathron")
    assert "Blue Thunder (Megathron)" in html
    assert "Antimatter Charge L" in html          # the fit survived the filter
    assert "Tritanium" not in html                # unrelated stock did not


def test_route_search_by_custom_name(client, assembled_ship):
    char, ship_item = assembled_ship
    html = _text(client, f"/assets?view={char}&search=blue+thunder")
    assert "Blue Thunder (Megathron)" in html
    assert "Antimatter Charge L" in html
    assert "Tritanium" not in html


def test_route_search_with_no_match_shows_nothing(client, assembled_ship):
    char, ship_item = assembled_ship
    html = _text(client, f"/assets?view={char}&search=zzz-no-such-item")
    assert "Blue Thunder (Megathron)" not in html
    assert "Antimatter Charge L" not in html
