"""Settings > Data sources (GitHub #186, sizeup/datasources.py): every data pull in one registry with its status,
Refresh now (admins, rate-limited, audited) and, for the slow ones, a schedule the admins pick (kept in the CAD
settings), with jitter, backoff and the quiet hours. No network: the providers get the recorded fixtures."""
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from sizeup import datasources as ds
from sizeup.cad.api import CadApi, needs
from sizeup.cloud.access import Identity
from sizeup.datasources import CHOICES, HELD, DataSources, Source, TooSoon
from tests.test_cad import make_cad
from tests.test_roles import COREY, JULIAN, VAL, call, people
from tests.test_settings import post

T0 = 1_791_987_600.0                     # 2026-10-14 10:20 EDT (a weekday morning, out of the quiet hours)
STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"


class Clock:
    def __init__(self, t=T0):
        self.t = float(t)

    def __call__(self):
        return self.t


class Pull:
    """A provider as the registry sees one: its status and its fetch (counted), failing on demand."""

    def __init__(self, clock, n=3):
        self.clock, self.n = clock, n
        self.st = {"at": None, "error": "", "next": 0.0}
        self.calls, self.fail = 0, ""

    def status(self):
        return {"at": self.st["at"], "error": self.st["error"], "n": self.n, "bytes": 2048}

    def refresh(self):
        self.calls += 1
        if self.fail:
            self.st["error"] = self.fail
            return False
        self.st.update(at=self.clock(), error="")
        return True


def registry(clock=None, choices=None, rng=lambda: 0.5, **kw):
    clock = clock or Clock()
    logs = []
    r = DataSources(lambda: dict(choices or {}), log=logs.append, clock=clock, rng=rng, spawn=lambda fn, name: fn(), **kw)
    r.logs, r.t = logs, clock
    return r


def managed(r, key="layer", every="daily", quiet=False, **kw):
    p = Pull(r.t)
    r.register(Source(key, key.title(), "Map layers", "County", status=p.status, refresh=p.refresh, every=every,
                      hold=lambda: p.st.__setitem__("next", HELD), quiet=quiet, **kw))
    return p


# ---- the registry -------------------------------------------------------------------------------------------------


def test_a_source_registers_once_with_a_key_a_group_and_a_known_schedule():
    r = registry()
    managed(r)
    with pytest.raises(ValueError):
        managed(r)                                                     # twice
    for bad in (dict(key="Bad Key"), dict(key="x", group="Elsewhere"), dict(key="y", every="fortnightly")):
        with pytest.raises(ValueError):
            r.register(Source(**{"name": "X", "group": "Map layers", **bad}))
    pub = r.public()
    assert pub["groups"] == ["Map layers", "Cameras", "Transit", "Weather", "Aircraft", "Radio"]
    assert pub["choices"] == ["off", "hourly", "daily", "weekly", "monthly"]
    row = pub["sources"][0]
    assert {k: row[k] for k in ("key", "name", "group", "source", "schedule", "every", "default_every", "refreshable",
                                "running", "ok", "items", "bytes", "last_fetched")} == {
        "key": "layer", "name": "Layer", "group": "Map layers", "source": "County", "schedule": "managed", "every": "daily",
        "default_every": "daily", "refreshable": True, "running": False, "ok": True, "items": 3, "bytes": 2048, "last_fetched": None}


def test_a_managed_source_holds_the_providers_own_timer_and_takes_it_back_after_each_run():
    r = registry()
    p = managed(r)
    assert p.st["next"] == HELD                                        # the provider's own loop never fetches it now
    p.st["next"] = 0.0                                                 # its refresh resets that ...
    r.refresh("layer")
    assert p.calls == 1 and p.st["next"] == HELD                       # ... and the registry holds it again


def test_the_rev_changes_only_when_something_shown_changes():
    r = registry()
    p = managed(r)
    a = r.public()["rev"]
    assert r.public()["rev"] == a
    r.refresh("layer")
    assert r.public()["rev"] != a and p.calls == 1


def test_a_status_that_raises_is_that_rows_error_not_the_pages():
    r = registry()
    r.register(Source("broken", "Broken", "Radio", status=lambda: 1 / 0))
    row = r.public()["sources"][0]
    assert not row["ok"] and "ZeroDivisionError" in row["error"] and not row["refreshable"]


# ---- the schedule -------------------------------------------------------------------------------------------------


def test_never_fetched_runs_at_once_one_at_a_time_then_on_its_interval_with_jitter():
    r = registry()
    order = []
    hourly, daily = managed(r, "zeta", every="hourly"), managed(r, "alpha")
    for key, p in (("zeta", hourly), ("alpha", daily)):
        r.sources[key].refresh = lambda key=key, f=p.refresh: order.append(key) or f()
    assert r.next_due("alpha") == r.next_due("zeta") == T0 and r.soonest() == T0
    assert r.due() == ["zeta", "alpha"]                                # registration order, not the key's
    assert r.tick() == 2 and hourly.calls == daily.calls == 1 and order == ["zeta", "alpha"]
    at = r.t()
    assert r.due() == []
    # next = last fetched + interval + jitter (rng 0.5 -> half of JITTER of the interval, at most JITTER_MAX_S)
    assert r.next_due("zeta") == at + 3600 + 0.5 * 3600 * ds.JITTER
    assert r.next_due("alpha") == at + 86400 + 0.5 * min(86400 * ds.JITTER, ds.JITTER_MAX_S)
    assert r.soonest() == r.next_due("zeta")
    r.t.t = r.next_due("zeta") + 1
    assert r.due() == ["zeta"]


def test_jitter_spreads_the_same_interval_and_stays_bounded():
    vals = iter([0.0, 0.99, 0.5, 0.0, 0.99, 0.5])
    r = registry(rng=lambda: next(vals))
    ps = [managed(r, k, every="monthly") for k in ("a", "b", "c")]
    for p in ps:
        p.st["at"] = T0
    dues = [r.next_due(k) - T0 - CHOICES["monthly"] for k in ("a", "b", "c")]
    assert len(set(dues)) == 3 and all(0 <= d <= ds.JITTER_MAX_S for d in dues)


def test_a_failure_backs_off_doubling_never_past_the_interval_and_shows_its_error():
    r = registry()
    p = managed(r, every="hourly")
    p.fail = "HTTP 503"
    r.tick()
    row = r.row("layer")
    assert not row["ok"] and row["error"] == "HTTP 503" and row["fails"] == 1
    waits = []
    for _ in range(4):
        nd = r.next_due("layer")
        waits.append(nd - r.t())
        assert r.due() == []                                          # not every tick
        r.t.t = nd
        r.tick()
    assert waits == [300, 600, 1200, 2400]
    r.t.t = r.next_due("layer")
    r.tick()
    assert r.next_due("layer") - r.t() == 3600                        # capped at its interval (hourly)
    p.fail = ""
    r.t.t = r.next_due("layer")
    r.tick()
    row = r.row("layer")
    assert row["ok"] and row["fails"] == 0 and r.next_due("layer") == p.st["at"] + 3600 + 0.5 * 3600 * ds.JITTER
    assert any("HTTP 503" in m for m in r.logs)


def test_heavy_pulls_wait_for_the_quiet_hours_once_they_have_data():
    r = registry(rng=lambda: 0.0)
    p = managed(r, "faa", every="weekly", quiet=True)
    assert r.next_due("faa") == T0                                     # no data yet: at once
    r.tick()
    r.t.t += 7 * 86400 + 60                                            # a week on, mid-morning
    nd = r.next_due("faa")
    d = datetime.fromtimestamp(nd, ds.TZ)
    assert ds.QUIET_HOURS[0] <= d.hour < ds.QUIET_HOURS[1] and nd > r.t() and nd - r.t() < 86400
    assert r.due() == []
    r.t.t = nd
    assert r.due() == ["faa"] and r.tick() == 1 and p.calls == 2
    r.choices = lambda: {"faa": "hourly"}                              # hourly: not held for the night
    assert r.next_due("faa") == r.t() + 3600 and not r.row("faa")["quiet"]
    # the quiet hours are local time: 01:00 EDT is 05:00 UTC
    assert datetime.fromtimestamp(r.quiet_at(datetime(2026, 10, 14, 12, tzinfo=ds.TZ).timestamp()), ds.TZ).hour == 1


def test_the_admins_choice_changes_the_interval_and_off_stops_it():
    choices = {}
    r = registry(choices=choices)
    r.choices = lambda: choices
    p = managed(r, every="monthly")
    r.tick()
    assert r.next_due("layer") > r.t() + 29 * 86400
    choices["layer"] = "hourly"
    assert r.row("layer")["every"] == "hourly" and r.next_due("layer") < r.t() + 2 * 3600
    choices["layer"] = "off"
    assert r.next_due("layer") is None and r.row("layer")["next_due"] is None
    r.t.t += 365 * 86400
    assert r.tick() == 0 and p.calls == 1
    choices["layer"] = "nonsense"                                     # a bad value: the default
    assert r.choice("layer") == "monthly"


def test_a_built_in_schedule_is_shown_from_the_provider_and_never_run_by_the_registry():
    r = registry()
    calls = []
    st = {"at": T0 - 30, "next": T0 + 90, "error": ""}
    r.register(Source("weather:alerts", "NWS alerts", "Weather", status=lambda: st, refresh=lambda: calls.append(1) or True,
                      builtin="every 2 min"))
    row = r.row("weather:alerts")
    assert row["schedule"] == "builtin" and row["builtin"] == "every 2 min" and row["next_due"] == T0 + 90
    r.t.t += 86400
    assert r.tick() == 0 and not calls                                 # the provider's own loop runs it
    st["next"] = 0.0
    assert r.row("weather:alerts")["next_due"] == r.t()               # its 0: due now


# ---- Refresh now: in the background, one at a time, rate-limited -------------------------------------------------


def test_refresh_now_runs_off_the_request_and_resets_the_schedule():
    started = []
    r = registry()
    r.spawn = lambda fn, name: started.append((fn, name))
    p = managed(r, every="daily")
    r.tick()
    r.t.t += 3 * 3600
    row = r.refresh("layer", by="Corey <corey@example.org>")
    assert row["running"] and p.calls == 1 and started[0][1] == "datasource-layer"
    with pytest.raises(TooSoon, match="refreshing now"):
        r.refresh("layer")                                             # never two at once
    assert r.due(r.t() + 10 * 86400) == []                             # nor the schedule while it runs
    started[0][0]()
    assert p.calls == 2 and not r.row("layer")["running"]
    assert r.next_due("layer") >= r.t() + 86400                        # the schedule starts again from now
    assert r.row("layer")["last_run"]["by"] == "Corey <corey@example.org>"


def test_at_most_one_manual_refresh_per_source_per_gap_longer_for_heavy_ones():
    r = registry()
    managed(r, "a")
    managed(r, "faa", every="weekly", min_gap_s=ds.HEAVY_GAP_S)
    r.refresh("a")
    r.refresh("faa")
    r.t.t += 60
    with pytest.raises(TooSoon, match=r"once every 2 min at most"):
        r.refresh("a")
    r.t.t += 61
    r.refresh("a")                                                     # 2 minutes on: again
    r.t.t += 1800
    with pytest.raises(TooSoon, match=r"once every 1 h at most"):
        r.refresh("faa")
    r.t.t += 1800
    r.refresh("faa")


def test_a_source_resting_after_a_refusal_or_a_rate_limit_is_not_asked_again():
    r = registry()
    st = {"at": None, "error": "Thruway rate limit (HTTP 429)", "next": T0 + 300}
    r.register(Source("roads:thruway:events", "Thruway events", "Cameras", status=lambda: st, refresh=lambda: True,
                      builtin="every 2 min", blocked=lambda: ds._resting(st, r.t)))
    with pytest.raises(TooSoon, match="resting after Thruway rate limit"):
        r.refresh("roads:thruway:events")
    r.t.t += 301
    r.refresh("roads:thruway:events")


def test_not_refreshable_and_unknown():
    r = registry()
    r.register(Source("gazetteer", "Gazetteer", "Map layers", status=lambda: {"at": T0}, note="built offline"))
    with pytest.raises(TooSoon, match="built offline"):
        r.refresh("gazetteer")
    with pytest.raises(KeyError):
        r.refresh("nope")


def test_a_refresh_that_raises_is_an_error_on_its_row():
    r = registry()
    r.register(Source("x", "X", "Radio", refresh=lambda: 1 / 0, every="daily"))
    r.refresh("x")
    row = r.row("x")
    assert not row["running"] and row["last_run"]["ok"] is False and "ZeroDivisionError" in row["error"]


# ---- the adapters: each one calls its provider's own refresh path (fixtures, no network) ----------------------------


def test_gis_layers_refresh_through_gis_refresh_on_their_own_cadence(tmp_path):
    from tests.test_gis import FakeFetch, make
    fetch = FakeFetch(keys=["hydrants", "fire_districts"])
    from tests.test_gis import BY_KEY
    g = make(tmp_path, layers=[BY_KEY["hydrants"], BY_KEY["fire_districts"]], fetch=fetch)
    r = registry(clock=Clock(g.clock()))
    for s in ds.gis_sources(g):
        r.register(s)
    assert g.due() == []                                               # Gis's own loop holds off
    row = r.row("gis:hydrants")
    assert row["every"] == "monthly" and row["quiet"] and row["group"] == "Map layers" and row["source"] == "Westchester County GIS"
    r.refresh("gis:hydrants")
    assert any("MapServer/147" in u for u in fetch.urls) and not any("MapServer/153" in u for u in fetch.urls)
    row = r.row("gis:hydrants")
    assert row["ok"] and row["items"] == g.status["hydrants"]["n"] > 0 and row["bytes"] and row["last_fetched"] == g.clock()
    assert (tmp_path / "gis" / "hydrants.geojson").is_file() and g.due() == []


def test_camera_lists_are_on_the_registry_and_the_live_feeds_keep_their_own(tmp_path):
    from tests.test_roads import TURLS, FakeFetch, make
    fetch = FakeFetch()
    roads = make(tmp_path, key="", urls={}, fetch=fetch, thruway=True)
    r = registry(clock=Clock(roads.clock()))
    for s in ds.roads_sources(roads):
        r.register(s)
    keys = [s["key"] for s in r.public()["sources"]]
    assert keys == ["roads:thruway:cameras", "roads:thruway:events", "roads:thruway:signs"]   # 511NY not configured: no rows
    assert r.row("roads:thruway:cameras")["every"] == "daily" and roads.thruway.status["cameras"]["next"] == HELD
    assert r.row("roads:thruway:events")["builtin"] == "every 2 min" and roads.thruway.status["events"]["next"] != HELD
    roads._swept = 123.0
    r.refresh("roads:thruway:cameras")
    assert fetch.urls == [TURLS["cameras"]] and roads._swept == 0.0 and r.row("roads:thruway:cameras")["items"] == 15
    r.refresh("roads:thruway:events")
    assert fetch.urls[-1] == TURLS["events"] and r.row("roads:thruway:events")["ok"]


def test_metro_north_static_daily_overnight_and_the_live_feed_by_hand(tmp_path):
    from tests.test_trains import GTFS_URL, RT_URL, provider
    tr, clock, fetch = provider(tmp_path)
    r = registry(clock=clock)
    for s in ds.trains_sources(tr):
        r.register(s)
    tr.tick()
    assert fetch.n(GTFS_URL) == 0                                      # the provider's own loop leaves it to the registry
    assert r.row("trains:static")["every"] == "daily" and r.row("trains:static")["quiet"]
    r.tick()
    assert fetch.n(GTFS_URL) == 1 and tr.model is not None and r.row("trains:static")["items"] == len(tr.model.area_stations)
    r.refresh("trains:rt")
    assert fetch.n(RT_URL) == 1 and r.row("trains:rt")["ok"]


def test_weather_feeds_and_the_radar_cache(tmp_path):
    from tests.test_weather import make
    w, fake = make(tmp_path)
    r = registry(clock=Clock(w.clock()))
    for s in ds.weather_sources(w):
        r.register(s)
    assert [s["key"] for s in r.public()["sources"]] == ["weather:alerts", "weather:conditions", "weather:tides",
                                                         "weather:predictions", "weather:radar"]
    r.refresh("weather:alerts")
    assert [fake.kind(u) for u in fake.calls] == ["alerts"] and r.row("weather:alerts")["items"] == len(w.alerts) > 0
    w.radar_tile("now", 8, 75, 95)
    assert len(w._tiles) == 1
    r.refresh("weather:radar")
    assert len(w._tiles) == 0 and fake.kind(fake.calls[-1]) == "radar" and r.row("weather:radar")["ok"]


def test_the_faa_registry_weekly_overnight_at_most_hourly_by_hand(tmp_path):
    from tests.test_aircraft import FAA_URL, layer
    ly = layer(cache_dir=tmp_path)
    r = registry(clock=Clock(ly.clock()))
    for s in ds.aircraft_sources(ly):
        r.register(s)
    ly.ask()
    ly.tick()                                                          # someone looks: the layer no longer starts it itself
    assert FAA_URL not in ly.fetch.calls and ly.registry.st["next"] == HELD
    row = r.row("aircraft:registry")
    assert row["every"] == "weekly" and row["quiet"] and row["min_gap_s"] == 3600
    r.refresh("aircraft:registry")
    assert ly.fetch.calls.count(FAA_URL) == 1 and ly.registry.lookup("a19e97")["n"] == "N203QS"
    row = r.row("aircraft:registry")
    assert row["ok"] and row["items"] > 0 and row["bytes"] > 0
    with pytest.raises(TooSoon):
        r.refresh("aircraft:registry")
    r.refresh("aircraft:live")
    assert any("lol.example" in u for u in ly.fetch.calls)


def test_the_airport_layer_reloads_from_its_file(monkeypatch, tmp_path):
    from sizeup.cad import airport
    f = tmp_path / "khpn_airport.geojson"
    f.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature"}]}))
    monkeypatch.setattr(airport, "LAYER_FILE", f)
    monkeypatch.setattr(airport, "_cache", {"features": []})
    r = registry()
    r.register(ds.airport_source())
    assert r.row("airport")["every"] == "off" and r.row("airport")["action"] == "Reload"
    r.refresh("airport")
    assert airport.layer()["features"] == [{"type": "Feature"}] and r.row("airport")["items"] == 1


def test_the_tone_list_reloads_its_file_and_keeps_the_configs_own(tmp_path):
    from sizeup.config import read_tones_file
    from sizeup.radio.tones import ToneCode
    f = tmp_path / "tones.csv"
    f.write_text("# a list\nname,a_hz,b_hz,estimated,source,fetched\nRye FD,600.9,1092.4,0,wiki,2026-10-01\n")
    have = [*read_tones_file(f), ToneCode("Ours", 500.0, 600.0, source="config")]
    applied = []

    def reload():
        codes = read_tones_file(f) + [c for c in have if c.source == "config"]
        applied.append([c.name for c in codes])
        return True
    r = registry()
    r.register(ds.tones_source(f, lambda: have, reload))
    row = r.row("tones")
    assert row["items"] == 1 and row["group"] == "Radio" and row["last_fetched"] == datetime(2026, 10, 1, tzinfo=ds.TZ).timestamp()
    f.write_text(f.read_text() + "Harrison FD,600.9,1122.5,1,wiki,2026-10-02\n")
    r.refresh("tones")
    assert applied == [["Rye FD", "Harrison FD (est.)", "Ours"]]


def test_build_registers_every_provider_of_the_app_and_one_failing_never_stops_the_rest(tmp_path):
    from types import SimpleNamespace

    from tests.test_gis import make as gis
    from tests.test_roads import make as roads
    from tests.test_trains import provider
    from tests.test_weather import make as weather
    from tests.test_aircraft import layer
    tr = provider(tmp_path)[0]
    app = SimpleNamespace(gis=gis(tmp_path), roads=roads(tmp_path, thruway=True), trains=tr, weather=weather(tmp_path)[0],
                          aircraft=layer(cache_dir=tmp_path), gaz=None)
    logs = []
    r = ds.build(app, log=logs.append)
    groups = {s["group"] for s in r.public()["sources"]}
    assert groups == {"Map layers", "Cameras", "Transit", "Weather", "Aircraft"}
    app.tone_list_source = lambda: 1 / 0
    r2 = ds.build(app, log=logs.append)
    assert len(r2.sources) == len(r.sources) and any("not registered" in m for m in logs)


# ---- the API: who may read, who may refresh, the rate limit's answer, the audit ---------------------------------------


@pytest.fixture
def api(tmp_path):
    a = CadApi(make_cad(tmp_path))
    a.cad.log = lambda m: None
    a.ids = people(a.cad)
    r = registry(clock=Clock())
    r.choices = lambda: a.cad.settings.overrides().get(ds.SECTION) or {}
    a.pull = managed(r, "gis:hydrants", every="monthly")
    a.cad.datasources = r
    return a


def get(api, email=None, q=""):
    code, raw, _ = api.get("/api/cad/datasources", q, Identity(email=email) if email else None)
    return code, json.loads(raw)


def test_admins_and_dispatchers_read_viewers_dont(api):
    for who in (COREY, JULIAN):
        code, out = get(api, who)
        assert code == 200 and out["configured"] and out["sources"][0]["key"] == "gis:hydrants"
    code, out = get(api, VAL)
    assert code == 403 and out["error"] == "Admins and dispatchers only"
    code, out = get(api, COREY, f"rev={get(api, COREY)[1]['rev']}")
    assert out == {"rev": out["rev"], "same": True}                    # nothing changed: the short answer


def test_only_admins_refresh_with_a_clear_answer_when_too_soon_and_an_audit_entry(api):
    path = "/api/cad/datasources/gis%3Ahydrants/refresh"
    assert needs(path) == needs("/api/cad/datasources/aircraft:registry/refresh") == "admin"
    for who in (JULIAN, VAL):
        code, out = call(api, path, {}, who)
        assert code == 403 and api.pull.calls == 0
    code, out = call(api, path, {}, COREY)
    assert code == 200 and out["ok"] and out["source"]["key"] == "gis:hydrants" and api.pull.calls == 1
    code, out = call(api, path, {}, COREY)
    assert code == 429 and re.search(r"refreshed \d+ s ago: once every 2 min at most", out["error"]) and api.pull.calls == 1
    acts = api.cad.activity.rows(COREY, "", None, 20)
    assert any(a["summary"] == "Refreshed data source: Gis:Hydrants" for a in acts)
    assert any(a["kind"] == "refused" and "once every 2 min" in a["summary"] for a in acts)
    audit = api.cad.settings_audit_log({"section": ["system"]})["audit"]
    row = next(a for a in audit if a["key"] == "datasources/gis:hydrants")
    assert "Corey" in row["operator"] and row["new"] == "Refresh now" and "never" in row["old"]
    code, out = call(api, "/api/cad/datasources/nope/refresh", {}, COREY)
    assert code == 400 and "no data source" in out["error"]


def test_without_a_registry_the_page_says_so(tmp_path):
    a = CadApi(make_cad(tmp_path))
    code, raw, _ = a.get("/api/cad/datasources", "", None)
    assert code == 200 and json.loads(raw)["configured"] is False
    code, raw, _ = a.post("/api/cad/datasources/x/refresh", b"{}", None)
    assert code == 400


# ---- the interval, persisted in the CAD settings (audited, exported, imported) ---------------------------------------


def test_the_interval_is_a_setting_that_survives_a_restart_and_is_audited(tmp_path):
    cad = make_cad(tmp_path)
    r = registry(choices=None)
    r.choices = lambda: cad.settings.overrides().get(ds.SECTION) or {}
    managed(r, "gis:hydrants", every="monthly")
    r.register(Source("weather:alerts", "NWS alerts", "Weather", refresh=lambda: True, builtin="every 2 min"))
    cad.datasources = r
    post(cad, "datasources", action="set", key="gis:hydrants", value="weekly")
    assert r.row("gis:hydrants")["every"] == "weekly"
    again = make_cad(tmp_path)                                         # a restart: the same database
    assert again.settings.overrides()[ds.SECTION] == {"gis:hydrants": "weekly"}
    rows = cad.settings_audit_log({"section": ["system"]})["audit"]
    assert any(a["key"] == "datasources/gis:hydrants" and a["old"] == "monthly" and a["new"] == "weekly"
               for a in rows)
    post(cad, "datasources", action="set", key="gis:hydrants", value="monthly")     # back to the default: nothing kept
    assert ds.SECTION not in cad.settings.overrides()
    for bad, why in ((dict(key="gis:hydrants", value="fortnightly"), "value"), (dict(key="nope", value="daily"), "no data source"),
                     (dict(key="weather:alerts", value="daily"), "own schedule")):
        with pytest.raises(Exception, match=why):
            post(cad, "datasources", action="set", **bad)
    post(cad, "datasources", action="set", key="gis:hydrants", value="off")
    exp = cad.settings_export()
    assert exp["overrides"][ds.SECTION] == {"gis:hydrants": "off"}
    post(cad, "datasources", action="reset")
    assert r.row("gis:hydrants")["every"] == "monthly"
    out = post(cad, "import", action="dry_run", settings=exp)
    assert any(c["store"] == ds.SECTION and c["key"] == "gis:hydrants" for c in out["changes"])
    exp["overrides"][ds.SECTION] = {"gis:hydrants": "fortnightly"}
    with pytest.raises(Exception, match="overrides.datasources"):
        post(cad, "import", action="dry_run", settings=exp)


def test_choosing_an_interval_is_admin_only(api):
    code, out = call(api, "/api/cad/settings/datasources", {"action": "set", "key": "gis:hydrants", "value": "daily"}, JULIAN)
    assert code == 403
    code, out = call(api, "/api/cad/settings/datasources", {"action": "set", "key": "gis:hydrants", "value": "daily"}, COREY)
    assert code == 200 and api.cad.datasources.row("gis:hydrants")["every"] == "daily"


# ---- the page --------------------------------------------------------------------------------------------------------


def test_the_panel_is_a_settings_section_that_polls_only_while_open():
    html, js, settings = ((STATIC / f).read_text() for f in ("cad.html", "datasources.js", "settings.js"))
    assert '<section id="s-data" class="s-panel" data-s="data" hidden></section>' in html
    assert '<script src="/static/datasources.js"></script>' in html
    assert '["data", "Data sources"]' in settings and "DataSourcesPanel.mount(panel" in settings
    assert "/api/cad/datasources${q}" in js and "`/api/cad/datasources/${encodeURIComponent(s.key)}/refresh`" in js
    assert "if (!isOpen()) return;" in js and "document.hidden" in js             # no poll while hidden
    assert 'sPost("datasources", { action: "set", key: s.key, value: v }' in js
    assert "localStorage" not in js
