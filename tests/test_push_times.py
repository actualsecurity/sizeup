"""#200: times in pushes, banners and status lines are in the console's time zone (America/New_York), never
the process's own: the hub container runs in UTC. Each test here runs with the process in UTC, as the hub is."""

import time
from datetime import datetime, timezone

import pytest

from sizeup import tz
from sizeup.cloud import autoupdate, health, hub, stationmode
from sizeup import transcribe

# 2026-11-01 13:29Z is 08:29 EST (summer time ended at 06:00Z that morning); 2026-07-01 13:29Z is 09:29 EDT.
CLIP_EST = datetime(2026, 11, 1, 13, 29, tzinfo=timezone.utc).timestamp()
CLIP_EDT = datetime(2026, 7, 1, 13, 29, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def utc_process(monkeypatch):
    """The process in UTC, as the hub container is: time.localtime() would give 13:29 here."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    assert time.strftime("%H:%M", time.localtime(CLIP_EST)) == "13:29"
    yield
    monkeypatch.undo()
    time.tzset()


def test_the_console_time_zone():
    assert tz.TZ_NAME == "America/New_York"
    assert tz.hhmm(CLIP_EST) == "08:29" and tz.hhmmss(CLIP_EST + 5) == "08:29:05"
    assert tz.hhmm(CLIP_EDT) == "09:29"
    assert tz.hhmm(None) == "?" and tz.hhmm(0, "") == "" and tz.hhmm("not a time") == "?"
    assert tz.clock(CLIP_EST, "%Y-%m-%d %H:%M") == "2026-11-01 08:29"


@pytest.mark.parametrize("fmt", [health._hhmm, hub._hhmm, transcribe._hhmm, stationmode._hhmm])
def test_every_formatter_says_the_console_time(fmt):
    assert fmt(CLIP_EST) == "08:29" and fmt(CLIP_EDT) == "09:29"


def test_the_speech_to_text_behind_push_reads_local_time():
    """The push from the issue: 5 h behind at 13:29 EST, its oldest clip from 08:29 EST (13:29Z)."""
    from tests.test_health import T0, Rig, station_status
    r = Rig()
    now = CLIP_EST + 5 * 3600
    st = station_status()
    st["health"]["stt_queue"] = {"queued": 1800, "live": 1700, "urgent": 20, "rerun": 100, "oldest_s": 5 * 3600,
                                 "dropped": 0, "skipped": 0, "last_drop": None, "max": 2000}
    r.at(now - T0, status=st)
    a = next(a for a in r.h.alerts() if a["key"] == "stt_backlog")
    assert "its oldest waiting clip is from 08:29 " in a["text"]               # the banner
    m = next(m for m in r.n.msgs if m["title"].startswith("Speech-to-text behind"))
    assert m["title"].startswith("Speech-to-text behind 5 h 0 min")
    assert "from 08:29 " in m["body"] and m["body"].endswith("(since 08:29).") and "13:29" not in m["body"]


def test_the_silent_push_and_its_recovery_read_local_time():
    from tests.test_health import T0, Rig, station_status
    r = Rig()
    r.at(CLIP_EST - T0, status=station_status())                          # last heard 08:29 EST
    r.at(CLIP_EST - T0 + 181, post=False)
    a = next(a for a in r.h.alerts() if a["key"] == "silent")
    assert "since 08:29:" in a["text"] and r.n.msgs[-1]["title"].endswith("last heard 08:29")
    r.at(CLIP_EST - T0 + 600, status=station_status())                    # back at 08:39
    assert r.n.msgs[-1]["title"].startswith("Recovered: Station silent") and "cleared at 08:39 " in r.n.msgs[-1]["body"]


def test_the_station_update_lines_read_local_time():
    sha = "c" * 40
    d = autoupdate.describe("b" * 40, sha, {"retry": {sha: {"next_at": CLIP_EST, "n": 2, "why": "no network"}}}, CLIP_EST - 60)
    assert d["state"] == "updating" and d["text"].endswith("again after 08:29")
    assert autoupdate._when(CLIP_EST) == "2026-11-01 08:29" and autoupdate._when("x") == "?"
    assert str(autoupdate.CONSOLE_TZ) == tz.TZ_NAME          # its own copy (stdlib only): the same zone
