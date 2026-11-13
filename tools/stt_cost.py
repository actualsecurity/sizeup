#!/usr/bin/env python3
"""Speech-to-text and audio storage cost per source (radio-intel PR 3). Read-only; aggregate numbers only.

    python3 -I tools/stt_cost.py <cad.sqlite> [--days 7] [--keep-days 30] [--json]
    python3 -I tools/stt_cost.py --minutes "p25:mrrs:9211=90" --minutes "tg:307=40" [--keep-days 30]

From a CAD database (a local copy: the Litestream restore, deleted afterwards), it reads radio_traffic's start / end per
source over the last --days and prints, per source (p25:<system>:<tg>, fm:<label>, air:<label>):

    clips/day    key-ups logged a day
    min/day      minutes of audio a day (what Amazon Transcribe would bill: per second, no minimum)
    aws $/mo     that at sizeup/transcribe.py PRICE_PER_MIN, for a 30-day month
    MB/day       the audio as the hub keeps it (8 kHz mono 16-bit WAV, 16 kB/s)
    s3 $/mo      --keep-days of it kept in S3 Standard (sizeup/audiostore.py COST_GB_MONTH) + the PUTs

Without a database, --minutes SOURCE=MIN_PER_DAY gives the volumes by hand (e.g. from Settings > Radios' tiers line).
Nothing else is read: never a transcript, a talker, a radio ID or an address, so the output can be shared.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sizeup.audiostore import COST_GB_MONTH  # noqa: E402
from sizeup.transcribe import PRICE_PER_MIN, RATE  # noqa: E402

BYTES_PER_S = RATE * 2              # the hub's clips: 8 kHz, 16-bit, mono
PUT_PER_1000 = 0.005                # S3 Standard PUT, us-east-1, $ per 1,000 requests
MONTH_DAYS = 30.0


def from_db(path: Path, days: float, now: float | None = None) -> dict[str, dict]:
    """{source: {clips_day, seconds_day}} over the last `days` (the span actually logged, at least an hour)."""
    now = time.time() if now is None else now
    c = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        since = now - days * 86400
        first = c.execute("SELECT MIN(start) FROM radio_traffic WHERE start >= ?", (since,)).fetchone()[0]
        span = max(3600.0, now - (first if first else since)) / 86400
        rows = c.execute(
            "SELECT CASE WHEN source != '' THEN source WHEN talkgroup IS NOT NULL THEN 'tg:' || talkgroup "
            "ELSE 'fm:' || channel END AS src, COUNT(*), SUM(MAX(0, end - start)) "
            "FROM radio_traffic WHERE start >= ? GROUP BY src", (since,)).fetchall()
    finally:
        c.close()
    return {str(src): {"clips_day": n / span, "seconds_day": (s or 0.0) / span} for src, n, s in rows}


def from_minutes(items: list[str]) -> dict[str, dict]:
    out = {}
    for it in items:
        src, _, m = it.rpartition("=")
        if not src:
            raise SystemExit(f"--minutes {it!r}: SOURCE=MINUTES_PER_DAY")
        out[src] = {"clips_day": None, "seconds_day": float(m) * 60}
    return out


def costs(vol: dict[str, dict], keep_days: float) -> list[dict]:
    rows = []
    for src, v in vol.items():
        sec = v["seconds_day"]
        mb = sec * BYTES_PER_S / 1e6
        gb_kept = mb * min(keep_days, MONTH_DAYS * 12) / 1000
        puts = (v["clips_day"] or 0) * MONTH_DAYS * PUT_PER_1000 / 1000
        rows.append({"source": src, "clips_day": round(v["clips_day"]) if v["clips_day"] is not None else None,
                     "min_day": round(sec / 60, 1), "aws_usd_month": round(sec / 60 * PRICE_PER_MIN * MONTH_DAYS, 2),
                     "mb_day": round(mb, 1), "gb_kept": round(gb_kept, 2),
                     "s3_usd_month": round(gb_kept * COST_GB_MONTH + puts, 2)})
    rows.sort(key=lambda r: -r["min_day"])
    return rows


def total(rows: list[dict]) -> dict:
    t = {"source": "total"}
    for k in ("clips_day", "min_day", "aws_usd_month", "mb_day", "gb_kept", "s3_usd_month"):
        vals = [r[k] for r in rows if r[k] is not None]
        t[k] = round(sum(vals), 2) if vals else None
    return t


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("db", nargs="?", help="a CAD database (cad.sqlite), read only")
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--keep-days", type=float, default=30.0, help="audio kept in S3 this long (the retention)")
    ap.add_argument("--minutes", action="append", default=[], help="SOURCE=MIN_PER_DAY, instead of a database")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.db and not a.minutes:
        ap.error("a database or --minutes")
    vol = from_db(Path(a.db), a.days) if a.db else from_minutes(a.minutes)
    rows = costs(vol, a.keep_days)
    rows.append(total(rows))
    if a.json:
        print(json.dumps({"price_per_min": PRICE_PER_MIN, "s3_gb_month": COST_GB_MONTH, "keep_days": a.keep_days,
                          "rows": rows}, indent=1))
        return 0
    print(f"Amazon Transcribe ${PRICE_PER_MIN}/min (per second, no minimum) · S3 ${COST_GB_MONTH}/GB-month, "
          f"{a.keep_days:g} days kept · 30-day month")
    print(f"{'source':<28}{'clips/day':>10}{'min/day':>9}{'aws $/mo':>10}{'MB/day':>8}{'GB kept':>9}{'s3 $/mo':>9}")
    for r in rows:
        clips = "" if r["clips_day"] is None else f"{r['clips_day']:.0f}"
        print(f"{r['source'][:27]:<28}{clips:>10}{r['min_day']:>9.1f}{r['aws_usd_month']:>10.2f}{r['mb_day']:>8.1f}"
              f"{r['gb_kept']:>9.2f}{r['s3_usd_month']:>9.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
