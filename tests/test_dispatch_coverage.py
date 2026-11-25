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


def test_fire_ems_15_down_4_min_is_no_push_5_min_one_urgent_push_then_every_30_min_and_all_clear():
    r = Rig(channels=PAGING)
    run(r, 0, 4 * 60, p25_gone)
    assert r.n.msgs == []                                                 # 4 min: nothing pushed, not even "SDR gone"
    assert {"sdr:00000103", "recorder"} <= set(r.keys())                  # ... though Health shows them
    run(r, 4 * 60 + TICK, COVER_DOWN_S, p25_gone)
    (m,) = r.n.msgs
    assert m["title"] == "Fire/EMS 15 down 5 min: SDR 103 gone · PCFD station" and m["priority"] == 5
    assert "rotating_light" in m["tags"] and "our dispatch not heard" in m["body"]
    (a,) = [a for a in r.h.alerts() if a["key"] == "cover:Fire/EMS 15"]
    assert a["severity"] == "critical" and a["since"] == T0 and a["title"] == "Fire/EMS 15 down: SDR 103 gone"
    run(r, COVER_DOWN_S + TICK, 66 * 60, p25_gone)
    assert titles(r) == ["Fire/EMS 15 down 5 min: SDR 103 gone", "Still: Fire/EMS 15 down 35 min: SDR 103 gone",
                         "Still: Fire/EMS 15 down 65 min: SDR 103 gone"]
    assert [m["priority"] for m in r.n.msgs] == [5, 5, 5]
    # the dongle back: "All clear" once it has stayed back, and nothing for the causes (never pushed themselves)
    run(r, 66 * 60 + TICK, 66 * 60 + COVER_SETTLE_S + TICK, station_status)
    assert titles(r)[3:] == ["All clear: Fire/EMS 15 back"]
    assert r.n.msgs[3]["priority"] == 4 and "2 reminder(s) sent" in r.n.msgs[3]["body"]
    assert "(SDR 103 gone)" in r.n.msgs[3]["body"]
    run(r, 66 * 60 + COVER_SETTLE_S + 2 * TICK, 90 * 60, station_status)
    assert len(r.n.msgs) == 4 and r.keys() == []


def test_south_2_down_is_the_same_whatever_the_cause_paging_by_its_kind():
    # the paging channel by the config's kind ([radio.channels] kind = "paging")
    r = Rig(channels=PAGING)
    run(r, 0, 4 * 60, paging_no_audio)
    assert r.n.msgs == [] and "noaudio:00000101" in r.keys()
    run(r, 4 * 60 + TICK, 36 * 60, paging_no_audio)
    assert titles(r) == ["South 2 down 5 min: SDR 101 no audio", "Still: South 2 down 35 min: SDR 101 no audio"]
    assert [m["priority"] for m in r.n.msgs] == [5, 5]
    run(r, 36 * 60 + TICK, 36 * 60 + COVER_SETTLE_S + TICK, station_status)
    assert titles(r)[-1] == "All clear: South 2 back"
    # ... or by the station's row (no channels in the hub's config): its dongle gone, the 5 min and the 30 min the same
    r = Rig()

    def gone():
        st = station_status(radios=[radio("South 2", 470.2, channel_kind="paging"), radio("FG1"), radio(P25, None)])
        st["sdr"]["sdrs"][0]["present"] = False
        return st
    run(r, 0, COVER_DOWN_S - TICK, gone)
    assert r.n.msgs == []
    run(r, COVER_DOWN_S, COVER_DOWN_S + COVER_REMIND_S, gone)
    assert titles(r) == ["South 2 down 5 min: SDR 101 gone", "Still: South 2 down 35 min: SDR 101 gone"]


def test_trunk_recorder_down_alone_is_the_cause_and_a_stand_in_keeps_the_coverage():
    r = Rig(channels=PAGING)
    st = station_status()
    st["health"]["recorder"] = {"state": "down", "restarts": 3, "detail": "exited 1", "expected": True}
    run(r, 0, COVER_DOWN_S, st)
    assert titles(r) == ["Fire/EMS 15 down 5 min: trunk-recorder down"]  # (not "trunk-recorder down 2 min" first)
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


def test_a_dongle_swap_under_5_min_pushes_nothing():
    r = Rig(channels=PAGING)
    run(r, 0, 4 * 60, p25_gone)
    run(r, 4 * 60 + TICK, 40 * 60, station_status)
    assert r.n.msgs == []
    assert any(h["key"] == "sdr:00000103" and not h["pushed"] for h in r.h.panel()["history"])


def test_the_updaters_switch_is_no_push_and_its_5_min_start_after_it():
    r = Rig(channels=PAGING)
    switching = {"state": "updating", "to": HUB, "since": T0, "from": "release-a"}

    def during():
        return p25_gone(version={"build": HUB, "hub": HUB, "update": switching})
    run(r, 0, 10 * 60, during)                                            # 10 min into the switch: nothing
    assert r.n.msgs == [] and "cover:Fire/EMS 15" not in r.keys()
    done = {"state": "current", "since": T0 + 600, "last": {"at": T0 - 60, "seconds": 660, "result": "ok", "to": HUB}}

    def after(down=True):
        return (p25_gone if down else station_status)(version={"build": HUB, "hub": HUB, "update": done})
    run(r, 10 * 60 + TICK, 10 * 60 + UPDATE_GRACE_S - TICK, after)        # the grace after the switch: nothing
    assert r.n.msgs == []
    end = 10 * 60 + UPDATE_GRACE_S
    run(r, end, end + COVER_DOWN_S - TICK, after)                          # still down after it: 5 min counted from then
    assert r.n.msgs == []
    run(r, end + COVER_DOWN_S, end + COVER_DOWN_S, after)
    assert titles(r) == ["Fire/EMS 15 down 5 min: SDR 103 gone"] and r.n.msgs[0]["priority"] == 5
    # a switch whose radios come back in its window: nothing at all
    r = Rig(channels=PAGING)
    run(r, 0, 4 * 60, during)
    run(r, 4 * 60 + TICK, 40 * 60, lambda: after(down=False))
    assert r.n.msgs == []


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
    assert not any(k.startswith("cover:") for k in r.keys())


def test_mute_still_works():
    r = Rig(channels=PAGING)
    r.h.mutes = lambda: {"cover": {"until": T0 + 24 * 3600}}
    run(r, 0, 70 * 60, p25_gone)
    assert r.n.msgs == []                                                 # muted: nothing, its causes included
    (a,) = [a for a in r.h.alerts() if a["key"] == "cover:Fire/EMS 15"]
    assert a["muted"] is True and "cover" in H.MUTE_KINDS and "cover" in H.MUTE_TIMED_ONLY
    # muting "SDR gone" doesn't hide our dispatch
    r = Rig(channels=PAGING)
    r.h.mutes = lambda: {"sdr": {"until": None}, "recorder": {"until": None}}
    run(r, 0, COVER_DOWN_S, p25_gone)
    assert titles(r) == ["Fire/EMS 15 down 5 min: SDR 103 gone"]


class FlakyNotifier(FakeNotifier):
    """ntfy refusing the first `fail` pushes (the Notifier gave up after its retries): the outcome through done."""

    def __init__(self, fail=1):
        super().__init__()
        self.fail, self.tried = fail, 0

    def send(self, title, body, priority=3, tags=(), click="", who=None, done=None):
        self.tried += 1
        if self.tried <= self.fail:
            done({"ok": False, "at": 0.0, "status": 0, "error": "connection refused"})
            return True
        super().send(title, body, priority, tags, click, who)
        done({"ok": True, "at": 0.0, "status": 200, "id": "x"})
        return True


def test_a_first_push_ntfy_did_not_take_is_tried_again_as_a_first_push_not_a_reminder_3_h_on():
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
    assert r.n.tried == 1 + H.PUSH_RETRIES


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
    run(r, 0, COVER_DOWN_S + PUSH_RETRY_S, p25_gone)
    assert r.n.tried == 2 and titles(r) == ["Fire/EMS 15 down 6 min: SDR 103 gone"]
