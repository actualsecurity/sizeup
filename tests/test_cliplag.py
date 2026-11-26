"""#986: the clips' lag from their air start on the hub (sizeup/perf.py ClipLag): reaching the hub, playable, the first
browser fetch; p50 / p90 by source kind, bounded, a "lag 1h:" line next to the hourly perf line. Synthetic clips only."""

import re
import time

import pytest

from sizeup import perf as perf_mod
from sizeup.perf import (HOUR_TICKS, LAG_CLIPS, LAG_KEEP, LAG_LATE_S, LAG_LIVE_S, ClipLag, Perf, air_start, lag_kind,
                         quantile)
from sizeup.radio.page import Page
from tests.test_audio_store import tone
from tests.test_cloud import HubRun, call, hub_cfg

T0 = 1_790_000_000.0
LAG_LINE = re.compile(r"lag (?:\d+m|\d+h): (?:[a-z0-9-]+\[n=\d+ upload=\S+ recv=\S+ audio=\S+ play=\S+ played=\d+"
                      r"(?: late=\d+)?\] ?)+")


# -- the maths -------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("vals, q, want", [
    (list(range(1, 11)), 0.5, 5), (list(range(1, 11)), 0.9, 9), (list(range(10, 0, -1)), 0.9, 9),
    ([7.0], 0.5, 7.0), ([7.0], 0.9, 7.0), ([1, 2], 0.5, 1), ([1, 2], 0.9, 2),
    (list(range(1, 21)), 0.9, 18), (list(range(1, 101)), 0.5, 50), (list(range(1, 101)), 0.9, 90),
    ([3, 1, 2], 0.5, 2), ([5, 5, 5, 100], 0.9, 100)])
def test_nearest_rank_percentiles(vals, q, want):
    assert quantile(vals, q) == want


def test_each_stage_is_measured_from_the_clips_air_start():
    lag = ClipLag(clock=lambda: T0)
    for i in range(10):                                             # 10 county turns: on the hub i+5 s after air start
        start = T0 - 100 + i
        lag.event(f"tg-{i}", "county", start, end=start + 3, audio=True, now=start + 5 + i)
        lag.fetched(f"tg-{i}", now=start + 10 + i)
    st = lag.stats(now=T0)["county"]
    assert st["recv"] == {"n": 10, "late": 0, "p50": 9.0, "p90": 13.0}
    assert st["audio"] == st["recv"] and st["play"] == {"n": 10, "late": 0, "p50": 14.0, "p90": 18.0}
    assert st["upload"] == {"n": 10, "late": 0, "p50": 6.0, "p90": 10.0}           # (recv - end)


def test_a_backlog_is_late_and_never_in_the_percentiles():
    lag = ClipLag()
    lag.event("a", "fm", T0, audio=True, now=T0 + 2)
    lag.event("b", "fm", T0, audio=True, now=T0 + LAG_LATE_S + 1)       # an outbox backlog after an outage
    st = lag.stats(now=T0 + LAG_LATE_S + 2)["fm"]["recv"]
    assert st == {"n": 1, "late": 1, "p50": 2.0, "p90": 2.0}
    lag2 = ClipLag()
    lag2.event("c", "fm", T0, now=T0 + LAG_LATE_S + 1)
    assert lag2.stats(now=T0 + LAG_LATE_S + 2)["fm"]["recv"] == {"n": 0, "late": 1, "p50": None, "p90": None}


def test_the_window_rolls():
    lag = ClipLag()
    lag.event("old", "airband", T0, audio=True, now=T0 + 1)
    lag.event("new", "airband", T0 + 3000, audio=True, now=T0 + 3003)
    assert lag.stats(now=T0 + 3500)["airband"]["recv"]["n"] == 2
    assert lag.stats(now=T0 + 3602)["airband"]["recv"] == {"n": 1, "late": 0, "p50": 3.0, "p90": 3.0}
    assert lag.stats(now=T0 + 9999) == {}


# -- once per clip, the first fetch only -----------------------------------------------------------------------------------

def test_a_clip_counts_once_and_only_its_first_fetch_while_live():
    lag = ClipLag()
    lag.event("x", "paging", T0, end=T0 + 30, audio=False, now=T0 + 31)    # its event, its audio later (page_text first)
    lag.fetched("x", now=T0 + 32)                                       # not playable yet: nothing, and not "used up"
    lag.event("x", "paging", T0, end=T0 + 30, audio=True, now=T0 + 34)     # sent again, with its audio
    lag.event("x", "paging", T0, end=T0 + 30, audio=True, now=T0 + 90)     # and again (a resend): never twice
    for t in (36, 37, 50, 60):                                          # the player, then replays
        lag.fetched("x", now=T0 + t)
    lag.fetched("unknown", now=T0 + 40)                                 # a clip it never saw (another leader's, older)
    st = lag.stats(now=T0 + 100)["paging"]
    assert {k: (v["n"], v["p50"]) for k, v in st.items()} == {"upload": (1, 1.0), "recv": (1, 31.0), "audio": (1, 34.0),
                                                             "play": (1, 36.0)}
    late = ClipLag()                                                    # first fetched long after it was playable: a replay
    late.event("y", "fm", T0, audio=True, now=T0 + 1)
    late.fetched("y", now=T0 + 1 + LAG_LIVE_S + 1)
    late.fetched("y", now=T0 + 1 + LAG_LIVE_S + 2)
    assert "play" not in late.stats(now=T0 + 200)["fm"]


# -- bounded --------------------------------------------------------------------------------------------------------------

def test_memory_is_bounded_however_many_clips():
    lag = ClipLag()
    for i in range(3 * LAG_CLIPS):
        cid = f"FG1-{1790000000000 + i}"
        lag.event(cid, ("county", "fm", "mrrs")[i % 3], T0 + i, end=T0 + i + 1, audio=True, now=T0 + i + 2)
        lag.fetched(cid, now=T0 + i + 3)
    lag.event("junk", "not-a-kind", T0, now=T0)                         # an unknown kind: never a new series
    lag.event("nostart", "fm", None, now=T0)
    assert len(lag._clips) == LAG_CLIPS
    assert len(lag._s) == 3 * 4 and all(len(q) == LAG_KEEP for q in lag._s.values())
    st = lag.stats(window=10 ** 9, now=T0 + 3 * LAG_CLIPS)
    assert set(st) == {"county", "fm", "mrrs"} and st["fm"]["recv"]["n"] == LAG_KEEP and st["fm"]["play"]["p90"] == 3.0


# -- what a clip is, by kind only -------------------------------------------------------------------------------------------

def test_source_kinds_and_air_starts():
    assert lag_kind(True) == "paging"
    assert lag_kind(False, "", 307) == "county"
    assert lag_kind(False, "mrrs", 9212, context=True) == "mrrs"
    assert lag_kind(False, "othersys", 5, context=True) == "p25-other"
    assert lag_kind(False, "", None, "airband") == "airband" and lag_kind(False, "airband", None) == "airband"
    assert lag_kind(False, "", None, "nfm") == "fm" and lag_kind(False, "analog", None) == "fm"
    assert air_start("FG1-1790000000123") == pytest.approx(1790000000.123)
    assert air_start("FG1-1790000000123", 1790000005.0) == 1790000005.0          # the event's own start wins
    assert air_start("3f2a9c1d0e4b") is None and air_start("x", True) is None    # a page's uuid: no time in it


# -- the line -----------------------------------------------------------------------------------------------------------------

def test_the_line_says_kinds_and_numbers_only():
    lag = ClipLag()
    assert lag.line(now=T0) is None                                       # nothing heard: no line
    lag.event("SecretLabel-1790000000000", "county", T0, end=T0 + 8, audio=True, now=T0 + 9)
    lag.fetched("SecretLabel-1790000000000", now=T0 + 13.25)
    lag.event("3f2a9c1d0e4b", "paging", T0, now=T0 + 40)                  # no audio yet: n/a for audio and play
    lag.event("FG1-1790000000000", "fm", T0, audio=True, now=T0 + LAG_LATE_S + 5)
    line = lag.line(now=T0 + 60)
    assert LAG_LINE.fullmatch(line), line
    assert line == ("lag 1h: county[n=1 upload=1.0/1.0 recv=9.0/9.0 audio=9.0/9.0 play=13.2/13.2 played=1] "
                    "paging[n=1 upload=n/a recv=40.0/40.0 audio=n/a play=n/a played=0] "
                    "fm[n=0 upload=n/a recv=n/a audio=n/a play=n/a played=0 late=2]")
    assert "Secret" not in line and "3f2a" not in line and "FG1" not in line and "\n" not in line
    assert lag.line(window=300, now=T0 + 60).startswith("lag 5m: ")


def test_the_lag_line_follows_the_hourly_perf_line(monkeypatch):
    lines, now = [], [0.0]
    p = Perf(log=lines.append, tick_s=3600, vcpu=0.5, mono=lambda: now[0], clock=lambda: T0)
    monkeypatch.setattr(p.threads, "read", lambda tids=None: {})
    p.start()
    try:
        p.lag.event("FG1-1790000000000", "fm", T0 - 4, audio=True, now=T0 - 2)
        for _ in range(HOUR_TICKS):
            now[0] += 60.0
            p.tick()
        assert [x.split(":")[0] for x in lines] == ["perf 5m", "lag 5m", "perf 1h", "lag 1h"]
        assert lines[-1] == "lag 1h: fm[n=1 upload=n/a recv=2.0/2.0 audio=2.0/2.0 play=n/a played=0]"
        assert p.snapshot()["lag"] == {"fm": {"recv": {"n": 1, "late": 0, "p50": 2.0, "p90": 2.0},
                                             "audio": {"n": 1, "late": 0, "p50": 2.0, "p90": 2.0}}}
    finally:
        p.stop()


# -- on a hub, end to end -----------------------------------------------------------------------------------------------------

def test_the_hub_measures_its_clips_and_counts_a_browsers_first_fetch(tmp_path):
    h = HubRun(hub_cfg(tmp_path))
    try:
        now = time.time()
        tid = f"Fire/EMS15-{int((now - 9) * 1000)}".replace("/", "")
        h.app.apply("traffic", {"id": tid, "source": "Fire/EMS 15", "talkgroup": 307, "start": now - 9, "end": now - 2,
                                "text": "", "status": "done"}, tone(0.2))
        fm = f"FG1-{int((now - 3) * 1000)}"
        h.app.apply("traffic", {"id": fm, "source": "FG1", "start": now - 3, "end": now - 1, "text": "", "status": "done"},
                    tone(0.2))
        page = Page("9c1d0e4b3f2a", "South 2", 470.2, now - 40, now - 6, "voice", [], 2.0, None, 20.0)
        h.app.apply("page", page.as_dict(), tone(0.2))
        for _ in range(3):                                              # the player's fetch, then two more
            code, wav = call(f"{h.url}/api/audio/{tid}.wav", token=None)
            assert code == 200 and wav[:4] == b"RIFF"
        assert call(f"{h.url}/api/audio/{page.id}.wav", token=None)[0] == 200
        assert call(f"{h.url}/api/audio/nosuch-1790000000000.wav", token=None)[0] == 404
        st = h.app.perf.lag.stats()
        assert set(st) == {"county", "fm", "paging"}
        assert st["county"]["recv"]["n"] == 1 and 9 <= st["county"]["recv"]["p50"] < 30
        assert st["county"]["play"]["n"] == 1 and st["county"]["play"]["p50"] >= st["county"]["audio"]["p50"]
        assert st["paging"]["recv"]["n"] == 1 and 40 <= st["paging"]["recv"]["p50"] < 60
        assert st["paging"]["play"]["n"] == 1 and "play" not in st["fm"]
        line = h.app.perf.lag.line()
        assert LAG_LINE.fullmatch(line) and "307" not in line and "South" not in line and "FG1" not in line
    finally:
        h.close()


def test_a_bad_event_is_never_held_up_by_the_measurement(tmp_path, monkeypatch):
    h = HubRun(hub_cfg(tmp_path))
    try:
        monkeypatch.setattr(perf_mod.ClipLag, "event", lambda *a, **k: 1 / 0)
        now = time.time()
        h.app.apply("traffic", {"id": f"FG1-{int(now * 1000)}", "source": "FG1", "start": now - 3, "end": now - 1,
                                "text": "", "status": "done"}, tone(0.2))
        assert any(t.source == "FG1" for t in h.app.store.loose)
    finally:
        h.close()
