"""Dispatch coverage lost is an urgent push (sizeup/cloud/health.py "Dispatch coverage", cover:<job>).

2026-11-24: the station lost the dongles of Fire/EMS 15 (the county P25 system, trunk-recorder), South 2 (the paging
backup) and three more for ~6 h. Health showed "SDR ... gone" and "trunk-recorder down", but the pushes were the usual
priority 4 ones (no way through Do Not Disturb) and the next word was "Still: ..." 3 h later. Now the county P25 job or
a paging channel not running for 5 min is one urgent push (ntfy 5), again every 30 min, and "All clear" once back; a
shorter dongle swap and the updater's switch push nothing; other channels are pushed as before, at once.

The station posts are tests/test_health.py's station_status(): the shape the station sends every 5 s (its radios, the
dongles of its "sdr" block, the health block with trunk-recorder and the jobs), with made-up serials and names. The
hub's clock is the rig's (no sleeping): a tick every TICK s, as HubApp's watcher."""
from types import SimpleNamespace

from sizeup.cloud import health as H
from sizeup.cloud.health import COVER_DOWN_S, COVER_REMIND_S, COVER_SETTLE_S, PUSH_RETRY_S, UPDATE_GRACE_S
from tests.test_health import HUB, T0, FakeNotifier, Rig, radio, station_status

TICK = 5
P25 = "Fire/EMS 15"
PAGING = [SimpleNamespace(label="South 2", kind="paging"), SimpleNamespace(label="FG1", kind="traffic")]


def p25_gone(**kw):
    """The P25 job's dongle unplugged: trunk-recorder down with it (the station's words for it)."""
    st = station_status(**kw)
    st["sdr"]["sdrs"][2]["present"] = False
    st["health"]["recorder"] = {"state": "down", "restarts": 6, "detail": "exited 1: no device", "expected": True}
    return st


def paging_no_audio(**kw):
    """South 2's dongle opens but streams nothing (#703/#710): out, its job retrying on it as the last resort."""
    st = station_status(**kw)
    st["sdr"]["sdrs"][0]["noaudio"] = {"since": T0, "job": "South 2"}
    st["health"]["jobs"]["South 2"] = {"state": "down", "restarts": 9, "detail": "rtl_fm stalled: no audio for 5 s",
                                       "expected": True}
    return st


def run(r, t0, t1, status):
    """A post and a tick every TICK s from t0 to t1 (seconds after T0), t1 included."""
    t = t0
    while t <= t1:
        r.at(t, status=status() if callable(status) else status)
        t += TICK


def titles(r):
    return [m["title"].split(" · ")[0] for m in r.n.msgs]


def urgent(r):
    """The urgent pushes (ntfy 5): our dispatch lost."""
    return [m["title"].split(" · ")[0] for m in r.n.msgs if m["priority"] == 5]


def test_fire_ems_15_down_4_min_is_no_urgent_push_5_min_one_then_every_30_min_and_all_clear():
    r = Rig(channels=PAGING)
    run(r, 0, 4 * 60, p25_gone)
    assert urgent(r) == []                                                # 4 min: no urgent push ...
    assert titles(r) == ["SDR 103 gone", "trunk-recorder down 2 min"]    # ... its causes pushed as always (P4)
    assert {m["priority"] for m in r.n.msgs} == {4}
    run(r, 4 * 60 + TICK, COVER_DOWN_S, p25_gone)
    m = r.n.msgs[-1]
    assert m["title"] == "Fire/EMS 15 down 5 min: SDR 103 gone · PCFD station" and m["priority"] == 5
    assert "rotating_light" in m["tags"] and "our dispatch not heard" in m["body"]
    (a,) = [a for a in r.h.alerts() if a["key"] == "cover:Fire/EMS 15"]
    assert a["severity"] == "critical" and a["since"] == T0 and a["title"] == "Fire/EMS 15 down: SDR 103 gone"
    run(r, COVER_DOWN_S + TICK, 66 * 60, p25_gone)
    assert urgent(r) == ["Fire/EMS 15 down 5 min: SDR 103 gone", "Still: Fire/EMS 15 down 35 min: SDR 103 gone",
                         "Still: Fire/EMS 15 down 65 min: SDR 103 gone"]
    assert len(r.n.msgs) == 5                                             # its causes: not reminded by themselves
    # the dongle back: "All clear" once it has run again 2 min straight
    run(r, 66 * 60 + TICK, 66 * 60 + COVER_SETTLE_S, station_status)
    assert len(r.n.msgs) == 5
    run(r, 66 * 60 + COVER_SETTLE_S + TICK, 66 * 60 + COVER_SETTLE_S + TICK, station_status)
    clear = r.n.msgs[5]
    assert clear["title"].startswith("All clear: Fire/EMS 15 back") and clear["priority"] == 4
    assert "2 reminder(s) sent" in clear["body"] and "(SDR 103 gone)" in clear["body"]
    assert "back at " + H._hhmm(T0 + 66 * 60 + TICK) in clear["body"]


def test_flapping_down_4_min_up_30_s_pushes():
    """The review's case: down 4 min, up 30 s, over and over for 2 h. Each spell alone is under 5 min, but 5 min in all
    within 10 min is lost: urgent, and no "All clear" while it keeps flapping (never 2 min up straight)."""
    r = Rig(channels=PAGING)
    t = 0
    while t < 2 * 3600:
        run(r, t, t + 240 - TICK, p25_gone)
        run(r, t + 240, t + 270 - TICK, station_status)
        t += 270
    u = urgent(r)
    assert u[0].startswith("Fire/EMS 15 down") and len(u) == 1 + (2 * 3600 - 330) // COVER_REMIND_S
    assert all(x.startswith("Still: Fire/EMS 15 down") for x in u[1:])
    assert not any(x.startswith("All clear") for x in titles(r))
    # a crash loop: South 2 down 50 s, up 10 s, for 1 h
    r = Rig(channels=PAGING)
    loop = station_status()
    loop["health"]["jobs"]["South 2"] = {"state": "down", "restarts": 30, "detail": "rtl_fm exited", "expected": True}
    for t in range(0, 3600, 60):
        run(r, t, t + 50 - TICK, loop)
        run(r, t + 50, t + 60 - TICK, station_status)
    assert urgent(r)[:1] and urgent(r)[0].startswith("South 2 down")
    assert len(urgent(r)) == 2                                            # (first, then once 30 min on)


def test_once_fired_a_short_up_never_says_all_clear():
    r = Rig(channels=PAGING)
    run(r, 0, COVER_DOWN_S, p25_gone)
    assert urgent(r) == ["Fire/EMS 15 down 5 min: SDR 103 gone"]
    t = COVER_DOWN_S + TICK
    while t < COVER_DOWN_S + 65 * 60:
        run(r, t, t + 30 - TICK, station_status)                         # up 30 s
        run(r, t + 30, t + 270 - TICK, p25_gone)                         # down 4 min
        t += 270
    assert not any(x.startswith("All clear") for x in titles(r))
    assert [x.split(":")[0] for x in urgent(r)] == ["Fire/EMS 15 down 5 min", "Still", "Still"]
    run(r, t, t + COVER_SETTLE_S + TICK, station_status)                 # a real recovery: 2 min straight
    assert titles(r)[-1] == "All clear: Fire/EMS 15 back"


def test_south_2_down_is_the_same_whatever_the_cause_paging_by_its_kind():
    # the paging channel by the config's kind ([radio.channels] kind = "paging")
    r = Rig(channels=PAGING)
    run(r, 0, 4 * 60, paging_no_audio)
    assert urgent(r) == [] and "noaudio:00000101" in r.keys()
    run(r, 4 * 60 + TICK, 36 * 60, paging_no_audio)
    assert urgent(r) == ["South 2 down 5 min: SDR 101 no audio", "Still: South 2 down 35 min: SDR 101 no audio"]
    run(r, 36 * 60 + TICK, 36 * 60 + COVER_SETTLE_S + TICK, station_status)
    assert titles(r)[-1] == "All clear: South 2 back"
    # ... or by the station's row (no channels in the hub's config): its dongle gone, the 5 min and the 30 min the same
    r = Rig()

    def gone():
        st = station_status(radios=[radio("South 2", 470.2, channel_kind="paging"), radio("FG1"), radio(P25, None)])
        st["sdr"]["sdrs"][0]["present"] = False
        return st
    run(r, 0, COVER_DOWN_S - TICK, gone)
    assert urgent(r) == []
    run(r, COVER_DOWN_S, COVER_DOWN_S + COVER_REMIND_S, gone)
    assert urgent(r) == ["South 2 down 5 min: SDR 101 gone", "Still: South 2 down 35 min: SDR 101 gone"]


def test_trunk_recorder_down_alone_is_the_cause_and_a_stand_in_keeps_the_coverage():
    r = Rig(channels=PAGING)
    st = station_status()
    st["health"]["recorder"] = {"state": "down", "restarts": 3, "detail": "exited 1", "expected": True}
    run(r, 0, COVER_DOWN_S, st)
    assert titles(r) == ["trunk-recorder down 2 min", "Fire/EMS 15 down 5 min: trunk-recorder down"]
    # South 2's dongle gone, its job on a stand-in (an antenna group's fallback): covered, so the usual push at once
    r = Rig(channels=PAGING)
    st = station_status()
    st["sdr"]["sdrs"][0]["present"] = False
    st["sdr"]["sdrs"][1].update(mode="South 2", own="FG1", group="UHF")
    st["sdr"]["sdrs"][0]["on"] = "00000102"
    st["health"]["jobs"]["South 2"] = {"state": "ok", "restarts": 0, "expected": True}
    run(r, 0, 10 * 60, st)
    assert titles(r) == ["SDR 101 gone"] and r.n.msgs[0]["priority"] == 4
    assert not any(k.startswith("cover:") for k in r.keys())


def test_a_dongle_swap_under_5_min_is_no_urgent_push():
    r = Rig(channels=PAGING)
    run(r, 0, 4 * 60, p25_gone)
    run(r, 4 * 60 + TICK, 40 * 60, station_status)
    assert urgent(r) == [] and titles(r)[:2] == ["SDR 103 gone", "trunk-recorder down 2 min"]


def test_a_hub_restart_4_min_in_still_pushes_at_5(tmp_path):
    from sizeup.cloud.hubstore import MonitorDb
    db = MonitorDb(tmp_path / "hub.sqlite")
    r = Rig(db, channels=PAGING)
    run(r, 0, 4 * 60, p25_gone)
    r2 = Rig(db, channels=PAGING)                                         # the redeployed hub, its clock kept
    run(r2, 4 * 60 + TICK, COVER_DOWN_S, p25_gone)
    assert urgent(r2) == ["Fire/EMS 15 down 5 min: SDR 103 gone"]


def ver(update):
    return {"build": HUB, "hub": HUB, "update": update}


def restarting(**kw):
    """The station's radios while sizeup restarts (a switch, a reboot): starting, trunk-recorder too."""
    st = station_status(**kw)
    st["health"]["jobs"] = {j: {"state": "starting", "restarts": 0, "expected": True} for j in ("South 2", "FG1")}
    st["health"]["recorder"] = {"state": "starting", "restarts": 0, "detail": "", "expected": True}
    return st


def test_the_updaters_switch_is_no_push_and_its_5_min_start_after_it():
    """Only the switch (the updater's phase "switch": sizeup and its radios restart) and 5 min after it ended hold the
    alert, 10 min at most; its 5 min start after that."""
    r = Rig(channels=PAGING)
    switching = {"state": "updating", "phase": "switch", "to": HUB, "since": T0, "from": "release-a"}
    run(r, 0, 4 * 60, lambda: p25_gone(version=ver(switching)))            # 4 min into the switch: no urgent
    assert urgent(r) == [] and "cover:Fire/EMS 15" not in r.keys()
    # the switch ended at T0 + 100 s (the attempt began with its build 5 min before it)
    done = {"state": "current", "since": T0 + 100,
            "last": {"at": T0 - 300, "staged_s": 240, "waited_s": 60, "switch_at": T0, "seconds": 400, "result": "ok"}}
    run(r, 4 * 60 + TICK, 400 - TICK, lambda: p25_gone(version=ver(done)))   # 5 min after it ended: nothing
    assert urgent(r) == []
    run(r, 400, 400 + COVER_DOWN_S - TICK, lambda: p25_gone(version=ver(done)))   # still down: 5 min from then
    assert urgent(r) == []
    run(r, 400 + COVER_DOWN_S, 400 + COVER_DOWN_S, lambda: p25_gone(version=ver(done)))
    assert urgent(r) == ["Fire/EMS 15 down 5 min: SDR 103 gone"]
    # a switch whose radios restart and come back in its window: nothing at all
    r = Rig(channels=PAGING)
    run(r, 0, 50, lambda: restarting(version=ver(switching)))
    run(r, 50 + TICK, 40 * 60, lambda: talking(r, version=ver(done)))
    assert r.n.msgs == []


def test_the_hold_is_10_min_at_most_however_long_the_switch_says_it_takes():
    r = Rig(channels=PAGING)
    switching = {"state": "updating", "phase": "switch", "to": HUB, "since": T0}
    run(r, 0, H.SWITCH_HOLD_MAX_S + COVER_DOWN_S - TICK, lambda: p25_gone(version=ver(switching)))
    assert urgent(r) == []
    run(r, H.SWITCH_HOLD_MAX_S + COVER_DOWN_S, H.SWITCH_HOLD_MAX_S + COVER_DOWN_S, lambda: p25_gone(version=ver(switching)))
    assert urgent(r) == ["Fire/EMS 15 down 5 min: SDR 103 gone"]
    # an older station's record (no switch_at): its switch from the build and the wait, 5 min after it ended
    r = Rig(channels=PAGING)
    old = {"state": "current", "since": T0, "last": {"at": T0 - 400, "staged_s": 300, "waited_s": 60, "seconds": 400,
                                                      "result": "ok"}}
    run(r, 0, UPDATE_GRACE_S - TICK, lambda: p25_gone(version=ver(old)))
    assert "cover:Fire/EMS 15" not in r.keys()
    run(r, UPDATE_GRACE_S, UPDATE_GRACE_S + COVER_DOWN_S, lambda: p25_gone(version=ver(old)))
    assert urgent(r) == ["Fire/EMS 15 down 5 min: SDR 103 gone"]


def test_an_outage_during_the_build_or_the_staged_wait_pushes_at_5_min():
    """The build (and the wait for a quiet moment) leave the radios running: an outage then is one like any other."""
    for upd in ({"state": "updating", "phase": "build", "to": HUB, "since": T0},
                {"state": "updating", "to": HUB, "since": T0},                       # an older station: no phase
                {"state": "deferred", "to": HUB, "since": T0, "hold": {"phase": "settle", "until": T0 + 3600}}):
        r = Rig(channels=PAGING)
        run(r, 0, COVER_DOWN_S - TICK, lambda: p25_gone(version=ver(upd)))
        assert urgent(r) == []
        run(r, COVER_DOWN_S, COVER_DOWN_S, lambda: p25_gone(version=ver(upd)))
        assert urgent(r) == ["Fire/EMS 15 down 5 min: SDR 103 gone"], upd


def talking(r, **kw):
    """The station posting, the county system's calls with it (no "P25 trunk silent")."""
    return station_status(radios=[radio("South 2", 470.2), radio("FG1"), radio(P25, None, last_heard=r.hub.now)], **kw)


def test_the_station_silent_is_one_urgent_push_for_its_dispatch_and_station_silent_is_not_pushed_itself():
    r = Rig(channels=PAGING)
    r.at(0, status=station_status())
    for t in range(TICK, 4 * 60 + TICK, TICK):
        r.at(t, post=False)
    assert "silent" in r.keys() and r.n.msgs == []                       # "Station silent" on Health, not pushed
    for t in range(4 * 60 + TICK, int(COVER_DOWN_S + TICK), TICK):
        r.at(t, post=False)
    (m,) = r.n.msgs
    assert m["title"].startswith("Fire/EMS 15, South 2 down 5 min: station silent") and m["priority"] == 5
    for t in range(int(COVER_DOWN_S + TICK), int(COVER_DOWN_S + COVER_REMIND_S + TICK), TICK):
        r.at(t, post=False)
    assert titles(r) == ["Fire/EMS 15, South 2 down 5 min: station silent",
                         "Still: Fire/EMS 15, South 2 down 35 min: station silent"]
    back = COVER_DOWN_S + COVER_REMIND_S + TICK
    run(r, back, back + COVER_SETTLE_S + TICK, lambda: talking(r))
    assert titles(r)[2:] == ["All clear: station back"] and r.n.msgs[2]["priority"] == 4
    run(r, back + COVER_SETTLE_S + 2 * TICK, back + 3600, lambda: talking(r))
    assert len(r.n.msgs) == 3                                            # nor "Recovered: Station silent"


def test_a_10_min_reboot_is_one_urgent_push_and_one_all_clear_nothing_else():
    """The review's case: back from a reboot, trunk-recorder and the jobs "starting" for a while: not our dispatch lost
    (they are given JOB_GRACE_S); only the jobs already lost before the silence carry on from then."""
    r = Rig(channels=PAGING)
    run(r, 0, 60, lambda: talking(r))
    for t in range(60 + TICK, 660, TICK):                               # 10 min silent
        r.at(t, post=False)
    run(r, 660, 660 + 50, lambda: restarting(radios=talking(r)["radios"]))   # back: starting for 50 s
    run(r, 660 + 55, 660 + 3600, lambda: talking(r))
    assert titles(r) == ["Fire/EMS 15, South 2 down 5 min: station silent", "All clear: station back"]


def test_the_station_back_with_fire_ems_15_still_down_carries_on_at_once():
    r = Rig(channels=PAGING)
    r.at(0, status=p25_gone())
    for t in range(TICK, 10 * 60, TICK):                                  # the dongle and then the station gone
        r.at(t, post=False)
    assert urgent(r) == ["Fire/EMS 15, South 2 down 5 min: station silent"]
    run(r, 10 * 60, 10 * 60, p25_gone)                                    # back, its P25 dongle still gone
    assert urgent(r)[1:] == ["Fire/EMS 15 down 10 min: SDR 103 gone"]   # lost before the silence: pushed at once
    run(r, 10 * 60 + TICK, 10 * 60 + COVER_SETTLE_S + TICK, p25_gone)
    assert titles(r)[-1] == "All clear: station back"


def test_a_station_silent_through_its_switch_is_counted_after_it():
    r = Rig(channels=PAGING)
    switching = {"state": "updating", "phase": "switch", "to": HUB, "since": T0}
    r.at(0, status=station_status(version=ver(switching)))               # its last post before the restart
    for t in range(TICK, int(H.SWITCH_HOLD_MAX_S + COVER_DOWN_S), TICK):
        r.at(t, post=False)
    assert r.n.msgs == []
    r.at(H.SWITCH_HOLD_MAX_S + COVER_DOWN_S, post=False)
    assert titles(r) == ["Fire/EMS 15, South 2 down 5 min: station silent"]


def test_p25_trunk_silent_is_the_county_systems_coverage_lost(monkeypatch):
    from tests.test_trunk_silence import DAY, p25
    monkeypatch.setattr(H, "_part", lambda t: DAY)                       # the day's window (20 min)
    r = Rig(channels=PAGING)
    r.at(0, status=p25(0))
    for t in range(60, 21 * 60, 60):
        r.at(t, status=p25(0))
    assert r.n.msgs == []
    r.at(21 * 60, status=p25(0))                                           # no call 21 min: "P25 trunk silent"
    assert {"trunk", "cover:Fire/EMS 15"} <= set(r.keys())
    assert titles(r) == ["Fire/EMS 15 down 21 min: no P25 call since " + H._hhmm(T0)]   # not "P25 trunk silent" too
    assert r.n.msgs[0]["priority"] == 5
    for t in range(22 * 60, 52 * 60, 60):
        r.at(t, status=p25(0))
    assert len(r.n.msgs) == 2 and r.n.msgs[1]["title"].startswith("Still: Fire/EMS 15 down 51 min")
    r.at(52 * 60, status=p25(52 * 60 - 5))                                # a call
    r.at(52 * 60 + COVER_SETTLE_S, status=p25(52 * 60 + COVER_SETTLE_S - 5))
    assert titles(r)[-1] == "All clear: Fire/EMS 15 back"
    # the control channel lost: counted from when it was
    healthy = {"rate": 40.0, "low_since": None, "low_s": 0, "silent_since": None, "silent_s": 0}
    r = Rig(channels=PAGING)
    for t in range(0, 6 * 60, TICK):
        cc = dict(healthy, rate=None, silent_since=T0, silent_s=t) if t else healthy
        r.at(t, status=p25(t - 1, cc=cc))
    assert titles(r) == ["Fire/EMS 15 down 5 min: P25 control channel not decoded"]


def test_a_dongle_held_by_shortfinal_says_so():
    r = Rig(channels=PAGING)
    st = station_status()
    st["sdr"]["sdrs"][0]["held_by"] = {"app": "shortfinal", "mode": "FG1"}
    st["health"]["jobs"]["South 2"] = {"state": "held", "restarts": 0, "detail": "held by shortfinal", "expected": True}
    run(r, 0, COVER_DOWN_S, st)
    assert urgent(r) == ["South 2 down 5 min: SDR held by shortfinal"]


def off_status(job_serial="00000103", by="person", **kw):
    st = station_status(**kw)
    d = next(d for d in st["sdr"]["sdrs"] if d["serial"] == job_serial)
    d["mode"] = "Off"
    if by:
        d["set_by"] = by
    if d["serial"] == "00000103":
        st["health"]["recorder"] = {"state": "off", "restarts": 0, "detail": "", "expected": False}
    else:
        st["health"]["jobs"].pop("South 2", None)
    return st


def test_fire_ems_15_or_paging_switched_off_is_one_push_urgent_if_auto_did_it():
    r = Rig(channels=PAGING)
    run(r, 0, 3 * 3600 + 60, off_status)
    (a,) = [a for a in r.h.alerts() if a["key"] == "off:Fire/EMS 15"]
    assert a["title"] == "Fire/EMS 15 Off (set by person)"
    assert titles(r) == ["Fire/EMS 15 Off (set by person) 60 s"] and r.n.msgs[0]["priority"] == 4   # once, no reminders
    # auto set South 2 Off (a lock, a fallback): our dispatch lost, urgent
    r = Rig(channels=PAGING)
    run(r, 0, 31 * 60 + 60, lambda: off_status("00000101", "auto"))
    assert urgent(r) == ["South 2 down 60 s: set Off by auto", "Still: South 2 down 31 min: set Off by auto"]
    # a swap that left the old dongle removed and the new one Off
    r = Rig(channels=PAGING)
    st = station_status()
    st["sdr"]["sdrs"][0] = {"serial": "00000104", "name": "104", "mode": "Off", "present": True}
    st["health"]["jobs"].pop("South 2", None)
    run(r, 0, 120, st)
    assert titles(r) == ["South 2 Off (no dongle runs it) 60 s"]


def test_other_channels_are_pushed_at_once_at_the_usual_priority_and_reminded_every_3_h():
    r = Rig(channels=PAGING)
    st = station_status()
    st["sdr"]["sdrs"][1]["present"] = False                               # FG1's dongle
    r.at(0, status=st)
    assert titles(r) == ["SDR 102 gone"] and r.n.msgs[0]["priority"] == 4   # the first push at once, not at 3 h
    for t in range(60, 3 * 3600 + 60, 60):
        r.at(t, status=st)
    sdr = [m for m in r.n.msgs if "SDR 102" in m["title"]]                # (the fixture's P25 system hears no call)
    assert [m["title"].split(" · ")[0] for m in sdr] == ["SDR 102 gone", "Still: SDR 102 gone 3 h 0 min"]
    assert [m["priority"] for m in sdr] == [4, 4]
    assert "cover:FG1" not in r.keys()


def test_mute_still_works():
    r = Rig(channels=PAGING)
    r.h.mutes = lambda: {"cover": {"until": T0 + 24 * 3600}}
    run(r, 0, 70 * 60, p25_gone)
    assert urgent(r) == []                                                # muted: no urgent ...
    assert titles(r) == ["SDR 103 gone", "trunk-recorder down 2 min"]    # ... its causes as before
    (a,) = [a for a in r.h.alerts() if a["key"] == "cover:Fire/EMS 15"]
    assert a["muted"] is True and "cover" in H.MUTE_KINDS and "cover" in H.MUTE_TIMED_ONLY
    # muting "SDR gone" or "P25 trunk silent" doesn't hide our dispatch (the mute list's hover says so)
    r = Rig(channels=PAGING)
    r.h.mutes = lambda: {"sdr": {"until": None}, "recorder": {"until": None}, "trunk": {"until": None}}
    run(r, 0, COVER_DOWN_S, p25_gone)
    assert titles(r) == ["Fire/EMS 15 down 5 min: SDR 103 gone"]
    assert "mute this" in H.MUTE_HINTS["cover"] and "Dispatch coverage" in H.MUTE_HINTS["trunk"]
    # a "Station silent" mute (one set before the station's dispatch lost was its own alert) mutes that too
    r = Rig(channels=PAGING)
    r.h.mutes = lambda: {"silent": {"until": None}}
    r.at(0, status=station_status())
    for t in range(TICK, 3600, TICK):
        r.at(t, post=False)
    assert r.n.msgs == [] and r.h.muted("cover:station")
    (a,) = [a for a in r.h.alerts() if a["key"] == "cover:station"]
    assert a["muted"] is True


class FlakyNotifier(FakeNotifier):
    """ntfy refusing pushes (the Notifier gave up after its retries): the outcome through done, at once or later."""

    def __init__(self, fail=1, later=False):
        super().__init__()
        self.fail, self.tried, self.later, self.waiting, self.tries = fail, 0, later, [], []

    def send(self, title, body, priority=3, tags=(), click="", who=None, done=None):
        self.tried += 1
        self.tries.append((title, priority))
        if self.tried <= self.fail if isinstance(self.fail, int) else self.fail(title):
            out = {"ok": False, "at": 0.0, "status": 0, "error": "connection refused"}
            self.waiting.append(lambda: done(out)) if self.later else done(out)
            return True
        super().send(title, body, priority, tags, click, who)
        done({"ok": True, "at": 0.0, "status": 200, "id": "x"})
        return True


def test_a_first_push_ntfy_did_not_take_is_tried_again_as_a_first_push_not_a_reminder_3_h_on(tmp_path):
    r = Rig(channels=PAGING)
    r.h.notifier = r.n = FlakyNotifier()
    st = station_status()
    st["sdr"]["sdrs"][1]["present"] = False
    run(r, 0, PUSH_RETRY_S - TICK, st)
    assert r.n.tried == 1 and r.n.msgs == []
    run(r, PUSH_RETRY_S, PUSH_RETRY_S, st)
    assert titles(r) == ["SDR 102 gone 60 s"] and r.n.tried == 2
    assert any("not delivered" in m for m in r.logs)
    # ... and an urgent one the same; never more than PUSH_RETRIES tries of one alert
    r = Rig(channels=PAGING)
    r.h.notifier = r.n = FlakyNotifier(fail=100)
    run(r, 0, COVER_DOWN_S + 10 * PUSH_RETRY_S, p25_gone)
    assert sum(1 for _t, p in r.n.tries if p == 5) == 1 + H.PUSH_RETRIES
    # the retry is kept (hub.sqlite): a takeover in between still tries again
    from sizeup.cloud.hubstore import MonitorDb
    db = MonitorDb(tmp_path / "hub.sqlite")
    r = Rig(db, channels=PAGING)
    r.h.notifier = r.n = FlakyNotifier()
    r.at(0, status=st)
    r2 = Rig(db, channels=PAGING)
    run(r2, PUSH_RETRY_S, PUSH_RETRY_S, st)
    assert titles(r2) == ["SDR 102 gone 60 s"]


def test_a_push_that_failed_after_its_alert_cleared_has_no_recovery_and_a_recovery_is_retried():
    r = Rig(channels=PAGING)
    r.h.notifier = r.n = FlakyNotifier(later=True)
    st = station_status()
    st["sdr"]["sdrs"][1]["present"] = False
    r.at(0, status=st)
    run(r, TICK, 30, station_status)                                     # back before ntfy's answer
    r.n.waiting.pop()()                                                   # "not delivered"
    run(r, 30 + TICK, 30 + H.SETTLE_S + 60, station_status)
    assert r.n.msgs == []                                                 # never told: no "Recovered" either
    # an "All clear" ntfy didn't take: tried again like a first push
    r = Rig(channels=PAGING)
    r.h.notifier = r.n = FlakyNotifier(fail=lambda title: title.startswith("All clear") and r.n.tried < 99
                                       and sum(1 for t, _p in r.n.tries if t.startswith("All clear")) == 1)
    run(r, 0, COVER_DOWN_S, p25_gone)
    run(r, COVER_DOWN_S + TICK, COVER_DOWN_S + COVER_SETTLE_S + TICK + PUSH_RETRY_S + TICK, station_status)
    assert titles(r).count("All clear: Fire/EMS 15 back") == 1
    assert sum(1 for t, _p in r.n.tries if t.startswith("All clear")) == 2


def test_a_push_not_even_queued_is_tried_again_too():
    class FullQueue(FakeNotifier):
        def __init__(self):
            super().__init__()
            self.tried = 0

        def send(self, title, body, priority=3, tags=(), click="", who=None):
            self.tried += 1
            return False if self.tried == 1 else super().send(title, body, priority, tags, click, who)
    r = Rig(channels=PAGING)
    r.h.notifier = r.n = FullQueue()
    run(r, 0, COVER_DOWN_S, p25_gone)
    assert titles(r)[0] == "SDR 103 gone 60 s" and urgent(r) == ["Fire/EMS 15 down 5 min: SDR 103 gone"]
