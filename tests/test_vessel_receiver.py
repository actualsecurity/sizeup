"""Our own AIS receiver (#402): the station's decoder (sizeup/radio/aisd.py, sizeup-ais.service), its forwarder to
the hub (sizeup/cloud/vessellink.py) and the hub's ingest (POST /api/station/vessels -> sizeup/vessels.py StationFeed).

Synthetic data only: invented MMSIs (9990000xx) and names, AIS-catcher's JSON_FULL shapes (gpsd-style field names).
AIS-catcher is never started and no dongle is opened: Popen and the device list are fakes, the claims dir a tmp dir."""

from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path

import pytest

from sizeup.cloud.client import HubClient, HubError
from sizeup.cloud.vessellink import EVERY_S, PATH, VesselForwarder
from sizeup.radio import aisd
from sizeup.vessels import Fleet, VesselLayer, ingest_messages

SERIAL = "ais162"
T0 = 1_800_000_000.0

POS = {"class": "AIS", "device": "AIS-catcher", "channel": "A", "type": 1, "repeat": 0, "mmsi": 999000001, "status": 0,
       "status_text": "Under way using engine", "turn": 0, "speed": 7.4, "accuracy": True, "lon": -73.66, "lat": 40.97,
       "course": 211.0, "heading": 210, "second": 12, "maneuver": 0, "raim": False, "radio": 0, "signalpower": -30.1,
       "nmea": ["!AIVDM,1,1,,A,synthetic,0*00"]}
STATIC5 = {"class": "AIS", "type": 5, "mmsi": 999000001, "imo": 0, "callsign": "XX0001", "shipname": "TEST TUG ONE@@",
           "shiptype": 52, "to_bow": 12, "to_stern": 8, "to_port": 3, "to_starboard": 4, "destination": "TEST HARBOR",
           "nmea": ["!AIVDM,2,1,,A,synthetic,0*00"]}
B18 = {"class": "AIS", "type": 18, "mmsi": 999000002, "speed": 4.1, "lon": -73.68, "lat": 40.98, "course": 90.0,
       "heading": 511}
B24A = {"class": "AIS", "type": 24, "mmsi": 999000002, "partno": 0, "shipname": "TEST SKIFF"}
B24B = {"class": "AIS", "type": 24, "mmsi": 999000002, "partno": 1, "shiptype": 37, "callsign": "XX0002", "to_bow": 4,
        "to_stern": 3, "to_port": 1, "to_starboard": 1}


def config(tmp_path, enabled=True, sdr=SERIAL, extra="") -> str:
    p = tmp_path / "config.toml"
    p.write_text(f'[vessels.receiver]\nenabled = {"true" if enabled else "false"}\nsdr = "{sdr}"\n{extra}')
    return str(p)


class FakeProc:
    def __init__(self, lines=()):
        self.stdout = iter(lines)
        self.pid = 4242
        self.code = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = 0

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def decoder(tmp_path, cfg, found=((0, "rtl1"), (1, SERIAL)), exe="/opt/test/AIS-catcher", lines=(), clock=None):
    started = []

    def popen(argv, **kw):
        started.append(argv)
        p = FakeProc(lines)
        started_procs.append(p)
        return p
    started_procs: list = []
    d = aisd.Decoder(config=cfg, messages=str(tmp_path / "run" / "messages.json"), status=str(tmp_path / "run" / "decoder.json"),
                     find_binary=lambda: exe, find_devices=lambda: list(found) if found is not None else None, popen=popen,
                     claims=tmp_path / "claims", clock=clock or Clock(), log=lambda m: None)
    return d, started, started_procs


# ---- aisd: what it runs, and when not -------------------------------------------------------------------------------


def test_the_command_line_is_the_dongles_index_json_full_on_stdout_and_no_network_ports():
    argv = aisd.ais_argv("/opt/sizeup/ais-catcher/bin/AIS-catcher", 3, "auto", 0)
    assert argv == ["/opt/sizeup/ais-catcher/bin/AIS-catcher", "-d:3", "-gr", "rtlagc", "on", "tuner", "auto", "-p", "0",
                    "-o", "5"]
    assert aisd.ais_argv("x", 0, "33.8", -2)[5:9] == ["tuner", "33.8", "-p", "-2"]
    assert not {"-N", "-u", "-P", "-S", "-H", "-X"} & set(argv)          # no web viewer, UDP, TCP, HTTP or sharing


def test_settings_read_the_config_and_refuse_a_bad_serial(tmp_path):
    s = aisd.settings(config(tmp_path, extra='gain = 33.8\nppm = 2\n'))
    assert s == {"enabled": True, "sdr": SERIAL, "gain": "33.8", "ppm": 2, "problem": ""}
    assert aisd.settings(config(tmp_path, sdr="00000001"))["problem"]
    assert aisd.settings(str(tmp_path / "none.toml"))["enabled"] is False


@pytest.mark.parametrize("enabled,sdr", [(False, SERIAL), (True, "")])
def test_off_or_no_dongle_named_runs_nothing_and_touches_no_dongle(tmp_path, enabled, sdr):
    looked = []
    d, started, _ = decoder(tmp_path, config(tmp_path, enabled, sdr))
    d.find_devices = lambda: looked.append(1) or []
    st = d.step()
    assert st["state"] == "off" and not started and not looked
    assert not (tmp_path / "claims").exists()


def test_a_dongle_another_app_holds_is_left_alone_and_tried_again(tmp_path):
    claims = tmp_path / "claims"
    claims.mkdir()
    (claims / f"{SERIAL}.json").write_text(json.dumps({"app": "shortfinal", "pid": os.getpid(), "mode": "adsb"}))
    clock = Clock()
    d, started, _ = decoder(tmp_path, config(tmp_path), clock=clock)
    st = d.step()
    assert st["state"] == "held" and st["held_by"] == {"app": "shortfinal", "mode": "adsb"} and not started
    assert json.loads((claims / f"{SERIAL}.json").read_text())["app"] == "shortfinal"     # never taken over
    (claims / f"{SERIAL}.json").unlink()                                                  # they let it go
    clock.t += aisd.BACKOFF_S[0]
    st = d.step()
    assert st["state"] == "starting" and started == [aisd.ais_argv("/opt/test/AIS-catcher", 1, "auto", 0)]
    c = json.loads((claims / f"{SERIAL}.json").read_text())
    assert (c["app"], c["mode"], c["pid"]) == ("sizeup", "ais", os.getpid())
    d.stop("test")
    assert not (claims / f"{SERIAL}.json").exists()                                       # released


def test_not_installed_unplugged_or_two_dongles_with_its_serial_start_nothing(tmp_path):
    d, started, _ = decoder(tmp_path, config(tmp_path), exe="")
    assert d.step()["state"] == "missing" and "not installed" in d.status["detail"]
    d, started2, _ = decoder(tmp_path, config(tmp_path), found=((0, "rtl1"),))
    assert d.step()["state"] == "missing" and "plugged" in d.status["detail"]
    d, started3, _ = decoder(tmp_path, config(tmp_path), found=((0, SERIAL), (1, SERIAL)))
    assert d.step()["state"] == "ambiguous"
    assert not started and not started2 and not started3
    assert not (tmp_path / "claims" / f"{SERIAL}.json").exists()


def test_an_exit_is_restarted_with_backoff_and_releases_the_claim_meanwhile(tmp_path):
    clock = Clock()
    d, started, procs = decoder(tmp_path, config(tmp_path), clock=clock)
    d.step()
    procs[0].code = 1
    st = d.step()
    assert st["state"] == "down" and "exited (1)" in st["detail"] and not (tmp_path / "claims" / f"{SERIAL}.json").exists()
    clock.t += 1.0
    d.step()
    assert len(started) == 1                                   # still waiting (2 s)
    clock.t += 1.5
    d.step()
    assert len(started) == 2
    procs[1].code = 1
    d.step()
    assert d.next_start - clock.t == pytest.approx(4.0)        # doubling


def test_a_settings_change_restarts_it(tmp_path):
    cfg = config(tmp_path)
    d, started, procs = decoder(tmp_path, cfg)
    d.step()
    Path(cfg).write_text(f'[vessels.receiver]\nenabled = true\nsdr = "{SERIAL}"\ngain = 40\n')
    d.step()
    assert procs[0].terminated and started[-1][6] == "40"


# ---- aisd: the messages kept ----------------------------------------------------------------------------------------


def test_the_latest_of_each_kind_is_kept_trimmed_and_aged_out():
    k = aisd.Keeper()
    assert k.line(json.dumps(POS), T0)
    assert k.line(json.dumps(dict(POS, lat=40.971)), T0 + 10)            # the newer position replaces the older
    assert k.line(json.dumps(STATIC5), T0)
    for m in (B18, B24A, B24B):
        assert k.line(json.dumps(m), T0 + 5)
    assert not k.line("AIS-catcher v0.61 starting", T0)                  # not JSON: ignored
    assert not k.line(json.dumps({"type": 4, "mmsi": 3669999}), T0)      # a base station: not kept
    msgs = k.dump(T0 + 11)
    assert len(msgs) == 5
    pos = next(m for m in msgs if m["mmsi"] == 999000001 and m["type"] == 1)
    assert pos["lat"] == 40.971 and pos["t"] == T0 + 10 and "nmea" not in pos and "signalpower" not in pos
    assert k.stats()["vessels"] == 2
    msgs = k.dump(T0 + 10 + aisd.POS_KEEP_S + 1)                         # positions gone, static data kept
    assert {m["type"] for m in msgs} == {5, 24}
    assert k.dump(T0 + aisd.STATIC_KEEP_S + 20) == []


def test_the_messages_file_round_trips_into_the_hubs_vessel_model(tmp_path):
    clock = Clock()
    lines = [json.dumps(m) + "\n" for m in (POS, STATIC5, B18, B24A, B24B)]
    d, _started, procs = decoder(tmp_path, config(tmp_path), lines=lines, clock=clock)
    d.step()
    d._reader(procs[0])                                       # (the thread's work, here)
    clock.t += aisd.WRITE_S
    st = d.step()
    assert st["state"] == "ok" and st["vessels"] == 2
    doc = json.loads((tmp_path / "run" / "messages.json").read_text())
    assert doc["at"] == clock.t and len(doc["msgs"]) == 5
    f = Fleet()
    assert ingest_messages(doc, f, "receiver") >= 3
    by = {v["id"]: v for v in f.records(clock.t)}
    a, b = by["999000001"], by["999000002"]
    assert (a["name"], a["cs"], a["cls"], a["len"], a["beam"], a["sog"], a["hdg"], a["dest"]) == \
        ("TEST TUG ONE", "XX0001", "tug", 20, 7, 7.4, 210, "TEST HARBOR")
    assert (b["name"], b["cls"], b["hdg"], b["c"]) == ("TEST SKIFF", "pleasure", None, "B")
    assert json.loads((tmp_path / "run" / "decoder.json").read_text())["state"] == "ok"


def test_nothing_heard_for_long_says_no_signal_and_keeps_running(tmp_path):
    clock = Clock()
    d, started, procs = decoder(tmp_path, config(tmp_path), clock=clock)
    d.step()
    clock.t += aisd.NO_SIGNAL_S + 1
    st = d.step()
    assert st["state"] == "no_signal" and not procs[0].terminated and len(started) == 1


# ---- the forwarder ------------------------------------------------------------------------------------------------


class FakeClient:
    def __init__(self):
        self.down, self.bodies, self.answer = False, [], {"ok": True, "n": 0, "every": 5.0}

    def request(self, method, path, body=None, timeout=None, gzip_body=False):
        assert method == "POST" and path == PATH and gzip_body
        gzip.compress(json.dumps(body).encode())                  # it must serialise
        if self.down:
            raise HubError("can't reach the hub: ConnectionRefusedError")
        self.bodies.append(body)
        return dict(self.answer)

    def close(self):
        pass


def messages_file(tmp_path, t) -> str:
    p = tmp_path / "messages.json"
    msgs = [dict({k: v for k, v in m.items() if k in aisd.FIELDS}, t=t) for m in (POS, STATIC5, B18, B24A, B24B)]
    p.write_text(json.dumps({"at": t, "msgs": msgs}))
    return str(p)


def cfg(path, on=True):
    return type("Cfg", (), {"vessels_receiver": on, "vessels_receiver_json": path,
                            "vessels_bbox": (-73.83, 40.78, -73.50, 41.07)})()


def test_off_the_forwarder_never_posts(tmp_path):
    fc = FakeClient()
    f = VesselForwarder(cfg(messages_file(tmp_path, T0), on=False), lambda t: fc, log=lambda m: None, clock=Clock())
    assert f.step() == "off" and f.start()._thread is None and not fc.bodies


def test_the_forwarder_posts_the_whole_list_as_often_as_the_hub_says(tmp_path):
    fc = FakeClient()
    clock = Clock(T0 + 1)
    f = VesselForwarder(cfg(messages_file(tmp_path, T0)), lambda t: fc, log=lambda m: None, clock=clock,
                        status=lambda: {"state": "ok", "sdr": SERIAL, "messages_per_s": 0.5, "pid": 1, "argv": ["x"]})
    assert f.step() == "posted"
    body = fc.bodies[0]
    assert body["v"] == 1 and {v["id"] for v in body["vessels"]} == {"999000001", "999000002"}
    assert body["rx"] == {"state": "ok", "sdr": SERIAL, "messages_per_s": 0.5}        # only RX_KEYS
    assert f.every == 5.0
    clock.t += 2
    assert f.step() == "waiting"
    clock.t += 3
    assert f.step() == "posted"
    fc.answer["every"] = 999                                   # kept within EVERY_S
    clock.t += 5
    f.step()
    assert f.every == EVERY_S[1]


def test_hub_offline_drops_and_backs_off_never_queues(tmp_path):
    fc = FakeClient()
    fc.down = True
    clock = Clock(T0 + 1)
    f = VesselForwarder(cfg(messages_file(tmp_path, T0)), lambda t: fc, log=lambda m: None, clock=clock, status=lambda: {})
    assert f.step() == "failed" and f.dropped_posts == 1
    first = f.next_post - clock.t
    clock.t = f.next_post
    assert f.step() == "failed"
    assert f.next_post - clock.t > first                       # doubling
    fc.down = False
    clock.t = f.next_post
    assert f.step() == "posted" and len(fc.bodies) == 1        # one post: nothing queued meanwhile
    assert f.stats()["ok"] and f.stats()["dropped_posts"] == 2


def test_no_messages_file_yet_says_so_and_posts_an_empty_list(tmp_path):
    fc = FakeClient()
    f = VesselForwarder(cfg(str(tmp_path / "missing.json")), lambda t: fc, log=lambda m: None, clock=Clock(), status=lambda: None)
    assert f.step() == "posted" and fc.bodies[0]["vessels"] == [] and "sizeup-ais" in f.reading


# ---- the hub ------------------------------------------------------------------------------------------------------


def test_the_hub_keeps_the_stations_vessels_as_its_receiver_feed():
    clock = Clock(T0)
    hub = VesselLayer(station_feed=True, clock=clock, log=lambda m: None)
    f = Fleet()
    ingest_messages(json.loads(json.dumps({"at": T0, "msgs": [dict(m, t=T0 - 30) for m in (POS, STATIC5)]})), f, "receiver")
    recs = f.records(T0)
    out = hub.station_ingest({"v": 1, "at": T0, "vessels": recs, "rx": {"state": "ok", "argv": ["no"]}})
    assert out["ok"] and out["n"] == 1 and out["every"] in (5.0, 30.0)
    d = hub.public()
    (v,) = d["vessels"]
    age = v["age"] + (clock.t - d["at"])                      # (ages are as at the answer's "at"; the page adds the rest)
    assert v["id"] == "999000001" and v["name"] == "TEST TUG ONE" and v["src"] == "receiver" and age == pytest.approx(30, abs=0.2)
    feed = next(x for x in hub.public()["feeds"] if x["key"] == "receiver")
    assert feed["station"] and feed["rx"] == {"state": "ok"}
    with pytest.raises(ValueError):
        hub.station_ingest({"vessels": "nope"})
    assert VesselLayer(clock=clock, log=lambda m: None).configured is False


def test_the_hub_endpoint_takes_gzip_with_the_station_token_only(tmp_path):
    from tests.test_cloud import TOKEN, HubRun, hub_cfg
    hub = HubRun(hub_cfg(tmp_path))
    try:
        now = time.time()
        f = Fleet()
        ingest_messages({"at": now, "msgs": [dict(m, t=now - 5) for m in (POS, STATIC5, B18, B24A, B24B)]}, f, "receiver")
        body = {"v": 1, "at": now, "vessels": f.records(now)}
        ok = HubClient(hub.url, TOKEN).request("POST", PATH, body, gzip_body=True)
        assert ok["ok"] and ok["n"] == 2
        assert {v["id"] for v in hub.app.vessels.public()["vessels"]} == {"999000001", "999000002"}
        with pytest.raises(HubError) as e:
            HubClient(hub.url, "not-the-token-0000000000").request("POST", PATH, body, gzip_body=True)
        assert e.value.status == 401
        with pytest.raises(HubError) as e:
            HubClient(hub.url, TOKEN).request("POST", PATH, {"vessels": "nope"})
        assert e.value.status == 400
        assert "999000001" not in json.dumps(hub.app.cad.state())         # never in the state push
    finally:
        hub.close()


def test_the_station_link_starts_the_forwarder_only_with_the_receiver_on():
    src = (Path(__file__).resolve().parents[1] / "sizeup" / "cloud" / "station.py").read_text()
    assert "VesselForwarder(cfg, self._client, log)" in src and "self.vessels.start()" in src and "self.vessels.stop()" in src
    unit = (Path(__file__).resolve().parents[1] / "deploy" / "station" / "systemd" / "sizeup-ais.service").read_text()
    assert "ExecStart=/usr/bin/python3 -I /usr/local/lib/sizeup/aisd.py run" in unit and "RuntimeDirectory=sizeup-ais" in unit
    assert aisd.MESSAGES.startswith("/run/sizeup-ais/")
