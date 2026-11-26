"""The SSO callback only accepts the login this app started.

The app put a random `state` into the login URL but never compared what came
back, so any request to localhost:5173/callback - an old SSO tab, another
program - ended the wait with its code. PKCE stops a foreign code from being
redeemed; the state check is what the SSO documentation asks for on top.
"""
from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from app.auth import esi_oauth as oauth


@pytest.fixture
def server():
    oauth._CallbackHandler.code = None
    oauth._CallbackHandler.expected_state = "the-real-state"
    srv = HTTPServer(("127.0.0.1", 0), oauth._CallbackHandler)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield srv, t
    srv.shutdown()
    srv.server_close()
    oauth._CallbackHandler.expected_state = None
    oauth._CallbackHandler.code = None


def _get(srv, query):
    url = f"http://127.0.0.1:{srv.server_address[1]}/callback?{query}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.parametrize("query", ["code=foreign&state=someone-else", "code=foreign", ""])
def test_a_callback_from_another_login_is_turned_away_and_the_wait_goes_on(server, query):
    srv, thread = server
    assert _get(srv, query) == 400
    assert oauth._CallbackHandler.code is None
    thread.join(0.3)
    assert thread.is_alive(), "still waiting for the real redirect"


def test_the_real_callback_is_taken_and_ends_the_wait(server):
    srv, thread = server
    assert _get(srv, "code=foreign&state=wrong") == 400
    assert _get(srv, "code=the-code&state=the-real-state") == 200
    assert oauth._CallbackHandler.code == "the-code"
    thread.join(5)
    assert not thread.is_alive()


def test_without_a_login_in_progress_nothing_is_accepted(server):
    srv, _t = server
    oauth._CallbackHandler.expected_state = None
    assert _get(srv, "code=x&state=") == 400
    assert oauth._CallbackHandler.code is None


def test_both_login_flows_arm_the_check():
    import inspect
    for fn in (oauth.login, oauth.start_web_login):
        assert "_CallbackHandler.expected_state = state" in inspect.getsource(fn), fn.__name__
