"""On-air A/B of an airband dongle (GitHub #275, PR #289's review): rtl_fm against the channelizer, before turning on
[radio] airband_multichannel. Run on the station, while sizeup-station runs and the channel is on a dongle:

    python3 tools/airband_ab.py "NY App 126.4/120.8" --minutes 60

The station alternates the dongle between rtl_fm (its own frequency alone) and the channelizer (with the channel's
`also` frequencies) every --phase seconds, always between transmissions, and keeps each transmission's carrier over
the floor, floor (dBFS) and voice SNR by path. This prints the medians when it is over (they are also in the channel's
Radios row tooltip data, "ab", and in the log). Read them as: the channelizer's carrier reads about +1 dB (its noise
bandwidth is narrower); a lower SNR or a carrier more than ~1 dB down on the channel's own frequency means the wideband
capture costs it something at this site: leave airband_multichannel off.

Stdlib only. Never touches a dongle itself: the station's own receiver does the switching.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def _post(base: str, body: dict) -> dict:
    req = urllib.request.Request(f"{base}/api/airband-ab", json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}") or {"error": str(e)}


def _row(base: str, label: str) -> dict | None:
    with urllib.request.urlopen(f"{base}/api/state", timeout=10) as r:
        st = json.load(r)
    return next((x for x in st.get("radios") or [] if x.get("label") == label), None)


def table(ab: dict) -> str:
    lines = [f"{'path':<12} {'MHz':>8} {'n':>4} {'carrier dB':>11} {'floor dBFS':>11} {'SNR dB':>7}"]
    for path in ("rtl_fm", "channelizer"):
        for mhz, v in (ab.get("results", {}).get(path) or {}).items():
            fmt = lambda x: "?" if x is None else f"{x:g}"   # noqa: E731
            lines.append(f"{path:<12} {mhz:>8} {v['n']:>4} {fmt(v['carrier_db']):>11} {fmt(v['floor_dbfs']):>11} "
                         f"{fmt(v['snr_db']):>7}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("channel", help='the airband channel, e.g. "NY App 126.4/120.8"')
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--phase", type=float, default=300.0, help="seconds per rtl_fm / channelizer turn")
    ap.add_argument("--url", default="http://127.0.0.1:8091")
    a = ap.parse_args(argv)
    got = _post(a.url, {"channel": a.channel, "minutes": a.minutes, "phase_s": a.phase})
    if got.get("error"):
        print(f"not started: {got['error']}", file=sys.stderr)
        return 1
    print(f"A/B on {a.channel}: {a.minutes:g} min, {a.phase:g} s turns")
    while True:
        time.sleep(30)
        row = _row(a.url, a.channel) or {}
        ab = row.get("ab") or {}
        if not ab.get("running"):
            print(table(ab))
            return 0
        print(f"  {ab.get('phase')} now; until {time.strftime('%H:%M', time.localtime(ab.get('until', 0)))}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
