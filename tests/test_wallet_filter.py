"""Wallet journal/transaction checkbox filters.

Both tables already ship every row to the browser, so the filter is client-side.
These cover the server side of that contract — the hooks the JS needs — plus the
ref_type humanising the filter list depends on to be readable.
"""
from __future__ import annotations

import re


def test_ref_types_are_humanized_for_display_and_filtering(app_module):
    """The filter lists whatever the rows say, so raw ESI tokens would leak into it."""
    h = app_module.wallet_api.humanize_ref_type
    assert h("brokers_fee") == "Broker's Fee"
    assert h("market_transaction") == "Market Transaction"
    assert h("industry_job_tax") == "Industry Job Tax"
    # Unknown ref_type still reads as words rather than snake_case.
    assert h("some_new_ccp_ref") == "Some New Ccp Ref"
    assert h("") == ""


def _wallet_html(client, app_module, journal, txns):
    """Render /wallet with fixed journal/transaction data, no ESI."""
    async def _bal(*a, **k): return 1_000_000.0
    async def _jr(*a, **k): return journal
    async def _tx(*a, **k): return txns
    async def _names(conn, j, t, tok): return {}
    orig = (app_module.wallet_api.fetch_balance, app_module.wallet_api.fetch_journal,
            app_module.wallet_api.fetch_transactions, app_module._wallet_names)
    app_module.wallet_api.fetch_balance = _bal
    app_module.wallet_api.fetch_journal = _jr
    app_module.wallet_api.fetch_transactions = _tx
    app_module._wallet_names = _names
    try:
        return client.get("/wallet?char=900000001").text
    finally:
        (app_module.wallet_api.fetch_balance, app_module.wallet_api.fetch_journal,
         app_module.wallet_api.fetch_transactions, app_module._wallet_names) = orig


JOURNAL = [
    {"date": "2026-08-01T10:00:00Z", "ref_type": "brokers_fee", "amount": -1.0,
     "balance": 1.0, "id": 1},
    {"date": "2026-08-02T10:00:00Z", "ref_type": "bounty_prizes", "amount": 2.0,
     "balance": 3.0, "id": 2},
    {"date": "2026-08-03T10:00:00Z", "ref_type": "brokers_fee", "amount": -1.0,
     "balance": 2.0, "id": 3},
]
TXNS = [
    {"date": "2026-08-01T12:00:00Z", "type_id": 34, "quantity": 5, "unit_price": 5.0,
     "is_buy": True, "transaction_id": 1, "location_id": 60003760},
    {"date": "2026-08-02T12:00:00Z", "type_id": 641, "quantity": 1, "unit_price": 1.0,
     "is_buy": False, "transaction_id": 2, "location_id": 60003760},
]


def test_every_row_carries_the_value_the_filter_groups_by(client, app_module):
    import html as _html
    page = _wallet_html(client, app_module, JOURNAL, TXNS)
    # Jinja escapes the apostrophe in "Broker's Fee" into the attribute; the
    # browser hands the JS the decoded value back through dataset.fval, so unescape
    # here rather than asserting on the entity.
    vals = [_html.unescape(v) for v in re.findall(r'<tr data-fval="([^"]*)"', page)]
    # Journal rows carry the humanized ref_type, transactions carry the item name.
    assert vals.count("Broker's Fee") == 2
    assert "Bounty Prizes" in vals
    assert "Tritanium" in vals and "Megathron" in vals


def test_both_tables_get_a_filter_bound_to_them(client, app_module):
    html = _wallet_html(client, app_module, JOURNAL, TXNS)
    for key, label in (("journal", "Type"), ("txns", "Item")):
        assert f'data-filter-key="{key}"' in html
        assert f'id="tbl-{key}"' in html          # the id the filter looks up
        assert f'>{label}</span>' in html
    assert "vf-options" in html and "vf-search" in html
    assert "vf-all" in html and "vf-none" in html


def test_filter_marks_itself_when_active(client, app_module):
    """A forgotten filter silently hiding rows is the failure mode to avoid."""
    html = _wallet_html(client, app_module, JOURNAL, TXNS)
    assert "of ' + rows.length + ' shown'" in html
    assert "btn-eve" in html


def test_empty_tables_hide_the_filter(client, app_module):
    html = _wallet_html(client, app_module, [], [])
    assert "box.style.display = 'none'" in html   # no rows → nothing to filter
