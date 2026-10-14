"""Weather and water (sizeup/weather.py, #132): NWS alerts, conditions and radar, NOAA CO-OPS tides, on recorded
fixtures (tests/fixtures/weather/, read 2026-10-14):

    alerts_mixed.json     six real alerts from /alerts/active nationwide (a warning, a red flag warning, a watch,
                          an advisory, two statements: our zones had none that day; the request names the zones)
    alerts_marine.json    a Small Craft Advisory (marine zones only); alerts_none.json: NYZ071 / NYC119 / NYZ211, empty
    khpn_*.json           KHPN's latest observation, one with a gust, one with no wind direction
    rye_beach_*.json      8518091's high / low and hourly predictions (GMT, MLLW, ft)
    kings_point_*.json    8516945's latest water level and its 6-min predictions; coops_error.json: CO-OPS's error
    radar_capabilities.xml   the NWS radar WMS's capabilities (its time dimension)

the schedule and backoff, the banner's states, the tide summary, the spoken warning and its setting, the radar
tiles, the config, and the page's static checks. Never NOAA itself (conftest.py)."""

import json
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from sizeup import USER_AGENT
from sizeup import weather as wx
from sizeup.config import ConfigError, load
from sizeup.weather import (BACKOFF_S, EVERY_S, REFUSED_S, Weather, active, alerts_url, banner_state, compass, coops_url,
                            level_of, parse_alerts, parse_frames, parse_observation, parse_predictions, parse_water_level,
                            radar_url, spoken, tide_summary, tile_bbox, wind_text)
from tests.test_cad import make_cad
from tests.test_cloud import HubRun, hub_cfg
from tests.test_settings import post

FIX = Path(__file__).parent / "fixtures" / "weather"
STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NOW = wx._iso("2026-10-14T15:40:00Z")           # the fixtures' afternoon (11:40 EDT)


def fx(name):
    return json.loads((FIX / name).read_text())


def raw(name) -> bytes:
    return (FIX / name).read_bytes()


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class FakeNoaa:
    """The answers api.weather.gov, CO-OPS and the radar WMS give for our requests, from the fixtures. fail:
    {"alerts" | "conditions" | "tides" | "predictions" | "radar" | "tile": status or exception} to fail one."""

    def __init__(self, alerts="alerts_mixed.json", obs="khpn_gust.json"):
        self.alerts, self.obs = alerts, obs
        self.calls: list[str] = []
        self.fail: dict = {}

    def kind(self, url: str) -> str:
        u = urlsplit(url)
        q = parse_qs(u.query)
        if u.path == "/alerts/active":
            return "alerts"
        if u.path.endswith("/observations/latest"):
            return "conditions"
        if u.hostname == "api.tidesandcurrents.noaa.gov":
            return "tides" if q["product"] == ["water_level"] else "predictions"
        return "tile" if q.get("request") == ["GetMap"] else "radar"

    def __call__(self, url, timeout, limit):
        self.calls.append(url)
        k = self.kind(url)
        f = self.fail.get(k)
        if isinstance(f, Exception):
            raise f
        if isinstance(f, int):
            raise HTTPError(url, f, f"HTTP {f}", None, None)
        q = parse_qs(urlsplit(url).query)
        if k == "alerts":
            return 200, {"content-type": "application/geo+json"}, raw(self.alerts)
        if k == "conditions":
            return 200, {"content-type": "application/geo+json"}, raw(self.obs)
        if k == "tides":
            return 200, {"content-type": "application/json"}, raw("kings_point_latest.json")
        if k == "predictions":
            name = {("8518091", "hilo"): "rye_beach_hilo.json", ("8518091", "h"): "rye_beach_hourly.json",
                    ("8516945", "6"): "kings_point_6min.json"}[(q["station"][0], q["interval"][0])]
            return 200, {"content-type": "application/json"}, raw(name)
        if k == "radar":
            return 200, {"content-type": "application/vnd.ogc.wms_xml"}, raw("radar_capabilities.xml")
        return 200, {"content-type": "image/png"}, b"\x89PNG\r\n\x1a\n" + url.encode()[-40:]


def make(tmp_path=None, **kw):
    fake = kw.pop("fake", None) or FakeNoaa()
    w = Weather(cache=(tmp_path / "weather.json") if tmp_path else None, fetch=fake, log=kw.pop("log", lambda m: None),
                clock=kw.pop("clock", None) or Clock(), **kw)
    return w, fake


# ---- alerts -----------------------------------------------------------------------------------------------------


def test_our_zones_in_one_request_as_ourselves():
    u = urlsplit(alerts_url())
    assert u.netloc == "api.weather.gov" and u.path == "/alerts/active"
    assert parse_qs(u.query)["zone"] == ["NYZ071,NYC119,NYZ211,ANZ335"]       # forecast, county, fire weather, the Sound
    assert "contact" in USER_AGENT and "@" in USER_AGENT                     # api.weather.gov wants a way to reach us


def test_every_request_names_us(monkeypatch):
    seen = []

    class R:
        status, headers = 200, {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"{}"

    monkeypatch.setattr(wx, "urlopen", lambda req, timeout: (seen.append(req), R())[1])
    wx.http_get(alerts_url(), 5, 100)
    wx.http_get(radar_url(8, 75, 95), 5, 100)
    assert [r.get_header("User-agent") for r in seen] == [USER_AGENT, USER_AGENT]
    assert "geo+json" in seen[0].get_header("Accept") and seen[1].get_header("Accept") == "image/png"


@pytest.mark.parametrize("event,level", [
    ("Tornado Warning", "warning"), ("Severe Thunderstorm Warning", "warning"), ("Red Flag Warning", "warning"),
    ("Extreme Wind Warning", "warning"), ("Flash Flood Emergency", "warning"), ("Winter Storm Watch", "watch"),
    ("Wind Advisory", "advisory"), ("Air Quality Alert", "advisory"), ("Special Weather Statement", "statement"),
    ("Hydrologic Outlook", "statement"), ("Coastal Flood Statement", "statement"), ("", "statement")])
def test_level_of_an_event(event, level):
    assert level_of(event) == level


def test_alerts_parsed_most_serious_first():
    al = parse_alerts(fx("alerts_mixed.json"), NOW)
    assert [a["event"] for a in al] == ["Flash Flood Warning", "Red Flag Warning", "Flood Watch", "Wind Advisory",
                                        "Special Weather Statement", "Coastal Flood Statement"]
    ff = al[0]
    assert ff["level"] == "warning" and ff["severity"] == "Severe" and ff["urgency"] in ("Immediate", "Expected")
    assert ff["headline"].startswith("Flash Flood Warning issued October 14") and ff["description"] and ff["instruction"]
    assert ff["until"] == wx._iso("2026-10-14T12:45:00-05:00") and ff["onset"] and ff["sent"] and "OKC027" in ff["zones"]
    rf = al[1]
    assert rf["nws_headline"].startswith("RED FLAG WARNING") and rf["headline"].startswith("Red Flag Warning issued")
    assert rf["ends"] and rf["until"] == rf["ends"]                         # in force until it ends, not when the message expires
    sws = next(a for a in al if a["event"] == "Special Weather Statement")
    assert sws["ends"] is None and sws["until"] == sws["expires"] and sws["marine"]       # PMZ: marine zones only
    assert not any(a["marine"] for a in al if a["event"] != "Special Weather Statement")
    marine = parse_alerts(fx("alerts_marine.json"), NOW)
    assert [(a["event"], a["level"], a["marine"]) for a in marine] == [("Small Craft Advisory", "advisory", True)]
    assert parse_alerts(fx("alerts_none.json"), NOW) == []
    with pytest.raises(ValueError):
        parse_alerts({"title": "nothing"}, NOW)


def test_a_cancel_a_test_an_expired_and_a_replaced_alert_are_left_out():
    doc = fx("alerts_mixed.json")
    first, second = doc["features"][0]["properties"], doc["features"][2]["properties"]
    assert len(parse_alerts(doc, NOW)) == 6
    later = parse_alerts(doc, wx._iso("2026-10-14T17:46:00Z"))              # the flash flood warning ended at 17:45Z
    assert "Flash Flood Warning" not in [a["event"] for a in later]
    second["status"] = "Test"
    first["messageType"] = "Cancel"
    events = [a["event"] for a in parse_alerts(doc, NOW)]
    assert "Flash Flood Warning" not in events and "Wind Advisory" not in events and len(events) == 4
    doc = fx("alerts_mixed.json")
    doc["features"][1]["properties"]["references"] = [{"identifier": doc["features"][3]["properties"]["id"]}]
    assert "Flood Watch" not in [a["event"] for a in parse_alerts(doc, NOW)]   # superseded by another message


@pytest.mark.parametrize("keep,tone,count", [
    ([], "", 0),
    (["Flash Flood Warning", "Flood Watch"], "err", 2),          # a warning: red
    (["Flood Watch"], "warn", 1),                                 # a watch: amber
    (["Wind Advisory", "Coastal Flood Statement"], "warn", 2),    # an advisory: amber
    (["Coastal Flood Statement"], "info", 1),                     # a statement: info
    (["Special Weather Statement"], "", 0),                       # marine only: the marine block, no banner
])
def test_the_banner_states(keep, tone, count):
    al = [a for a in parse_alerts(fx("alerts_mixed.json"), NOW) if a["event"] in keep]
    b = banner_state(al)
    assert b["tone"] == tone and b["count"] == count
    assert (b["top"]["event"] == keep[0]) if count else b["top"] is None
    assert sum(b["levels"].values()) == count


def test_an_alert_ages_out_of_a_cached_list():
    al = parse_alerts(fx("alerts_mixed.json"), NOW)
    assert len(active(al, NOW)) == 6
    assert "Flash Flood Warning" not in [a["event"] for a in active(al, wx._iso("2026-10-14T18:00:00Z"))]


def test_a_warning_as_the_dispatch_voice_says_it():
    a = parse_alerts(fx("alerts_mixed.json"), NOW)[0]
    assert spoken(a, now=NOW) == ("Weather alert. The National Weather Service has issued a Flash Flood Warning for "
                                  "Southern Westchester, until 1345.")                  # 17:45Z is 13:45 EDT, said "thirteen forty-five"
    rf = parse_alerts(fx("alerts_mixed.json"), NOW)[1]
    assert spoken(rf, "Port Chester", NOW).endswith("for Port Chester, until Saturday 0100.")   # 05:00Z Sat is 01:00 EDT Sat
    from sizeup.cad.speech import words
    assert "thirteen forty-five" in words(spoken(a, now=NOW))


# ---- conditions --------------------------------------------------------------------------------------------------


def test_conditions_from_khpn():
    c = parse_observation(fx("khpn_latest.json"))
    assert c["station"] == "KHPN" and c["at"] == wx._iso("2026-10-14T15:15:00+00:00") and c["text"] == "Clear"
    assert (c["temp_f"], c["humidity"], c["wind_mph"], c["gust_mph"], c["wind_deg"], c["wind_dir"]) == (75, 69, 10, None, 290, "WNW")
    assert c["visibility_mi"] == 10.0 and c["short"] == "75°F · WNW 10 mph"
    g = parse_observation(fx("khpn_gust.json"))
    assert g["short"] == "75°F · W 10 G 18 mph" and g["gust_mph"] == 18
    v = parse_observation(fx("khpn_nodir.json"))
    assert v["wind_deg"] is None and v["wind"] == "Var 3 mph"                       # light and variable: no arrow
    with pytest.raises(ValueError):
        parse_observation({"properties": {}})


def test_wind_words_and_units():
    assert wind_text({"wind_mph": 0, "gust_mph": None}) == "Calm"
    assert wind_text({"wind_mph": None}) == ""
    assert wind_text({"wind_mph": 12, "gust_mph": 22, "wind_dir": "NW"}) == "NW 12 G 22 mph"
    assert wind_text({"wind_mph": 12, "gust_mph": 12, "wind_dir": "NW"}) == "NW 12 mph"
    assert [compass(d) for d in (0, 11, 12, 315, 350, 359, None)] == ["N", "N", "NNE", "NW", "N", "N", ""]
    doc = fx("khpn_gust.json")
    p = doc["properties"]
    p["windSpeed"] = {"unitCode": "wmoUnit:m_s-1", "value": 5.0, "qualityControl": "V"}     # 11.2 mph
    p["temperature"] = {"unitCode": "wmoUnit:degC", "value": 30.0, "qualityControl": "X"}    # rejected by the NWS
    c = parse_observation(doc)
    assert c["wind_mph"] == 11 and c["temp_f"] is None and c["short"] == "W 11 G 18 mph"


# ---- tides -------------------------------------------------------------------------------------------------------


def tides_at(now, gauge=True):
    hilo = parse_predictions(fx("rye_beach_hilo.json"))
    hourly = parse_predictions(fx("rye_beach_hourly.json"))
    g = parse_water_level(fx("kings_point_latest.json")) if gauge else None
    gp = parse_predictions(fx("kings_point_6min.json"))
    return tide_summary(hilo, hourly, g, gp, now)


def test_coops_requests():
    u = urlsplit(coops_url("predictions", "8518091", interval="hilo", begin_date="20261013", end_date="20261016"))
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert u.netloc == "api.tidesandcurrents.noaa.gov" and q["application"] == "sizeup_pcfd" and q["time_zone"] == "gmt"
    assert q["datum"] == "MLLW" and q["units"] == "english" and q["format"] == "json"
    assert wx.predict_range(NOW) == {"begin_date": "20261013", "end_date": "20261016"}


def test_the_tide_summary_rising():
    t = tides_at(NOW)                                                  # 11:40 EDT: low 08:08, high 14:13
    assert t["station"] == "8518091" and t["name"] == "Rye Beach" and t["datum"] == "MLLW"
    assert t["trend"] == "rising" and t["ft"] == 4.7
    assert [(x["type"], x["ft"]) for x in t["next"]] == [("high", 7.44), ("low", 0.37)]
    assert t["next"][0]["t"] == wx._gmt("2026-10-14 18:13") and t["last"]["type"] == "low"
    assert t["hours"][0][0] >= NOW - 3600 and t["hours"][-1][0] <= NOW + 24 * 3600
    g = t["gauge"]
    assert (g["name"], g["ft"], g["predicted_ft"], g["offset_ft"], g["stale"]) == ("Kings Point", 4.9, 4.7, 0.2, False)
    assert g["at"] == wx._gmt("2026-10-14 15:30")


def test_the_tide_summary_falling_stale_and_out_of_range():
    t = tides_at(wx._iso("2026-10-14T20:00:00Z"))                     # after the 18:13Z high
    assert t["trend"] == "falling" and t["next"][0]["type"] == "low" and t["last"]["type"] == "high"
    assert t["gauge"]["stale"] and t["gauge"]["predicted_ft"] is not None     # the gauge read 4.5 hours before
    assert tides_at(NOW, gauge=False)["gauge"] is None
    assert tides_at(wx._iso("2026-10-20T12:00:00Z")) is None                  # the predictions don't reach: nothing shown
    with pytest.raises(ValueError, match="No data was found"):
        wx._json(raw("coops_error.json"))
    with pytest.raises(ValueError):
        parse_water_level({"data": []})


# ---- the schedule, the backoff, the cache ------------------------------------------------------------------------


def test_each_feed_on_its_schedule(tmp_path):
    w, fake = make(tmp_path)
    clock = w.clock
    assert w.due() == ["alerts", "conditions", "tides", "predictions"]
    for f in w.due():
        assert w.refresh(f)
    assert w.due() == [] and len(fake.calls) == 6                    # 1 + 1 + 1 + the 3 predictions
    clock.t += EVERY_S["alerts"]
    assert w.due() == ["alerts"]
    clock.t += EVERY_S["conditions"] - EVERY_S["alerts"]
    assert w.due() == ["alerts", "conditions", "tides"]
    clock.t = NOW + EVERY_S["predictions"]
    assert "predictions" in w.due()
    assert EVERY_S == {"alerts": 120.0, "conditions": 600.0, "tides": 600.0, "predictions": 21600.0}


def test_a_failure_backs_off_and_never_stops_the_others(tmp_path):
    logs = []
    w, fake = make(tmp_path, log=logs.append)
    fake.fail["alerts"] = 503
    fake.fail["conditions"] = URLError("timed out")
    assert not w.refresh("alerts") and not w.refresh("conditions")
    assert w.refresh("tides") and w.refresh("predictions")           # the others still read
    st = w.status["alerts"]
    assert st["error"] == "HTTP 503" and st["next"] == NOW + BACKOFF_S[0] and st["fails"] == 1
    waits = []
    for _ in range(6):
        w.clock.t = st["next"]
        w.refresh("alerts")
        waits.append(st["next"] - w.clock.t)
    assert waits == [120.0, 240.0, 480.0, 900.0, 900.0, 900.0]       # doubling, to 15 min
    fake.fail["alerts"] = 403
    w.clock.t = st["next"]
    w.refresh("alerts")
    assert st["next"] - w.clock.t == REFUSED_S and st["error"].startswith("refused")
    del fake.fail["alerts"]
    w.clock.t = st["next"]
    assert w.refresh("alerts") and st["error"] == "" and st["fails"] == 0 and st["next"] == w.clock.t + EVERY_S["alerts"]
    assert "Timed out" in w.status["conditions"]["error"] or "timed out" in w.status["conditions"]["error"]
    fake.fail["conditions"] = RuntimeError("surprise")
    assert not w.refresh("conditions")                                  # never raises
    assert any("weather: alerts: HTTP 503" in m for m in logs)


def test_a_bad_answer_keeps_what_was_read(tmp_path):
    w, fake = make(tmp_path)
    assert w.refresh("alerts") and len(w.alerts) == 6
    fake.alerts = "coops_error.json"                                     # not an alerts collection
    w.clock.t += EVERY_S["alerts"]
    assert not w.refresh("alerts") and len(w.alerts) == 6


def test_a_restart_spends_no_calls(tmp_path):
    w, fake = make(tmp_path)
    for f in list(w.due()):
        w.refresh(f)
    doc = json.loads((tmp_path / "weather.json").read_text())
    assert doc["signature"][0] == ["NYZ071", "NYC119", "NYZ211"] and len(doc["alerts"]) == 6
    w2, fake2 = make(tmp_path, clock=Clock(NOW + 30))
    assert w2.due() == [] and fake2.calls == []
    assert w2.public()["conditions"]["short"] == "75°F · W 10 G 18 mph" and w2.public()["tides"]["trend"] == "rising"
    w3, _ = make(tmp_path, station="KLGA")                               # another station: fetched afresh
    assert w3.due() and w3.conditions is None


def test_start_and_stop_with_fixtures(tmp_path):
    w, fake = make(tmp_path)
    w.start()
    try:
        import time
        end = time.time() + 5
        while time.time() < end and not all(w.status[f]["at"] for f in wx.FEEDS):
            time.sleep(0.02)
        assert all(w.status[f]["at"] for f in wx.FEEDS)
    finally:
        w.stop()
    off, _ = make(enabled=False)
    assert off.start()._thread is None and off.due() == [] and not off.refresh("alerts")


# ---- what the pages get ------------------------------------------------------------------------------------------


def test_the_page_document():
    w, _ = make(fake=FakeNoaa(alerts="alerts_mixed.json"))
    for f in wx.FEEDS:
        w.refresh(f)
    code, body, ctype, h = w.get("/api/cad/weather", "")
    d = json.loads(body)
    assert code == 200 and ctype == "application/json" and h["Cache-Control"] == "no-store"
    assert [a["event"] for a in d["alerts"]] == ["Flash Flood Warning", "Red Flag Warning", "Flood Watch", "Wind Advisory",
                                                 "Coastal Flood Statement"]
    assert [a["event"] for a in d["marine_alerts"]] == ["Special Weather Statement"]
    assert d["banner"]["tone"] == "err" and d["banner"]["count"] == 5
    assert d["conditions"]["short"] == "75°F · W 10 G 18 mph" and d["conditions"]["name"] == "Westchester County Airport"
    assert not d["conditions"]["stale"] and d["tides"]["gauge"]["name"] == "Kings Point"
    assert d["radar"]["on"] and d["radar"]["tiles"] == "/api/cad/weather/radar/{frame}/{z}/{x}/{y}.png"
    assert "NOAA" in d["radar"]["attribution"] and d["speak"] is False
    assert {s["key"] for s in d["sources"]} == {"nws", "coops"} and all(s["at"] for s in d["sources"])
    assert "api.weather.gov" not in body.decode() and "datagetter" not in body.decode()     # the feeds' addresses stay here
    w.clock.t += 3 * 3600
    assert w.public()["conditions"]["stale"]                             # three hours old: shown as old
    assert w.get("/api/cad/weather/nope", "")[0] == 404 and not w.handles("/api/cad/weathers")


def test_a_warning_is_spoken_only_when_settings_allow_and_only_once():
    w, _ = make()
    w.refresh("alerts")
    said = []
    w.speaker = lambda text: (said.append(text), ("/api/cad/audio/abc.wav", "Voice: Polly Matthew"))[1]
    ff = next(a for a in w.alerts if a["event"] == "Flash Flood Warning")
    code, body, _, _ = w.get("/api/cad/weather/speech", f"id={ff['id']}")
    assert code == 403 and "Settings" in json.loads(body)["error"] and said == []      # off by default
    w.speak_on = lambda: True
    assert w.public()["speak"] is True
    for _ in range(3):
        code, body, _, _ = w.get("/api/cad/weather/speech", f"id={ff['id']}")
        d = json.loads(body)
        assert code == 200 and d["audio_url"] == "/api/cad/audio/abc.wav" and d["text"].startswith("Weather alert.")
    assert len(said) == 1                                                 # rendered once
    watch = next(a for a in w.alerts if a["level"] == "watch")
    assert w.speech(watch["id"])[0] == 404 and w.speech("urn:nope")[0] == 404          # warnings only
    w.speak_on = lambda: (_ for _ in ()).throw(RuntimeError("db gone"))
    assert w.speech(ff["id"])[0] == 403                                   # a setting that can't be read: off


# ---- radar -------------------------------------------------------------------------------------------------------


def test_radar_tiles_through_the_hub_cached():
    w, fake = make()
    w0, s0, e0, n0 = tile_bbox(0, 0, 0)
    assert round(w0) == -20037508 and round(n0) == 20037508
    q = {k: v[0] for k, v in parse_qs(urlsplit(radar_url(8, 75, 95)).query).items()}
    assert q["request"] == "GetMap" and q["layers"] == "conus_bref_qcd" and q["crs"] == "EPSG:3857" and "time" not in q
    code, body, ctype, h = w.get("/api/cad/weather/radar/now/8/75/95.png", "")
    assert code == 200 and ctype == "image/png" and body.startswith(b"\x89PNG") and "max-age=120" in h["Cache-Control"]
    assert w.get("/api/cad/weather/radar/now/8/75/95.png", "")[0] == 200 and len(fake.calls) == 1     # a second viewer: no call
    w.clock.t += 121
    assert w.get("/api/cad/weather/radar/now/8/75/95.png", "")[0] == 200 and len(fake.calls) == 2     # the next composite
    assert w.get("/api/cad/weather/radar/now/2/1/1.png", "")[0] == 404                 # too far out
    assert w.get("/api/cad/weather/radar/now/11/600/760.png", "")[0] == 404            # the maps overzoom past 10
    assert w.get("/api/cad/weather/radar/now/8/256/1.png", "")[0] == 404
    assert w.get("/api/cad/weather/radar/20261014T153613Z/8/75/95.png", "")[0] == 404  # not a frame we listed
    fake.fail["tile"] = 500
    w.clock.t += 121
    code, _, _, h = w.get("/api/cad/weather/radar/now/8/75/95.png", "")
    assert code == 200 and h.get("X-Radar-Stale") == "1"                 # the last one, rather than a hole
    assert w.get("/api/cad/weather/radar/now/8/76/95.png", "")[0] == 502


def test_a_wms_error_is_not_a_tile():
    w, _ = make()
    w.fetch = lambda url, t, n: (200, {"content-type": "text/xml"}, b"<ServiceExceptionReport/>")   # WMS errors answer 200
    assert w.get("/api/cad/weather/radar/now/8/75/95.png", "")[0] == 502


def test_radar_frames_for_the_animation():
    fr = parse_frames(raw("radar_capabilities.xml"))
    assert len(fr) == 7 and fr[-1]["id"] == "20261014T153613Z"
    gaps = [b["t"] - a["t"] for a, b in zip(fr, fr[1:])]
    assert all(500 <= g <= 700 for g in gaps)                          # about every 10 min over the last hour
    w, fake = make()
    d = json.loads(w.get("/api/cad/weather/radar/frames", "")[1])
    assert [f["id"] for f in d["frames"]] == [f["id"] for f in fr] and d["error"] == ""
    json.loads(w.get("/api/cad/weather/radar/frames", "")[1])
    assert sum(1 for c in fake.calls if "GetCapabilities" in c) == 1   # at most every 5 min
    code, _, _, _ = w.get(f"/api/cad/weather/radar/{fr[0]['id']}/8/75/95.png", "")
    assert code == 200 and "time=2026-10-14T14%3A36%3A10.000Z" in fake.calls[-1]
    with pytest.raises(ValueError):
        parse_frames(b"<WMS_Capabilities/>")
    off, _ = make(radar=False)
    assert off.get("/api/cad/weather/radar/now/8/75/95.png", "")[0] == 404 and off.radar_frames()["frames"] == []
    assert off.public()["radar"]["on"] is False


# ---- the hub, the setting, the config ------------------------------------------------------------------------------


def test_the_hub_serves_weather_and_the_setting_gates_the_voice(tmp_path):
    h = HubRun(hub_cfg(tmp_path))
    try:
        w = h.app.weather
        assert w._thread is None                                         # tests: never the real NOAA
        w.fetch = FakeNoaa()
        for f in wx.FEEDS:
            assert w.refresh(f)
        d = h.get("/api/cad/weather")
        assert d["banner"]["tone"] == "err" and d["speak"] is False and d["conditions"]["temp_f"] == 75
        ff = d["alerts"][0]
        from tests.test_cloud import call
        assert call(h.url + "/api/cad/weather/speech?id=" + ff["id"], token=None)[0] == 403
        assert post(h.app.cad, "dispatch", action="set", key="weather_speak", value=True) == {"ok": True}
        assert h.get("/api/cad/weather")["speak"] is True
        code, said = call(h.url + "/api/cad/weather/speech?id=" + ff["id"], token=None)
        assert code == 200 and said["text"].startswith("Weather alert.")    # (no voice on this test hub: the text stands)
        assert (tmp_path / "hub" / "weather.json").is_file()
    finally:
        h.close()


def test_the_setting():
    cad = make_cad()
    d = cad.settings_get()["dispatch"]["weather_speak"]
    assert d == {"value": False, "default": False, "edited": False} and not getattr(cad, "weather_speak", False)
    with pytest.raises(Exception, match="true or false"):
        post(cad, "dispatch", action="set", key="weather_speak", value="yes")
    post(cad, "dispatch", action="set", key="weather_speak", value=True)
    assert cad.weather_speak is True and cad.settings_get()["dispatch"]["weather_speak"]["edited"]
    assert any(r["key"] == "weather_speak" for r in cad.settings.audit_log("dispatch"))
    post(cad, "dispatch", action="reset", key="weather_speak")
    assert cad.weather_speak is False and not cad.settings_get()["dispatch"]["weather_speak"]["edited"]
    post(cad, "dispatch", action="set", key="weather_speak", value=True)
    post(cad, "dispatch", action="reset_section")
    assert cad.weather_speak is False


def load_cfg(tmp_path, text: str):
    p = tmp_path / "config.toml"
    p.write_text(text)
    return load(p)


def test_config(tmp_path):
    c = load_cfg(tmp_path, "")
    assert c.weather_enabled and c.weather_radar and c.weather_zones == ("NYZ071", "NYC119", "NYZ211")
    assert c.weather_marine_zones == ("ANZ335",) and c.weather_station == "KHPN"
    assert c.weather_tide_station == ("8518091", "Rye Beach") and c.weather_gauge_station == ("8516945", "Kings Point")
    c = load_cfg(tmp_path, '[weather]\nenabled = false\nzones = ["NYZ072"]\nmarine_zones = []\nstation = "klga"\n'
                           'tide_station = "8516945"\ntide_name = "Kings Point"\n')
    assert not c.weather_enabled and c.weather_zones == ("NYZ072",) and c.weather_marine_zones == () and c.weather_station == "KLGA"
    assert c.weather_tide_station == ("8516945", "Kings Point")
    for bad in ('enabled = "yes"', 'zones = ["Westchester"]', "zones = []", 'station = "K HPN"', 'tide_station = "rye"',
                'nope = 1'):
        with pytest.raises(ConfigError, match="weather"):
            load_cfg(tmp_path, f"[weather]\n{bad}\n")


# ---- the page (static) ---------------------------------------------------------------------------------------------


def test_the_page_loads_the_weather_and_has_its_places():
    html = (STATIC / "cad.html").read_text()
    assert '<script src="/static/weather.js"></script>' in html
    assert html.index("/static/maplayers.js") < html.index("/static/weather.js") < html.index("/static/cad.js")
    for hook in ('id="alert-banners"', 'id="wx-banner"', 'id="wx-now"', 'id="d-tides"', 'id="b-marine"'):
        assert hook in html, hook
    # the banner sits with the other alert banners under the header, before the columns
    assert html.index('id="role-banner"') < html.index('id="alert-banners"') < html.index("<main")
    assert html.index('id="wx-now"') < html.index('id="clock"')


def test_the_script_only_talks_to_the_hub_and_says_its_sources():
    js = (STATIC / "weather.js").read_text()
    assert not re.search(r"https?://(?:api\.weather\.gov|api\.tidesandcurrents|opengeo\.ncep)", js)   # never NOAA directly
    for path in ("/api/cad/weather", "/api/cad/weather/speech", "/api/cad/weather/radar/frames"):
        assert f'"{path}' in js or f"`{path}" in js, path
    assert "MapLayers.addGroup" in js and 'const RADAR = "wx:radar"' in js     # the Layers box: radar, off by default
    assert "keys() { return [[RADAR, false]]; }" in js and "this.control.on(RADAR, false)" in js
    assert "localStorage" in js and "sizeup.wxSpoken" in js             # spoken once per alert, remembered
    assert "Transmit" not in js or "never keyed" in js
    for level in ("warning", "watch", "advisory", "statement"):
        assert level in js
    for marine in ('"RESCUE"', '"WATER"', '"ACFT-WATER"', '"BOAT"', '"marine"'):
        assert marine in js, marine                                       # the marine calls the tides show on


def test_the_setting_is_on_the_dispatch_page():
    js = (STATIC / "settings.js").read_text()
    assert 'key: "weather_speak"' in js and "d.weather_speak" in js
