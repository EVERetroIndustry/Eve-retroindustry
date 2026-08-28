"""An existing install has to pick up a new SDE, and counting rows cannot see one.

This has now bitten twice in the same shape. First a new COLUMN (`volume` on
sde_types) never reached an existing eve_cache.db, because the refresh only
compared type and group counts. Then a whole new BUILD: 3482594 has exactly as
many types and groups as 3470007, no new tables and no new columns, and yet it
added a region (Exordium, 53 systems and 53 stations), renamed ten items and
changed eleven published flags. The tables that grew are stations and systems -
precisely the ones no count in that function looks at.

So the build number is the trigger, and this pins it.
"""
from __future__ import annotations

import sqlite3


def _make_sde(path: str, build: str, types: int, stations: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sde_types (type_id INTEGER PRIMARY KEY, name TEXT,"
                 " market_group_id INTEGER, published INTEGER)")
    conn.executemany("INSERT INTO sde_types VALUES (?,?,?,?)",
                     [(i, f"T{i}", 4, 1) for i in range(1, types + 1)])
    conn.execute("CREATE TABLE sde_groups (group_id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO sde_groups VALUES (1, 'G')")
    conn.execute("CREATE TABLE sde_stations (station_id INTEGER PRIMARY KEY, name TEXT,"
                 " system_id INTEGER, region_id INTEGER)")
    conn.executemany("INSERT INTO sde_stations VALUES (?,?,?,?)",
                     [(60000000 + i, f"S{i}", 30000000 + i, 10000001)
                      for i in range(stations)])
    conn.execute("CREATE TABLE sde_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO sde_meta VALUES ('sde_build', ?)", (build,))
    conn.commit()
    conn.close()


def test_a_newer_build_refreshes_even_when_every_count_matches(app_module, tmp_path,
                                                              monkeypatch):
    """The reported shape: same type count, same group count, no new table, no new
    column - and a region's worth of new stations waiting in the bundle."""
    user = tmp_path / "user.db"
    bundle = tmp_path / "bundle.db"
    _make_sde(str(user), "3470007", types=50, stations=5)
    _make_sde(str(bundle), "3482594", types=50, stations=8)

    monkeypatch.setattr(app_module, "_bundled_sde_path", lambda: str(bundle))
    conn = sqlite3.connect(str(user))
    try:
        # user data must survive the refresh
        conn.execute("CREATE TABLE characters (character_id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO characters VALUES (900000001)")
        conn.commit()

        app_module._refresh_sde_from_bundle(conn)

        assert conn.execute("SELECT COUNT(*) FROM sde_stations").fetchone()[0] == 8
        assert conn.execute(
            "SELECT value FROM sde_meta WHERE key='sde_build'").fetchone()[0] == "3482594"
        assert conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0] == 1
    finally:
        conn.close()


def test_the_same_build_is_left_alone(app_module, tmp_path, monkeypatch):
    """The refresh copies whole tables, so it must not run on every start."""
    user = tmp_path / "user.db"
    bundle = tmp_path / "bundle.db"
    _make_sde(str(user), "3482594", types=50, stations=8)
    _make_sde(str(bundle), "3482594", types=50, stations=8)

    monkeypatch.setattr(app_module, "_bundled_sde_path", lambda: str(bundle))
    conn = sqlite3.connect(str(user))
    try:
        conn.execute("UPDATE sde_stations SET name='marker' WHERE station_id=60000000")
        conn.commit()
        app_module._refresh_sde_from_bundle(conn)
        # untouched, so the row we scribbled on is still there
        assert conn.execute(
            "SELECT name FROM sde_stations WHERE station_id=60000000").fetchone()[0] \
            == "marker"
    finally:
        conn.close()


def test_an_older_bundle_never_downgrades(app_module, tmp_path, monkeypatch):
    """A user who downloaded the SDE themselves must not be dragged back by an
    older bundled copy."""
    user = tmp_path / "user.db"
    bundle = tmp_path / "bundle.db"
    _make_sde(str(user), "3482594", types=50, stations=8)
    _make_sde(str(bundle), "3470007", types=50, stations=5)

    monkeypatch.setattr(app_module, "_bundled_sde_path", lambda: str(bundle))
    conn = sqlite3.connect(str(user))
    try:
        app_module._refresh_sde_from_bundle(conn)
        assert conn.execute("SELECT COUNT(*) FROM sde_stations").fetchone()[0] == 8
    finally:
        conn.close()


def test_a_database_with_no_build_stamp_is_refreshed(app_module, tmp_path, monkeypatch):
    """Copies made before the stamp existed read as build 0, so they upgrade once."""
    user = tmp_path / "user.db"
    bundle = tmp_path / "bundle.db"
    _make_sde(str(user), "3470007", types=50, stations=5)
    conn = sqlite3.connect(str(user))
    conn.execute("DROP TABLE sde_meta")
    conn.commit()
    conn.close()
    _make_sde(str(bundle), "3482594", types=50, stations=8)

    monkeypatch.setattr(app_module, "_bundled_sde_path", lambda: str(bundle))
    conn = sqlite3.connect(str(user))
    try:
        app_module._refresh_sde_from_bundle(conn)
        assert conn.execute("SELECT COUNT(*) FROM sde_stations").fetchone()[0] == 8
    finally:
        conn.close()
