"""Replay: real ADS-B around HPN through the aircraft layer (GitHub #283; sizeup/aircraft.py).

The data is private and not in git: tests/replay.py (tools/replay_sync.py pull, or tools/capture_fixture.py adsb).
Each set is the public feeds' answers (adsb.lol, adsb.fi) around HPN every few seconds over a window, as served.
The layer reads them as it reads the feeds, second by second on a fake clock (Replay below), so the map's picture
(AircraftLayer.view: each aircraft's phase, runway, approach) is the one the hub would have drawn that day.

  - departures are never drawn as arrivals: an aircraft seen lifting off HPN (low, close in, climbing) is, while it
    keeps climbing and opening from the field, never 'arriving' ("▼ inbound" on the map, #279);
  - the feed loop survives a long outage: the station's receiver silent and both APIs down for hours (the station
    feed failing every second or two: past the 1,024 failures that once overflowed its backoff, #269), then the
    real data comes back and the map has aircraft again, with every feed healthy, no traceback, the log quiet.
"""

from __future__ import annotations

import json
from urllib.error import URLError

import pytest

from sizeup import aircraft as A
from sizeup.aircraft import AircraftLayer

pytestmark = pytest.mark.replay("adsb")

LOL = "https://adsblol.replay.invalid/{lat}/{lon}/{radius}"
FI = "https://adsbfi.replay.invalid/{lat}/{lon}/{radius}"


class Replay:
    """A set's answers in time order. fetch() answers a feed with its latest answer at the clock (shifted by `offset`
    seconds: the outage test moves the day on); `down` makes every feed fail like a lost network."""

    def __init__(self, s):
        self.lines = sorted(s.jsonl("snapshots.jsonl.gz"), key=lambda d: d["t"])
        self.feeds = sorted({d["feed"] for d in self.lines})
        self.t = self.lines[0]["t"]
        self.offset = 0.0
        self.down = False
        self.i = 0
        self.latest: dict[str, dict] = {}

    @property
    def start(self) -> float:
        return self.lines[0]["t"]

    @property
    def end(self) -> float:
        return self.lines[-1]["t"]

    def clock(self) -> float:
        return self.t + self.offset

    def advance(self, t: float) -> None:
        """The recording up to its own time t."""
        self.t = t
        while self.i < len(self.lines) and self.lines[self.i]["t"] <= t:
            d = self.lines[self.i]
            self.latest[d["feed"]] = d
            self.i += 1

    def fetch(self, url, timeout, limit, headers=None):
        if self.down:
            raise URLError("replay: network down")
        feed = "adsblol" if "adsblol" in url else "adsbfi"
        d = self.latest.get(feed)
        if d is None:
            raise URLError(f"replay: no {feed} answer yet")
        body = dict(d["body"])
        if self.offset and isinstance(body.get("now"), (int, float)):
            body["now"] = body["now"] + self.offset * 1000.0          # the feeds' "now" is in ms
        return 200, {"content-type": "application/json"}, json.dumps(body).encode()

    def station_post(self, seq: int) -> dict | None:
        """What the station would forward of the same sky (our receiver): the latest adsb.fi (else adsb.lol) answer."""
        d = self.latest.get("adsbfi") or self.latest.get("adsblol")
        if d is None:
            return None
        snap = A.parse_snapshot(d["body"])
        return {"v": 1, "epoch": "replay", "seq": seq, "full": True, "gone": [],
                "ac": [A.wire(p) for p in snap.planes if p.has_position][:A.WIRE_MAX]}


def layer(rp: Replay, station: bool = False, logs: list | None = None) -> AircraftLayer:
    feeds = tuple(f for f in ("adsblol", "adsbfi") if f in rp.feeds)
    return AircraftLayer(feeds=feeds, adsblol_url=LOL, adsbfi_url=FI, registry=False, fetch=rp.fetch, clock=rp.clock,
                         log=(logs.append if logs is not None else (lambda m: None)), airlines={}, watch=False,
                         station_feed=station)


def run(rp: Replay, ly: AircraftLayer, until: float, step: float = 1.0, on_view=None, station_every: float = 0.0,
        seq: list | None = None) -> None:
    """Second by second up to the recording's time `until`: a viewer looking, the layer's tick; on_view(t, view)
    after each new picture; with station_every, the station posting that often."""
    last_seq, last_post = ly.st["seq"], -1e9
    t = rp.t
    while t < until:
        t += step
        rp.advance(t)
        if station_every and t - last_post >= station_every and not rp.down:
            post = rp.station_post(seq[0])
            if post is not None:
                ly.station_ingest(post, 1000)
                seq[0] += 1
                last_post = t
        ly.asked = rp.clock()
        ly.tick()
        if on_view is not None and ly.st["seq"] != last_seq:
            last_seq = ly.st["seq"]
            on_view(t, list(ly.view))


# ---- departures are never arrivals (#279) --------------------------------------------------------------------------

LIFTOFF_NM = 2.0          # first seen this close to the field ...
LIFTOFF_AGL = 1500.0      # ... this low ...
CLIMB_FPM = 300.0         # ... and climbing at least this fast: it just took off
CHECK_NM = 8.0            # checked while within this of the field, climbing and opening
RETURNING_S = 300.0       # seen descending toward the field within this long before: a go-around / touch-and-go, not tested


def departures(history: dict[str, list]) -> dict[str, list]:
    """From the positions the map showed (not from its phases): each aircraft that lifted off HPN, and the part of
    its track from lift-off while it kept climbing (CLIMB_FPM) and opening from the field, within CHECK_NM."""
    out = {}
    for hx, seen in history.items():
        for k, (t, a, d) in enumerate(seen):
            alt, vr = a["alt"], a["vr"]
            if not isinstance(alt, (int, float)) or vr is None:
                continue
            if not (d <= LIFTOFF_NM and alt - A.ELEV_FT <= LIFTOFF_AGL and vr >= CLIMB_FPM):
                continue
            if any(t - t0 <= RETURNING_S and (b["vr"] or 0) <= -CLIMB_FPM and d0 <= 5.0 for t0, b, d0 in seen[:k]):
                break                                   # it was coming down to the field: a go-around, not a departure
            window = [(t, a, d)]
            for t2, b, d2 in seen[k + 1:]:
                if b["vr"] is None or b["vr"] < CLIMB_FPM or d2 < window[-1][2] or d2 > CHECK_NM:
                    break
                window.append((t2, b, d2))
            out[hx] = window
            break
    return out


@pytest.mark.xfail(reason="#279: a departure off runway 34 lies on the approach path at lift-off, is read as arriving there "
                          "(PhaseMemory then holds it as 'maneuvering' while it climbs out); being fixed in #279",
                   strict=True)      # strict: once #279 lands this passes, and the XPASS says to take the mark off
def test_departures_are_never_labelled_arriving(replay_sets):
    wrong, found = [], 0
    for s in replay_sets("adsb"):
        rp = Replay(s)
        ly = layer(rp)
        history: dict[str, list] = {}

        def seen(t, view):
            for a in view:
                history.setdefault(a["hex"], []).append((t, a, A.nm(A.ARP[0], A.ARP[1], a["lat"], a["lon"])))
        run(rp, ly, rp.end, on_view=seen)
        deps = departures(history)
        found += len(deps)
        for hx, window in deps.items():
            for t, a, d in window:
                if a["phase"] == A.ARRIVING:
                    label = a["appr"] or (f"RWY {a['rwy']}" if a["rwy"] else "inbound")
                    wrong.append(f"{s.name} +{t - rp.start:.0f}s: a departure {d:.1f} nm out, {a['alt']} ft, "
                                 f"{a['vr']:+.0f} fpm, track {a['trk']}: '{label}'")
                    break
    if not found:
        pytest.skip("no departure from HPN in the recordings")
    assert not wrong, f"{len(wrong)} of {found} departures drawn as arrivals:\n" + "\n".join(wrong)


# ---- the feed loop survives a long outage (#269) -------------------------------------------------------------------

OUTAGE_S = 6 * 3600.0
WARMUP_S = 300.0


def test_the_feed_loop_survives_a_long_outage(replay_sets):
    checked = 0
    for s in replay_sets("adsb"):
        rp = Replay(s)
        if rp.end - rp.start < WARMUP_S + 120:
            continue
        checked += 1
        logs: list[str] = []
        ly = layer(rp, station=True, logs=logs)
        seq = [0]
        run(rp, ly, rp.start + WARMUP_S, station_every=1.0, seq=seq)
        before = len(ly.view)
        assert ly.st["error"] == "" and ly.st["fetched"] is not None, f"{s.name}: not reading before the outage"
        assert all(f.st["ok"] is not None for f in ly.feeds), f"{s.name}: {[(f.key, f.st['why']) for f in ly.feeds]}"

        # the outage: the station silent, both APIs failing, for hours (the recording paused, the clock running)
        rp.down = True
        n_logs = len(logs)
        paused, t = rp.t, 0.0
        while t < OUTAGE_S:
            t += 1.0
            rp.offset = t
            rp.t = paused
            ly.asked = rp.clock()
            ly.tick()                                    # never raises: a bug here stops every feed (#269)
        station = ly.station_feed()
        assert station.st["fails"] > 1024, f"{s.name}: the station feed failed only {station.st['fails']} times"
        assert ly.st["error"].startswith("no feed answering"), ly.st["error"]
        # each feed's failure logged as it rests (at most every BACKOFF_S[1] for an API, RECEIVER_LOG_EVERY_S for
        # ours): a few lines an hour, never one a second (#269 logged the same line every second for 25 min)
        assert len(logs) - n_logs < OUTAGE_S / 60, f"{s.name}: {len(logs) - n_logs} log lines in the outage"
        assert not any("Traceback" in m or "OverflowError" in m for m in logs), "\n".join(logs[-5:])

        # back: the rest of the recording, the day moved on by the outage
        rp.down = False
        back = rp.t
        pictures = []
        run(rp, ly, min(rp.end, back + 600.0), station_every=1.0, seq=seq, on_view=lambda t, v: pictures.append((t, len(v))))
        assert pictures, f"{s.name}: no new picture after the outage"
        first = pictures[0][0] - back
        assert first <= 15.0, f"{s.name}: the first picture {first:.0f} s after the feeds came back"
        assert ly.st["error"] == "", ly.st["error"]
        assert station.st["fails"] == 0 and station.st["ok"] is not None and rp.clock() - station.st["ok"] <= 5.0
        if rp.t - back >= A.BACKOFF_S[1] + 30:          # the APIs rest at most BACKOFF_S[1], then answer again
            for f in ly.api_feeds():
                assert f.st["fails"] == 0 and rp.clock() - f.st["ok"] <= 60.0, f"{s.name} {f.key}: {f.st['why']}"
        if before:
            assert max(n for _, n in pictures) > 0, f"{s.name}: aircraft before the outage, none after"
        assert not any("Traceback" in m or "OverflowError" in m for m in logs), "\n".join(logs[-5:])
    assert checked, f"no recording long enough ({WARMUP_S + 120:.0f} s) for the outage test"
