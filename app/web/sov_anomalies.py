"""Which combat anomalies a Threat Detection Array provides, by system.

Nothing in the static data says this: an upgrade carries distribution ids that
lead nowhere in dungeons.yaml. CCP published it in the patch notes instead,
and the tables below are copied from them:

* Patch notes 22.01, 2024-07-18.1 - Minor Threat Detection Array 1-3 and
  Major Threat Detection Array 1-3, pirate space and drone space.
* Patch notes 22.01, 2024-09-18.1 - new tables for Major 1 and Major 2 ("we've
  increased the number of sites provided by the level 1 and level 2
  upgrades"); they replace the July rows for those two.
* Checked for later changes in 22.02, 23.01, 23.02 and 24.01: none to these
  counts (22.02 only tightened the warp-ins of Havens and drone sites).

The band is the system's TRUE security, as in the tables. Site names are the
generic ones the tables use; in game they carry the region's pirate faction
("Guristas Haven"), which the static data does not record per region.
"""
from __future__ import annotations

import re

SOURCE = ("CCP patch notes 22.01: 2024-07-18.1 (Minor 1-3, Major 3) and "
          "2024-09-18.1 (Major 1-2)")
RESPAWN_NOTE = "Maximum respawn time 12 minutes (2024-07-18.1)."

BANDS = ["0.0 to -0.25", "-0.25 to -0.45", "-0.45 to -0.65", "-0.65 to -0.85", "-0.85 to -1.0"]

# The rogue drone regions of null security space: drone sites instead of
# pirate ones. Not in the static data (no faction on these regions).
DRONE_REGIONS = {
    "Cobalt Edge", "Etherium Reach", "Malpais", "Oasa", "Outer Passage",
    "Perrigen Falls", "The Kalevala Expanse", "The Spire",
}

_T = {
    ("pirate", "Minor", 1): [
        "2x Refuge, 2x Den, 1x Hidden Den",
        "1x Refuge, 2x Den, 1x Hidden Den, 1x Forsaken Den",
        "1x Refuge, 2x Den, 2x Hidden Den, 1x Forsaken Den",
        "2x Den, 1x Hidden Den, 2x Forsaken Den, 1x Forlorn Den",
        "2x Den, 1x Hidden Den, 2x Forsaken Den, 2x Forlorn Den",
    ],
    ("pirate", "Minor", 2): [
        "1x Refuge, 3x Den, 1x Hidden Den, 1x Forsaken Den, 1x Forlorn Den, 2x Rally Point, 1x Hidden Rally Point",
        "1x Refuge, 3x Den, 2x Hidden Den, 1x Forsaken Den, 1x Forlorn Den, 2x Rally Point, 1x Hidden Rally Point",
        "1x Refuge, 2x Den, 2x Hidden Den, 2x Forsaken Den, 1x Forlorn Den, 2x Rally Point, 1x Hidden Rally Point",
        "2x Den, 2x Hidden Den, 2x Forsaken Den, 2x Forlorn Den, 3x Rally Point, 1x Hidden Rally Point",
        "2x Den, 2x Hidden Den, 2x Forsaken Den, 2x Forlorn Den, 2x Rally Point, 1x Hidden Rally Point, 2x Forsaken Rally Point",
    ],
    ("pirate", "Minor", 3): [
        "2x Refuge, 4x Den, 3x Hidden Den, 2x Forsaken Den, 1x Forlorn Den, 2x Rally Point, 1x Hidden Rally Point",
        "2x Refuge, 4x Den, 2x Hidden Den, 2x Forsaken Den, 1x Forlorn Den, 2x Rally Point, 2x Hidden Rally Point, 1x Forsaken Rally Point",
        "1x Refuge, 4x Den, 2x Hidden Den, 2x Forsaken Den, 2x Forlorn Den, 2x Rally Point, 1x Hidden Rally Point, 2x Forsaken Rally Point, 1x Forlorn Rally Point",
        "2x Den, 2x Hidden Den, 2x Forsaken Den, 2x Forlorn Den, 4x Rally Point, 2x Hidden Rally Point, 2x Forsaken Rally Point, 2x Forlorn Rally Point",
        "2x Den, 2x Hidden Den, 2x Forsaken Den, 2x Forlorn Den, 2x Rally Point, 3x Hidden Rally Point, 3x Forsaken Rally Point, 3x Forlorn Rally Point",
    ],
    ("pirate", "Major", 1): [
        "2x Hub, 3x Hidden Hub, 2x Forsaken Hub",
        "2x Hub, 2x Hidden Hub, 2x Forsaken Hub, 1x Forlorn Hub",
        "2x Hidden Hub, 2x Forsaken Hub, 2x Forlorn Hub, 1x Haven",
        "2x Hidden Hub, 2x Forsaken Hub, 2x Forlorn Hub, 2x Haven",
        "3x Forsaken Hub, 2x Forlorn Hub, 2x Haven, 1x Sanctum",
    ],
    ("pirate", "Major", 2): [
        "3x Hub, 3x Hidden Hub, 2x Forsaken Hub, 2x Forlorn Hub, 1x Haven",
        "2x Hub, 3x Hidden Hub, 2x Forsaken Hub, 2x Forlorn Hub, 2x Haven",
        "3x Hidden Hub, 3x Forsaken Hub, 3x Forlorn Hub, 3x Haven",
        "2x Hidden Hub, 2x Forsaken Hub, 2x Forlorn Hub, 4x Haven, 2x Sanctum",
        "3x Forsaken Hub, 2x Forlorn Hub, 5x Haven, 2x Sanctum",
    ],
    ("pirate", "Major", 3): [
        "4x Hub, 3x Hidden Hub, 3x Forsaken Hub, 3x Forlorn Hub, 2x Haven",
        "2x Hub, 3x Hidden Hub, 3x Forsaken Hub, 3x Forlorn Hub, 4x Haven, 1x Sanctum",
        "3x Hidden Hub, 3x Forsaken Hub, 3x Forlorn Hub, 6x Haven, 2x Sanctum",
        "2x Hidden Hub, 3x Forsaken Hub, 2x Forlorn Hub, 7x Haven, 3x Sanctum, 1x Forsaken Sanctum",
        "2x Forsaken Hub, 2x Forlorn Hub, 8x Haven, 4x Sanctum, 3x Forsaken Sanctum",
    ],
    ("drone", "Minor", 1): [
        "3x Drone Assembly, 2x Drone Gathering",
        "2x Drone Assembly, 2x Drone Gathering, 1x Drone Surveillance",
        "2x Drone Assembly, 3x Drone Gathering, 1x Drone Surveillance",
        "1x Drone Assembly, 2x Drone Gathering, 2x Drone Surveillance, 1x Drone Menagerie",
        "2x Drone Gathering, 2x Drone Surveillance, 3x Drone Menagerie",
    ],
    ("drone", "Minor", 2): [
        "2x Drone Assembly, 3x Drone Gathering, 3x Drone Surveillance, 2x Drone Menagerie",
        "2x Drone Assembly, 3x Drone Gathering, 3x Drone Surveillance, 3x Drone Menagerie",
        "2x Drone Assembly, 3x Drone Gathering, 3x Drone Surveillance, 2x Drone Menagerie, 1x Drone Herd",
        "1x Drone Assembly, 3x Drone Gathering, 3x Drone Surveillance, 3x Drone Menagerie, 2x Drone Herd",
        "3x Drone Gathering, 3x Drone Surveillance, 3x Drone Menagerie, 3x Drone Herd, 1x Drone Squad",
    ],
    ("drone", "Minor", 3): [
        "2x Drone Assembly, 4x Drone Gathering, 5x Drone Surveillance, 3x Drone Menagerie, 1x Drone Herd",
        "2x Drone Assembly, 4x Drone Gathering, 4x Drone Surveillance, 3x Drone Menagerie, 3x Drone Herd",
        "2x Drone Assembly, 3x Drone Gathering, 4x Drone Surveillance, 4x Drone Menagerie, 3x Drone Herd, 1x Drone Squad",
        "1x Drone Assembly, 3x Drone Gathering, 4x Drone Surveillance, 4x Drone Menagerie, 4x Drone Herd, 2x Drone Squad",
        "3x Drone Gathering, 3x Drone Surveillance, 5x Drone Menagerie, 4x Drone Herd, 3x Drone Squad",
    ],
    ("drone", "Major", 1): [
        "4x Drone Herd, 3x Drone Squad",
        "3x Drone Herd, 4x Drone Squad",
        "5x Drone Squad, 2x Drone Patrol",
        "4x Drone Squad, 4x Drone Patrol",
        "3x Drone Squad, 4x Drone Patrol, 1x Drone Horde",
    ],
    ("drone", "Major", 2): [
        "5x Drone Herd, 5x Drone Squad, 1x Drone Patrol",
        "4x Drone Herd, 4x Drone Squad, 3x Drone Patrol",
        "5x Drone Squad, 6x Drone Patrol, 1x Drone Horde",
        "2x Drone Squad, 6x Drone Patrol, 4x Drone Horde",
        "1x Drone Squad, 7x Drone Patrol, 5x Drone Horde",
    ],
    ("drone", "Major", 3): [
        "4x Drone Herd, 6x Drone Squad, 3x Drone Patrol, 2x Drone Horde",
        "2x Drone Herd, 4x Drone Squad, 5x Drone Patrol, 5x Drone Horde",
        "2x Drone Squad, 8x Drone Patrol, 7x Drone Horde",
        "8x Drone Patrol, 9x Drone Horde, 1x Teeming Drone Horde",
        "6x Drone Patrol, 10x Drone Horde, 3x Teeming Drone Horde",
    ],
}

_ITEM = re.compile(r"^(\d+)x (.+)$")
_UPGRADE = re.compile(r"^(Major|Minor) Threat Detection Array (\d)$")


def _parse(cell: str) -> list[tuple[int, str]]:
    out = []
    for part in cell.split(", "):
        m = _ITEM.match(part.strip())
        out.append((int(m.group(1)), m.group(2)))
    return out


def band(true_sec: float | None) -> int | None:
    """Index into BANDS. The tables' ranges share their end points; a value
    exactly on one (true security has six decimals) goes to the higher band."""
    if true_sec is None or true_sec > 0.0:
        return None
    for i, edge in enumerate((-0.25, -0.45, -0.65, -0.85)):
        if true_sec >= edge:
            return i
    return 4


def for_system(true_sec: float | None, region: str, installed_names) -> dict | None:
    """Every Threat Detection Array for this system's band and space, with the
    ones marked as installed flagged and summed."""
    b = band(true_sec)
    if b is None:
        return None
    space = "drone" if region in DRONE_REGIONS else "pirate"
    installed = set(installed_names)
    rows, total = [], {}
    for kind in ("Minor", "Major"):
        for level in (1, 2, 3):
            name = f"{kind} Threat Detection Array {level}"
            sites = _parse(_T[(space, kind, level)][b])
            is_in = name in installed
            rows.append({"upgrade": name, "sites": sites, "installed": is_in,
                         "count": sum(n for n, _ in sites)})
            if is_in:
                for n, site in sites:
                    total[site] = total.get(site, 0) + n
    return {"band": BANDS[b], "space": space, "rows": rows,
            "installed_total": sorted(total.items(), key=lambda kv: -kv[1]),
            "source": SOURCE, "respawn": RESPAWN_NOTE}


def is_threat_array(name: str) -> bool:
    return bool(_UPGRADE.match(name or ""))
