"""What a contract's contents are worth - and what that number is allowed to claim.

Two bugs a tester found on real data, both of them the same mistake in different
clothes: the app stated something it did not know.

  1. Contracts titled "10/20" or "WYVERN ME 9 / TE 16" hold blueprint COPIES, but
     every "Wyvern Blueprint" line was valued at 18.00b - the price of the ORIGINAL,
     which shares its type_id. A 600M contract came out at 83.64b (+13841 %). A copy
     cannot be listed on the market at all, so no market number describes it.
  2. A Thanatos on contracts in C-N at 2.18b was appraised at "Jita sell 2.70b",
     so a 2.25b contract read as a 20 % bargain when it was the third cheapest
     offer on the screen. The comparison sits next to the contract's asking price,
     so the reference has to be the cheapest offer we can actually see.
"""
from __future__ import annotations

import pytest

from app.web import contracts_helper as ch

NPC_STATION = 60003760
JITA_SYSTEM = 30000142
FORGE = 10000002
TRIT = 34                     # nothing about Tritanium can ever be a copy


@pytest.fixture
def conn(app_module):
    c = app_module.get_conn()
    ch.ensure_public_contract_tables(c)
    ch.ensure_alliance_contract_tables(c)
    for t in ("public_contracts", "public_contract_items", "market_price_cache",
              "alliance_contracts", "alliance_contract_items"):
        c.execute(f"DELETE FROM {t}")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def bp(conn) -> int:
    """A real blueprint type_id from the SDE (the thing with two identities)."""
    row = conn.execute(
        "SELECT b.blueprint_type_id FROM sde_blueprints b"
        " JOIN sde_types t ON t.type_id = b.blueprint_type_id"
        " ORDER BY (t.name LIKE 'Wyvern%') DESC LIMIT 1").fetchone()
    assert row, "test SDE has no blueprints"
    return int(row[0])


def _price(conn, type_id: int, sell: float | None, buy: float | None = None):
    conn.execute("INSERT OR REPLACE INTO market_price_cache (type_id, sell_price,"
                 " buy_price, cached_at) VALUES (?,?,?,0)", (type_id, sell, buy))
    conn.commit()


def _pub(conn, cid: int, price: float, items: list[dict], system=JITA_SYSTEM):
    conn.execute(
        "INSERT OR REPLACE INTO public_contracts (contract_id, region_id, type, price,"
        " reward, collateral, buyout, volume, date_expired, title, start_location_id,"
        " end_location_id, issuer_id, system_id)"
        " VALUES (?,?,'item_exchange',?,0,0,0,1.0,'2026-12-31T00:00:00Z','t',?,0,1,?)",
        (cid, FORGE, price, NPC_STATION, system))
    conn.commit()
    ch.store_public_items(conn, {cid: items})


def _item(type_id: int, *, qty=1, copy=False, runs=None, me=None, te=None) -> dict:
    it = {"type_id": type_id, "quantity": qty, "is_included": True}
    if copy:
        it["is_blueprint_copy"] = True
    if runs is not None:
        it["runs"] = runs
    if me is not None:
        it.update({"material_efficiency": me, "time_efficiency": te})
    return it


def _appraise(conn, cid):
    items = ch.get_contract_items(conn, cid)
    return items, ch.appraise_items(conn, items, cid)


# ── blueprint copies ──────────────────────────────────────────────────────────

def test_a_copy_is_never_worth_the_original(conn, bp):
    """The reported bug: 18b of BPO price on a line holding a copy.

    With no contract holding just a copy of this blueprint, there is nothing to
    price it from - and "nothing" is the honest answer. The bundle here is the
    "WYVERN ME 9 / TE 16" shape: a copy plus a pile of other things, so its own
    price says nothing about the copy either.
    """
    _price(conn, bp, 18_000_000_000.0, 1_000_000_000.0)
    _price(conn, TRIT, 5.0)
    _pub(conn, 5001, 650_000_000.0, [_item(bp, copy=True, runs=1, me=10, te=20),
                                     _item(TRIT, qty=100)])

    items, a = _appraise(conn, 5001)
    line = next(i for i in items if i["type_id"] == bp)
    assert line["is_bpc"] is True
    assert line["value"] is None and line["unit"] is None
    assert a["unpriced"] == 1
    assert a["net"] == pytest.approx(500.0)        # the ore, and nothing else
    # Not the market's buy price either - a copy has no market side at all.
    assert line["buy"] is None


def test_a_copy_is_priced_from_a_contract_holding_a_copy(conn, bp):
    _price(conn, bp, 18_000_000_000.0)
    _pub(conn, 5002, 500_000_000.0, [_item(bp, copy=True, runs=1)])   # the reference
    _pub(conn, 5003, 650_000_000.0, [_item(bp, copy=True, runs=1),
                                     _item(TRIT, qty=100)])
    _price(conn, TRIT, 5.0)

    items, a = _appraise(conn, 5003)
    line = next(i for i in items if i["type_id"] == bp)
    assert line["unit"] == 500_000_000.0 and line["price_source"] == "contract"
    assert a["unpriced"] == 0
    assert a["net"] == pytest.approx(500_000_000.0 + 500.0)


def test_an_original_is_not_priced_off_a_copy_contract(conn, bp):
    """The inverse mistake: cheap copies must not drag the original's price down."""
    _price(conn, bp, 18_000_000_000.0)
    _pub(conn, 5004, 500_000_000.0, [_item(bp, copy=True, runs=1)])
    _pub(conn, 5005, 17_000_000_000.0, [_item(bp)])                   # the original

    items, a = _appraise(conn, 5005)
    assert items[0]["is_bpc"] is False
    # Cheapest of the market (18b) and contracts holding an ORIGINAL (17b).
    assert items[0]["unit"] == 17_000_000_000.0
    assert items[0]["price_source"] == "contract"
    assert a["self_priced"] == 1                  # this very contract is the cheapest


def test_contract_unit_prices_keeps_the_two_identities_apart(conn, bp):
    _pub(conn, 5006, 500_000_000.0, [_item(bp, copy=True, runs=1)])
    _pub(conn, 5007, 17_000_000_000.0, [_item(bp)])

    originals = ch.contract_unit_prices(conn, [bp])
    copies = ch.contract_unit_prices(conn, [bp], copies=True)
    assert originals[bp][0] == 17_000_000_000.0 and originals[bp][2] == 5007
    assert copies[bp][0] == 500_000_000.0 and copies[bp][2] == 5006


def test_a_product_price_from_contracts_ignores_copies(conn, bp):
    """The Prices/Plan lookup asks what the market item costs on a contract."""
    _pub(conn, 5008, 500_000_000.0, [_item(bp, copy=True, runs=1)])
    assert ch.best_contract_price(conn, FORGE, bp) is None
    _pub(conn, 5009, 17_000_000_000.0, [_item(bp)])
    got = ch.best_contract_price(conn, FORGE, bp)
    assert got and got["contract_id"] == 5009


def test_runs_and_efficiency_come_along(conn, bp):
    """They are what a copy is worth, and the in-game titles ("10/20") say so."""
    _pub(conn, 5010, 1.0, [_item(bp, copy=True, runs=3, me=10, te=20)])
    line = ch.get_contract_items(conn, 5010)[0]
    assert (line["runs"], line["me"], line["te"]) == (3, 10, 20)


def test_positive_runs_alone_marks_a_copy(conn, bp):
    """ESI documents runs as "-1 if it is an original", so runs > 0 means copy."""
    _pub(conn, 5011, 1.0, [{"type_id": bp, "quantity": 1, "is_included": True,
                            "runs": 5}])
    assert ch.get_contract_items(conn, 5011)[0]["is_bpc"] is True
    _pub(conn, 5012, 1.0, [{"type_id": bp, "quantity": 1, "is_included": True,
                            "runs": -1}])
    line = ch.get_contract_items(conn, 5012)[0]
    assert line["is_bpc"] is False and line["runs"] is None


# ── rows indexed before the copy flag existed ─────────────────────────────────

def _legacy_row(conn, cid, type_id):
    """A row as the older index wrote it: no is_bpc at all."""
    conn.execute("INSERT INTO public_contract_items (contract_id, type_id, quantity,"
                 " is_included) VALUES (?,?,1,1)", (cid, type_id))
    conn.commit()


def test_an_unread_copy_flag_claims_nothing(conn, bp):
    _price(conn, bp, 18_000_000_000.0)
    _pub(conn, 5013, 650_000_000.0, [])
    _legacy_row(conn, 5013, bp)

    assert ch.public_items_need_reread(conn, 5013) is True
    items, a = _appraise(conn, 5013)
    assert items[0]["copy_unknown"] is True
    assert items[0]["value"] is None
    assert a["unpriced"] == 1 and a["unpriced_names"]


def test_an_old_row_that_cannot_be_a_copy_is_priced_as_before(conn):
    """Only a blueprint is ambiguous; a NULL flag on ore means "original"."""
    _price(conn, TRIT, 5.0, 4.0)
    _pub(conn, 5014, 100.0, [])
    _legacy_row(conn, 5014, TRIT)

    assert ch.public_items_need_reread(conn, 5014) is False
    items, a = _appraise(conn, 5014)
    assert items[0]["unit"] == 5.0 and items[0]["price_source"] == "jita"
    assert a["unpriced"] == 0


def test_rereading_one_contract_repairs_it(conn, bp):
    _pub(conn, 5015, 650_000_000.0, [])
    _legacy_row(conn, 5015, bp)
    # what the endpoint does with what ESI hands back
    ch.store_public_items(conn, {5015: [_item(bp, copy=True, runs=1, me=10, te=20)]})
    assert ch.public_items_need_reread(conn, 5015) is False
    assert ch.get_contract_items(conn, 5015)[0]["is_bpc"] is True


# ── the reference price is the cheapest offer we can see ──────────────────────

THANATOS_JITA = 2_700_000_000.0
THANATOS_CN = 2_180_000_000.0


def test_a_cheaper_contract_beats_the_jita_sell_order(conn):
    """The C-N case: quoting Jita made every C-N contract look like a bargain."""
    _price(conn, TRIT, THANATOS_JITA)          # stand-in for the hull, same maths
    _pub(conn, 5016, THANATOS_CN, [_item(TRIT)])
    _pub(conn, 5017, 2_250_000_000.0, [_item(TRIT)])

    items, a = _appraise(conn, 5017)
    assert items[0]["unit"] == THANATOS_CN
    assert items[0]["price_source"] == "contract"
    # The tooltip has to be able to say WHY a contract set the price.
    assert items[0]["jita_sell"] == THANATOS_JITA
    # And the contract now reads as what it is: not the cheapest way to get it.
    assert a["net"] - 2_250_000_000.0 < 0


def test_the_market_wins_when_it_is_cheaper(conn):
    _price(conn, TRIT, 1_000_000_000.0)
    _pub(conn, 5018, 2_180_000_000.0, [_item(TRIT)])
    items, _ = _appraise(conn, 5018)
    assert items[0]["unit"] == 1_000_000_000.0
    assert items[0]["price_source"] == "jita"


def test_a_bait_price_does_not_become_the_reference(conn):
    """One 1M contract for a 2.7b hull must not reprice everything else."""
    _price(conn, TRIT, THANATOS_JITA)
    _pub(conn, 5019, 1_000_000.0, [_item(TRIT)])
    _pub(conn, 5020, 2_250_000_000.0, [_item(TRIT)])
    items, _ = _appraise(conn, 5020)
    assert items[0]["unit"] == THANATOS_JITA and items[0]["price_source"] == "jita"


def test_with_no_market_price_the_contract_is_all_there_is(conn, bp):
    """The capital case - and the reason the bait floor cannot apply there."""
    _pub(conn, 5021, 42_500_000_000.0, [_item(bp)])
    items, _ = _appraise(conn, 5021)
    assert items[0]["unit"] == 42_500_000_000.0
    assert items[0]["price_source"] == "contract"
    assert items[0].get("jita_sell") is None


# ── what the UI is allowed to say ─────────────────────────────────────────────

def test_the_contract_tag_talks_about_the_item_on_the_line():
    """It used to explain a Wyvern's price with "a titan is never on a Jita sell
    order" - a general example reads as a claim about what is on screen."""
    js = open("app/web/templates/base.html").read()
    tag = js.split("const tagFor")[1].split("const bpcBadge")[0]
    assert "titan" not in tag
    assert "it.name" in tag                    # names the actual item
    assert "it.jita_sell" in tag               # or says the market is dearer
    assert "runs and ME/TE" in tag             # copies are not exact matches
    # And a copy gets its own reason: "this item is not on a Jita sell order" is
    # false for a copy - the ORIGINAL of that blueprint usually is, at a hundred
    # times the price, which is exactly what went wrong in the first place.
    assert "a copy is never on the market" in tag
    assert tag.index("it.is_bpc") < tag.index("it.jita_sell")
