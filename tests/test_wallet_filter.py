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


# ── how many rows reach the page ──────────────────────────────────────────────

def test_row_cap_is_five_times_the_old_500(app_module):
    """Chosen from measurement, not taste: 500 rows/tab render in ~290 ms, 2500 in
    ~590 ms, 5000 in ~1.1 s and 6.5 MB of HTML. Going past this wants virtualised
    rows rather than a bigger constant."""
    assert app_module._WALLET_ROW_CAP == 2500


def test_cap_applies_to_both_tables(client, app_module):
    cap = app_module._WALLET_ROW_CAP
    journal = [{"date": "2026-08-01T10:00:00Z", "ref_type": "brokers_fee",
                "amount": -1.0, "balance": 1.0, "id": i} for i in range(cap + 500)]
    txns = [{"date": "2026-08-01T12:00:00Z", "type_id": 34, "quantity": 1,
             "unit_price": 1.0, "is_buy": True, "transaction_id": i,
             "location_id": 60003760} for i in range(cap + 500)]
    page = _wallet_html(client, app_module, journal, txns)
    assert len(re.findall(r'<tr data-fval=', page)) == cap * 2
    # And it says so, so nobody has to wonder why the number is what it is.
    assert "Showing the newest" in page


def test_no_truncation_notice_when_everything_fits(client, app_module):
    page = _wallet_html(client, app_module, JOURNAL, TXNS)
    assert "Showing the newest" not in page


def test_journal_fetch_pages_until_the_limit_is_reached(app_module):
    """Page size is not assumed: sources disagree between 1000 and 2500 per page,
    so it keeps pulling until it has enough or ESI runs out."""
    import asyncio

    class _Resp:
        status_code = 200
        def __init__(self, n, pages): self._n, self.headers = n, {"x-pages": str(pages)}
        def json(self): return [{"id": i} for i in range(self._n)]

    class _Client:
        def __init__(self, per_page, pages):
            self.per_page, self.pages, self.calls = per_page, pages, []
        async def get(self, url, **kw):
            self.calls.append(kw.get("params", {}).get("page"))
            return _Resp(self.per_page, self.pages)

    # 1000-row pages → three calls to cover a 2500 limit.
    c = _Client(1000, 10)
    got = asyncio.run(app_module.wallet_api.fetch_journal(c, 1, "t", limit=2500))
    assert c.calls == [1, 2, 3] and len(got) == 3000

    # 2500-row pages → one call is enough, no wasted requests.
    c = _Client(2500, 10)
    asyncio.run(app_module.wallet_api.fetch_journal(c, 1, "t", limit=2500))
    assert c.calls == [1]

    # Fewer pages than the limit needs → stops at x-pages, does not loop.
    c = _Client(100, 2)
    got = asyncio.run(app_module.wallet_api.fetch_journal(c, 1, "t", limit=2500))
    assert c.calls == [1, 2] and len(got) == 200


def test_journal_fetch_is_bounded_even_if_esi_reports_nonsense(app_module):
    import asyncio

    class _Resp:
        status_code = 200
        headers = {"x-pages": "99999"}
        def json(self): return [{"id": 1}]           # one row per page, never reaches the limit

    class _Client:
        def __init__(self): self.calls = 0
        async def get(self, url, **kw):
            self.calls += 1
            return _Resp()

    c = _Client()
    asyncio.run(app_module.wallet_api.fetch_journal(c, 1, "t", limit=2500))
    assert c.calls == app_module.wallet_api._MAX_JOURNAL_PAGES
