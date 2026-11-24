"""The sources intel could push from (sizeup/intel/engine.py, "Push sources"): speech-to-text's priority hook.

    IntelEngine.push_sources() -> (frozenset of source keys, revision); sizeup.intel.push_sources() without the engine.

Held here, over the public talkgroups files (the county's and MRRS's) and the packs as shipped:
  - the police pack gives exactly its three talkgroups; Area PD (neighbor_police) at log gives nothing; raised to push
    in Settings it adds every other MRRS police talkgroup, under another revision;
  - intel off, the kill switch off, no intel topic in effect: nothing (the empty set's revision, never 0); no change:
    the same revision;
  - the revision is a content hash of the set: the same set has the same one in another engine (a hub restarted),
    another set another one;
  - an admin's radio_services choice moves a source in or out (the service as rows are tagged live);
  - the set is what feed() reads: the same Pack.in_scope over the same rows;
  - no engine here (a station): (frozenset(), 0).

Synthetic only (this repository is public): invented topics, no transcripts. Talkgroup numbers are the public files'."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import sizeup.intel
from sizeup import notify, sourcekeys
from sizeup.intel import engine as engine_mod
from sizeup.intel import rules as rules_mod
from sizeup.intel.engine import IntelEngine, RecordingSink, push_rev, source_rows
from sizeup.intel.events import MemoryEvents
from sizeup.radio.systems import read_talkgroups
from sizeup.talkgroups import TalkgroupPrefs
from tests.test_cad import make_cad
from tests.test_intel import Clock

TR = Path(__file__).resolve().parents[1] / "deploy" / "trunk-recorder"
OURS = frozenset({"p25:mrrs:9211", "p25:mrrs:9212", "p25:mrrs:9213"})
CHANNELS = (SimpleNamespace(label="South 2", kind="paging"), SimpleNamespace(label="FG1", kind="traffic"),
            SimpleNamespace(label="HPN Tower", kind="airband"))
TOPIC = "pcfd-xmpl-intel-sources"


def prefs() -> TalkgroupPrefs:
    """The talkgroups as the app knows them: the county's file (as config.py reads it) and MRRS's (#409)."""
    with (TR / "talkgroups.csv").open() as f:
        county = {int(r["Decimal"]): {"name": r["Alpha Tag"], "group": r.get("Tag") or "",
                                      "encrypted": "E" in (r.get("Mode") or "").upper()} for r in csv.DictReader(f)}
    return TalkgroupPrefs(county, set(), None, others={"mrrs": read_talkgroups(TR / "systems" / "mrrs" / "talkgroups.csv",
                                                                             "mrrs")})


def mrrs_police() -> set[str]:
    with (TR / "systems" / "mrrs" / "talkgroups.csv").open() as f:
        return {f"p25:mrrs:{r['Decimal']}" for r in csv.DictReader(f) if r["Service"] == "police"}


def got(keys) -> tuple[frozenset, int]:
    """What push_sources() answers for this set: the keys and their content hash."""
    return frozenset(keys), push_rev(keys)


class Topic:
    """The INTEL kind's notifier as LiveSink reads it: enabled while a topic is in effect."""
    enabled = True


def engine(settings: dict | None = None, notifier=None):
    """An engine over the shipped packs, with Settings read at each reload() (as the CAD's are) and every known
    source: the public talkgroups files and three channels."""
    cfg = settings if settings is not None else {}
    e = IntelEngine(settings=lambda: cfg, events=MemoryEvents(), sink=RecordingSink(), thread=False, clock=Clock())
    e.notifier = Topic() if notifier is None else notifier
    tg = prefs()
    e.sources = lambda: source_rows(tg.state(), CHANNELS)
    return e, cfg


def test_the_police_pack_gives_exactly_its_three_talkgroups_area_pd_at_log_none():
    e, _cfg = engine()
    keys, rev = e.push_sources()
    assert keys == OURS
    assert rev == push_rev(OURS) and 0 < rev < 2 ** 53          # a content hash, JSON-safe, never 0
    assert e.push_sources() == got(OURS)                       # no change: the same revision (cached)
    # every pack's sources are known here: Area PD reads the other MRRS police talkgroups, at log
    assert e.pack("neighbor_police").mode == "log"
    known = {r["source"] for r in e.sources()}
    assert mrrs_police() <= known and "p25:westchester:359" in known and {"fm:FG1", "air:HPN Tower"} <= known


def test_area_pd_raised_to_push_in_settings_adds_the_other_mrrs_police_talkgroups():
    e, cfg = engine()
    assert e.push_sources() == got(OURS)
    cfg["intel_packs"] = {"neighbor_police": {"mode": "push"}}
    e.reload()                                                  # (the Settings op reloads: test_settings_ops_*)
    keys, rev = e.push_sources()
    assert keys == mrrs_police() and rev == push_rev(keys) != push_rev(OURS)
    assert keys - OURS >= {"p25:mrrs:9563", "p25:mrrs:9564", "p25:mrrs:9000", "p25:mrrs:8000", "p25:mrrs:8002"}
    assert "p25:mrrs:9570" not in keys                          # PC-Rye EMS: neither pack's service
    e.reload()                                                  # a reload that changes nothing: the same revision
    assert e.push_sources() == got(keys)
    # its every category at log again: nothing of Area PD's (its rules, from police.json, stay at push)
    cfg["intel_packs"] = {"neighbor_police": {"mode": "push", "categories": {
        c: {"mode": "log"} for c in e.pack("neighbor_police").categories}}}
    e.reload()
    assert e.push_sources() == got(OURS)


def test_a_rule_or_category_at_push_is_needed_with_the_pack():
    cats = {p.name: p for p in rules_mod.load_packs()}["police"].categories
    e, cfg = engine({"intel_packs": {"police": {"categories": {c: {"mode": "log"} for c in cats}}}})
    pol = e.pack("police")
    assert pol.mode == "push" and not engine_mod.can_push(pol)
    assert e.push_sources() == got(frozenset())
    cfg["intel_packs"] = {}
    cfg["intel_rules"] = {r.id: {"mode": "log"} for r in pol.rules}          # every rule at log
    e.reload()
    assert e.push_sources() == got(frozenset())                 # still none: the same revision
    cfg["intel_rules"] = {r.id: {"mode": "log"} for r in pol.rules if r.id != "pd.shots"}
    e.reload()
    assert e.push_sources() == got(OURS)                        # one rule at push in a category at push: the pack's


@pytest.mark.parametrize("off", ["enabled", "push", "topic", "notifier"])
def test_intel_off_the_kill_switch_or_no_topic_empties_it_and_changes_the_revision(off):
    e, cfg = engine()
    assert e.push_sources() == got(OURS)
    if off == "topic":
        e.notifier.enabled = False
        e.sources_changed()                                     # (Settings' notify section: settings._use)
    elif off == "notifier":
        e.notifier = None
        e.sources_changed()
    else:
        cfg["intel"] = {off: False}
        e.reload()
    assert e.push_sources() == got(frozenset())
    assert e.push_sources() == got(frozenset())
    if off == "topic":
        e.notifier.enabled = True
        e.sources_changed()
    elif off == "notifier":
        e.notifier = Topic()
        e.sources_changed()
    else:
        cfg["intel"] = {}
        e.reload()
    assert e.push_sources() == got(OURS)


def test_an_admins_service_choice_moves_a_source_in_or_out():
    e, _cfg = engine()
    assert e.push_sources() == got(OURS)
    sourcekeys.set_service_overrides({"p25:mrrs:9212": "ems"})     # (conftest puts the process's map back)
    e.sources_changed()
    assert e.push_sources() == got(OURS - {"p25:mrrs:9212"})
    sourcekeys.set_service_overrides({})
    e.sources_changed()
    assert e.push_sources() == got(OURS)


def test_recomputed_at_most_push_refresh_s_after_the_last_without_a_change_told(monkeypatch):
    e, _cfg = engine()
    assert e.push_sources() == got(OURS)
    sourcekeys.set_service_overrides({"p25:mrrs:9213": "other"})   # nothing told
    assert e.push_sources() == got(OURS)                        # cached
    t = engine_mod.time.monotonic() + engine_mod.PUSH_REFRESH_S + 1
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: t)
    assert e.push_sources() == got(OURS - {"p25:mrrs:9213"})


def test_the_set_is_what_feed_reads_one_predicate():
    """Every known source a pack that can push reads (Pack.in_scope, as feed() calls it), and no other; a pack with
    "stt_high": false (the highway pack: data/intel/highway.json) pushes without making its sources ones."""
    e, cfg = engine({"intel_packs": {"neighbor_police": {"mode": "push"}}})
    keys, _rev = e.push_sources()
    for r in e.sources():
        row = {**r, "id": "t1", "text": "x", "start": 1.0}
        read = [p.name for p in e.packs if p.in_scope(row) and engine_mod.can_push(p) and p.stt_high]
        assert (r["source"] in keys) == bool(read), r["source"]


def test_source_rows_tag_as_rows_are_tagged_live():
    rows = {r["source"]: r for r in source_rows(prefs().state(), CHANNELS)}
    assert rows["p25:mrrs:9211"] == {"source": "p25:mrrs:9211", "system": "mrrs", "service": "police",
                                     "tier": "alert", "dept": ""}
    assert rows["p25:westchester:359"]["service"] == "police" and rows["p25:westchester:359"]["tier"] == ""
    assert rows["fm:South 2"]["service"] == "fire" and rows["fm:South 2"]["system"] == "analog"
    assert rows["air:HPN Tower"]["service"] == "air" and rows["air:HPN Tower"]["system"] == "airband"
    # the station's row first (the hub's list), the hub's own file after it: the first one's
    first = source_rows([{"source": "p25:mrrs:9211", "system": "mrrs", "service_auto": "ems", "tier": "text"},
                         *prefs().state()])
    assert next(r for r in first if r["source"] == "p25:mrrs:9211")["service"] == "ems"


def test_no_engine_here_is_none_and_a_station_forwarding_is_none(monkeypatch):
    monkeypatch.setattr(engine_mod, "_live", None)
    assert sizeup.intel.push_sources() == (frozenset(), 0)
    e, _cfg = engine()
    engine_mod.set_live(e)
    assert sizeup.intel.push_sources() == got(OURS)
    e.active = lambda: False                                    # a station forwarding to its hub
    assert sizeup.intel.push_sources() == (frozenset(), 0)


def test_settings_ops_recompute_it(tmp_path):
    """The CAD's Settings: intel's (the kill switch, a pack's mode), radio_services and the intel topic."""
    cad = make_cad(tmp_path)
    cad.comms.context_broadcast = True                          # (an MRRS talkgroup's service: signed-in admins)
    cad.pushes = notify.Pushes(intel_topic=TOPIC, settings=lambda: cad.settings.section(notify.SECTION),
                               log=lambda m: None)
    e = cad.intel
    e.notifier = cad.pushes.notifier(notify.INTEL)
    tg = prefs()
    e.sources = lambda: source_rows(tg.state(), CHANNELS)
    post = lambda sec, **b: cad.settings_post(sec, {"operator": "Lt. Test", **b})      # noqa: E731
    assert e.push_sources() == got(OURS)
    post("intel", action="pack", pack="neighbor_police", mode="push")
    assert e.push_sources() == got(mrrs_police())
    post("intel", action="pack", pack="neighbor_police", mode="log")
    assert e.push_sources() == got(OURS)
    post("radio_services", action="set", key="p25:mrrs:9211", service="ems")
    cad.retag_thread.join(10)
    assert e.push_sources() == got(OURS - {"p25:mrrs:9211"})
    post("radio_services", action="set", key="p25:mrrs:9211", service="automatic")
    cad.retag_thread.join(10)
    assert e.push_sources() == got(OURS)
    post("intel", action="set", key="push", value=False)
    assert e.push_sources() == got(frozenset())
    post("intel", action="set", key="push", value=True)
    assert e.push_sources() == got(OURS)
    post("notify", action="off", key=notify.INTEL)
    assert e.push_sources() == got(frozenset())
    post("notify", action="reset", key=notify.INTEL)
    assert e.push_sources() == got(OURS)
    assert e.push_sources() == got(OURS)


def test_the_hub_wires_it_its_files_then_the_stations_list(tmp_path, monkeypatch):
    """HubApp (App's wiring): the engine is this process's, its sources the hub's own talkgroups files and channels
    until the station says what it lists; the station's rows win then."""
    from sizeup.cloud.hub import HubApp
    from sizeup.radio.systems import load
    from tests.test_cloud import hub_cfg
    monkeypatch.setattr(engine_mod, "_live", None)              # (put back after: the process's engine is the app's)
    app = HubApp(hub_cfg(tmp_path, alerts_intel_topic=TOPIC, p25_systems=(load(TR / "systems" / "mrrs"),)))
    assert engine_mod._live is app.cad.intel
    assert sizeup.intel.push_sources() == got(OURS)
    assert {"fm:South 2", "fm:FG1"} <= {r["source"] for r in app.cad.intel.sources()}
    # the station's list (its talkgroups file says EMS for 9212, say): the station's row first
    app.hub.talkgroups = [{"source": "p25:mrrs:9212", "system": "mrrs", "tg": 9212, "service_auto": "ems",
                           "service": "ems", "tier": "text"}]
    app.cad.intel.sources_changed()
    assert sizeup.intel.push_sources() == got(OURS - {"p25:mrrs:9212"})


def test_the_revision_is_a_content_hash_the_same_after_a_restart():
    """A hub restarted (another engine, another process's counters) gives the same set the same rev, so a station
    holding that rev holds those keys; another set never shares it (#716 review: no in-memory counter)."""
    a, _ = engine()
    b, cfg = engine()
    assert a.push_sources() == b.push_sources() == got(OURS)
    cfg["intel_packs"] = {"neighbor_police": {"mode": "push"}}
    b.reload()
    assert b.push_sources()[1] != a.push_sources()[1]
    cfg["intel_packs"] = {}
    b.reload()
    assert b.push_sources() == a.push_sources()                # back to the same set: the same rev again
    # the hash itself: the sorted keys, one per line; order never matters; 53 bits; the empty set's is not 0
    want = int.from_bytes(hashlib.sha256("\n".join(sorted(OURS)).encode()).digest()[:8], "big") & ((1 << 53) - 1)
    assert push_rev(OURS) == want == push_rev(list(reversed(sorted(OURS))))
    assert push_rev(frozenset()) == (int.from_bytes(hashlib.sha256(b"").digest()[:8], "big") & ((1 << 53) - 1) or 1)
    assert push_rev(frozenset()) not in (0, push_rev(OURS))
    assert json.loads(json.dumps(push_rev(OURS))) == push_rev(OURS) < 2 ** 53


def test_a_hash_of_zero_is_one(monkeypatch):
    class Zero:
        def digest(self):
            return bytes(32)
    monkeypatch.setattr(engine_mod.hashlib, "sha256", lambda b=b"": Zero())
    assert push_rev({"p25:mrrs:9211"}) == 1


# -- is_push_source: speech-to-text's channel keys ("tg:307", a bare "FG1") normalised as rows are keyed -----------------

def fire_at_push() -> dict:
    """Settings that raise the fire pack (shipped at log: pack, categories and rules) to push."""
    fire = {p.name: p for p in rules_mod.load_packs()}["fire"]
    return {"intel_packs": {"fire": {"mode": "push", "categories": {c: {"mode": "push"} for c in fire.categories}}},
            "intel_rules": {r.id: {"mode": "push"} for r in fire.rules}}


def test_a_talkgroup_key_and_a_bare_fm_label_map_as_rows_are_keyed():
    e, _cfg = engine(fire_at_push())
    keys, _rev = e.push_sources()
    assert {"p25:westchester:307", "fm:South 2", "fm:FG1"} <= keys and "tg:307" not in keys and "FG1" not in keys
    # speech-to-text's keys (sourcekeys.channel_key / app.traffic_key): the county's "tg:<n>", a bare label
    assert e.is_push_source("tg:307") and e.is_push_source("p25:westchester:307")
    assert e.is_push_source("FG1") and e.is_push_source("FG1", "traffic") and e.is_push_source("South 2", "paging")
    assert e.is_push_source("fm:FG1")
    assert not e.is_push_source("FG1", "airband")              # an airband channel of that name would be air:FG1
    assert e.is_push_source("p25:mrrs:9211") and not e.is_push_source("p25:mrrs:9570")
    assert not e.is_push_source("tg:9211")                      # a bare number is the county's, never MRRS's
    assert not e.is_push_source("") and not e.is_push_source("Nowhere")
    # with the fire pack at log again: none of them (the police pack's own still are)
    e2, _ = engine()
    assert not e2.is_push_source("tg:307") and not e2.is_push_source("FG1") and e2.is_push_source("p25:mrrs:9211")


def test_an_airband_label_maps_to_its_air_key():
    raw = json.loads((rules_mod.DATA / "police.json").read_text())
    raw.update(name="air_test", label="AIR", scope={"sources": ["air:*"]}, towns={})
    e = IntelEngine(packs=rules_mod.load_packs([raw]), events=MemoryEvents(), sink=RecordingSink(), thread=False,
                    clock=Clock())
    e.notifier = Topic()
    tg = prefs()
    e.sources = lambda: source_rows(tg.state(), CHANNELS)
    assert e.push_sources()[0] == frozenset({"air:HPN Tower"})
    assert e.is_push_source("HPN Tower") and e.is_push_source("HPN Tower", "airband") and e.is_push_source("air:HPN Tower")
    assert not e.is_push_source("HPN Tower", "traffic") and not e.is_push_source("FG1")


def test_the_module_check_normalises_too_and_is_false_with_no_engine():
    assert sizeup.intel.is_push_source("p25:mrrs:9211") is False      # (conftest: no engine left registered)
    e, _cfg = engine(fire_at_push())
    engine_mod.set_live(e)
    assert sizeup.intel.is_push_source("tg:307") and sizeup.intel.is_push_source("FG1")
    assert sizeup.intel.is_push_source("p25:mrrs:9211")
    e.active = lambda: False                                    # a station forwarding: the hub's engine decides
    assert not sizeup.intel.is_push_source("tg:307")
    assert engine_mod.source_key("tg:307") == ("p25:westchester:307",)
    assert engine_mod.source_key("HPN Tower") == ("fm:HPN Tower", "air:HPN Tower")
    assert engine_mod.source_key("HPN Tower", "airband") == ("air:HPN Tower",)
