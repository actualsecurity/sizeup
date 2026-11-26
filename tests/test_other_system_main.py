"""Main on another P25 system's talkgroup (Settings > Radios): Our calls only.

For another system (MRRS and the like) the Main switch means ONLY "part of Our calls": the Radio page's Our calls view,
Listening's Our calls and its "Our calls only" toggle (the hub's `ours`, Cad.ours_tg). The county's Main (`main`) keeps
driving speech-to-text class and priority, automode, the dispatch-coverage alerts, the engine migration's context test
and the incident attach; another system's Main touches none of them. Encrypted and Activity-only talkgroups keep Main
disabled. No radios, no network; every talkgroup number here is invented (or an MRRS one other tests already use).
"""

from __future__ import annotations

from sizeup import sourcekeys
from sizeup.cad.api import DOES, needs
from sizeup.stt import LIVE
from sizeup.talkgroups import TalkgroupPrefs
from sizeup.incidents import Traffic
from tests.test_comms_views import PD, c, ids, other, q  # noqa: F401 (fixture)
from tests.test_p25_systems import MRRS, PD as MRRS_PD, PD_SECURE, prefs, two  # noqa: F401 (fixture)

ELSEWHERE = "metro"


def test_the_two_meanings_are_two_sets(tmp_path):
    p = prefs(tmp_path)
    assert p.ours(307) and p.main(307) and p.ours_others() == []
    p.set(MRRS_PD, main=True, system="mrrs")
    assert p.ours(MRRS_PD, "mrrs") and not p.main(MRRS_PD, "mrrs") and p.ours_others() == [("mrrs", MRRS_PD)]
    assert p.main(307) and p.ours(307)                              # the county's: unchanged
    assert not p.ours(307, "mrrs")                                  # the same number on another system is not 307
    assert prefs(tmp_path).ours_others() == [("mrrs", MRRS_PD)]     # kept across a restart
    p.set(MRRS_PD, main=False, system="mrrs")
    assert p.ours_others() == []
    p.set(PD_SECURE, main=True, system="mrrs")                      # encrypted: never
    assert not p.ours(PD_SECURE, "mrrs") and p.ours_others() == []
    p.set(307, main=False)
    assert not p.main(307) and not p.ours(307)


def test_the_stored_main_of_a_talkgroup_that_became_activity_only_is_not_ours(tmp_path):
    p = prefs(tmp_path)
    p.set(MRRS_PD, main=True, system="mrrs")
    assert p.set_tier(MRRS_PD, "off", "mrrs") is None and p.ours(MRRS_PD, "mrrs")
    assert p.sealed(PD_SECURE, "mrrs") and not p.sealed(MRRS_PD, "mrrs")


def test_the_station_refuses_main_on_an_encrypted_talkgroup_and_changes_nothing_else(two):
    app, _said, _logs = two
    row = lambda tg: next(x for x in app.talkgroups_state() if x["source"] == f"p25:mrrs:{tg}")   # noqa: E731
    before = {k: v for k, v in row(MRRS_PD).items() if k not in ("ours",)}
    assert app.set_talkgroup(MRRS_PD, None, True, None, "mrrs") is None
    after = row(MRRS_PD)
    assert after["ours"] is True and {k: v for k, v in after.items() if k != "ours"} == before   # main, tier, record: as were
    assert "never Our calls" in app.set_talkgroup(PD_SECURE, None, True, None, "mrrs") and not row(PD_SECURE)["ours"]
    assert app.tg.ours_others() == [("mrrs", MRRS_PD)]
    assert app.cad.ours_tg(MRRS_PD, "mrrs") is True and app.cad.ours_tg(MRRS_PD, "other") is False


def test_no_stt_class_no_coverage_no_automode_no_engine_context_change(two):
    app, _said, _logs = two
    item = Traffic("9211-1-call_1-t0", "PD Dispatch", 0.0, 1.0, "", talkgroup=MRRS_PD)
    key = f"p25:mrrs:{MRRS_PD}"
    county = Traffic("307-1-call_1-t0", "Fire/EMS 15", 0.0, 1.0, "", talkgroup=307)
    seen = lambda: (app._stt_class(item, key), app._stt_class(county, "tg:307"), app.context_tg(key),   # noqa: E731
                    app.context_tg("tg:307"), app.tg.heard(), app.cad.main_tg(307), app.cad.main_tg(MRRS_PD),
                    app.tg.main(MRRS_PD, "mrrs"), sorted(app.cad.s.incident_talkgroups))
    off = seen()
    assert app.set_talkgroup(MRRS_PD, None, True, None, "mrrs") is None
    assert app.tg.ours(MRRS_PD, "mrrs")
    assert seen() == off                                            # class, priority, engine context, heard (coverage)
    assert off[2] is True and off[0] != LIVE                        # (still other-system context, queued by its tier)
    assert off[5] is True and off[6] is False                       # the county's Main reader: 307 yes, 9211 never
    # the coverage alert reads the county's heard talkgroups only
    assert all(k.startswith("tg:") for k in app.tg.heard(extra=[MRRS_PD]))
    # the county's Main is as before
    app.set_talkgroup(307, None, False)
    assert app.context_tg("tg:307") is True


def test_the_county_main_is_unchanged(c):  # noqa: F811
    assert ids(q(c, view="ours")) == ["307-1-call_1-t0", "fg-1"]
    c.ours_others = lambda: [(ELSEWHERE, PD)]
    assert c.main_tg(307) and not c.main_tg(PD)                     # the county reader never sees it
    assert c.ours_tg(307) and c.ours_tg(PD, ELSEWHERE) and not c.ours_tg(PD) and not c.ours_tg(PD, "other")


def test_an_other_systems_main_is_in_the_view_the_rows_and_the_tree(c):  # noqa: F811
    t = c.clockref.t
    c.record_traffic(other("clash-1", t - 1, 307, "Clash", "police", "Police"))      # another system, our Main's number
    assert ids(q(c, view="ours")) == ["307-1-call_1-t0", "fg-1"]
    c.ours_others = lambda: [(ELSEWHERE, PD)]
    out = q(c, view="ours")
    assert ids(out) == ["pd-2", "pd-1", "307-1-call_1-t0", "fg-1"] and all(r["main"] and r["ours"] for r in out["rows"])
    assert "clash-1" not in ids(out)                                # the number alone is never read
    rows = {r["id"]: r for r in q(c, view="all")["rows"]}
    assert rows["pd-1"]["main"] and rows["pd-1"]["ours"] == 1 and not rows["tr-1"]["main"] and rows["tr-1"]["ours"] == 0
    assert not rows["clash-1"]["main"]
    old = c.comms_query({})                                         # the Radio page's Main section too
    assert {r["id"] for r in old["rows"] if r["main"]} == {"pd-1", "pd-2", "307-1-call_1-t0", "fg-1"}
    assert old["counts"]["main"] == 4
    tree = {s["key"]: s for s in c.comms_sources()["sources"]}
    assert tree[f"p25:{ELSEWHERE}:{PD}"]["ours"] is True and tree[f"p25:{ELSEWHERE}:9002"]["ours"] is False
    assert tree["p25:" + sourcekeys.COUNTY + ":307"]["ours"] is True
    # a muted source of Our calls is never left out of it
    assert "pd-1" in ids(q(c, view="all", mute=f"p25:{ELSEWHERE}:{PD}"))
    # switched off again: gone from the view, and the tree's rev moved (the rail asks again)
    rev = c.comms_sources()["rev"]
    c.ours_others = lambda: []
    assert ids(q(c, view="ours")) == ["307-1-call_1-t0", "fg-1"] and c.comms_sources()["rev"] != rev


def test_listening_reads_the_row_and_source_ours_flags(c):  # noqa: F811
    """Listening's Our calls and "Our calls only" are the hub's `ours` on each row and source (live-player.js isOurs /
    the Our calls view's `ours` filter), so a row of the Main talkgroup says ours now, and a switch changed since counts."""
    c.ours_others = lambda: [(ELSEWHERE, PD)]
    row = next(r for r in c.comms_query({"view": ["all"]})["rows"] if r["id"] == "pd-1")
    assert row["ours"] == 1
    c.ours_others = lambda: []
    row = next(r for r in c.comms_query({"view": ["all"]})["rows"] if r["id"] == "pd-1")
    assert row["ours"] == 0


def test_admin_only_and_audited():
    assert needs("/api/talkgroup", {"source": f"p25:mrrs:{MRRS_PD}", "main": True}) == "admin"
    said = next(f for rx, f in DOES if rx.pattern == "/api/talkgroup")
    assert said(None, {"source": f"p25:mrrs:{MRRS_PD}", "main": True}, None, None)[0] == f"Talkgroup p25:mrrs:{MRRS_PD}: main on"
    assert said(None, {"source": f"p25:mrrs:{MRRS_PD}", "main": False}, None, None)[0] == f"Talkgroup p25:mrrs:{MRRS_PD}: main off"


def test_a_hub_reads_the_stations_ours_rows_not_its_main(tmp_path):
    from tests.test_channels_by_system import tg_row
    from tests.test_stt_engine_select import AWS_TEXT, FakeTranscribe, _hub
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = _hub(tmp_path, fake)
    try:
        mine = dict(tg_row(MRRS_PD, "PD Dispatch", "Police", "mrrs", tier="alert"), ours=True, tier_set=True)
        enc = dict(tg_row(PD_SECURE, "PD Secure", "Police", "mrrs", tier="activity", encrypted=True), tier_set=True)
        county = dict(tg_row(307, "Fire/EMS 15", "Fire/EMS", "westchester", main=True), tier_set=False)
        hub.app.hub.talkgroups = [mine, enc, county]
        assert hub.app.cad.ours_others() == [("mrrs", MRRS_PD)]
        assert hub.app.cad.ours_tg(MRRS_PD, "mrrs") and not hub.app.cad.ours_tg(MRRS_PD)
        assert hub.app.context_tg(f"p25:mrrs:{MRRS_PD}") is True       # the engine migration's test: still context
        assert not hub.app.cad.main_tg(MRRS_PD)
    finally:
        hub.close()
        fake.close()
