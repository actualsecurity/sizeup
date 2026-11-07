"""HPN's tower hours (GitHub #275), from the FAA Chart Supplement (airnav.com/airport/KHPN, FAA information effective
29 October 2026):

    WESTCHESTER TOWER   118.575 [0600-2300]       CTAF 118.575: with the tower closed, pilots call on it as a CTAF
    WESTCHESTER GROUND  121.825 [0600-2300]       nobody on it while the tower is closed
    NEW YORK APPROACH   126.4 0700-2300, 120.8 2300-0700, 124.65
    CLEARANCE DELIVERY  126.4; WHEN HPN TWR CLSD 127.25

Local (America/New_York) wall-clock hours, so they follow DST. Used for the rows' labels (118.575 is "CTAF" while the
tower is closed), the Radios page and the tower's mark on the map; the dongles' own hours are their config's.
"""

from __future__ import annotations

import time
from datetime import datetime

from sizeup.tz import TZ

TOWER_MHZ = 118.575
GROUND_MHZ = 121.825
TOWER_OPEN = (6 * 60, 23 * 60)        # 06:00-23:00 local: the ATCT and Ground
HOURS = "06:00-23:00"


def _minute(t: float) -> int:
    d = datetime.fromtimestamp(t, TZ)
    return d.hour * 60 + d.minute


def tower_open(t: float | None = None) -> bool:
    m = _minute(time.time() if t is None else t)
    return TOWER_OPEN[0] <= m < TOWER_OPEN[1]


def freq_name(mhz: float, t: float | None = None) -> str:
    """'CTAF' on 118.575 while the tower is closed, 'closed' on Ground's then, else ''."""
    if tower_open(t):
        return ""
    if round(float(mhz), 4) == TOWER_MHZ:
        return "CTAF"
    if round(float(mhz), 4) == GROUND_MHZ:
        return "closed"
    return ""


def tower(t: float | None = None) -> dict:
    """{open, hours, next}: the tower now, and the local time it next opens or closes ('06:00' / '23:00')."""
    is_open = tower_open(t)
    nxt = TOWER_OPEN[1] if is_open else TOWER_OPEN[0]
    return {"open": is_open, "hours": HOURS, "next": f"{nxt // 60:02d}:{nxt % 60:02d}"}
