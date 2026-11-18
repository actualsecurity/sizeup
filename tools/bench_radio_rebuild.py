"""What a stored transmission costs the hub, by kind: the write and the console state's next build.

    PYTHONPATH=<checkout> python tools/bench_radio_rebuild.py [--incidents 10] [--rows 40] [--each 60]

A Cad (sizeup/cad/service.py) on a temp database, the repo's own CAD lists, --incidents invented open incidents with
--rows invented transmissions each on their radio logs (FG1, our units' words, linked by hand as "Add to event" does).
Then --each transmissions of every kind below are recorded as the hub records them (Cad.record_traffic), each followed
by Cad.state() as the broadcaster's next push makes it. Per kind it prints, as one JSON line: how many of those
pushes rebuilt the state, and the CPU ms (this thread) of the write + that push, p50 / p95, and of the push alone.

  context        another P25 system's talkgroup (context only)
  county_other   one of the county's talkgroups that is not the 60 Control feed's
  airband        an airband row
  fg1_no_id      our fireground channel, no radio ID (on no incident, #163)
  feed           the 60 Control feed's talkgroup (the state shows it: rebuilds)
  on_incident    our fireground channel, a rig on an open incident (on its radio log: rebuilds)

Invented data only; nothing leaves this machine. The same script measures any checkout: point PYTHONPATH at it.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time

WORDS = ["Engine 63 to command, primary search complete, all clear", "Ladder 31 on location, going to the roof",
         "Engine 60 responding", "Engine 64, water supply established at the hydrant", "Car 2395 to command, copy"]


def pct(xs: list[float]) -> list[float]:
    xs = sorted(xs)
    return [round(statistics.median(xs), 2), round(xs[int(len(xs) * 0.95) - 1], 2)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--incidents", type=int, default=10)
    ap.add_argument("--rows", type=int, default=40, help="transmissions on each incident's radio log")
    ap.add_argument("--each", type=int, default=60, help="transmissions recorded of each kind")
    a = ap.parse_args()

    import sizeup
    from sizeup.cad.automode import AutoDispatch
    from sizeup.cad.service import Cad, Settings
    AutoDispatch.hand_dispatch = lambda self: None      # dispatch by hand as PCFD would (the bench's incidents)
    tmp = tempfile.mkdtemp(prefix="bench-radio-")
    c = Cad(Settings(db=os.path.join(tmp, "cad.sqlite")), log=lambda m: None)
    c.comms.context_broadcast = True                    # the hub behind Access
    c.radiolog.inline = True
    now = time.time()
    names = [u["name"] for u in c.store.units() if u["board"]][:a.incidents]
    incs = []
    for n, unit in enumerate(names):
        inc = c.create({"type": "STRU", "address": f"{n + 1} Example Ave", "operator": "bench"})
        c.dispatch(inc["id"], {"unit_ids": [next(u["id"] for u in c.store.units() if u["name"] == unit)], "operator": "bench"})
        incs.append((inc["id"], unit))
        for k in range(a.rows):
            tid = f"seed-{n}-{k}"
            c.record_traffic({"id": tid, "start": now - 3000 + k, "end": now - 2998 + k, "source": "FG1", "talkgroup": None,
                              "unit": "", "rig": "", "radio_id": "", "text": WORDS[k % len(WORDS)] + f" {k}", "status": "done"})
            c.link_traffic(inc["id"], {"traffic_id": tid, "operator": "bench"})
    c.state()
    feed_tg = c.feed.cfg.talkgroups[0]
    seq = [0]

    def row(kind: str) -> dict:
        seq[0] += 1
        t = {"id": f"{kind}-{seq[0]}", "start": time.time(), "end": time.time() + 3, "source": "FG1", "talkgroup": None,
             "unit": "", "rig": "", "radio_id": "", "text": f"made-up words {seq[0]}", "status": "done"}
        if kind == "context":
            t.update(source="PD Dispatch", talkgroup=9001, unit="Car 7", system="metro", source_key="p25:metro:9001",
                     group="Police", service="police", tier="text")
        elif kind == "county_other":
            t.update(source="Ops 2", talkgroup=8123, unit="Car 3")
        elif kind == "airband":
            t.update(source="Tower", mhz=118.575, service="air")
        elif kind == "feed":
            t.update(source="Fire Dispatch", talkgroup=feed_tg, unit="60 Ctrl")
        elif kind == "on_incident":
            t.update(rig=incs[seq[0] % len(incs)][1], radio_id="12345", text=WORDS[seq[0] % len(WORDS)])
        return t

    out = {"sizeup": os.path.dirname(sizeup.__file__), "incidents": a.incidents, "rows_each": a.rows,
           "state_kb": round(len(json.dumps(c.state(), default=str)) / 1024)}
    for kind in ("context", "county_other", "airband", "fg1_no_id", "feed", "on_incident"):
        both, push, rebuilt = [], [], 0
        for _ in range(a.each):
            t = row(kind)
            c0 = time.thread_time()
            c.record_traffic(t)
            c1 = time.thread_time()
            was = c._state
            c.state()
            c2 = time.thread_time()
            rebuilt += c._state is not was
            both.append((c2 - c0) * 1000)
            push.append((c2 - c1) * 1000)
        out[kind] = {"rebuilt": f"{rebuilt}/{a.each}", "write_and_push_ms": pct(both), "push_ms": pct(push)}
    print(json.dumps(out))
    c.store.shutdown()


if __name__ == "__main__":
    sys.exit(main())
