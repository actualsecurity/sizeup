"""The Vessels layer (#402, sizeup/vessels.py): AIS messages from aisstream.io and AIS-catcher merged into one picture of
western Long Island Sound, served to the page; a marine call's named and nearest vessels.

Synthetic data only: invented MMSIs (9990000xx) and vessel names, in the shapes aisstream.io's published message model
(github.com/aisstream/ais-message-models) and AIS-catcher's JSON_FULL output (gpsd-style names) use. No captured feed."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading

import pytest

from sizeup import config as config_mod
from sizeup import vessels
from sizeup.vessels import (BBOX, DROP_S, STALE_S, AisStreamFeed, Fleet, VesselLayer, from_aiscatcher, from_aisstream,
                            is_marine, named, nearest, ship_class)
from sizeup.wsclient import GUID, WebSocket

T0 = 1_800_000_000.0
KEY = "test-key-0123456789abcdef"            # made up: the real one is in SSM / the Keychain only


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def stream(kind, body, mmsi=999000001, name=""):
    """An aisstream.io message envelope (its published shape)."""
    return {"MessageType": kind, "MetaData": {"MMSI": mmsi, "MMSI_String": str(mmsi), "ShipName": name,
                                              "latitude": body.get("Latitude", 0), "longitude": body.get("Longitude", 0),
                                              "time_utc": "2027-01-15 14:05:09.123456789 +0000 UTC"},
            "Message": {kind: body}}


def pos_report(mmsi=999000001, lat=40.97, lon=-73.66, sog=7.4, cog=211.0, hdg=210, nav=0):
    return stream("PositionReport", {"Cog": cog, "CommunicationState": 0, "Latitude": lat, "Longitude": lon, "MessageID": 1,
                                     "NavigationalStatus": nav, "PositionAccuracy": True, "Raim": False, "RateOfTurn": 0,
                                     "RepeatIndicator": 0, "Sog": sog, "Spare": 0, "SpecialManoeuvreIndicator": 0,
                                     "Timestamp": 12, "TrueHeading": hdg, "UserID": mmsi, "Valid": True}, mmsi)


def static5(mmsi=999000001, name="TEST TUG ONE", stype=52, dest="TEST HARBOR"):
    return stream("ShipStaticData", {"AisVersion": 0, "CallSign": "XX0001", "Destination": dest + "@@@",
                                     "Dimension": {"A": 12, "B": 8, "C": 3, "D": 4}, "Dte": False,
                                     "Eta": {"Day": 15, "Hour": 18, "Minute": 0, "Month": 1}, "FixType": 1,
                                     "ImoNumber": 0, "MaximumStaticDraught": 3.1, "MessageID": 5, "Name": name + "@@@@",
                                     "RepeatIndicator": 0, "Spare": False, "Type": stype, "UserID": mmsi, "Valid": True}, mmsi)


def class_b(mmsi=999000002, lat=40.98, lon=-73.68, sog=4.1, cog=90.0):
    return stream("StandardClassBPositionReport", {"AssignedMode": False, "ClassBBand": True, "ClassBDisplay": False,
                                                   "ClassBDsc": True, "ClassBMsg22": True, "ClassBUnit": True, "Cog": cog,
                                                   "CommunicationState": 0, "CommunicationStateIsItdma": True,
                                                   "Latitude": lat, "Longitude": lon, "MessageID": 18, "PositionAccuracy": False,
                                                   "Raim": False, "RepeatIndicator": 0, "Sog": sog, "Spare1": 0, "Spare2": 0,
                                                   "Timestamp": 30, "TrueHeading": 511, "UserID": mmsi, "Valid": True}, mmsi)


def static24(mmsi=999000002, part_a=True, name="TEST SKIFF", stype=37):
    return stream("StaticDataReport", {"MessageID": 24, "PartNumber": not part_a, "RepeatIndicator": 0, "Reserved": 0,
                                       "ReportA": {"Name": name, "Valid": part_a},
                                       "ReportB": {"CallSign": "XX0002", "Dimension": {"A": 4, "B": 3, "C": 1, "D": 1},
                                                   "FixType": 0, "ShipType": stype, "Spare": 0, "Valid": not part_a,
                                                   "VenderIDModel": 0, "VenderIDSerial": 0, "VendorIDName": ""},
                                       "UserID": mmsi, "Valid": True}, mmsi)


def ext_b(mmsi=999000003):
    return stream("ExtendedClassBPositionReport", {"AssignedMode": False, "Cog": 45.0, "Dimension": {"A": 10, "B": 5, "C": 2, "D": 2},
                                                   "Dte": False, "FixType": 1, "Latitude": 40.95, "Longitude": -73.70,
                                                   "MessageID": 19, "Name": "TEST SLOOP", "PositionAccuracy": True,
                                                   "Raim": False, "RepeatIndicator": 0, "Sog": 5.5, "Spare1": 0, "Spare2": 0,
                                                   "Spare3": 0, "Timestamp": 1, "TrueHeading": 40, "Type": 36, "UserID": mmsi,
                                                   "Valid": True}, mmsi)


def long_range(mmsi=999000001, lat=40.971, lon=-73.661):
    return stream("LongRangeAisBroadcastMessage", {"Cog": 200, "Latitude": lat, "Longitude": lon, "MessageID": 27,
                                                   "NavigationalStatus": 0, "PositionAccuracy": False, "PositionLatency": False,
                                                   "Raim": False, "RepeatIndicator": 0, "Sog": 7, "Spare": False,
                                                   "UserID": mmsi, "Valid": True}, mmsi)


# ---- decoding each message type ----------------------------------------------------------------------------------------


def test_aisstream_position_reports_class_a_b_and_long_range():
    m = from_aisstream(pos_report())
    assert m == {"mmsi": 999000001, "type": 1, "lat": 40.97, "lon": -73.66, "sog": 7.4, "cog": 211.0, "hdg": 210.0, "nav": 0}
    b = from_aisstream(class_b())
    assert b["type"] == 18 and b["hdg"] is None and "nav" not in b and b["sog"] == 4.1   # 511: no heading
    e = from_aisstream(ext_b())
    assert e["type"] == 19 and e["name"] == "TEST SLOOP" and e["stype"] == 36 and e["len"] == 15 and e["beam"] == 4
    lr = from_aisstream(long_range())
    assert lr["type"] == 27 and lr["hdg"] is None and lr["sog"] == 7 and lr["nav"] == 0


def test_aisstream_static_data_type_5_and_24_parts():
    s = from_aisstream(static5())
    assert s["type"] == 5 and s["name"] == "TEST TUG ONE" and s["cs"] == "XX0001" and s["dest"] == "TEST HARBOR"
    assert s["len"] == 20 and s["beam"] == 7 and s["stype"] == 52 and s["imo"] is None and "lat" not in s
    a = from_aisstream(static24(part_a=True))
    assert a == {"mmsi": 999000002, "type": 24, "name": "TEST SKIFF"}
    b = from_aisstream(static24(part_a=False))
    assert b["cs"] == "XX0002" and b["stype"] == 37 and b["len"] == 7 and "name" not in b


def test_aisstream_unavailable_values_other_types_and_errors_are_dropped():
    m = from_aisstream(pos_report(sog=102.3, cog=360, hdg=511))
    assert m["sog"] is None and m["cog"] is None and m["hdg"] is None
    assert from_aisstream(pos_report(lat=91, lon=181)) is None                   # no position
    assert from_aisstream({"MessageType": "SubscriptionConfirmation", "Message": {"CompressionEnabled": False}}) is None
    assert from_aisstream({"MessageType": "BaseStationReport", "Message": {"BaseStationReport": {"UserID": 3660000}}}) is None
    assert from_aisstream({"error": "Api Key Is Not Valid"}) is None
    assert from_aisstream(stream("PositionReport", {"UserID": 999000001, "Valid": False, "Latitude": 40.9, "Longitude": -73.7})) is None
    assert from_aisstream("not a message") is None


def catcher(**kw):
    """An AIS-catcher JSON_FULL message (scaled), the console docs' field names."""
    return {"class": "AIS", "device": "AIS-catcher", "rxtime": "20270115140509", "scaled": True, "channel": "B",
            "nmea": ["!AIVDM,1,1,,B,synthetic,0*00"], "signalpower": -40.0, "ppm": 0.0, "repeat": 0, **kw}


def test_ais_catcher_json_each_type():
    p = from_aiscatcher(catcher(type=3, mmsi=999000001, status=5, status_text="Moored", turn=0, speed=0.0, accuracy=True,
                                lon=-73.66, lat=40.97, course=360.0, heading=511, second=12))
    assert p == {"mmsi": 999000001, "type": 3, "lat": 40.97, "lon": -73.66, "sog": 0.0, "cog": None, "hdg": None, "nav": 5}
    s = from_aiscatcher(catcher(type=5, mmsi=999000001, imo=1234567, callsign="XX0001", shipname="TEST FERRY",
                                shiptype=60, to_bow=30, to_stern=10, to_port=5, to_starboard=5, destination="TEST PIER"))
    assert s["name"] == "TEST FERRY" and s["imo"] == 1234567 and s["len"] == 40 and s["beam"] == 10 and s["dest"] == "TEST PIER"
    b = from_aiscatcher(catcher(type=18, mmsi=999000002, speed=4.1, lon=-73.68, lat=40.98, course=90.0, heading=511))
    assert b["type"] == 18 and b["hdg"] is None and "nav" not in b
    e = from_aiscatcher(catcher(type=19, mmsi=999000003, speed=5.5, lon=-73.70, lat=40.95, course=45.0, heading=40,
                                shipname="TEST SLOOP", shiptype=36, to_bow=10, to_stern=5, to_port=2, to_starboard=2))
    assert e["name"] == "TEST SLOOP" and e["stype"] == 36 and e["lat"] == 40.95
    assert from_aiscatcher(catcher(type=24, mmsi=999000002, partno=0, shipname="TEST SKIFF")) == \
        {"mmsi": 999000002, "type": 24, "name": "TEST SKIFF"}
    pb = from_aiscatcher(catcher(type=24, mmsi=999000002, partno=1, shiptype=37, callsign="XX0002", to_bow=4, to_stern=3,
                                 to_port=1, to_starboard=1))
    assert pb["stype"] == 37 and pb["cs"] == "XX0002"
    lr = from_aiscatcher(catcher(type=27, mmsi=999000001, speed=7, lon=-73.66, lat=40.97, course=200, status=0))
    assert lr["type"] == 27 and lr["nav"] == 0 and lr["hdg"] is None
    assert from_aiscatcher(catcher(type=4, mmsi=3669999)) is None             # a base station: not a vessel
    assert from_aiscatcher(catcher(type=1, mmsi=999000001, lat=91.0, lon=181.0)) is None


def test_ship_type_labels():
    assert [ship_class(c) for c in (70, 79, 80, 60, 40, 31, 52, 37, 36, 30, 55, 51, 35, 99, 0, None)] == \
        ["cargo", "cargo", "tanker", "passenger", "passenger", "tug", "tug", "pleasure", "sailing", "fishing", "law", "sar",
         "other", "other", "unknown", "unknown"]


# ---- merging ------------------------------------------------------------------------------------------------------------


def test_position_and_static_merge_by_mmsi_whatever_the_source():
    f = Fleet()
    f.apply(from_aisstream(pos_report()), T0, "aisstream")
    f.apply(from_aiscatcher(catcher(type=5, mmsi=999000001, shipname="TEST TUG ONE", callsign="XX0001", shiptype=52,
                                    to_bow=12, to_stern=8, to_port=3, to_starboard=4, destination="TEST HARBOR")), T0 + 1, "receiver")
    f.apply(from_aisstream(class_b()), T0, "aisstream")
    f.apply(from_aisstream(static24(part_a=True)), T0, "aisstream")
    f.apply(from_aisstream(static24(part_a=False)), T0, "aisstream")
    by = {r["id"]: r for r in f.records(T0 + 10)}
    tug, skiff = by["999000001"], by["999000002"]
    assert (tug["name"], tug["cs"], tug["cls"], tug["label"], tug["len"], tug["beam"]) == ("TEST TUG ONE", "XX0001", "tug", "Tug", 20, 7)
    assert (tug["sog"], tug["cog"], tug["hdg"], tug["nav"], tug["dest"], tug["age"], tug["c"]) == (7.4, 211, 210, "Under way", "TEST HARBOR", 10.0, "A")
    assert (skiff["name"], skiff["label"], skiff["len"], skiff["hdg"], skiff["nav"], skiff["c"]) == ("TEST SKIFF", "Pleasure", 7, None, "", "B")


def test_an_older_report_and_a_coarse_long_range_fix_never_replace_a_fresh_one():
    f = Fleet()
    f.apply(from_aisstream(pos_report(lat=40.97)), T0, "aisstream")
    f.apply(from_aisstream(pos_report(lat=40.96)), T0 - 5, "receiver")          # older: ignored
    f.apply(from_aisstream(long_range(lat=40.90)), T0 + 60, "aisstream")       # type 27 over a 1-minute-old precise fix
    (r,) = f.records(T0 + 61)
    assert r["lat"] == 40.97 and r["c"] == "A"
    f.apply(from_aisstream(long_range(lat=40.90)), T0 + 400, "aisstream")      # the precise one is 400 s old: taken
    (r,) = f.records(T0 + 401)
    assert r["lat"] == 40.9 and r["c"] == "LR"


def test_stale_then_dropped_and_names_kept_for_a_vessel_seen_again():
    f = Fleet()
    f.apply(from_aisstream(pos_report()), T0, "aisstream")
    f.apply(from_aisstream(static5()), T0, "aisstream")
    (r,) = f.records(T0 + STALE_S + 1)
    assert r["age"] > STALE_S                         # the page fades it (stale_s in the answer)
    assert f.records(T0 + DROP_S + 1) == []           # dropped
    f.apply(from_aisstream(pos_report()), T0 + 3600, "aisstream")
    (r,) = f.records(T0 + 3600)
    assert r["name"] == "TEST TUG ONE"                # its static data, kept by MMSI


# ---- the area -----------------------------------------------------------------------------------------------------------


def test_the_default_box_and_anything_outside_it_dropped():
    w, s, e, n = BBOX
    assert (w, s, e, n) == (-73.83, 40.78, -73.50, 41.07)              # Throgs Neck to Stamford, CT shore to LI's north shore
    f = Fleet()
    f.apply(from_aisstream(pos_report(mmsi=999000005, lat=40.60, lon=-74.05)), T0, "aisstream")   # NY Harbor: outside
    assert f.records(T0) == [] and f.outside == 1
    f.apply(from_aisstream(pos_report(mmsi=999000006, lat=41.00, lon=-73.60)), T0, "aisstream")
    f.apply(from_aisstream(pos_report(mmsi=999000006, lat=41.15, lon=-73.20)), T0 + 30, "aisstream")  # sails out east
    assert f.records(T0 + 30) == []


def test_the_box_is_a_config_setting(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[vessels]\nbbox = [-73.9, 40.7, -73.4, 41.1]\n")
    c = config_mod.load(p)
    assert c.vessels_bbox == (-73.9, 40.7, -73.4, 41.1)
    ly = VesselLayer.from_config(c, log=lambda m: None)
    assert ly.bbox == c.vessels_bbox and ly.fleet.bbox == c.vessels_bbox
    for bad in ("[-73.5, 40.7, -73.9, 41.1]", "[-75.0, 40.0, -72.0, 41.1]", '"west"'):
        p.write_text(f"[vessels]\nbbox = {bad}\n")
        with pytest.raises(config_mod.ConfigError, match="vessels.bbox"):
            config_mod.load(p)
    sub = json.loads(AisStreamFeed(KEY, (-73.9, 40.7, -73.4, 41.1), Fleet(), lambda: True).subscription())
    assert sub["BoundingBoxes"] == [[[40.7, -73.9], [41.1, -73.4]]]        # aisstream's corners: [lat, lon]
    assert "PositionReport" in sub["FilterMessageTypes"] and "ShipStaticData" in sub["FilterMessageTypes"]


def test_no_key_means_the_source_is_off(tmp_path, monkeypatch):
    monkeypatch.delenv("SIZEUP_AISSTREAM_KEY", raising=False)
    p = tmp_path / "c.toml"
    p.write_text("")
    c = config_mod.load(p)
    assert c.vessels_aisstream_key == "" and c.vessels_receiver is False
    ly = VesselLayer.from_config(c, log=lambda m: None)
    assert ly.aisstream is None and not ly.configured and ly.why_not().startswith("no source")
    d = ly.public()
    assert d["configured"] is False and d["key"] == "missing" and d["vessels"] == [] and KEY not in json.dumps(d)
    monkeypatch.setenv("SIZEUP_AISSTREAM_KEY", KEY)
    c = config_mod.load(p)
    ly = VesselLayer.from_config(c, log=lambda m: None)
    assert ly.aisstream is not None and ly.configured and ly.public()["key"] == "set"
    assert KEY not in json.dumps(ly.public())                                     # never shown
    monkeypatch.setenv("SIZEUP_AISSTREAM_KEY", "short")
    with pytest.raises(config_mod.ConfigError, match="aisstream"):
        config_mod.load(p)


def test_the_receivers_dongle_is_never_one_of_our_radios(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[aircraft.receiver]\nsdr = "adsb1090"\n[vessels.receiver]\nenabled = true\nsdr = "adsb1090"\n')
    with pytest.raises(config_mod.ConfigError, match="the ADS-B dongle"):
        config_mod.load(p)
    p.write_text('[radio]\nsdrs = ["ais162"]\n[vessels.receiver]\nenabled = true\nsdr = "ais162"\n')
    with pytest.raises(config_mod.ConfigError, match="one of sizeup's radios"):
        config_mod.load(p)
    p.write_text('[vessels.receiver]\nenabled = true\nsdr = "00000001"\n')
    with pytest.raises(config_mod.ConfigError, match="not all"):
        config_mod.load(p)


# ---- aisstream.io: one connection, gently ----------------------------------------------------------------------------


class FakeWS:
    def __init__(self, msgs, then=None):
        self.msgs, self.then = msgs, then
        self.sent = []
        self.closed = False
        self.sock = type("S", (), {"settimeout": lambda self, t: None})()

    def send_text(self, t):
        self.sent.append(t)

    def messages(self, idle_s=None):
        for m in self.msgs:
            yield m if m is None else json.dumps(m).encode()
        if self.then:
            raise self.then

    def close(self):
        self.closed = True


def test_a_session_subscribes_once_and_fills_the_fleet():
    clock, logs, made = Clock(), [], []
    ws = FakeWS([{"MessageType": "SubscriptionConfirmation", "Message": {"CompressionEnabled": False}},
                 pos_report(), None, static5(), class_b(), static24(), ext_b(), long_range(mmsi=999000004)])
    ly = VesselLayer(aisstream_key=KEY, log=logs.append, clock=clock, connect=lambda: made.append(1) or ws)
    ly.ask()
    ly.aisstream.session()
    assert made == [1] and len(ws.sent) == 1 and json.loads(ws.sent[0])["APIKey"] == KEY and ws.closed
    d = ly.public()
    assert {v["id"] for v in d["vessels"]} == {"999000001", "999000002", "999000003", "999000004"}
    assert d["feeds"][0]["key"] == "aisstream" and ly.aisstream.st["n"] == 7 and d["credit"] == "aisstream.io"
    assert not any(KEY in m for m in logs)


def test_failures_back_off_and_a_refused_key_waits_long_and_is_never_logged():
    clock, logs = Clock(), []
    feed = AisStreamFeed(KEY, BBOX, Fleet(), lambda: True, log=logs.append, clock=clock,
                         connect=lambda: (_ for _ in ()).throw(OSError("connection refused")))
    waits = []
    for _ in range(4):
        feed.session()
        waits.append(feed.st["rest_until"] - clock.t)
    assert waits == [5.0, 10.0, 20.0, 40.0] and feed.st["state"] == "resting"
    feed2 = AisStreamFeed(KEY, BBOX, Fleet(), lambda: True, log=logs.append, clock=clock,
                          connect=lambda: FakeWS([{"error": "Api Key Is Not Valid"}]))
    feed2.session()
    assert feed2.st["rest_until"] - clock.t == vessels.REFUSED_S and "key refused" in feed2.st["error"]
    assert logs and not any(KEY in m for m in logs)


def test_a_clean_close_never_reconnects_in_a_tight_loop():
    clock, logs, made = Clock(), [], []

    def connect():
        made.append(clock.t)
        return FakeWS([])                                  # subscribes, then the server closes at once
    feed = AisStreamFeed(KEY, BBOX, Fleet(), lambda: True, log=logs.append, clock=clock, connect=connect)
    feed.session()
    assert feed.st["rest_until"] - clock.t == vessels.REFUSED_S and "right after subscribing" in feed.st["error"]
    clock.t += 30
    class Quiet(FakeWS):                                   # quiet for a minute, then closed, nothing ever received
        def messages(self, idle_s=None):
            for _ in range(2):
                clock.t += 30
                yield None
    feed.connect = lambda: Quiet([])
    feed.st["rest_until"] = 0.0
    feed.session()
    assert feed.st["fails"] == 2 and feed.st["error"] == "closed with nothing received" and feed.st["rest_until"] > clock.t
    feed.connect = lambda: FakeWS([pos_report()])          # heard something, then closed: at least BACKOFF_S[0]
    feed.st["rest_until"] = 0.0
    feed.session()
    assert feed.st["rest_until"] >= clock.t + vessels.BACKOFF_S[0] and feed.st["fails"] == 0 and feed.st["state"] == "idle"
    # the thread: however often the server closes, connections are spaced out
    clock2 = Clock()
    n = []
    f2 = AisStreamFeed(KEY, BBOX, Fleet(), lambda: True, clock=clock2, log=lambda m: None,
                       connect=lambda: n.append(1) or FakeWS([pos_report()]))
    f2.start()
    import time as _t
    _t.sleep(0.3)
    f2.stop()
    assert len(n) == 1                                     # the next connection waits BACKOFF_S[0] on the (frozen) clock
    assert not any(KEY in m for m in logs)


def test_only_the_leader_connects():
    clock = Clock()
    ly = VesselLayer(aisstream_key=KEY, clock=clock, connect=lambda: FakeWS([]))
    ly.ask()
    assert ly.wanted() is True
    ly.leader = lambda: False                              # a standby, or a task whose lease is gone
    assert ly.wanted() is False
    ly.hot = lambda: True
    clock.t += vessels.HOT_EVERY_S
    assert ly.wanted() is False

    def boom():
        raise RuntimeError("lease unknown")
    ly.leader = boom
    assert ly.wanted() is False


def test_the_standbys_warm_providers_never_connect(tmp_path, monkeypatch):
    from sizeup import app as app_mod
    monkeypatch.setenv("SIZEUP_AISSTREAM_KEY", KEY)
    p = tmp_path / "c.toml"
    p.write_text("[gis]\nenabled = false\n[roads]\nenabled = false\n")
    c = config_mod.load(p)
    prov = app_mod.make_providers(c, None)                 # what prewarm() builds on the standby: constructed, not started
    ly = prov["vessels"]
    assert ly.aisstream is not None and ly.aisstream._thread is None


def test_it_connects_only_while_someone_looks_or_a_marine_call_is_open():
    clock = Clock()
    ly = VesselLayer(aisstream_key=KEY, clock=clock, connect=lambda: FakeWS([]))
    assert ly.wanted() is False
    ly.ask()
    clock.t += vessels.LINGER_S - 1
    assert ly.wanted() is True                         # lingers after the layer closes (names come slowly)
    clock.t += 2
    assert ly.wanted() is False
    ly.hot = lambda: True                              # an open marine call
    clock.t += vessels.HOT_EVERY_S
    assert ly.wanted() is True


def test_the_handshake_names_us_honestly(monkeypatch):
    seen = {}
    monkeypatch.setattr(vessels.WebSocket, "connect", classmethod(lambda cls, url, headers=None, timeout=0: seen.update(url=url, headers=headers) or FakeWS([])))
    feed = AisStreamFeed(KEY, BBOX, Fleet(), lambda: True)
    feed.connect()
    assert seen["url"] == "wss://stream.aisstream.io/v0/stream"
    assert seen["headers"]["User-Agent"].startswith("Port Chester Fire Department - sizeup") and KEY not in json.dumps(seen)


def test_the_api_serves_the_picture_with_an_etag_that_holds_until_something_changes():
    from sizeup.web.server import etag_matches
    clock = Clock()
    ly = VesselLayer(aisstream_key=KEY, clock=clock, connect=lambda: FakeWS([]))
    ly.fleet.apply(from_aisstream(pos_report()), clock.t, "aisstream")
    code, body, ctype, hdrs = ly.get("/api/cad/vessels")
    d = json.loads(body)
    assert code == 200 and ctype == "application/json" and hdrs["ETag"] and ly.demanded()
    assert d["stale_s"] == STALE_S and d["drop_s"] == DROP_S and d["bbox"] == list(BBOX) and len(d["vessels"]) == 1
    assert d["at"] == clock.t and d["vessels"][0]["age"] == 0.0           # ages as at "at": the page adds the rest
    clock.t += 7
    ly.fleet.apply(from_aisstream(pos_report()), clock.t - 7, "aisstream")     # the same report heard again: no change
    code2, body2, _, hdrs2 = ly.get("/api/cad/vessels")
    assert body2 == body and etag_matches(hdrs["ETag"], hdrs2["ETag"])          # the server answers 304
    ly.fleet.apply(from_aisstream(pos_report(lat=40.971)), clock.t, "aisstream")
    _, body3, _, hdrs3 = ly.get("/api/cad/vessels")
    assert not etag_matches(hdrs["ETag"], hdrs3["ETag"]) and json.loads(body3)["at"] == clock.t
    assert ly.handles("/api/cad/vessels/near") and not ly.handles("/api/cad/aircraft")


def test_the_304_round_trip_through_the_server(tmp_path):
    import urllib.request
    from urllib.error import HTTPError

    from sizeup.web.server import WebServer
    clock = Clock()
    ly = VesselLayer(aisstream_key=KEY, clock=clock, connect=lambda: FakeWS([]))
    ly.fleet.apply(from_aisstream(pos_report()), clock.t, "aisstream")
    srv = WebServer("127.0.0.1", 0, {}, lambda: {}, lambda *_: None, lambda *_: False, vessels=ly)
    port = srv.httpd.server_address[1]
    t = threading.Thread(target=srv.httpd.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{port}/api/cad/vessels"
        with urllib.request.urlopen(url, timeout=5) as r:
            etag = r.headers["ETag"]
            assert r.status == 200 and json.loads(r.read())["vessels"]
        clock.t += 5                                                          # time passes, nothing new heard
        with pytest.raises(HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(url, headers={"If-None-Match": etag}), timeout=5)
        assert e.value.code == 304
    finally:
        srv.httpd.shutdown()
        srv.httpd.server_close()


def test_the_fleet_stays_bounded_however_long_nobody_asks():
    from collections import OrderedDict
    f = Fleet()
    assert isinstance(f.static, OrderedDict)
    for i in range(vessels.MAX_POS + 50):
        f.apply(from_aisstream(pos_report(mmsi=990000000 + i, lat=40.9 + (i % 100) / 1000)), T0 + i / 100, "aisstream")
    assert len(f.pos) == vessels.MAX_POS
    f.apply(from_aisstream(pos_report(mmsi=999000001)), T0 + DROP_S + 100, "aisstream")   # prunes as messages come
    assert len(f.pos) == 1
    for i in range(vessels.STATIC_MAX + 10):
        f.apply(from_aisstream(static5(mmsi=980000000 + i)), T0 + DROP_S + 100, "aisstream")
    assert len(f.static) == vessels.STATIC_MAX and 980000000 not in f.static and 980000000 + vessels.STATIC_MAX + 9 in f.static


# ---- incidents: marine calls, the vessels named, the nearest -----------------------------------------------------------


def inc(subtype="WATER", text="", lat=40.975, lon=-73.665, units=(), typ="RESCUE"):
    return {"id": 7, "type": typ, "subtype": subtype, "lat": lat, "lon": lon, "remarks": text, "status": "active",
            "assignments": [{"name": n, "kind": k, "cleared_at": None} for n, k in units], "events": []}


def test_a_marine_call_by_subtype_or_a_marine_unit_never_a_placeholder():
    assert is_marine(inc("WATER")) and is_marine(inc("BOAT", typ="MVF")) and is_marine(inc("BOAT", typ="SERVICE"))
    assert is_marine(inc("ACFT-WATER", typ="AIRPORT"))
    assert not is_marine(inc("WATER", typ="NOTIF"))                   # a water-department notification: not marine
    assert not is_marine(inc("ACFT-WATER", typ="RESCUE"))             # pairs, not subtypes alone
    assert not is_marine(inc("STRUCTURAL")) and not is_marine(None)
    assert not is_marine(inc("", units=[("M40", "marine")]) | {"assignments": [{"name": "M40", "kind": "marine", "cleared_at": 5.0}]})
    assert is_marine(inc("", units=[("M40", "marine")]))
    assert not is_marine(inc("", units=[("DPT239", "marine")]))          # a department placeholder: never counts
    assert not is_marine(inc("", units=[("E60", "engine")]))


def fleet_of(*specs):
    f = Fleet()
    for mmsi, lat, lon, name, stype, sog in specs:
        f.apply(from_aisstream(pos_report(mmsi=mmsi, lat=lat, lon=lon, sog=sog)), T0, "aisstream")
        if name:
            f.apply(from_aisstream(static5(mmsi=mmsi, name=name, stype=stype)), T0, "aisstream")
    return f.records(T0)


def test_the_vessels_a_call_names_by_name_or_mmsi():
    vs = fleet_of((999000001, 40.97, -73.66, "TEST TUG ONE", 52, 7), (999000002, 40.98, -73.68, "TEST BREEZE", 37, 0),
                  (999000003, 40.99, -73.69, "WATER", 37, 0), (999000004, 40.99, -73.70, "", 0, 3))
    assert named("Boat fire aboard the test-breeze off the breakwater", vs) == ["999000002"]
    assert named("caller reports MMSI 999000004 taking on water", vs) == ["999000004"]   # "WATER" is a dispatch word
    assert named("tug test tug one assisting", vs) == ["999000001"]
    assert named("nothing named", vs) == []


def test_names_are_matched_in_what_was_said_never_the_location():
    from sizeup.vessels import call_text
    vs = fleet_of((999000001, 40.97, -73.66, "CITY ISLAND", 60, 7))
    where = inc("BOAT", typ="MVF") | {"address": "City Island Ave", "place": "City Island", "geo_label": "City Island",
                                      "cross_street": "City Island Rd"}
    assert named(call_text(where), vs) == []
    said = where | {"events": [{"kind": "note", "text": "caller is aboard the City Island"}]}
    assert named(call_text(said), vs) == ["999000001"]


def test_the_three_nearest_by_distance():
    vs = fleet_of((999000001, 40.990, -73.660, "FAR ONE", 70, 10), (999000002, 40.976, -73.666, "NEAREST", 37, 0),
                  (999000003, 40.980, -73.670, "SECOND", 30, 4), (999000004, 40.985, -73.660, "THIRD", 60, 12),
                  (999000005, 41.060, -73.510, "WAY OFF", 70, 10))
    near = nearest(vs, 40.975, -73.665)
    assert [v["name"] for v in near] == ["NEAREST", "SECOND", "THIRD"]
    assert near[0]["dist"] < near[1]["dist"] < near[2]["dist"] and near[0]["label"] == "Pleasure"
    assert nearest(vs, 40.975, -73.665, max_nm=0.01) == []


def test_the_ais_dongle_is_never_a_radio_or_a_job(tmp_path, monkeypatch):
    from dataclasses import replace

    from tests.test_adsb_receiver import station_app as fixture
    appmod, c, _plugged = fixture.__wrapped__(tmp_path, monkeypatch)
    c = replace(c, vessels_sdr="fire-fg", vessels_receiver=True)       # the dongle in fire-fg's USB slot, named for AIS
    app = appmod.App(c)
    try:
        assert "fire-fg" not in app.assign and app._sdr_ignored("fire-fg")
        assert app._start_mode("FG1", "fire-fg") is False                 # never given a job
        assert "AIS receiver" in app.set_adsb_receiver("fire-fg")        # and never named for ADS-B as well
        assert isinstance(app.vessels, VesselLayer)
    finally:
        app.stop()


def test_the_near_endpoint_for_a_marine_call_and_not_for_another(tmp_path):
    clock = Clock()
    ly = VesselLayer(aisstream_key=KEY, clock=clock, connect=lambda: FakeWS([]))
    for mmsi, lat, lon, name in ((999000001, 40.976, -73.666, "TEST TUG ONE"), (999000002, 40.99, -73.66, "TEST BREEZE")):
        ly.fleet.apply(from_aisstream(pos_report(mmsi=mmsi, lat=lat, lon=lon)), clock.t, "aisstream")
        ly.fleet.apply(from_aisstream(static5(mmsi=mmsi, name=name)), clock.t, "aisstream")
    calls = {7: inc("BOAT", "fire on the TEST BREEZE"), 8: inc("STRUCTURAL", "TEST BREEZE")}
    ly.lookup = lambda iid: calls[iid]
    d = json.loads(ly.get("/api/cad/vessels/near", "event=7")[1])
    assert d["marine"] and [v["id"] for v in d["named"]] == ["999000002"] and d["nearest"][0]["id"] == "999000001"
    d = json.loads(ly.get("/api/cad/vessels/near", "event=8")[1])
    assert d["marine"] is False and d["named"] == [] and d["nearest"] == []
    assert ly.get("/api/cad/vessels/near", "event=x")[0] == 400

    def gone(iid):
        raise ValueError(f"no event {iid}")         # the CAD store's CadError is a ValueError
    ly.lookup = gone
    assert ly.get("/api/cad/vessels/near", "event=99")[0] == 404


def test_the_hubs_receiver_feed_counts_only_once_the_station_has_posted():
    clock = Clock()
    ly = VesselLayer(station_feed=True, clock=clock)
    assert not ly.configured and ly.why_not().startswith("no source") and ly.public()["feeds"] == []
    ly.station_ingest({"v": 1, "vessels": [{"id": "999000001", "lat": 40.97, "lon": -73.66, "sog": 3.0, "cog": 90, "age": 2}]})
    assert ly.configured and [f["key"] for f in ly.public()["feeds"]] == ["receiver"]


def test_an_open_marine_call_keeps_aisstream_connected():
    from sizeup.app import MARINE_OPEN_SQL
    from sizeup.cad.store import Store
    st = Store(":memory:")
    st.sync_units()
    assert st.q(MARINE_OPEN_SQL) == []
    notif = st.create_incident(type="NOTIF", subtype="WATER", address="TEST MAIN")
    assert st.q(MARINE_OPEN_SQL) == []                                    # a water-department notification: no
    dpt = next(u for u in st.units() if u["name"].upper().startswith("DPT"))
    st.assign(notif, [dpt["id"]])
    st.q("UPDATE units SET kind = 'marine' WHERE id = ?", (dpt["id"],))
    assert st.q(MARINE_OPEN_SQL) == []                                    # a placeholder never counts
    m40 = next(u for u in st.units() if u["kind"] == "marine" and not u["name"].upper().startswith("DPT"))
    st.assign(notif, [m40["id"]])
    assert len(st.q(MARINE_OPEN_SQL)) == 1                                # a marine unit on it: yes
    st2 = Store(":memory:")
    iid = st2.create_incident(type="RESCUE", subtype="WATER", address="TEST PIER")
    assert len(st2.q(MARINE_OPEN_SQL)) == 1
    st2.q("UPDATE incidents SET status = 'open' WHERE id = ?", (iid,))
    assert len(st2.q(MARINE_OPEN_SQL)) == 1                               # "open" is open too
    for typ, sub in (("MVF", "BOAT"), ("SERVICE", "boat"), ("AIRPORT", "ACFT-WATER")):
        st2.q("UPDATE incidents SET type = ?, subtype = ? WHERE id = ?", (typ, sub, iid))
        assert len(st2.q(MARINE_OPEN_SQL)) == 1, (typ, sub)
    st2.q("UPDATE incidents SET type = 'NOTIF', subtype = 'WATER' WHERE id = ?", (iid,))
    assert st2.q(MARINE_OPEN_SQL) == []


# ---- the websocket client, against a local server ----------------------------------------------------------------------


def test_the_websocket_client_handshake_frames_ping_and_close():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = {}

    def serve():
        c, _ = srv.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += c.recv(4096)
        head = req.decode()
        key = next(ln.split(":", 1)[1].strip() for ln in head.split("\r\n") if ln.lower().startswith("sec-websocket-key"))
        got["ua"] = next(ln.split(":", 1)[1].strip() for ln in head.split("\r\n") if ln.lower().startswith("user-agent"))
        acc = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()
        c.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {acc}\r\n\r\n".encode())
        b0, b1 = c.recv(2)                          # the client's masked text frame
        n = b1 & 0x7F
        mask = c.recv(4)
        data = b""
        while len(data) < n:
            data += c.recv(n - len(data))
        got["sent"] = bytes(x ^ mask[i % 4] for i, x in enumerate(data)).decode()
        msg = json.dumps(pos_report()).encode()
        c.sendall(bytes([0x89, 2]) + b"hi")         # a ping
        c.sendall(bytes([0x82, 126]) + struct.pack(">H", len(msg)) + msg)   # a binary message (aisstream's)
        c.sendall(bytes([0x88, 2]) + struct.pack(">H", 1000))
        got["pong"] = c.recv(64)[:1]
        c.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    ws = WebSocket.connect(f"ws://127.0.0.1:{srv.getsockname()[1]}/v0/stream", {"User-Agent": "test-agent"}, timeout=5)
    ws.send_text('{"hello": 1}')
    msgs = list(ws.messages(idle_s=2))
    ws.close()
    t.join(5)
    srv.close()
    assert got["ua"] == "test-agent" and got["sent"] == '{"hello": 1}' and got["pong"] == bytes([0x8A])
    assert [json.loads(m)["MessageType"] for m in msgs if m] == ["PositionReport"]
