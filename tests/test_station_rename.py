"""Renaming the shared token's station never locks it out (sizeup/cloud/stations.py "Renaming the shared token's
station"; docs/cloud.md): the hub is the source of truth for the id, its former ids stay taken from the shared token,
the station adopts the id the hub answers with, what the hub keys by the id moves with it, a rename while the station
is connected is confirmed first (and audited), and a station refused as another id is a Health alert of its own.

Every id, name and token here is made up. The metrics are the real FG1 capture's (tests/fixtures/fg1)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from sizeup.cad.api import CadApi
from sizeup.cloud import health as health_mod
from sizeup.cloud import stations as st_mod
from sizeup.cloud.access import Identity
from sizeup.cloud.client import HubClient
from sizeup.cloud.hubstore import MonitorDb
from sizeup.cloud.metrics import PATH
from sizeup.cloud.stations import ConfirmNeeded, StationError, StationRegistry
from tests.test_cad import make_cad
from tests.test_cloud import TOKEN, HubRun, call, hub_cfg, station_cfg, wait
from tests.test_roles import CASEY, people
from tests.test_station_identity import SHARED, cli_env, lease, refused, registry
from tests.test_station_metrics import T0, fg1_segment, floors, store


# -- the registry ------------------------------------------------------------------------------------------------------

def test_a_rename_while_the_shared_station_is_live_asks_first_and_never_locks_it_out(tmp_path):
    reg, clock, logs = registry(tmp_path, legacy_id="station-a")
    p = reg.resolve(SHARED)
    reg.touch(p)                                                     # station-a posting on the shared token now
    with pytest.raises(ConfirmNeeded) as e:
        reg.set_shared_id("station-b", "casey")
    assert str(e.value) == ("station-a is connected with this token right now. It becomes station-b: the hub keeps "
                            "taking station-a from it and moves its history. Change anyway?")
    assert reg.legacy_id() == "station-a"                            # refused: nothing changed
    assert reg.set_shared_id("station-b", "casey", confirm=True) == ("station-a", "station-b")
    assert any("(confirmed)" in m for m in logs)
    assert reg.renames() == {"station-a": "station-b"}               # its history moves (the hub's job)
    p = reg.resolve(SHARED)
    assert p.id == "station-b"
    assert reg.claim(p, "station-a") == "station-b"                  # its config still says station-a: taken, as b
    assert reg.claim(p, "station-b") == "station-b" and reg.claim(p, None) == "station-b"
    assert refused(reg.claim, p, "station-c") == "id_mismatch"       # an id it never had: still refused
    assert reg.aliases() == ["station-a"] and reg.panel()["shared"]["aliases"] == ["station-a"]
    again = StationRegistry(reg.mdb, shared=SHARED, legacy_id="station-a")     # a restart: kept, config or not
    assert again.claim(again.resolve(SHARED), "station-a") == "station-b"
    # an own token never takes another id, alias or not
    own = reg.resolve(reg.add("station-d"))
    assert refused(reg.claim, own, "station-a") == "id_mismatch"


def test_not_live_no_question_and_the_aliases_go_once_the_station_is_reconfigured_or_cleared(tmp_path):
    reg, clock, logs = registry(tmp_path, legacy_id="station-a")
    reg.touch(reg.resolve(SHARED))
    clock.t += st_mod.RENAME_LIVE_S + 1                              # last seen 10 min ago: not live
    assert reg.rename_question("station-a", "station-b") is None
    reg.set_shared_id("station-b")
    reg.set_shared_id("station-c")                                   # two renames before it was reconfigured
    assert reg.aliases() == ["station-a", "station-b"]
    assert reg.claim(reg.resolve(SHARED), "station-a") == "station-c"
    reg.set_shared_id("station-a")                                   # back: never its own alias
    assert reg.aliases() == ["station-b", "station-c"]
    reg.reconfigured("station-a", "station-b")                       # its config says an old one: kept
    reg.reconfigured("station-b", "station-a")                       # it still posts as a former one: kept
    clock.t += st_mod.ALIAS_DROP_S + 1
    reg.reconfigured("station-b", "station-a")
    assert reg.aliases() == ["station-b", "station-c"]
    reg.reconfigured("station-a", "station-a")                       # both the current one: the clock starts
    clock.t += st_mod.ALIAS_DROP_S / 2
    reg.reconfigured("station-c", "station-a")                       # a former one again (in flight): starts over
    reg.reconfigured("station-a", "station-a")
    clock.t += st_mod.ALIAS_DROP_S - 1
    reg.reconfigured("station-a", "station-a")
    assert reg.aliases() == ["station-b", "station-c"]
    clock.t += 2
    reg.reconfigured("station-a", "station-a")                       # an hour of it: dropped
    assert reg.aliases() == [] and any("is reconfigured" in m for m in logs)
    assert refused(reg.claim, reg.resolve(SHARED), "station-c") == "id_mismatch"
    reg.set_shared_id("station-b")
    reg.set_shared_id("")                                            # cleared: back to the config's (station-a)
    assert reg.legacy_id() == "station-a" and reg.aliases() == ["station-b"]
    learn, _, _ = registry(tmp_path / "learn")
    learn.claim(learn.resolve(SHARED), "station-a")
    learn.set_shared_id("station-b")
    learn.set_shared_id("")                                          # cleared to "learned": none kept
    assert learn.aliases() == [] and learn.legacy_id() == ""


def test_a_station_on_its_own_token_under_the_old_id_is_told_it_will_be_rejected(tmp_path):
    reg, clock, _ = registry(tmp_path, legacy_id="station-a")
    own = reg.resolve(reg.add("station-a"))
    reg.touch(own)
    q = reg.rename_question("station-a", "station-b")
    assert q == ("station-a is connected with its own token right now and will be rejected until it's reconfigured to "
                 "station-b (a token for station-b). Change anyway?")
    reg.set_shared_id("station-b", confirm=True)
    assert reg.primary(own) == "station-b" != own.id                 # multi_station_off from its next post, as said
    clock.t += st_mod.RENAME_LIVE_S + 1
    assert reg.rename_question("station-b", "station-c") is None


def test_id_mismatch_refusals_are_one_episode_while_they_keep_coming(tmp_path):
    reg, clock, _ = registry(tmp_path)
    assert reg.mismatch() is None
    first = reg.mismatched("station-a", "station-b")
    clock.t += 5
    m = reg.mismatched("station-a", "station-b")
    assert m["since"] == first["since"] and m["n"] == 2 and reg.panel()["mismatch"]["said"] == "station-a"
    clock.t += st_mod.MISMATCH_HOLD_S + 1
    assert reg.mismatch() is None                                    # it stopped
    assert reg.mismatched("station-a", "station-b")["since"] == clock.t      # a new episode


# -- the metrics move with the id --------------------------------------------------------------------------------------

def test_the_metrics_move_to_the_new_id_merging_what_both_have(tmp_path):
    s, clock = store(tmp_path)
    s.ingest("station-a", floors(T0, [1.0, 2.0, 3.0]) + [{"k": "ev", "s": "FG1", "at": T0 + 30, "ev": "gain", "d": "x"}])
    s.ingest("station-b", floors(T0 + 180, [9.0]))                   # it posted under the new id already (same hour)
    s.maintain()
    assert s.rename_station("station-a", "station-b") == 4           # 3 raw + 1 event (the hour's rollups: b has it)
    s.maintain()                                                     # the hour both had: rolled again from raw
    assert {x["station"] for x in s.index()["series"]} == {"station-b"}
    q = s.query({"subject": "FG1", "from": T0, "to": T0 + 3600, "res": "hour", "metric": "idle_q90"})
    assert q["station"] == "station-b" and q["rows"][0]["n"] == 4 and q["rows"][0]["hi"] == 9.0
    assert [e["ev"] for e in q["events"]] == ["gain"]
    assert s.query({"subject": "FG1", "from": T0, "to": T0 + 3600, "res": "raw"})["station"] == "station-b"
    assert s.rename_station("station-a", "station-b") == 0           # nothing left under the old id
    empty, _ = store(tmp_path / "none")
    assert empty.rename_station("station-a", "station-b") == 0 and not (tmp_path / "none" / "metrics.sqlite").exists()


# -- end to end: the hub, the station, Health --------------------------------------------------------------------------

def test_renaming_the_live_station_keeps_it_working_moves_its_history_and_it_adopts_the_new_id(tmp_path):
    from sizeup.app import make_app
    hub = HubRun(hub_cfg(tmp_path, cloud_legacy_station_id="station-a"))
    st = make_app(station_cfg(tmp_path, hub.url, station_id="station-a"))   # threads not started
    try:
        reg = hub.app.hub.stations
        client = HubClient(hub.url, TOKEN)
        seg = fg1_segment()
        seg.start, seg.end = time.time() - 15, time.time() - 3
        st.metrics.tx("FG1", "voice", seg)                           # the real FG1 transmission's numbers
        link = st.link.metrics_link
        link._next = 0.0
        link.step(client)
        link.step(client, now=time.time() + 1)
        assert {x["station"] for x in hub.app.metric_store.index()["series"]} == {"station-a"}
        out = client.request("POST", "/api/station/state", {"at": time.time(), "status": st.link.status(False)})
        assert out["station"] == "station-a" and hub.app.hub.status["id"] == "station-a"
        # the admin names it for where it is: asked first (it's connected), then done
        with pytest.raises(ConfirmNeeded):
            reg.set_shared_id("station-b", "casey")
        reg.set_shared_id("station-b", "casey", confirm=True)
        assert hub.app.hub.status["id"] == "station-b"               # at once, before its next post
        assert wait(lambda: {x["station"] for x in hub.app.metric_store.index()["series"]} == {"station-b"}, 10)
        # (its history: moved in the background, off the admin's request)
        # the station, still configured station-a: every post taken, and told its id
        for path, body in (("/api/station/state", {"at": time.time(), "status": st.link.status(False)}),
                           ("/api/station/events", {"epoch": "e1", "station": "station-a", "events": []}),
                           (PATH, {"v": 1, "station": "station-a", "records": []})):
            code, out = call(hub.url + path, "POST", body)
            assert code == 200, (path, out)
        assert call(hub.url + "/api/station/state", "POST", {"at": time.time(), "status": {"id": "station-a"}}
                    )[1]["station"] == "station-b"
        # its state thread adopts it: events, status, logs and metrics say station-b from then on
        t = threading.Thread(target=st.link._state, daemon=True)
        t.start()
        deadline = time.time() + 15
        while st.link.station_id != "station-b" and time.time() < deadline:
            time.sleep(0.05)
        st.link._stop.set()
        t.join(15)
        assert st.link.station_id == "station-b" and st.link.logs.station == "station-b"
        assert st.link.metrics_link.station == "station-b"
        stat = st.link.status(False)
        assert stat["id"] == "station-b" and stat["config_id"] == "station-a"
        assert reg.aliases() == ["station-a"]                        # its config still says station-a
        st.metrics.tx("FG1", "voice", seg)
        link._next = 0.0
        link.step(client, now=time.time() + 2)
        assert link.error == "" and {x["station"] for x in hub.app.metric_store.index()["series"]} == {"station-b"}
        assert st.station.state()["station_id"] == "station-b" and st.station.state()["config_id"] == "station-a"
        # reconfigured to station-b, posting as it for an hour: the alias goes; an id the token never had is refused
        reconf = {"at": time.time(), "status": {"id": "station-b", "config_id": "station-b"}}
        assert call(hub.url + "/api/station/state", "POST", reconf)[0] == 200
        assert reg.aliases() == ["station-a"]                        # not yet: what it stamped before may be in flight
        reg._reconf_since -= st_mod.ALIAS_DROP_S
        assert call(hub.url + "/api/station/state", "POST", reconf)[0] == 200
        assert reg.aliases() == []
        code, out = call(hub.url + "/api/station/state", "POST", {"at": time.time(), "status": {"id": "station-a"}})
        assert code == 401 and out["reason"] == "id_mismatch"
    finally:
        st.link.stop()
        hub.close()


def test_a_station_refused_for_its_id_is_a_health_alert_naming_both_ids_logged_once_a_minute(tmp_path):
    logs: list[str] = []
    hub = HubRun(hub_cfg(tmp_path, cloud_legacy_station_id="station-b"))
    hub.app.hub.log = logs.append
    try:
        h = hub.app.health
        assert "id_mismatch" not in h.conditions(h.model(), time.time())[0]
        call(hub.url + "/api/station/state", "POST", {"at": time.time(), "status": {"id": "station-a"}})
        assert "id_mismatch" not in h.conditions(h.model(), time.time())[0]      # one post in flight: no episode
        assert health_mod.NOTIFY_AFTER["id_mismatch"] == health_mod.MISMATCH_NOTIFY_S >= 30    # pushed only if it lasts
        for _ in range(2):
            code, out = call(hub.url + "/api/station/state", "POST", {"at": time.time(), "status": {"id": "station-a"}})
            assert code == 401 and out["reason"] == "id_mismatch"
        alerts, _ = h.conditions(h.model(), time.time())
        a = alerts["id_mismatch"]
        assert a["severity"] == "critical"
        assert a["title"] == "Station rejected: id mismatch (station says station-a, token is station-b)"
        said = [m for m in logs if "id_mismatch" in m]
        assert said == ["hub: station request refused: id_mismatch (station station-b): the station says station-a, "
                        "the token is station-b"]
        assert "id_mismatch" in health_mod.HUB_OWN and "id_mismatch" in health_mod.MUTE_TIMED_ONLY
        mismatch = hub.app.hub.stations.panel()["mismatch"]
        assert mismatch["said"] == "station-a" and mismatch["token"] == "station-b"
        hub.app.hub._refused_said[("id_mismatch", "station-b")] -= 61   # a minute on: said again
        call(hub.url + "/api/station/state", "POST", {"at": time.time(), "status": {"id": "station-a"}})
        assert len([m for m in logs if "id_mismatch" in m]) == 2
    finally:
        hub.close()


# -- Settings > Station > Stations and the CLI ------------------------------------------------------------------------

def test_the_card_gets_a_question_while_live_and_the_confirmed_change_is_audited(tmp_path):
    cad = make_cad(tmp_path)
    people(cad)
    reg, clock, _ = registry(tmp_path, legacy_id="station-a")
    cad.stations = reg
    api, me = CadApi(cad), Identity(email=CASEY)
    reg.touch(reg.resolve(SHARED))
    res = api.post("/api/cad/stations", json.dumps({"action": "shared_id", "id": "station-b"}).encode(), me)
    out = json.loads(res[1])
    assert res[0] == 409 and out["confirm"] is True
    assert out["error"].startswith("station-a is connected with this token right now.") and reg.legacy_id() == "station-a"
    res = api.post("/api/cad/stations", json.dumps({"action": "shared_id", "id": "station-b", "confirm": True}).encode(), me)
    assert res[0] == 200 and json.loads(res[1]) == {"ok": True, "station": "station-b", "aliases": ["station-a"]}
    rows = [(r["key"], r["old"], r["new"]) for r in cad.settings.audit_log("system")]
    assert ("stations/shared token id", "station-a", "station-b (confirmed: station-a was connected)") in rows
    clock.t += st_mod.RENAME_LIVE_S + st_mod.TOUCH_S + 1               # not live: no question, no "confirmed"
    assert api.post("/api/cad/stations", json.dumps({"action": "shared_id", "id": "station-c"}).encode(), me)[0] == 200
    rows = [(r["key"], r["old"], r["new"]) for r in cad.settings.audit_log("system")]
    assert ("stations/shared token id", "station-b", "station-c") in rows
    src = (Path(st_mod.__file__).parents[1] / "web" / "static" / "stations.js").read_text()
    assert "e.confirm && await UI.confirm(e.message" in src and "{ ...body, confirm: true }" in src


def test_the_cli_asks_for_force_while_live_and_leaves_the_metrics_move_to_the_hub(tmp_path, capsys, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    hub_reg = StationRegistry(MonitorDb(state / "hub.sqlite"), shared=SHARED, legacy_id="station-a")
    hub_reg.touch(hub_reg.resolve(SHARED))                          # the running hub: last used, in hub.sqlite
    s, _ = store(state)
    s.ingest("station-a", floors(T0, [1.0, 2.0]))
    s.close()
    monkeypatch.setenv("SIZEUP_STATION_TOKEN", SHARED)
    lease(cli_env(monkeypatch, tmp_path))
    (tmp_path / "no-config.toml").write_text('[cloud]\nlegacy_station_id = "station-a"\n')   # the task's config
    assert st_mod.main(["--state", str(state), "reset-shared-id", "--id", "station-b"]) == 1
    assert "station-a is connected with this token right now" in capsys.readouterr().err
    assert st_mod.main(["--state", str(state), "reset-shared-id", "--id", "station-b", "--force"]) == 0
    printed = capsys.readouterr().out
    assert "also takes station-a" in printed and "the hub moves them to the new id" in printed
    assert hub_reg.legacy_id() == "station-b" and hub_reg.claim(hub_reg.resolve(SHARED), "station-a") == "station-b"
    # the CLI never writes metrics.sqlite (the hub's: one writer, its re-rolls kept): it logged the rename for the hub
    s2, _ = store(state)
    assert {x["station"] for x in s2.index()["series"]} == {"station-a"}
    s2.close()
    assert hub_reg.renames() == {"station-a": "station-b"}


# -- the review's cases --------------------------------------------------------------------------------------------------

def test_a_rename_back_keeps_the_former_id_while_the_station_still_sends_it(tmp_path):
    """station-a (its config) renamed to station-b, adopted, then back to station-a: what it still sends as station-b
    (in flight, spooled) is taken until it has posted as station-a, from its config, for ALIAS_DROP_S."""
    reg, clock, _ = registry(tmp_path, legacy_id="station-a")
    reg.set_shared_id("station-b")
    p = reg.resolve(SHARED)
    assert reg.claim(p, "station-b") == "station-b"                  # it adopted station-b
    reg.set_shared_id("station-a")                                   # back to its config's
    p = reg.resolve(SHARED)
    reg.reconfigured("station-b", "station-a")                       # its config says the current id: not enough
    assert reg.aliases() == ["station-b"] and reg.claim(p, "station-b") == "station-a"
    reg.reconfigured("station-a", "station-a")
    assert reg.claim(p, "station-b") == "station-a"                  # a log batch stamped station-b: still taken
    assert reg.renames() == {"station-b": "station-a"}               # (a -> b -> a: only b's rows move back)


def test_a_spooled_log_batch_is_stamped_with_the_station_id_when_sent(tmp_path):
    from sizeup.cloud import logship
    from tests.test_station_logs import shipper
    s, c = shipper(tmp_path)
    s.station = "station-a"
    s.step()
    s.now[0] += logship.BATCH_S
    s.spool(force=True)                                              # spooled as station-a, then the hub renames it
    s.station = "station-b"                                          # (StationLink.adopt)
    while s._batches():
        assert s.send(s.now[0]) == "sent"
    assert c.sent and all(b["station"] == "station-b" and b["batch"].startswith("station-b-") for _, b in c.sent)


def test_renaming_to_an_own_token_station_asks_and_two_live_machines_are_refused(tmp_path):
    reg, clock, _ = registry(tmp_path, legacy_id="station-a")
    own = reg.resolve(reg.add("station-b"))
    q = reg.rename_question("station-a", "station-b")                # neither live: still asked (it merges them)
    assert q == ("station-b has its own token: the shared token's station becomes that station (its history isn't "
                 "moved). Change anyway?")
    reg.touch(reg.resolve(SHARED))
    reg.touch(own)                                                   # both connected: two machines
    with pytest.raises(StationError, match="two stations can't share an id"):
        reg.set_shared_id("station-b", confirm=True)
    assert reg.legacy_id() == "station-a"
    clock.t += st_mod.RENAME_LIVE_S + st_mod.TOUCH_S + 1
    reg._used.clear()
    reg.shared_last = None
    with pytest.raises(ConfirmNeeded):
        reg.set_shared_id("station-b")
    reg.set_shared_id("station-b", confirm=True)                     # the same machine moving to its own token
    assert reg.legacy_id() == "station-b" and reg.renames() == {}    # nothing merged into the own-token station's


def test_a_rename_that_would_forget_a_former_id_asks(tmp_path):
    reg, clock, _ = registry(tmp_path, legacy_id="station-0")
    for i in range(1, st_mod.ALIASES_MAX + 1):
        reg.set_shared_id(f"station-{i}")
    assert len(reg.aliases()) == st_mod.ALIASES_MAX
    q = reg.rename_question(reg.legacy_id(), "station-x")
    assert q.startswith("station-0 will no longer be taken from the shared token") and q.endswith("Change anyway?")
    with pytest.raises(ConfirmNeeded):
        reg.set_shared_id("station-x")
    reg.set_shared_id("station-x", confirm=True)
    assert "station-0" not in reg.aliases() and len(reg.aliases()) == st_mod.ALIASES_MAX


def test_the_renames_follow_chains_and_undo_a_rename_back(tmp_path):
    reg, clock, _ = registry(tmp_path, legacy_id="station-a")
    reg.set_shared_id("station-b")
    reg.set_shared_id("station-c")
    assert reg.renames() == {"station-a": "station-c", "station-b": "station-c"}
    reg.set_shared_id("station-a")
    assert reg.renames() == {"station-b": "station-a", "station-c": "station-a"}


def test_clearing_an_override_equal_to_the_config_is_audited(tmp_path):
    cad = make_cad(tmp_path)
    people(cad)
    reg, clock, _ = registry(tmp_path, legacy_id="station-a")
    cad.stations = reg
    api, me = CadApi(cad), Identity(email=CASEY)
    reg.mdb.put(st_mod.META_LEGACY, "station-a")                      # an override that says what the config says
    assert api.post("/api/cad/stations", json.dumps({"action": "shared_id", "id": ""}).encode(), me)[0] == 200
    assert reg.mdb.get(st_mod.META_LEGACY, "") == ""
    rows = [(r["key"], r["old"], r["new"]) for r in cad.settings.audit_log("system")]
    assert ("stations/shared token id", "station-a", "station-a (override cleared: the config's)") in rows


def test_the_metrics_move_in_chunks_and_again_after_a_takeover_restores_an_older_copy(tmp_path):
    s, clock = store(tmp_path)
    s.ingest("station-a", floors(T0, list(range(1, 8))) + floors(T0 + 3600, [5.0, 6.0]))
    s.maintain()
    whole, _ = store(tmp_path / "whole")
    whole.ingest("station-a", floors(T0, list(range(1, 8))) + floors(T0 + 3600, [5.0, 6.0]))
    whole.maintain()
    assert s.rename_station("station-a", "station-b", chunk=2) == whole.rename_station("station-a", "station-b")
    q = {"subject": "FG1", "from": T0, "to": T0 + 7200, "res": "hour", "metric": "idle_q90"}
    assert s.query(q)["rows"] == whole.query(q)["rows"] and s.query(q)["station"] == "station-b"
    # the hub: the renames in hub.sqlite applied to a store that came back from a copy made before the rename
    hub = HubRun(hub_cfg(tmp_path / "hub", cloud_legacy_station_id="station-a"))
    try:
        reg, ms = hub.app.hub.stations, hub.app.metric_store
        now = (time.time() // 3600 - 2) * 3600
        assert ms.ingest("station-a", floors(now, [1.0]))["accepted"] == 1
        reg.set_shared_id("station-b")
        assert wait(lambda: {x["station"] for x in ms.index()["series"]} == {"station-b"}, 10)
        ms.ingest("station-a", floors(now - 600, [2.0]))             # a restored copy's pre-rename rows
        ms._next_upload = time.time() + 9999
        assert hub.app.apply_station_renames() == 1
        assert {x["station"] for x in ms.index()["series"]} == {"station-b"} and ms._next_upload <= time.time()
        assert hub.app.apply_station_renames() == 0                   # nothing left: an index lookup per table
    finally:
        hub.close()
