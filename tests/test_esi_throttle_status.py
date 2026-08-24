"""esi_throttle_status: telling a user "ESI is throttling us" from "the app hung".

Reported: loading prices for a custom station "loads some, looks stuck, loads a
bit more" - which is the throttle governor doing its job silently. This is the
introspection that lets a caller say what is actually happening instead.
"""
from __future__ import annotations

import time

import pytest

from app.esi import client as esi_client_mod
from app.esi.client import esi_throttle_status, _TokenBucketGovernor


@pytest.fixture(autouse=True)
def clean_governors():
    """Governor state is process-global by design (shared across all
    esi_client() instances) - reset it so tests don't see each other's pauses."""
    err = esi_client_mod._ERROR_LIMIT
    tok = esi_client_mod._TOKEN_LIMIT
    before = (err._pause_until, dict(tok._pause_until), dict(tok._group_of))
    err._pause_until = 0.0
    tok._pause_until = {}
    tok._group_of = {}
    yield
    err._pause_until, tok._pause_until, tok._group_of = before


HIST_URL = "https://esi.evetech.net/latest/markets/1/history/"


def test_nothing_paused_reports_clear():
    status = esi_throttle_status(HIST_URL)
    assert status == {"paused": False, "seconds": 0}


def test_a_paused_group_is_reported_with_a_positive_eta():
    sig = _TokenBucketGovernor.signature(__import__("httpx").URL(HIST_URL))
    esi_client_mod._TOKEN_LIMIT._group_of[sig] = "market-history"
    esi_client_mod._TOKEN_LIMIT._pause_until["market-history"] = time.monotonic() + 42
    status = esi_throttle_status(HIST_URL)
    assert status["paused"] is True
    assert 41 <= status["seconds"] <= 43


def test_a_different_groups_pause_does_not_leak_into_an_unrelated_endpoint():
    """The whole point of per-group state: a paused market bucket must not make
    an unrelated endpoint look throttled too."""
    other_sig = _TokenBucketGovernor.signature(
        __import__("httpx").URL("https://esi.evetech.net/latest/characters/1/wallet/"))
    esi_client_mod._TOKEN_LIMIT._group_of[other_sig] = "char-wallet"
    esi_client_mod._TOKEN_LIMIT._pause_until["char-wallet"] = time.monotonic() + 42
    assert esi_throttle_status(HIST_URL) == {"paused": False, "seconds": 0}


def test_the_old_error_limit_pause_always_counts_regardless_of_endpoint():
    """A 420 freezes ALL ESI traffic, not one group - so it must show up even
    when asking about an endpoint that never itself triggered it."""
    esi_client_mod._ERROR_LIMIT._pause_until = time.monotonic() + 10
    status = esi_throttle_status(HIST_URL)
    assert status["paused"] is True and status["seconds"] >= 9


def test_omitting_the_url_reports_the_worst_pause_across_every_group():
    esi_client_mod._TOKEN_LIMIT._group_of["/a/"] = "a"
    esi_client_mod._TOKEN_LIMIT._pause_until["a"] = time.monotonic() + 5
    esi_client_mod._TOKEN_LIMIT._group_of["/b/"] = "b"
    esi_client_mod._TOKEN_LIMIT._pause_until["b"] = time.monotonic() + 55
    status = esi_throttle_status()
    assert status["paused"] is True and 54 <= status["seconds"] <= 56


def test_a_pause_that_already_expired_reports_clear():
    sig = _TokenBucketGovernor.signature(__import__("httpx").URL(HIST_URL))
    esi_client_mod._TOKEN_LIMIT._group_of[sig] = "market-history"
    esi_client_mod._TOKEN_LIMIT._pause_until["market-history"] = time.monotonic() - 5
    assert esi_throttle_status(HIST_URL) == {"paused": False, "seconds": 0}
