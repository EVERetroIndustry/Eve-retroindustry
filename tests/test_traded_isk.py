"""ISK traded over 7 days: each day's volume at that day's average price.

ESI market history carries a daily average next to the volume, so the turnover
is exact - not volume x today's price. The averages are stored beside the
volumes in the ETag cache; a record stored before they were kept must get one
full body, or a 304 would leave it volume-only forever.
"""
from __future__ import annotations

import asyncio
import datetime
import sqlite3

from app.market import prices as P


def _day(n: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body
        self.headers = {"etag": 'W/"new"', "expires": "Wed, 21 Oct 2099 07:28:00 GMT"}

    def json(self):
        return self._body


class _Client:
    def __init__(self, resp):
        self.resp, self.headers = resp, []

    async def get(self, *a, headers=None, **k):
        self.headers.append(headers or {})
        return self.resp


def _clear():
    P._hist_etags.clear(); P._hist_etags_dirty.clear(); P._hist_avgs.clear()


def test_traded_isk_is_volume_at_each_days_average(tmp_path):
    conn = sqlite3.connect(tmp_path / "i.db")
    P.ensure_hist_etag_table(conn)
    _clear()
    history = [
        {"date": _day(20), "volume": 1000, "average": 1.0},   # outside the window
        {"date": _day(3), "volume": 10, "average": 5.0},
        {"date": _day(1), "volume": 4, "average": 7.5},
    ]
    vol = asyncio.run(P._fetch_region_volume(_Client(_Resp(200, history)), 10000002, 34))
    assert vol == 14
    P.flush_hist_etags(conn)
    assert P.region_traded_isk(conn, 10000002, [34]) == {34: 10 * 5.0 + 4 * 7.5}
    _clear()


def test_a_record_without_averages_gets_one_full_fetch(tmp_path):
    """A 304 would keep the volume-only record as it is, so no If-None-Match."""
    conn = sqlite3.connect(tmp_path / "o.db")
    P.ensure_hist_etag_table(conn)
    conn.execute("INSERT INTO market_hist_etag (region_id, type_id, etag, days_json,"
                 " cached_at, expires_at) VALUES (1, 34, 'W/\"old\"', ?, 1, 9e12)",
                 ('{"%s": 5}' % _day(1),))
    conn.commit()
    _clear()
    # Stored before averages: the page falls back to an estimate.
    assert P.region_traded_isk(conn, 1, [34]) == {}
    P.load_hist_etags(conn, 1)
    client = _Client(_Resp(200, [{"date": _day(1), "volume": 5, "average": 2.0}]))
    assert asyncio.run(P._fetch_region_volume(client, 1, 34)) == 5
    assert "If-None-Match" not in client.headers[0]
    P.flush_hist_etags(conn)
    assert P.region_traded_isk(conn, 1, [34]) == {34: 10.0}
    _clear()
