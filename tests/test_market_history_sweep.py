"""Which types the 7-day volume sweep is allowed to ask about.

Reported symptom: loading prices for a custom station "loads some volumes, looks
stuck, then after a long time loads a bit more". Measured against live ESI
(2026-08-24) on a region with nothing cached:

  * of 60 sampled types with no market group or published = 0, **60 answered 400
    or 404**; of 60 ordinary market types, none did. 379 of the 19 812 types in
    the sweep are such types, so every cold sweep fired ~379 GUARANTEED errors
    against an error budget of 100 per ~60 s window - and our own error-limit
    governor then froze ALL ESI traffic until the window reset, repeatedly.
  * ESI has also started rate-limiting this endpoint: at concurrency 30 (~460
    req/s) it answers 429 after ~6 000 requests; at 10 (~230 req/s) the same
    sweep ran 16 500 requests with zero errors.

Together: 11-14 volumes/s before, ~230/s after.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.market import prices as P


@pytest.fixture
def sde(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("CREATE TABLE sde_types (type_id INTEGER PRIMARY KEY, name TEXT,"
                 " market_group_id INTEGER, published INTEGER)")
    conn.executemany("INSERT INTO sde_types VALUES (?,?,?,?)", [
        (34, "Tritanium", 18, 1),          # ordinary market item
        (638, "Raven", 80, 1),
        (60, "Asset Safety Wrap", None, 0),  # the real 404 case
        (2233, "Unpublished thing", 12, 0),  # published = 0
        (999, "No market group", None, 1),
    ])
    conn.commit()
    return conn


def test_only_types_the_endpoint_can_answer_are_asked(sde):
    kept = P._types_with_market_history(sde, [34, 638, 60, 2233, 999])
    assert kept == [34, 638]


def test_order_is_preserved_and_duplicates_do_not_multiply(sde):
    assert P._types_with_market_history(sde, [638, 34, 638]) == [638, 34, 638]


def test_an_empty_list_is_not_a_query(sde):
    assert P._types_with_market_history(sde, []) == []
    assert P._types_with_market_history(sde, [None, 0]) == []


def test_a_missing_sde_asks_about_everything_rather_than_nothing(tmp_path):
    """Fewer volumes is a worse failure than a slow sweep, so the filter opens up
    rather than closing down when it cannot check."""
    empty = sqlite3.connect(tmp_path / "empty.db")
    assert P._types_with_market_history(empty, [34, 638]) == [34, 638]


def test_more_types_than_sqlite_takes_variables_are_chunked(sde):
    """The real list is ~19 800 ids; SQLite's variable limit is well under that."""
    sde.executemany("INSERT INTO sde_types VALUES (?,?,?,?)",
                    [(10_000 + i, f"t{i}", 5, 1) for i in range(2500)])
    sde.commit()
    ids = [10_000 + i for i in range(2500)] + [60]
    kept = P._types_with_market_history(sde, ids)
    assert len(kept) == 2500 and 60 not in kept


def test_history_concurrency_stays_where_it_was_measured():
    """30 answered 429 after ~6 000 requests; 10 sustained 16 500 with none. If this
    is raised again, re-measure first - the endpoint's limits changed under us once."""
    assert P._HIST_SEM._value <= 10
