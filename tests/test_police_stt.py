"""Radio-intel PR 3: the police speech-to-text profile, the cost meter and the PD audio budget.

- #423: Amazon Transcribe streaming is priced at us-east-1's $0.01 a minute, per second, with no minimum per stream.
- Two budgets that never share a dollar: ours (fire, with the reserve for the pages and Fire/EMS 15) and context
  (another P25 system's talkgroups, e.g. PD dispatch "p25:mrrs:9211"). Police past its own limit goes to the station's
  whisper (the hub's "deny"), never silence; the fire limit running out never stops police.
- sizeup-pd: the police sources' own vocabulary and respelling, within AWS's limits; the fire vocabulary is never used
  on a police source nor sizeup-pd's on ours, so a police "266" never becomes a fire callsign (Rye Brook FD's 266 /
  DPT266 are another namespace).
- whisper's police prompt: short, police phrasing, never a talker's or a person's name.
- The context clips the whisper queue sheds are counted (stt_shed_context) and shown as a Health hint.
Synthetic data only: "Example Ave", made-up talkers."""

import json
import re
import sqlite3
import time
from types import SimpleNamespace

import pytest

from sizeup import channels, stt_vocab
from sizeup import transcribe as tr
from sizeup.app import App
from sizeup.cloud.health import context_hints
from sizeup.stt import CONTEXT, CONTEXT_MAX_WAIT_S, SttQueue
from sizeup.transcribe import (CONTEXT as POOL, PD_VOCABULARY, PRICE_PER_MIN, AwsTranscriber, Settings, Usage,
                               build_vocabulary, is_context, respell)

PD = "p25:mrrs:9211"


@pytest.fixture(autouse=True)
def fake_creds(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDTESTTESTTEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")


def svc(kv=None, limit=25.0, ctx_limit=15.0):
    kv = {} if kv is None else kv
    return AwsTranscriber(Settings(endpoint="ws://127.0.0.1:9"), Usage(kv.get, kv.__setitem__), lambda *a: None,
                          lambda *a: None, log=lambda m: None, limit_usd=lambda: limit,
                          context_limit_usd=lambda: ctx_limit), kv


def gazetteer(tmp_path):
    """A made-up gazetteer: Example Ave and Sample St in the three towns, one elsewhere, a public and a private place."""
    p = tmp_path / "gaz.sqlite"
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE streets (id INTEGER PRIMARY KEY, key TEXT, name TEXT);
        CREATE TABLE addresses (street_id INTEGER, number INTEGER, suffix TEXT, muni TEXT, lat REAL, lon REAL, points INTEGER);
        CREATE TABLE lines (street_id INTEGER, muni TEXT, coords TEXT);
        CREATE TABLE landmarks (id INTEGER PRIMARY KEY, name TEXT, category TEXT, address TEXT, muni TEXT, lat REAL,
                                lon REAL, source TEXT, aliases TEXT);
        INSERT INTO streets VALUES (1, 'a', 'Example Ave'), (2, 'b', 'N Sample St'), (3, 'c', 'Faraway Rd'),
                                   (4, 'd', 'Lineonly Ln (Rye)');
        INSERT INTO addresses VALUES (1, 100, '', 'Port Chester', 0, 0, 1), (1, 102, '', 'Port Chester', 0, 0, 1),
                                     (2, 5, '', 'Rye Brook', 0, 0, 1), (3, 7, '', 'Elsewhere', 0, 0, 1);
        INSERT INTO lines VALUES (4, 'Rye', '[]');
        INSERT INTO landmarks VALUES (1, 'Example Park', 'park', '', 'Rye', 0, 0, 'x', 'Example Green'),
                                     (2, 'Law Office of Pat Doe', 'office=lawyer', '', 'Rye', 0, 0, 'x', ''),
                                     (3, 'Sample School', 'school', '', 'Elsewhere', 0, 0, 'x', '');
    """)
    c.commit()
    c.close()
    return p


# -- #423: the price ----------------------------------------------------------------------------------------------


def test_the_price_is_us_east_1_streaming_per_second_with_no_minimum():
    assert PRICE_PER_MIN == 0.01 and tr.MIN_BILLED_S == 0.0          # was 0.024 a minute, 15 s a stream (#423)
    t, kv = svc()
    t._bill(6.0, "FG1")                                               # a 6 s stream bills 6 s, not 15
    sp = t.spent()
    assert sp["billed_s"] == 6.0 and sp["usd"] == round(6 / 60 * 0.01, 2)
    t.usage.add(60 * 100, 60 * 100, "tg:307")                         # 100 min = $1.00
    assert t.spent()["usd"] == pytest.approx(1.0, abs=0.01)


# -- the two budgets -------------------------------------------------------------------------------------------------


def test_usage_is_kept_per_pool_and_per_channel():
    kv = {}
    u = Usage(kv.get, kv.__setitem__)
    u.add(30, 30, "FG1")
    u.add(20, 20, "FG1")
    u.add(60, 60, PD)
    ours, ctx = u.month(), u.month(POOL)
    assert (ours["billed_s"], ours["channels"]) == (50, {"FG1": 50})
    assert (ctx["billed_s"], ctx["channels"]) == (60, {PD: 60})
    assert any(k.startswith("aws_usage_context:") for k in kv) and any(k.startswith("aws_usage:") for k in kv)
    assert is_context(PD) and not is_context("tg:307") and not is_context("South 2") and not is_context(None)


def test_police_never_spends_the_fire_budget_or_its_reserve():
    t, _kv = svc(limit=25.0, ctx_limit=15.0)
    t.usage.add(15 / PRICE_PER_MIN * 60, 0, PD)                        # the police budget used up
    ok, why = t.available(PD)
    assert not ok and "police" in why and "$15" in why
    assert t.available("FG1") == (True, "") and t.available("tg:307") == (True, "")     # fire untouched
    assert t.spent()["usd"] == 0 and t.budget()["pct"] == 0 and t.budget()["context"]["pct"] >= 100
    assert t.budget()["context"]["ok"] is False
    # ... and the fire limit (and its reserve) used up never stops police while its own budget lasts
    t2, _ = svc(limit=25.0, ctx_limit=15.0)
    t2.usage.add(31 / PRICE_PER_MIN * 60, 0, "tg:307")                 # past $25 and the $30 reserve
    assert not t2.available("FG1")[0] and not t2.available("tg:307")[0]
    assert t2.available(PD) == (True, "") and PD not in t2.reserve_left()
    assert t2.spent(POOL)["usd"] == 0


def test_a_police_channel_never_uses_the_fire_vocabulary_nor_its_respelling():
    t, _ = svc()
    t.vocabulary = tr.VOCABULARY                                       # sizeup-pcfd READY
    assert "vocabulary-name=sizeup-pcfd" in t.url("tg:307")
    assert "vocabulary-name" not in t.url(PD)                          # no sizeup-pd yet: none, never the fire one
    t.channel_vocabulary[PD] = PD_VOCABULARY
    assert "vocabulary-name=sizeup-pd" in t.url(PD) and "vocabulary-name=sizeup-pcfd" in t.url("FG1")
    fire = {"engine sixty": "Engine 60"}
    pd = build_vocabulary(["266", "908", "10-4"])["respell"]
    t.respell = lambda s: respell(s, fire)
    t.channel_respell[PD] = lambda s: respell(s, pd)
    assert t._respell_for(PD)("car two sixty six ten four") == "car 266 10-4"
    assert t._respell_for(PD)("engine sixty") == "engine sixty"           # fire words stay words on police
    assert t._respell_for("tg:307")("two sixty six") == "two sixty six"   # police words never respelled on fire
    assert t._respell_for("p25:mrrs:9570")("engine sixty") == "engine sixty"   # another context source: neither


# -- 266: the police namespace -----------------------------------------------------------------------------------


def test_police_266_never_collides_with_the_fire_266():
    pd = stt_vocab.police_phrases(gazetteer=None)
    assert {"908", "266", "Headquarters"} <= set(pd)
    # the headquarters callsigns come from the police namespace of data/pd_units.json only
    units = json.loads((stt_vocab.DATA / "pd_units.json").read_text())
    hq = [c for s in units["systems"].values() for c in s["callsigns"] if c.get("role") == "headquarters"]
    assert {c["said"] for c in hq} == set(stt_vocab.hq_callsigns()) and {c["service"] for c in hq} == {"police"}
    # nothing of the fire namespace in sizeup-pd: no apparatus, no department placeholder, no fire department
    lists = json.loads((stt_vocab.DATA / "cad_lists.json").read_text())
    fire_codes = {u for u in json.dumps(lists).replace('"', " ").split() if re.fullmatch(r"DPT\d+", u)}
    assert fire_codes and not fire_codes & set(pd)
    assert not [p for p in pd if re.search(r"\b(?:Engine|Ladder|Tower Ladder|Rescue|DPT|Fire)\b", p)]
    # sizeup-pd's respelling turns "two sixty six" into 266; the fire one never does, and the two are never mixed
    fire_map = build_vocabulary(stt_vocab.county_phrases(battalions=stt_vocab.SUGGEST_BATTALIONS))["respell"]
    pd_map = build_vocabulary(pd)["respell"]
    assert pd_map["two sixty six"] == "266" and "two sixty six" not in fire_map


# -- sizeup-pd and the prompt -----------------------------------------------------------------------------------------


def test_police_phrases_come_from_the_gazetteer_with_public_landmarks_only(tmp_path):
    g = gazetteer(tmp_path)
    pd = stt_vocab.police_phrases(gazetteer=g)
    assert "Example Avenue" in pd and "North Sample Street" in pd and "Lineonly Lane" in pd
    assert "Faraway Road" not in pd                                       # another town
    assert {"Example Park", "Example Green"} <= set(pd) and not [p for p in pd if "Doe" in p or "Office" in p]
    assert "Sample School" not in pd
    # priority: callsigns and 10-codes before streets, streets before landmarks (a long list loses landmarks first)
    assert pd.index("266") < pd.index("10-4") < pd.index("Rye Brook") < pd.index("Example Avenue") < pd.index("Example Park")


AWS_PHRASE = re.compile(r"(?:[A-Za-z']+|(?:[A-Za-z]\.)+)(?:-(?:[A-Za-z']+|(?:[A-Za-z]\.)+))*")


def test_sizeup_pd_fits_aws_limits_on_the_real_gazetteer():
    """The real data's three towns: every phrase in AWS's list-form charset, at most 256 characters, the whole list
    under 50 KB and MAX_PHRASES, nothing skipped for a limit (counts only: no street is printed)."""
    pd = stt_vocab.police_phrases()
    built = build_vocabulary(pd)
    assert len(built["phrases"]) > 500                                # every street of three towns
    assert all(AWS_PHRASE.fullmatch(p) and len(p) <= tr.MAX_PHRASE for p in built["phrases"])
    assert sum(len(p.encode()) + 1 for p in built["phrases"]) <= tr.MAX_BYTES
    assert len(built["phrases"]) <= tr.MAX_PHRASES
    assert not [s for s in built["skipped"] if "over" in s["why"]]


def test_the_limits_drop_the_tail_never_the_callsigns():
    long = ["266", "908", "10-4"] + [f"Example {'Long ' * 8}Avenue {i}" for i in range(1, 3000)]
    built = build_vocabulary(long)
    assert built["phrases"][:3] == ["two-sixty-six", "nine-oh-eight", "ten-four"]
    assert len(built["phrases"]) <= tr.MAX_PHRASES and sum(len(p) + 1 for p in built["phrases"]) <= tr.MAX_BYTES
    assert any("over" in s["why"] for s in built["skipped"])


def test_the_police_profile_and_its_prompt(tmp_path):
    prof = stt_vocab.profile(PD)
    assert prof["name"] == "police" and prof["vocabulary"] == PD_VOCABULARY
    assert stt_vocab.profile("p25:mrrs:9212", "police")["name"] == "police"      # by service
    assert stt_vocab.profile("tg:307", "fire") is None
    assert stt_vocab.vocabulary_sources(PD_VOCABULARY) == [PD]
    p = stt_vocab.police_prompt(prof, gazetteer=gazetteer(tmp_path))
    assert "908" in p and "266" in p and "Headquarters" in p and "Example Avenue" in p and "Port Chester" in p
    assert len(p.split()) < 120                                        # short: few prompt insertions


class _PromptApp:
    _prompt = App._prompt
    _stt_profile = staticmethod(App._stt_profile)
    _profile_prompt = App._profile_prompt

    def __init__(self):
        self._kind = {}
        self.cfg = SimpleNamespace(stt_prompt="Port Chester Fire.", stt_talker_prompt="ids")


def test_the_police_prompt_never_names_a_talker():
    app = _PromptApp()
    pd_item = SimpleNamespace(source="PD Dispatch", source_key=PD, service="police", unit="Officer Pat Example",
                              start=time.time())
    p = app._prompt(pd_item)
    assert "Pat" not in p and "Officer" not in p and "Port Chester Fire" not in p and "266" in p
    rerun = SimpleNamespace(source="", source_key=PD, service="police", unit="Officer Pat Example", start=None)
    assert app._prompt(rerun) == p                                       # a re-run gets the same
    fire = SimpleNamespace(source="Fire/EMS 15", source_key="tg:307", unit="Engine 60", start=None)
    assert app._prompt(fire).startswith("Port Chester Fire.") and "266" not in app._prompt(fire)
    county = SimpleNamespace(source="x", source_key="p25:westchester:307", service="police", unit="", start=None)
    assert "266" not in app._prompt(county)                              # the county system is ours: no PD profile


# -- the station hears the police budget apart ------------------------------------------------------------------------


def test_the_station_obeys_deny_and_reserve_for_context_channels():
    st = SimpleNamespace(forwarding=lambda: True, aws=None)
    aws_ok = App.aws_ok.__get__(st)
    st._hub_aws = {"ok": True, "why": "", "reserve": [], "deny": [PD], "deny_why": "police budget used"}
    assert aws_ok("FG1") == (True, "") and aws_ok(PD) == (False, "police budget used")
    st._hub_aws = {"ok": False, "why": "the monthly AWS limit is reached", "reserve": ["tg:307", PD]}
    assert aws_ok(PD) == (True, "") and not aws_ok("FG1")[0]
    # the signature: the same with no deny as before (an older station agrees), another with one
    assert channels.sig({}, True) == channels.sig({}, True, deny=[]) != channels.sig({}, True, deny=[PD])


def test_the_hub_splits_the_two_budgets_for_the_station():
    t, _ = svc(limit=25.0, ctx_limit=15.0)
    hub = SimpleNamespace(aws=t, context_aws_keys=lambda: [PD])
    split = App.aws_split.__get__(hub)
    assert split(True) == ([], [], "")
    t.usage.add(16 / PRICE_PER_MIN * 60, 0, PD)
    more, deny, why = split(True)
    assert (more, deny) == ([], [PD]) and "police" in why
    t2, _ = svc(limit=25.0, ctx_limit=15.0)
    t2.usage.add(40 / PRICE_PER_MIN * 60, 0, "FG1")
    assert App.aws_split.__get__(SimpleNamespace(aws=t2, context_aws_keys=lambda: [PD]))(False) == ([PD], [], "")


def test_hub_to_station_police_budget_used_up_runs_whisper_on_police_only(tmp_path):
    from tests.fake_transcribe import FakeTranscribe
    from tests.test_channels import aws_cfg
    from tests.test_cloud import HubRun, call, hub_cfg, station, station_cfg
    from tests.test_transcribe import wait
    fake = FakeTranscribe()
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    st = None
    try:
        assert PD in hub.app.context_aws_keys()                          # PD dispatch defaults to AWS
        st = station(station_cfg(tmp_path, hub.url), {})
        assert wait(lambda: st._hub_aws["ok"], 10)
        assert st.aws_ok(PD)[0] and st.aws_ok("FG1")[0]
        assert call(hub.url + "/api/cad/settings/stt", "POST", {"action": "set", "key": "aws_context_budget_usd",
                                                                 "value": 0}, token=None)[0] == 200
        hub.app.channels_check(force=True)
        assert wait(lambda: PD in (st._hub_aws.get("deny") or []), 15)
        assert not st.aws_ok(PD)[0] and st.aws_ok("FG1")[0] and "police" in st.aws_ok(PD)[1]
        assert hub.get("/api/cad/stt")["context_limit"] == {"value": 0.0, "default": 15.0, "edited": True}
        # the fire limit used up while police has its own: police keeps AWS (the reserve list), fire goes to whisper
        assert call(hub.url + "/api/cad/settings/stt", "POST", {"action": "set", "key": "aws_context_budget_usd",
                                                                 "value": 15}, token=None)[0] == 200
        assert call(hub.url + "/api/cad/settings/stt", "POST", {"action": "set", "key": "aws_monthly_limit_usd",
                                                                 "value": 0}, token=None)[0] == 200
        hub.app.channels_check(force=True)
        assert wait(lambda: not st._hub_aws["ok"] and PD in st._hub_aws["reserve"], 15)
        assert st.aws_ok(PD)[0] and not st.aws_ok("FG1")[0] and not st._hub_aws.get("deny")
        assert call(hub.url + "/api/cad/settings/stt", "POST", {"action": "set", "key": "aws_context_budget_usd",
                                                                 "value": -1}, token=None)[0] == 400
    finally:
        if st is not None:
            st.stop()
        hub.close()
        fake.close()


# -- the shed context clips and the Health hints ------------------------------------------------------------------


def test_shed_context_clips_are_counted_never_just_quiet():
    now = [1000.0]
    q = SttQueue(clock=lambda: now[0])
    q.put((CONTEXT, None, "pd-1", None, PD))
    q.put((CONTEXT, None, "pd-2", None, PD))
    now[0] += CONTEXT_MAX_WAIT_S + 1
    q.put((-2, None, "fire", None, "tg:307"))
    assert q.get(timeout=0)[2] == "fire"
    s = q.stats()
    assert s["shed"] == 2 and s["shed_context"] == 2 and s["shed_context_h"] == 2
    now[0] += 3700
    assert q.stats()["shed_context_h"] == 0 and q.stats()["shed_context"] == 2


def test_health_hints_for_shed_police_clips_and_the_police_budget():
    assert context_hints(None, None) == []
    h = context_hints({"shed_context_h": 3}, {"budget": {"context": {"ok": False, "usd": 15.01, "limit_usd": 15,
                                                                      "month": "2026-11"}}})
    assert [x["channel"] for x in h] == ["PD", "PD"]
    assert "3 context clips shed" in h[0]["text"] and "$15.01 / $15" in h[1]["text"] and "whisper" in h[1]["text"]
    assert context_hints({"shed_context_h": 0}, {"budget": {"context": {"ok": True}}}) == []


# -- the config, the PD audio budget, the cost tool ------------------------------------------------------------------


def test_config_defaults_police_on_aws_its_budget_and_30_days_of_pd_audio(tmp_path):
    from sizeup.config import ConfigError, load
    p = tmp_path / "c.toml"
    p.write_text("[stt]\n")
    c = load(p)
    assert PD in c.stt_aws_channels and c.stt_aws_context_limit_usd == 15.0 and c.stt_aws_limit_usd == 25.0
    # the context audio budget bounds S3 too (AudioDb.prune_clips): 30 days of PD alert audio at up to 4 h a day
    assert c.context_audio_max_mb * 1e6 >= 30 * 4 * 3600 * 16000 and c.tier_keep_days["alert"] == 30
    p.write_text("[stt]\naws_context_budget_usd = 40\n")
    assert load(p).stt_aws_context_limit_usd == 40.0
    p.write_text("[stt]\naws_context_budget_usd = -1\n")
    with pytest.raises(ConfigError, match="aws_context_budget_usd"):
        load(p)
    hub = load(stt_vocab.DATA.parent / "deploy" / "hub" / "config.hub.toml")
    assert PD in hub.stt_aws_channels and hub.stt_aws_context_limit_usd == 15.0


def test_stt_cost_reads_aggregates_only(tmp_path, capsys):
    from tools import stt_cost
    db = tmp_path / "cad.sqlite"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE radio_traffic (id TEXT, start REAL, end REAL, channel TEXT, talkgroup INTEGER, "
              "source TEXT, text TEXT, talker TEXT)")
    now = time.time()
    rows = [(f"pd{i}", now - 86400 + i * 60, now - 86400 + i * 60 + 6, "PD Dispatch", 9211, PD, "secret words",
             "Officer Example") for i in range(600)]
    rows += [(f"fg{i}", now - 86400 + i * 60, now - 86400 + i * 60 + 3, "FG1", None, "", "", "") for i in range(100)]
    c.executemany("INSERT INTO radio_traffic VALUES (?,?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()
    assert stt_cost.main([str(db), "--days", "1"]) == 0
    out = capsys.readouterr().out
    assert PD in out and "fm:FG1" in out and "secret" not in out and "Officer" not in out
    assert stt_cost.main([str(db), "--days", "1", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    pd = next(r for r in d["rows"] if r["source"] == PD)
    assert pd["min_day"] == pytest.approx(60, abs=1) and pd["aws_usd_month"] == pytest.approx(18, abs=0.5)
    assert pd["mb_day"] == pytest.approx(57.6, abs=1)
