"""One-time fetch of ice belt counts per system from DOTLAN EveMaps.

Ice fields became anomalies in 2013 (Odyssey) and spawn in fixed systems,
but nothing CCP publishes says which: not the SDE, not ESI, not the client's
files. DOTLAN keeps the counts in its own database and shows them as
"Belts/Ice  6+3". The count does not change, so it is fetched once, written
to scripts/ice_belts_dotlan.json (committed), and import_sde.import_map puts
it into the bundled static data.

Reads only region pages (allowed by DOTLAN's robots.txt), one every two
seconds. As a check, the asteroid belt count DOTLAN shows next to the ice
count is compared with the SDE's; the script stops if many disagree, since
that would mean the page was read wrongly.

Usage: venv/bin/python scripts/fetch_dotlan_ice.py
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "scripts", "ice_belts_dotlan.json")
UA = "EVE Retroindustry one-time static data fetch (everetroindustry@gmail.com)"

_ROW = re.compile(r"<tr class=\"tlr\d\">(.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_SYSTEM = re.compile(r'href="/system/[^"]+" class="igb link-5-(\d+)"')
_BELTS = re.compile(r"^\s*(\d+)?\s*(?:<span[^>]*>\+(\d+)</span>)?\s*$")


def parse_region(html: str) -> dict[int, tuple[int, int]]:
    """system_id -> (asteroid belts, ice belts) from a region page's table."""
    out = {}
    for row in _ROW.findall(html):
        sm = _SYSTEM.search(row)
        if not sm:
            continue
        tds = _TD.findall(row)
        if len(tds) < 3:
            continue
        bm = _BELTS.match(tds[-2])             # the column before the moons
        if not bm:
            continue
        out[int(sm.group(1))] = (int(bm.group(1) or 0), int(bm.group(2) or 0))
    return out


def main() -> None:
    conn = sqlite3.connect(f"file:{os.path.join(ROOT, 'sde_base.db')}?immutable=1", uri=True)
    regions = conn.execute("""SELECT DISTINCT r.region_id, r.name FROM sde_regions r
                                JOIN sde_map_systems m ON m.region_id = r.region_id
                               WHERE r.region_id < 11000000 AND m.x2d IS NOT NULL
                               ORDER BY r.name""").fetchall()
    belts = dict(conn.execute("SELECT system_id, belts FROM sde_map_systems"))
    found: dict[int, tuple[int, int]] = {}
    with httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True) as c:
        for i, (rid, name) in enumerate(regions):
            url = "https://evemaps.dotlan.net/region/" + name.replace(" ", "_")
            r = c.get(url)
            if r.status_code != 200:
                print(f"  {name}: HTTP {r.status_code}", file=sys.stderr)
            else:
                got = parse_region(r.text)
                found.update(got)
                print(f"  [{i + 1}/{len(regions)}] {name}: {len(got)} systems,"
                      f" {sum(1 for v in got.values() if v[1])} with ice")
            time.sleep(2)
    checked = [sid for sid in found if sid in belts]
    wrong = [sid for sid in checked if found[sid][0] != belts[sid]]
    print(f"{len(found)} systems read, asteroid belts agree with the SDE for "
          f"{len(checked) - len(wrong)} of {len(checked)}")
    if len(wrong) > len(checked) * 0.05:
        sys.exit("too many belt counts disagree with the SDE - the page was not read right")
    ice = {str(sid): v[1] for sid, v in sorted(found.items()) if v[1]}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"source": "DOTLAN EveMaps (evemaps.dotlan.net), region pages",
                   "fetched": time.strftime("%Y-%m-%d", time.gmtime()),
                   "systems_read": len(found), "ice_belts": ice}, f, indent=0)
    print(f"{len(ice)} systems with ice belts -> {OUT}")


if __name__ == "__main__":
    main()
