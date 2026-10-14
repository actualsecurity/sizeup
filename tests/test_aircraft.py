"""Aircraft near HPN (sizeup/aircraft.py, #155): shortfinal's PCFD-focused picture as a CAD map layer. The feeds
(adsb.lol, adsb.fi, our own receiver's 1090 and 978 aircraft.json merged by ICAO address), the phase rules ported
from shortfinal (arriving on a published approach or a runway end, departing, on the ground, low, overflight), the
glitch filter and the stale marking, the FAA registry's types (never an owner), the schedule (only while someone
looks), the ETag, the config (a dongle named for ADS-B is kept out of sizeup's radios and never opened), and the page.

Never adsb.lol or the FAA itself (conftest.py turns the default addresses off and refuses any host but this one).
Fixtures (tests/fixtures/aircraft/, tools/record_aircraft_fixtures.py):
  adsblol_hpn_1.json, _2.json  adsb.lol's answer for 15 nm around HPN, recorded 2026-10-14 14:38:04 and 14:38:30
                               EDT, as served: EJA203 just landing on 34 (its last position 18 s, then 44 s old),
                               N41DW (SR22) on the ILS 34, an LGA arrival (RPA4546) over Westchester at 2,000 ft, a
                               Bell 427 helicopter, two light aircraft, two overflights.
  faa_registry.zip             the FAA Releasable Aircraft Database of 2026-10-13 trimmed to those US aircraft and
                               one more, real headers, the owner name and address columns blanked.
  readsb_aircraft.json         NOT recorded from a receiver (there is none yet): adsblol_hpn_2.json in readsb's
                               file layout ("now" in seconds, "aircraft").
  dump978_aircraft.json        NOT recorded: built by hand from the fields dump978-fa's skyaware writer emits
                               (skyaware_writer.cc): the SR22 on UAT with a fresher fix, N43632 with an older one, and
                               a TIS-B trackfile ("~" address)."""

import json
import re
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError

import pytest

from sizeup import aircraft as A
from sizeup.aircraft import (ARRIVING, DEPARTING, DROP_S, GROUND, LOW, OVERFLIGHT, STALE_S, AircraftLayer, Plane,
                             Procedures, ReceiverFeed, Registry, Snapshot, Tracks, build_registry, classify, merge,
                             parse_snapshot)
from sizeup.config import ConfigError, load
from tests.test_cloud import HubRun, hub_cfg

FIX = Path(__file__).parent / "fixtures" / "aircraft"
LOL = [(FIX / f"adsblol_hpn_{n}.json").read_bytes() for n in (1, 2)]
FAA = (FIX / "faa_registry.zip").read_bytes()
T1, T2 = 1792003084.501, 1792003110.501          # the two answers' own times
LOL_URL = "https://lol.example/v2/point/{lat}/{lon}/{radius}"
FI_URL = "https://fi.example/lat/{lat}/lon/{lon}/dist/{radius}"
FAA_URL = "https://faa.example/ReleasableAircraft.zip"


class Clock:
    def __init__(self, t=T1 + 0.5):
        self.t = t

    def __call__(self):
        return self.t


class FakeFetch:
    """adsb.lol's two answers in turn (then the last again), the registry zip; a refusal when asked for one."""

    def __init__(self, bodies=None, codes=None):
        self.bodies = list(bodies or LOL)
        self.codes = dict(codes or {})
        self.calls: list[str] = []

    def __call__(self, url, timeout, limit, headers=None):
        self.calls.append(url)
        for prefix, code in self.codes.items():
            if url.startswith(prefix):
                raise HTTPError(url, code, f"HTTP {code}", {"Retry-After": "40"} if code == 429 else {}, None)
        if url.endswith(".zip"):
            return 200, {}, FAA
        body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        return 200, {}, body


def layer(clock=None, fetch=None, **kw) -> AircraftLayer:
    kw = {"adsblol_url": LOL_URL, "registry_url": FAA_URL, "log": lambda m: None, **kw}
    return AircraftLayer(fetch=fetch or FakeFetch(), clock=clock or Clock(), **kw)


def both(ly: AircraftLayer, clock: Clock) -> dict:
    """The two recorded answers, 26 s apart, with the registry read: what the page gets."""
    ly.registry.refresh()
    ly.refresh()
    clock.t = T2 + 0.5
    ly.refresh()
    return ly.public()


def by_cs(d: dict) -> dict:
    return {a["cs"]: a for a in d["aircraft"]}


# ---- the feeds' JSON ---------------------------------------------------------------------------------------------------


def test_adsblol_and_readsb_read_the_same():
    a = parse_snapshot(json.loads(LOL[1]))
    r = parse_snapshot(json.loads((FIX / "readsb_aircraft.json").read_text()))
    assert a.now == pytest.approx(T2) and r.now == pytest.approx(T2) and len(a.planes) == len(r.planes) == 9
    assert [p.hex for p in a.planes] == [p.hex for p in r.planes]
    sr22 = next(p for p in a.planes if p.hex == "a4d227")
    assert (sr22.flight, sr22.reg, sr22.type, sr22.alt_baro, sr22.category, sr22.source) == ("N41DW", "N41DW", "SR22", 900, "A1", "adsb")
    assert next(p for p in a.planes if p.hex == "a19e97").type == ""          # EJA203: the feed has no type for it
    for bad in ({}, {"now": 0, "ac": []}, {"now": 5, "aircraft": "x"}, []):
        with pytest.raises(ValueError):
            parse_snapshot(bad)


def test_the_receivers_1090_and_978_merge_by_icao_address():
    r = parse_snapshot(json.loads((FIX / "readsb_aircraft.json").read_text()), "1090")
    u = parse_snapshot(json.loads((FIX / "dump978_aircraft.json").read_text()), "978")
    m = merge([r, u])
    assert m.now == pytest.approx(u.now) and len(m.planes) == 10                      # 9 on 1090, 1 more only on 978
    p = {x.hex: x for x in m.planes}
    assert p["a4d227"].band == "978" and p["a4d227"].lat == pytest.approx(41.04411)      # the fresher fix wins
    assert p["a4d227"].type == "SR22" and p["a4d227"].reg == "N41DW"                    # what only 1090 heard is kept
    assert p["a53c44"].band == "1090" and p["a53c44"].flight == "N43632"               # 978's fix was 9 s old
    assert p["a53c44"].seen_pos == pytest.approx(0.4, abs=0.01)                         # rebased onto the merged clock
    assert p["~2b0c4d"].source == "tisb" and p["~2b0c4d"].band == "978"


# ---- what each aircraft is doing (shortfinal's rules) ---------------------------------------------------------------------


PROC = Procedures()


def test_the_published_approaches_are_read():
    assert len(PROC) >= 20
    names = {p[0] for p in PROC.paths}
    assert {"ILS 16", "ILS 34", "RNAV (RNP) Z 34", "RNAV (GPS) Y 16"} <= names


def test_the_recorded_traffic_reads_as_shortfinal_would(tmp_path):
    clock = Clock()
    d = both(layer(clock, cache_dir=tmp_path), clock)
    a = by_cs(d)
    assert a["N41DW"]["phase"] == ARRIVING and a["N41DW"]["appr"] == "ILS 34" and a["N41DW"]["rwy"] == "34"
    assert a["EJA203"]["phase"] == ARRIVING and a["EJA203"]["rwy"] == "34"
    # LGA's arrival over Westchester at 1,925 ft and 12.8 nm: low traffic, not ours (the altitude-for-distance gate)
    assert a["RPA4546"]["phase"] == LOW and a["N83HH"]["phase"] == LOW and a["N83HH"]["kind"] == "helicopter"
    shown = set(a)
    assert "ROU1819" not in shown and "PTR2334" not in shown and "EJA267" not in shown    # overflights: not drawn
    assert [x["cs"] for x in d["aircraft"]][:2] == ["N41DW", "EJA203"] or [x["cs"] for x in d["aircraft"]][:2] == ["EJA203", "N41DW"]
    assert d["counts"] == {"arriving": 2, "low": 4, "emergency": 0}


def plane(**kw) -> Plane:
    base = dict(hex="abc123", lat=41.06, lon=-73.70, track=330.0, gs=120.0, alt_baro=1500, baro_rate=0.0, category="A1")
    return Plane(**{**base, **kw})


def test_ground_departing_low_overflight_and_other_ground():
    assert classify(plane(alt_baro="ground", gs=10.0), PROC).kind == GROUND
    assert classify(plane(alt_baro="ground", lat=40.777, lon=-73.872), PROC).kind == "other_ground"      # LGA
    # off runway 34 (heading 330), climbing away
    assert classify(plane(lat=41.085, lon=-73.722, alt_baro=1800, baro_rate=1500.0), PROC).kind == DEPARTING
    assert classify(plane(lat=41.00, lon=-73.66, alt_baro=3000, track=90.0), PROC).kind == LOW
    assert classify(plane(lat=41.00, lon=-73.66, alt_baro=12000, track=90.0), PROC).kind == OVERFLIGHT
    assert classify(plane(lat=None, lon=None), PROC).kind == OVERFLIGHT


def test_an_emergency_is_shown_whatever_it_is_doing_and_first():
    ly = layer()
    ly.tracks.update(Snapshot(T1, (plane(hex="e00001", flight="TEST1", lat=41.00, lon=-73.66, alt_baro=12000, track=90.0, squawk="7700"),
                                   plane(hex="e00002", flight="TEST2", alt_baro="ground", gs=5.0))))
    v = ly._view()
    assert [a["cs"] for a in v] == ["TEST1", "TEST2"] and v[0]["emerg"] == "squawk 7700 (emergency)" and v[0]["phase"] == OVERFLIGHT
    assert Plane(hex="x", emergency="minfuel").emergency_text == "minimum fuel" and Plane(hex="x", emergency="none").emergency_text == ""


def test_a_departure_stays_departing_while_it_climbs_out():
    ly = layer()
    ly.memory.phase(plane(alt_baro="ground", gs=10.0), T1)                                   # on our ground
    out = plane(lat=41.15, lon=-73.80, alt_baro=7000, baro_rate=1500.0, track=320.0, gs=250.0)   # 6 nm out, above the ceiling
    assert classify(out, PROC).kind == OVERFLIGHT and ly.memory.phase(out, T1 + 120).kind == DEPARTING


# ---- the track store: stale, dropped, glitches held back ----------------------------------------------------------------


def test_an_old_position_is_marked_and_gets_no_eta_and_an_aircraft_is_dropped(tmp_path):
    clock = Clock()
    d = both(layer(clock, cache_dir=tmp_path), clock)
    eja = by_cs(d)["EJA203"]
    assert eja["age"] > STALE_S and eja["eta"] is None                     # 44 s old: hollow on the map, no ETA from it
    assert by_cs(d)["N41DW"]["age"] < STALE_S and by_cs(d)["N41DW"]["eta"] is not None
    tr = Tracks()
    tr.update(Snapshot(T1, (plane(seen_pos=0.0),)))
    tr.update(Snapshot(T1 + DROP_S + 1, ()))
    assert not tr.live()


def test_a_position_jump_is_held_back_until_it_repeats():
    tr = Tracks()
    tr.update(Snapshot(T1, (plane(seen_pos=0.0),)))
    far = dict(lat=41.25, lon=-73.70, seen_pos=0.0)                        # 11 nm in 5 s
    for k in range(1, 3):
        tr.update(Snapshot(T1 + 5 * k, (plane(**far),)))
        assert tr.tracks["abc123"].plane.lat == pytest.approx(41.06)
    tr.update(Snapshot(T1 + 15, (plane(**far),)))
    assert tr.tracks["abc123"].plane.lat == pytest.approx(41.25)           # three times: it really is there


# ---- trails: every aircraft drawn, short, real fixes only; the turn rate the page bends its prediction by ------------------


def test_every_aircraft_drawn_has_a_short_trail_of_real_fixes(tmp_path):
    clock = Clock()
    ly = layer(clock, cache_dir=tmp_path)
    d = both(ly, clock)
    assert d["drop_s"] == DROP_S and d["stale_s"] == STALE_S
    for a in d["aircraft"]:
        t = ly.tracks.tracks[a["hex"]]
        assert a["trail"] == [[round(lon, 4), round(lat, 4)] for _, lat, lon in t.trail]          # what the store accepted
        assert len(a["trail"]) <= A.TRAIL_POINTS and all(t.pos_time - w <= A.TRAIL_S for w, _, _ in t.trail)
        assert a["cat"] == t.plane.category and "tr" in a
    a = by_cs(d)
    # not only arrivals, departures and emergencies now: low traffic too, once it has moved
    assert len(a["RPA4546"]["trail"]) == 2 and a["RPA4546"]["phase"] == LOW
    assert len(a["N41DW"]["trail"]) == 2 and a["N41DW"]["phase"] == ARRIVING


def test_a_trail_point_every_60_m_or_15_s_and_kept_short():
    tr = Tracks()
    north = 60.0 / 111195                                   # 60 m north, in degrees
    for k in range(40):                                     # 5 s apart, 30 m each: a point every other fix
        tr.update(Snapshot(T1 + 5 * k, (plane(lat=41.06 + k * north / 2, seen_pos=0.0),)))
    t = tr.tracks["abc123"]
    assert all(T1 + 5 * 39 - w <= A.TRAIL_S for w, _, _ in t.trail)          # the last 90 s only
    assert 6 <= len(t.trail) <= 10 and t.trail[-1][0] >= T1 + 5 * 37
    assert all(A.metres(a[1], a[2], b[1], b[2]) >= 59 for a, b in zip(t.trail, list(t.trail)[1:]))
    # parked: one point, its time refreshed, not a stack (shortfinal's _extend_trail)
    tr = Tracks()
    for k in range(10):
        tr.update(Snapshot(T1 + 20 * k, (plane(alt_baro="ground", gs=0.0, seen_pos=0.0),)))
    assert len(tr.tracks["abc123"].trail) == 1 and tr.tracks["abc123"].trail[0][0] == T1 + 180
    # slow: 20 m in 20 s is past the 15 s gap: a point anyway
    tr = Tracks()
    for k in range(4):
        tr.update(Snapshot(T1 + 20 * k, (plane(lat=41.06 + k * north / 3, gs=2.0, seen_pos=0.0),)))
    assert len(tr.tracks["abc123"].trail) == 4


def test_the_newest_fixes_are_kept_when_a_trail_is_thinned():
    pts = tuple((T1 + k, 41.0 + k / 1000, -73.7) for k in range(100))
    thin = A._thin(pts, A.TRAIL_POINTS)
    assert len(thin) == A.TRAIL_POINTS and thin[-1] == pts[-1] and thin[0] == pts[0]
    assert A._thin(pts[:5], A.TRAIL_POINTS) == pts[:5]


def test_a_trail_starts_over_on_a_jump_and_on_another_feed():
    tr = Tracks()
    tr.update(Snapshot(T1, (plane(seen_pos=0.0),)))
    tr.update(Snapshot(T1 + 5, (plane(lat=41.065, seen_pos=0.0),)))
    assert len(tr.tracks["abc123"].trail) == 2
    far = dict(lat=41.25, lon=-73.70, seen_pos=0.0)
    for k in range(3):
        tr.update(Snapshot(T1 + 10 + 5 * k, (plane(**far),)))
    t = tr.tracks["abc123"]
    assert [(lat, lon) for _, lat, lon in t.trail] == [(41.25, -73.70)] and t.turn is None     # no spike, no turn across it
    clock = Clock()
    ly = layer(clock, fetch=FakeFetch(), adsbfi_url=FI_URL, feeds=("adsblol", "adsbfi"))
    ly.refresh()
    first = ly.used
    ly.tracks.tracks["a8ca15"] = A.Track(plane(hex="a8ca15"), T1, T1)
    ly.resting["adsblol"] = (clock.t + 60, "rate limited")
    ly.refresh()
    assert ly.used is not first and "a8ca15" not in ly.tracks.tracks       # the other feed's ages: start afresh


def test_the_turn_rate_is_the_feeds_or_from_the_last_two_fixes_and_clamped():
    tr = Tracks()
    tr.update(Snapshot(T1, (plane(track=330.0, seen_pos=0.0),)))
    assert tr.tracks["abc123"].turn is None                                 # one fix: nothing to say
    tr.update(Snapshot(T1 + 5, (plane(lat=41.062, track=340.0, seen_pos=0.0),)))
    assert tr.tracks["abc123"].turn == pytest.approx(2.0)                   # 10 deg right in 5 s
    tr.update(Snapshot(T1 + 10, (plane(lat=41.064, track=300.0, seen_pos=0.0),)))
    assert tr.tracks["abc123"].turn == -A.MAX_TURN_DPS                      # 40 deg left in 5 s: clamped to standard rate
    tr.update(Snapshot(T1 + 40, (plane(lat=41.070, track=320.0, seen_pos=0.0),)))
    assert tr.tracks["abc123"].turn is None                                 # 30 s apart: it may have turned back
    tr.update(Snapshot(T1 + 45, (plane(lat=41.072, track=320.0, track_rate=-1.5, seen_pos=0.0),)))
    assert tr.tracks["abc123"].turn == -1.5                                 # the feed's own wins
    assert Plane.from_json({"hex": "x", "track_rate": 0.75}).track_rate == 0.75
    ly = layer()
    ly.tracks = tr
    assert ly._view()[0]["tr"] == -1.5


# ---- the FAA registry: types by ICAO address, never an owner -------------------------------------------------------------


def test_the_registry_gives_make_and_model_and_never_an_owner(tmp_path):
    reg = Registry(FAA_URL, tmp_path, FakeFetch(), log=lambda m: None, clock=Clock())
    assert reg.lookup("a19e97") is None and reg.refresh()
    assert reg.lookup("a19e97") == {"n": "N203QS", "make": "Bombardier Inc", "model": "CL-600-2B16", "year": "2025",
                                    "kind": "airplane", "engine": "turbofan", "engines": 2}
    assert reg.lookup("ab56b8")["kind"] == "helicopter" and reg.lookup("c04051") is None and reg.lookup("~2b0c4d") is None
    con = reg.con
    cols = {r[1] for t in ("aircraft", "models") for r in con.execute(f"PRAGMA table_info({t})")}
    assert not cols & {"name", "street", "city", "owner", "zip"}
    again = Registry(FAA_URL, tmp_path, FakeFetch(), log=lambda m: None, clock=Clock())    # the cache: no fetch
    assert again.lookup("a4d227")["model"] == "SR22" and not again.due()
    with pytest.raises(ValueError):
        build_registry(zipfile_with({"README.txt": "x"}), reg.con)


def zipfile_with(files: dict) -> bytes:
    import io
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return b.getvalue()


def test_a_type_comes_from_the_feed_or_the_registry_and_is_never_made_up(tmp_path):
    clock = Clock()
    a = by_cs(both(layer(clock, cache_dir=tmp_path), clock))
    assert a["EJA203"]["type"] == "" and a["EJA203"]["model"] == "Bombardier Inc CL-600-2B16" and a["EJA203"]["model_src"] == "faa"
    assert a["RPA4546"]["type"] == "E75L" and a["RPA4546"]["airline"] == "Republic Airlines, Inc."
    assert a["EJA203"]["airline"] == "Netjets Aviation, Inc." and a["N41DW"]["airline"] == ""
    no_reg = layer(Clock(), registry=False)
    no_reg.refresh()
    eja = by_cs(no_reg.public())["EJA203"]
    assert eja["type"] == "" and eja["model"] == "" and eja["model_src"] == ""                     # nothing to say: nothing said
    body = json.dumps(both(layer(clock := Clock(), cache_dir=tmp_path / "b"), clock)).lower()
    assert "ownop" not in body and "owner" not in body


# ---- the schedule: only while someone looks; one feed after another ----------------------------------------------------------


def test_nothing_is_fetched_until_someone_looks():
    fetch, clock = FakeFetch(), Clock()
    ly = layer(clock, fetch)
    ly.tick()
    assert fetch.calls == []
    ly.asked = clock.t
    ly.tick()
    assert any("lol.example" in c for c in fetch.calls)
    clock.t += A.DEMAND_S + 1
    n = len(fetch.calls)
    ly.st["next"] = 0
    ly.tick()
    assert len(fetch.calls) == n                                   # nobody looking any more: idle


def test_a_rate_limited_feed_rests_and_the_next_one_carries_on():
    fetch, clock = FakeFetch(codes={"https://lol.example": 429}), Clock()
    ly = layer(clock, fetch, feeds=("adsblol", "adsbfi"), adsbfi_url=FI_URL)
    assert ly.refresh() and ly.public()["feed"]["key"] == "adsbfi" and "40 s" in ly.resting["adsblol"][1]
    n = sum("lol.example" in c for c in fetch.calls)
    clock.t += 10
    ly.refresh()
    assert sum("lol.example" in c for c in fetch.calls) == n       # still resting
    alone = layer(Clock(), FakeFetch(codes={"https://lol.example": 500}))
    assert alone.refresh() is False and "adsb.lol" in alone.public()["error"] and alone.st["next"] > alone.clock()


def test_off_or_unconfigured_says_so():
    off = AircraftLayer(enabled=False, fetch=FakeFetch(), log=lambda m: None)
    assert not off.configured and "off" in off.why_not() and off.start() is off and off._thread is None
    d = json.loads(off.get(A.PATH)[1])
    assert d["configured"] is False and d["aircraft"] == [] and d["note"]
    assert not AircraftLayer(log=lambda m: None).configured        # (conftest: no default addresses in tests)
    assert AircraftLayer(log=lambda m: None).registry.url == ""


def test_the_same_data_has_the_same_etag(tmp_path):
    clock = Clock()
    ly = layer(clock, cache_dir=tmp_path)
    ly.refresh()
    ly.asked = clock.t
    c1, b1, ctype, h1 = ly.get(A.PATH)
    c2, b2, _, h2 = ly.get(A.PATH)
    assert c1 == c2 == 200 and ctype == "application/json" and h1["ETag"] == h2["ETag"] and h1["Cache-Control"] == "no-cache"
    clock.t = T2 + 0.5
    ly.refresh()
    assert ly.get(A.PATH)[3]["ETag"] != h1["ETag"]
    assert ly.get("/api/cad/aircraft/x")[0] == 404


# ---- our own receiver: files read, a named dongle never opened ------------------------------------------------------------


def test_the_receiver_reads_both_bands_and_says_when_a_decoder_stopped(tmp_path):
    r = json.loads((FIX / "readsb_aircraft.json").read_text())
    u = json.loads((FIX / "dump978_aircraft.json").read_text())
    (tmp_path / "a.json").write_text(json.dumps(r))
    (tmp_path / "u.json").write_text(json.dumps(u))
    clock = Clock(u["now"] + 1)
    feed = ReceiverFeed(str(tmp_path / "a.json"), str(tmp_path / "u.json"), FakeFetch(), clock)
    assert len(feed.read().planes) == 10 and feed.bands["978"]["n"] == 3 and not feed.bands["1090"]["error"]
    clock.t += 60                                                   # neither file has moved for a minute
    with pytest.raises(RuntimeError, match="decoder"):
        feed.read()
    (tmp_path / "u.json").unlink()
    with pytest.raises(RuntimeError):
        feed.read()
    assert "FileNotFoundError" in feed.bands["978"]["error"]


def test_a_named_adsb_dongle_is_reported_never_claimed(tmp_path):
    import sizeup.radio.claims as claims
    r = json.loads((FIX / "readsb_aircraft.json").read_text())
    (tmp_path / "a.json").write_text(json.dumps(r))
    held = {"adsb": {"app": "shortfinal", "pid": 1, "mode": "ADS-B"}}
    ly = layer(Clock(r["now"] + 1), feeds=("receiver", "adsblol"), receiver=True, receiver_1090=str(tmp_path / "a.json"),
               sdr="adsb", sdr_name="V3", devices=lambda: [(0, "RTL2838UHIDIR", "adsb")], holder=held.get)
    assert ly.refresh() and ly.public()["feed"]["key"] == "receiver"
    rx = ly.public()["receiver"]
    assert rx["present"] is True and rx["held_by"] == {"app": "shortfinal", "mode": "ADS-B"} and rx["name"] == "V3"
    assert rx["bands"]["1090"]["n"] == 9
    free = layer(Clock(), sdr="adsb", devices=lambda: [(0, "RTL2838UHIDIR", "adsb")], holder=lambda s: None).public()["receiver"]
    assert "no app holds it" in free["note"]
    gone = layer(Clock(), sdr="adsb", devices=lambda: [], holder=lambda s: None).public()["receiver"]
    assert gone["present"] is False and "not plugged in" in gone["note"]
    assert layer(Clock()).public()["receiver"] is None                                   # none set up: nothing said
    assert not claims.DIR.exists() or not list(claims.DIR.iterdir())                     # nothing was claimed
    src = (Path(A.__file__)).read_text()
    assert "claims.claim(" not in src and "rtl_sdr" not in src and "subprocess" not in src and "readsb --" not in src


# ---- config -----------------------------------------------------------------------------------------------------------------


def test_the_aircraft_section(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("")
    c = load(p)
    assert c.aircraft_enabled and c.aircraft_feeds == ("adsblol",) and not c.aircraft_receiver and c.aircraft_sdr == ""
    assert c.aircraft_radius_nm == 15 and c.aircraft_registry and "adsb" not in c.sdr_ignore
    p.write_text('[radio]\nsdrs = ["v4"]\nsdr_ignore = ["x1"]\n[aircraft]\nfeeds = ["receiver", "adsblol"]\n'
                 '[aircraft.receiver]\nenabled = true\nsdr = "adsb"\njson_1090 = "run/aircraft.json"\n'
                 'json_978 = "http://192.168.1.20/skyaware978/data/aircraft.json"\n')
    c = load(p)
    assert c.aircraft_feeds == ("receiver", "adsblol") and c.aircraft_receiver and c.aircraft_sdr == "adsb"
    assert c.sdr_ignore == ("x1", "adsb")                                   # sizeup's radios never use the ADS-B dongle
    assert c.aircraft_receiver_1090 == str((tmp_path / "run" / "aircraft.json").resolve())
    assert c.aircraft_receiver_978.startswith("http://192.168.1.20/")
    for bad in ('[aircraft]\nfeeds = ["opensky"]', '[aircraft]\nfeeds = []', '[aircraft]\nfeeds = ["receiver"]',
                '[aircraft]\nradius_nm = 200', '[aircraft]\nenabled = "yes"', '[aircraft]\nadsblol_url = "http://x.example/{lat}"',
                '[aircraft.receiver]\nenabled = true\njson_1090 = "/x.json"',
                '[aircraft.receiver]\nsdr = "00000001"', '[radio]\nsdrs = ["adsb"]\n[aircraft.receiver]\nsdr = "adsb"',
                '[aircraft.receiver]\njson_978 = "ftp://x/aircraft.json"', '[aircraft]\ncolour = 1', '[aircraft]\nreceiver = 1'):
        p.write_text(bad + "\n")
        with pytest.raises(ConfigError):
            load(p)


# ---- the hub ----------------------------------------------------------------------------------------------------------------


class FakeLol:
    def __init__(self):
        self.hits: list[str] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits.append(self.path)
                body = LOL[1]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def test_the_hub_serves_aircraft_only_when_asked_and_answers_304_when_unchanged(tmp_path, monkeypatch):
    import http.client
    import time
    lol = FakeLol()
    monkeypatch.setattr(A, "WAIT_FIRST_S", 10.0)
    h = HubRun(hub_cfg(tmp_path, aircraft_adsblol_url=f"http://127.0.0.1:{lol.port}/v2/point/{{lat}}/{{lon}}/{{radius}}",
                       aircraft_registry=False))
    try:
        time.sleep(1.5)
        assert lol.hits == []                                     # nobody looking: nothing fetched
        d = h.get("/api/cad/aircraft")                            # the first look waits for the read it wakes
        assert d["configured"] and d["fetched"] and lol.hits == ["/v2/point/41.0670/-73.7076/15"]
        assert d["receiver"] is None and d["feed"]["licence"] == "ODbL 1.0"
        conn = http.client.HTTPConnection("127.0.0.1", h.server.httpd.server_address[1], timeout=5)
        conn.request("GET", "/api/cad/aircraft")
        r = conn.getresponse()
        etag = r.getheader("ETag")
        r.read()
        assert r.status == 200 and etag
        conn.request("GET", "/api/cad/aircraft", headers={"If-None-Match": etag})
        r = conn.getresponse()
        assert r.status == 304 and r.read() == b""
        state = json.loads(json.dumps(h.app.state()))
        assert "aircraft" not in state                            # never in the state push (#146)
    finally:
        h.close()
        lol.close()


# ---- the page ------------------------------------------------------------------------------------------------------------------


STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"


def test_the_aircraft_layer_only_asks_the_hub_and_predicts_only_briefly_between_real_fixes():
    js = (STATIC / "aircraft.js").read_text()
    mo = (STATIC / "motion.js").read_text()
    html = (STATIC / "cad.html").read_text()
    assert '<script src="/static/aircraft.js"></script>' in html and '<script src="/static/motion.js"></script>' in html
    assert html.index("/static/maplayers.js") < html.index("/static/motion.js") < html.index("/static/aircraft.js")
    assert "MapLayers.addGroup" in js and '"/api/cad/aircraft"' in js
    for src in (js, mo):
        assert "api.adsb.lol" not in src and "adsb.fi" not in src and "faa.gov" not in src and "opensky" not in src.lower()
    code = re.sub(r"//.*", "", js)
    mcode = re.sub(r"//.*", "", mo)
    assert "localStorage" not in code and "EventSource" not in code and "fetch(" not in mcode
    # shortfinal's motion (app.js positionNow): at most 10 s past a fix, redrawn ~10 a second, only while something moves
    assert "HORIZON_S = 10" in code and "ANIMATE_MS = 100" in code and "motion.busy()" in code
    assert "Math.min(this.horizonS" in mcode and "age > this.staleS" in mcode and "gs < 30" in mcode
    assert "Motion.tracker(Motion.arc" in code                    # the aircraft predictor, plugged into the tracker
    # polled only while a map has it on and the page is visible; old positions hollow and fading, no age printed
    assert 'document.visibilityState === "hidden"' in code and "anyOn()" in code and "POLL_MS = 2000" in code
    assert "-old" in code and "stale_s" in code and "drop_s" in code
    assert "s old" not in code and "s ago" not in code and "Math.round(age)" not in code
    # every aircraft's trail, from the hub's real fixes, while the layer is on
    assert "a.trail" in code and '"ac-trail"' in code and "EMPHASIS" in code
    ml = (STATIC / "maplayers.js").read_text()
    assert "this.force.aircraft = on" in ml and "this.force.airport = on" in ml
    assert 'id="i-aircraft"' in (STATIC / "icons.svg").read_text()
    for cls in ("ac-list", "ac-row", "ac-tag", "ac-stale", "ac-emerg"):
        assert f".{cls}" in (STATIC / "cadmap.css").read_text()
