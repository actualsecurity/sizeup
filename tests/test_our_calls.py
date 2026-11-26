"""Our calls = the channels and talkgroups with Main on (Settings > Radios > Channels), one switch each and nothing else:
an FM / airband channel's Main is the hub's setting radio_main (on by default, an airband channel's off), a talkgroup's
its own (sizeup/talkgroups.py). The hub decides it for every row it serves (`ours`), for the Radio page's tree, its Main
and its Our calls view, so a switch changed counts at once, for old rows too. Admins only, audited. Every channel,
talkgroup and time here is invented."""

import pytest

from sizeup.cad.api import needs
from tests.test_comms_views import MAIN, c, ids, other, q  # noqa: F401  (fixture)
from tests.test_radios import tx
from tests.test_settings import post


def served(c, tid):
    return next(r for r in q(c, view="all", limit=50)["rows"] if r["id"] == tid)


def test_the_defaults_fg_and_paging_and_main_talkgroups_are_ours_airband_is_not(c):
    assert c.fm_kinds() == {"FG1": "traffic", "South 2": "paging", "Tower": "airband"}
    assert (c.fm_main("FG1"), c.fm_main("South 2"), c.fm_main("Tower")) == (True, True, False)
    assert c.not_main() == ("Tower",)
    assert served(c, "fg-1")["ours"] == 1 and served(c, "twr-1")["ours"] == 0
    assert served(c, f"{MAIN}-1-call_1-t0")["ours"] == 1                   # a county talkgroup marked Main
    assert served(c, "555-1-call_2-t0")["ours"] == 0 and served(c, "pd-1")["ours"] == 0
    assert set(ids(q(c, view="ours"))) == {"fg-1", f"{MAIN}-1-call_1-t0"}
    tree = {s["key"]: s["ours"] for s in c.comms_sources()["sources"]}
    assert tree["fm:FG1"] is True and tree["air:Tower"] is False and tree[f"p25:westchester:{MAIN}"] is True


def test_main_off_takes_a_channel_out_of_our_calls_everywhere_at_once(c):
    out = post(c, "radio_main", action="set", key="FG1", main=False)
    assert out["main"] is False and c.fm_main("FG1") is False and "FG1" in c.not_main()
    assert served(c, "fg-1")["ours"] == 0                                    # an old row too: the switch as of now
    assert "fg-1" not in ids(q(c, view="ours"))
    assert {s["key"]: s["ours"] for s in c.comms_sources()["sources"]}["fm:FG1"] is False
    main, _ = c.comms.split([MAIN], c.not_main())                             # the old Radio view's Main
    assert "fg-1" not in {r["id"] for r in main}
    audit = [a for a in c.settings.audit_log("radios") if a["key"] == "radio_main/FG1"]
    assert audit and (audit[0]["old"], audit[0]["new"]) == ("Main", "Other")
    # on again: its default, so no choice is kept (and the state's rev goes back to "")
    post(c, "radio_main", action="set", key="FG1", main=True)
    assert c.fm_main_over == {} and served(c, "fg-1")["ours"] == 1 and c.state()["radio_main"] == ""
    # an airband channel switched on: ours from then
    post(c, "radio_main", action="set", key="Tower", main=True)
    assert c.fm_main("Tower") is True and "Tower" not in c.not_main() and served(c, "twr-1")["ours"] == 1


def test_nothing_changed_is_not_written_or_audited_and_bad_ones_are_refused(c):
    before = len(c.settings.audit_log("radios"))
    assert post(c, "radio_main", action="set", key="FG1", main=True) == {"ok": True, "main": True}
    assert len(c.settings.audit_log("radios")) == before
    for body in ({"action": "set", "key": "Nope", "main": False}, {"action": "set", "key": "FG1", "main": "no"},
                 {"action": "reset", "key": "FG1"}, {"action": "set", "key": ["FG1"], "main": False}):
        with pytest.raises(Exception):
            c.settings_post("radio_main", dict(body, operator="JD"))
    assert c.fm_main_over == {}


def test_only_admins_set_main():
    assert needs("/api/cad/settings/radio_main", {"action": "set", "key": "FG1", "main": False}) == "admin"


def test_a_station_stamp_is_never_what_counts(c):
    """A station from before this stamps `ours` when it hears a row (an FM channel always, a Main talkgroup): the hub
    serves its own answer, from the switches now."""
    c.record_traffic(dict(tx("fg-2", c.clockref.t - 1), ours=True))
    post(c, "radio_main", action="set", key="FG1", main=False)
    assert served(c, "fg-2")["ours"] == 0
    c.record_traffic(dict(other("pd-9", c.clockref.t - 1), ours=True))
    assert served(c, "pd-9")["ours"] == 0                                    # another system's: never ours
