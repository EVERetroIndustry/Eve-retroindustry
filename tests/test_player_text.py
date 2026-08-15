"""Player-supplied text and skill-name resolution.

Ship, container and citadel names are chosen by players and may legally contain
anything the client accepts — diacritics, Cyrillic, CJK, box drawing, emoji, and
the HTML-significant characters & < > " '. All of it has to survive to the screen
intact, and none of it may execute.
"""
from __future__ import annotations

import html as _html
import json
import re
import time

NAMES = {
    "czech":      "Žluťoučký kůň",
    "cyrillic":   "Корабль Смерти",
    "chinese":    "宇宙飛船",
    "korean":     "우주선",
    "greek":      "Αστροπλοίο",
    "arabic":     "سفينة الفضاء",
    "symbols":    "▲ Fleet ▲ ◆ Alpha",
    "box":        "╔═ Logi ═╗",
    "emoji":      "Rifter 🚀 v2",
    "apostrophe": "Bob's Ship & Co",
    "markup":     "<b>bold</b> ship",
}


def test_every_character_class_survives_to_the_page(client, app_module, monkeypatch):
    """Rendered through the real /assets route, not just the label helper."""
    CHAR = 900000002
    conn = app_module.get_conn()
    row = conn.execute("SELECT data_json, cached_at FROM char_assets_cache"
                       " WHERE character_id=?", (CHAR,)).fetchone()
    original = (row[0], row[1]) if row else None
    now = time.time()
    assets, labels = [], {}
    for i, name in enumerate(NAMES.values()):
        ship_id = 810000 + i
        labels[ship_id] = name
        assets += [
            {"item_id": ship_id, "type_id": 641, "quantity": 1, "location_id": 60003760,
             "location_flag": "Hangar", "is_singleton": True},
            {"item_id": 820000 + i, "type_id": 34, "quantity": 10, "location_id": ship_id,
             "location_flag": "Cargo", "is_singleton": False},
        ]
    conn.execute("DELETE FROM char_assets_cache WHERE character_id=?", (CHAR,))
    conn.execute("INSERT INTO char_assets_cache (character_id, data_json, cached_at)"
                 " VALUES (?,?,?)", (CHAR, json.dumps(assets), now))
    conn.commit(); conn.close()

    async def _fake(char_id, token, container_ids, assets_raw):
        return {cid: (app_module._container_display_name(labels.get(cid, ""), "Megathron", cid),
                      60003760) for cid in container_ids}
    monkeypatch.setattr(app_module, "_resolve_container_names", _fake)
    try:
        page = client.get(f"/assets?view={CHAR}").text
        # Unescape first: Jinja escapes & < > ' correctly, and the browser turns
        # them back — asserting on the raw bytes would fail on exactly the names
        # that are handled properly.
        text = _html.unescape(page)
        for label, name in NAMES.items():
            assert f"{name} (Megathron)" in text, label
        # …and the markup one must be escaped rather than live in the document.
        assert "<b>bold</b> ship" not in page
        assert "&lt;b&gt;bold&lt;/b&gt; ship" in page
    finally:
        conn = app_module.get_conn()
        conn.execute("DELETE FROM char_assets_cache WHERE character_id=?", (CHAR,))
        if original:
            conn.execute("INSERT INTO char_assets_cache (character_id, data_json, cached_at)"
                         " VALUES (?,?,?)", (CHAR, original[0], original[1]))
        conn.commit(); conn.close()


def test_names_injected_into_innerhtml_are_escaped(client):
    """Citadels, ships and containers are player-named, and several places build
    HTML strings from them — an unescaped "&" mangles the name and a "<" is an
    injection. The helper lives in base.html so every page has it."""
    base = client.get("/").text
    assert "window.escHtml" in base

    for route in ("/assets", "/prices", "/plan", "/contracts"):
        page = client.get(route).text
        # No template literal may drop a name straight into markup.
        bare = re.findall(r"\$\{(?:item|s|it|data|o)\.(?:name|location|group_name"
                          r"|solar_system_name)\b[^}]*\}", page)
        unescaped = [b for b in bare if "escHtml" not in b]
        assert not unescaped, (route, unescaped)


def test_skill_name_is_resolved_for_the_skill_actually_shown(client, app_module, monkeypatch):
    """Reported as "#3304 V" on the dashboard.

    ESI parks a finished skill at queue position 0 until the pilot logs in, so the
    entry displayed is the first one finishing in the future. Only [0]'s name was
    resolved, which is never the one on screen in that state.
    """
    async def _loc(c, cid, tok): return {"solar_system_id": 30000142}
    async def _ship(c, cid, tok): return {}
    async def _sq(c, cid, tok):
        return [
            {"skill_id": 3300, "finished_level": 5, "finish_date": "2020-01-01T00:00:00Z"},
            {"skill_id": 3304, "finished_level": 5, "finish_date": "2030-01-01T00:00:00Z"},
        ]
    monkeypatch.setattr(app_module, "fetch_location", _loc)
    monkeypatch.setattr(app_module, "fetch_skill_queue", _sq)
    monkeypatch.setattr(app_module, "fetch_ship", _ship)

    data = client.get("/api/dashboard/live").json()
    skills = [(c.get("training") or {}).get("skill") for c in data["chars"].values()]
    assert "Medium Hybrid Turret" in skills, skills
    assert not any(str(s).startswith("#") for s in skills if s), skills


def test_dashboard_isk_has_no_decimals(client, app_module, monkeypatch):
    async def _loc(c, cid, tok): return {}
    async def _sq(c, cid, tok): return []
    async def _ship(c, cid, tok): return {}
    monkeypatch.setattr(app_module, "fetch_location", _loc)
    monkeypatch.setattr(app_module, "fetch_skill_queue", _sq)
    monkeypatch.setattr(app_module, "fetch_ship", _ship)

    data = client.get("/api/dashboard/live").json()
    money = [data.get("agg_wallet_str"), data.get("agg_value_str")]
    for c in data["chars"].values():
        money += [c.get("wallet_str"), c.get("asset_value_str"), c.get("net_worth_str")]
    shown = [m for m in money if m]
    assert shown, "no ISK values to check"
    for m in shown:
        assert "." not in m and "," not in m, m      # cents are noise at billions
