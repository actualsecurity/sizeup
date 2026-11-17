"""The speech-to-text input, station -> hub (sizeup/cloud/sttinput.py, flac.py): the exact audio the station's whisper
transcribes goes up beside the listening clip, losslessly, so the hub's engines transcribe the same audio.

Synthetic audio and invented ids only."""

from __future__ import annotations

import base64
import io
import json
import shutil
import sqlite3
import subprocess
import time
import wave

import numpy as np
import pytest

from sizeup.audiostore import FsAudioStore, key_id, object_key
from sizeup.cloud import flac, sttinput
from sizeup.cloud.hubstore import AudioDb
from sizeup.cloud.outbox import STT_SHARE, Outbox
from sizeup.incidents import Traffic
from sizeup.radio.fm import RATE, Channel, Segment
from sizeup.stt import WhisperClient, radio_voice, wav_bytes
from tests.test_app import PAGE_TEXT, tone, voice, wait
from tests.test_cloud import HubRun, all_traffic, call, free_port, hub_cfg, station_cfg

# -- the codec ----------------------------------------------------------------------------------------------------


def _speechlike(n: int, seed: int = 0) -> np.ndarray:
    if n < 64:
        return (np.random.default_rng(seed).integers(-3000, 3000, n)).astype(np.int16)
    return sttinput.stt_pcm(voice(n / RATE + 0.01, seed)[:n], fm=True)


@pytest.mark.parametrize("n", [0, 1, 2, 3, 5, 255, 256, 257, 1023, 1024, 1025, 4097, 3 * RATE + 11])
def test_flac_round_trips_bit_exact_at_every_length(n):
    pcm = _speechlike(n, n)
    for rate in (8000, 16000):
        out, r = flac.decode(flac.encode(pcm, rate))
        assert r == rate and out.dtype == np.int16 and np.array_equal(out, pcm)


def test_flac_round_trips_the_extremes_and_compresses_speech():
    rng = np.random.default_rng(3)
    for pcm in (np.zeros(5000, np.int16), np.full(3000, -32768, np.int16), np.full(3000, 32767, np.int16),
                rng.integers(-32768, 32768, 5000).astype(np.int16),            # white noise at full scale: VERBATIM
                np.array([32767, -32768] * 2000, np.int16),                    # the widest residuals there are
                np.r_[np.zeros(1000), 30000, np.zeros(1000)].astype(np.int16)):
        out, _ = flac.decode(flac.encode(pcm, 16000))
        assert np.array_equal(out, pcm)
    pcm = _speechlike(5 * RATE)
    assert len(flac.encode(pcm, 16000)) < 0.75 * 2 * len(pcm)                 # well under the WAV


def test_flac_refuses_what_it_did_not_write_and_a_damaged_stream():
    data = flac.encode(_speechlike(3000), 16000)
    with pytest.raises(flac.FlacError):
        flac.decode(b"RIFF" + data[4:])
    with pytest.raises(flac.FlacError):
        flac.decode(data[:len(data) // 2])                                    # cut short
    bad = bytearray(data)
    bad[-40] ^= 0x10                                                          # one bit in the last frame's residual
    with pytest.raises(flac.FlacError):
        flac.decode(bytes(bad))
    with pytest.raises(flac.FlacError):
        flac.encode(np.zeros(10, np.float32), 16000)


def test_flac_crc16_is_the_frame_crc():
    data = np.random.default_rng(5).integers(0, 256, 5000).astype(np.uint8).tobytes()
    c = 0
    for b in data:                                     # bit by bit: x^16 + x^15 + x^2 + 1, no reflection, from 0
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x8005) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    assert flac.crc16(data) == c and flac.crc16(b"") == 0


def test_a_standard_decoder_reads_our_flac_bit_exact():
    """libsndfile (libFLAC) and ffmpeg, where installed: what we write is plain FLAC (AWS, a GPU worker take it)."""
    pcm = _speechlike(2 * RATE + 100)
    data = flac.encode(pcm, 16000)
    checked = 0
    try:
        import soundfile
    except ImportError:
        soundfile = None
    if soundfile is not None:
        got, rate = soundfile.read(io.BytesIO(data), dtype="int16")
        assert rate == 16000 and np.array_equal(got, pcm)
        checked += 1
    if shutil.which("ffmpeg"):
        out = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"],
                             input=data, capture_output=True, check=True).stdout
        assert np.array_equal(np.frombuffer(out, "<i2"), pcm)
        checked += 1
    if not checked:
        pytest.skip("no reference FLAC decoder here (soundfile / ffmpeg)")


# -- the station: what goes up with each transmission and page ----------------------------------------------------


class RecordingWhisper:
    """whisper-server stand-in that keeps every array it was given (Transcriber._run -> client.transcribe)."""

    def __init__(self, texts):
        self.texts, self.got = texts, []

    def transcribe(self, audio, prompt="", **kw):
        self.got.append(np.array(audio, copy=True))
        return self.texts.get(round(len(audio) / RATE), "Engine 60 responding"), 0.9


def offline_station(tmp_path, texts=None):
    """A station whose hub isn't there: everything waits in its outbox, where the test reads it."""
    from sizeup.app import make_app
    app = make_app(station_cfg(tmp_path, f"http://127.0.0.1:{free_port()}"))
    app.stt.client = RecordingWhisper(texts or {})
    app.stt.start()
    app.link.start()
    return app


def queued(app, kind, cid):
    rows = [r for r in app.link.outbox.batch(max_bytes=1 << 30) if r["type"] == kind and r["data"].get("id") == cid]
    return rows[0] if rows else None


def whisper_input(arr) -> np.ndarray:
    """What whisper-server reads from WhisperClient's WAV: the float array as int16 (stt.wav_bytes)."""
    return (np.clip(arr, -1, 1) * 32767).astype("<i2")


def test_fm_traffic_goes_up_with_its_clip_and_the_exact_whisper_input(tmp_path):
    st = offline_station(tmp_path)
    try:
        now = time.time()
        a = voice(3.0, seed=7)
        st._segment(Channel(453.0375, "FG1"), Segment(now, now + 3.0, a, 20.0, []))
        tid = f"FG1-{int(now * 1000)}"
        assert wait(lambda: queued(st, "traffic", tid) is not None, 10)
        ev = queued(st, "traffic", tid)
        with wave.open(io.BytesIO(ev["audio"])) as w:                   # the listening clip: as before
            assert w.getframerate() == 8000
        data, meta = ev["stt"]
        assert meta == {"v": 1, "format": "flac", "input": "stt-src", "rate": 16000, "samples": len(a)}
        pcm, rate = flac.decode(data)
        assert rate == 16000 and len(st.stt.client.got) == 1
        assert np.array_equal(pcm, whisper_input(st.stt.client.got[0]))   # byte for byte what whisper transcribed
        assert np.array_equal(pcm, sttinput.stt_pcm(a, fm=True))           # FM: de-emphasised, as whisper's
        assert not np.array_equal(pcm, sttinput.stt_pcm(a, fm=False))
        # WhisperClient's own WAV body (its 0.5 s pads) is what the hub can rebuild from it
        assert sttinput.SttAudio(pcm, 16000, "stt-src").whisper_wav() == wav_bytes(
            np.concatenate([np.zeros(8000, np.float32), st.stt.client.got[0].astype(np.float32), np.zeros(8000, np.float32)]))
        assert st.link.stt_stash.stats()["waiting"] == 0                   # taken by its event
    finally:
        st.link.stop()


def _transmission(st, tid, source, a, fm, eng="local", key=None, talkgroup=None):
    t = Traffic(tid, source, time.time() - len(a) / RATE, time.time(), "", status="pending", talkgroup=talkgroup)
    t.fm = fm                                                    # type: ignore[attr-defined]
    st._keep_clip(t.id, radio_voice(a, deemph=fm), processed=True)
    st._keep_source(t.id, a)
    st._stt(t, a, eng, key or source)
    return t


@pytest.mark.parametrize("kind", ["am", "p25"])
def test_airband_and_p25_pieces_go_up_without_de_emphasis(tmp_path, kind):
    st = offline_station(tmp_path)
    try:
        a = voice(2.0, seed=11)
        if kind == "p25":                                        # trunk-recorder's 8 kHz voice, brought to 16 kHz
            a = np.repeat(a[::2], 2)
            t = _transmission(st, "307-1790000000.5_476112500.0-call_1-t2", "Fire/EMS 15", a, False, key="tg:307",
                              talkgroup=307)
        else:
            t = _transmission(st, "HPNATC-1790000000500", "HPN ATC", a, False)
        assert wait(lambda: queued(st, "traffic", t.id) is not None, 10)
        data, meta = queued(st, "traffic", t.id)["stt"]
        assert meta["rate"] == 16000 and meta["input"] == "stt-src" and "voice_offset_s" not in meta
        pcm, _ = flac.decode(data)
        assert np.array_equal(pcm, whisper_input(st.stt.client.got[-1]))
        assert np.array_equal(pcm, sttinput.stt_pcm(a, fm=False))
    finally:
        st.link.stop()


def test_a_clip_for_the_hubs_engine_still_carries_the_station_whisper_input(tmp_path):
    """eng "aws" on a station: whisper never runs here, the hub transcribes; the STT input is the audio it would get."""
    st = offline_station(tmp_path)
    try:
        a = voice(2.0, seed=3)
        t = _transmission(st, "FG1-1790000000123", "FG1", a, True, eng="aws")
        ev = queued(st, "traffic", t.id)
        assert ev is not None and ev["data"].get("transcript_pending") == "aws" and st.stt.client.got == []
        pcm, _ = flac.decode(ev["stt"][0])
        assert np.array_equal(pcm, sttinput.stt_pcm(a, fm=True))
    finally:
        st.link.stop()


def test_a_page_goes_up_with_its_voice_after_the_tones_and_their_offset(tmp_path):
    st = offline_station(tmp_path, {6: PAGE_TEXT})
    try:
        a = np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0), np.zeros(int(0.2 * RATE)), voice(6.0)]).astype(np.float32)
        now = time.time()
        st._segment(Channel(470.2, "South 2"), Segment(now, now + len(a) / RATE, a, 25.0))
        assert wait(lambda: st.pages and st.pages[0].status == "done", 15)
        page = st.pages[0]
        ev = queued(st, "page", page.id)
        assert ev is not None and ev["audio"]
        data, meta = ev["stt"]
        assert page.voice_offset_s > 3.5 and meta["voice_offset_s"] == round(page.voice_offset_s, 4)
        pcm, rate = flac.decode(data)
        assert rate == 16000 and len(pcm) == len(page.voice) == meta["samples"]
        assert np.array_equal(pcm, whisper_input(st.stt.client.got[0]))      # the voice whisper got, tones cut off
        page_text = queued(st, "page_text", page.id)
        assert page_text is not None and "stt" not in page_text
        assert st.link.stt_stash.stats()["waiting"] == 0                     # made once, before its event
    finally:
        st.link.stop()


def test_the_stash_is_bounded_and_counts_what_it_pushed_out():
    s = sttinput.Stash(max_bytes=2500)
    for k in range(4):
        s.put(f"FG1-{k}", (b"x" * 1000, {}))
    assert s.stats() == {"waiting": 2, "bytes": 2000, "evicted": 2, "failed": 0}
    assert s.take("FG1-0") is None and s.take("FG1-3")[0] == b"x" * 1000
    assert s.stats()["bytes"] == 1000


def test_the_station_whisper_never_waits_on_a_failed_stt_input(tmp_path, monkeypatch):
    st = offline_station(tmp_path)
    try:
        monkeypatch.setattr(sttinput, "blob", lambda item, audio: 1 / 0)
        t = _transmission(st, "FG1-1790000000999", "FG1", voice(2.0), True)
        assert wait(lambda: queued(st, "traffic", t.id) is not None, 10)
        ev = queued(st, "traffic", t.id)
        assert "stt" not in ev and ev["audio"] and st.link.stt_stash.failed == 1
    finally:
        st.link.stop()


# -- the outbox: the STT input goes first --------------------------------------------------------------------------


def test_the_outbox_drops_stt_inputs_before_any_clip_and_counts_them(tmp_path):
    ob = Outbox(tmp_path / "o.sqlite", max_audio_bytes=6000)                 # STT inputs' own budget: 1800
    for k in range(3):
        ob.add("traffic", {"id": f"FG1-{k}"}, b"a" * 1000, stt=(b"s" * 500, {"rate": 16000}))
    rows = ob.batch()
    assert [("stt" in r, r.get("stt_dropped", False), r["audio_dropped"]) for r in rows] == [(True, False, False)] * 3
    ob.add("traffic", {"id": "FG1-3"}, b"a" * 1000, stt=(b"s" * 500, {"rate": 16000}))   # over their budget: the oldest
    assert [r.get("stt_dropped", False) for r in ob.batch()] == [True, False, False, False]
    ob.add("traffic", {"id": "FG1-4"}, b"a" * 1500)            # the whole over the limit: STT inputs go, no clip
    rows = ob.batch()
    assert [r.get("stt_dropped", False) for r in rows] == [True, True, True, False, False]
    assert all(r["audio"] is not None for r in rows) and ob.dropped_audio == 0 and ob.dropped_stt == 3
    ob.add("traffic", {"id": "FG1-5"}, b"a" * 1000)            # only then a clip: the last STT input first
    rows = ob.batch()
    assert ob.dropped_stt == 4 and ob.stt_bytes == 0 and ob.dropped_audio == 1
    assert [r["audio_dropped"] for r in rows] == [True, False, False, False, False, False]
    assert ob.audio_bytes + ob.stt_bytes <= 6000


def test_the_stt_inputs_keep_inside_their_own_budget(tmp_path):
    ob = Outbox(tmp_path / "o.sqlite", max_audio_bytes=10_000)
    budget = int(10_000 * STT_SHARE)
    for k in range(6):
        ob.add("traffic", {"id": f"FG1-{k}"}, b"a" * 100, stt=(b"s" * 1000, {"rate": 16000}))
    assert ob.stt_bytes <= budget and ob.dropped_stt == 6 - budget // 1000
    rows = ob.batch()
    assert [("stt" in r) for r in rows] == [False] * (6 - budget // 1000) + [True] * (budget // 1000)
    assert all(r["audio"] for r in rows)
    data, meta = rows[-1]["stt"]
    assert data == b"s" * 1000 and meta == {"rate": 16000}
    ob.ack(rows[-1]["seq"])
    assert ob.stt_bytes == 0 and ob.audio_bytes == 0
    # the whole limit counts clips and STT inputs together: a batch's size too
    ob.add("traffic", {"id": "FG1-9"}, b"a" * 100, stt=(b"s" * 1000, {}))
    assert len(ob.batch(max_bytes=500)) == 1


def test_an_outbox_from_before_the_stt_input_opens_and_sends_its_rows(tmp_path):
    p = tmp_path / "o.sqlite"
    db = sqlite3.connect(p)
    db.executescript("""CREATE TABLE outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL,
                        data TEXT NOT NULL, audio BLOB, audio_dropped INTEGER NOT NULL DEFAULT 0);
                        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                        INSERT INTO meta VALUES ('epoch', 'e0');""")
    db.execute("INSERT INTO outbox (at, kind, data, audio) VALUES (1, 'traffic', '{\"id\":\"FG1-1\"}', x'0102')")
    db.commit()
    db.close()
    ob = Outbox(p)
    rows = ob.batch()
    assert len(rows) == 1 and rows[0]["audio"] == b"\x01\x02" and "stt" not in rows[0] and ob.epoch == "e0"
    ob.add("traffic", {"id": "FG1-2"}, b"a", stt=(b"fLaC", {"rate": 16000}))
    ob.close()
    again = Outbox(p)
    assert again.stt_bytes == 4 and again.batch()[1]["stt"] == (b"fLaC", {"rate": 16000})


def test_the_station_status_shows_dropped_stt_inputs(tmp_path):
    st = offline_station(tmp_path)
    try:
        st.link.outbox.dropped_stt = 3
        assert "3 STT input(s) dropped" in st.link.status_row()["detail"]
        ob = st.link.status(talkgroups=False)["outbox"]
        assert ob["dropped_stt"] == 3 and set(ob["stt"]) == {"waiting", "bytes", "evicted", "failed"}
    finally:
        st.link.stop()


# -- the hub: stored beside the clip, read for its engines ----------------------------------------------------------


def _event(seq, tid, a, fm=True, stt=True, end=None):
    end = end or time.time()
    data = {"id": tid, "source": "FG1", "start": end - len(a) / RATE, "end": end, "text": "Engine 60 responding",
            "status": "done", "unit": "", "talkgroup": None, "rig": "", "radio_id": "", "raw": "Engine 60 responding",
            "confidence": 0.9, "model": "m.bin", "attach": True, "pairs": None}
    clip = wav_bytes(radio_voice(a, deemph=fm)[::2], 8000)
    ev = {"seq": seq, "at": end, "type": "traffic", "data": data, "audio": base64.b64encode(clip).decode()}
    if stt:
        pcm = sttinput.stt_pcm(a, fm)
        ev["stt"] = sttinput.to_wire(flac.encode(pcm, 16000), {"v": 1, "format": "flac", "input": "stt-src",
                                                                "rate": 16000, "samples": len(pcm)})
    return ev


def test_the_hub_stores_the_stt_input_beside_the_clip_and_reads_it_for_its_engines(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        a, b = voice(2.0, seed=1), voice(1.5, seed=2)
        batch = {"epoch": "e1", "events": [_event(1, "FG1-1790000000001", a), _event(2, "FG1-1790000000002", b, stt=False)]}
        assert call(hub.url + "/api/station/events", "POST", batch)[1]["acked"] == 2
        got = hub.app.read_stt_audio("FG1-1790000000001")
        assert got.input == "stt-src" and got.rate == 16000 and got.voice_offset_s == 0.0
        assert np.array_equal(got.pcm, sttinput.stt_pcm(a, True)) and got.flac[:4] == b"fLaC"
        assert np.array_equal(got.pcm8k(), got.pcm[::2])
        assert json.loads(hub.app.audio.note("FG1-1790000000001.src"))["input"] == "stt-src"
        # an older station's transmission (or one whose STT input the outbox dropped): the listening clip, said so
        old = hub.app.read_stt_audio("FG1-1790000000002")
        assert old.input == "listening-8k" and old.rate == 8000 and old.flac is None
        assert np.array_equal(old.pcm8k(), old.pcm) and len(old.whisper_wav()) == 44 + 2 * (2 * len(old.pcm) + 16000)
        assert hub.app.read_stt_audio("FG1-0") is None and hub.app.read_stt_audio("../x") is None
        # never a clip anyone plays: /api/audio refuses it, the clip itself is as before
        assert call(hub.url + "/api/audio/FG1-1790000000001.src.wav", token=None)[0] == 404
        assert hub.get("/api/audio/FG1-1790000000001.wav")[:4] == b"RIFF"
        assert not hub.app.has_clip("FG1-1790000000001.src") and hub.app.has_clip("FG1-1790000000001")
        # a broken STT input never costs the event: applied, the clip read instead
        bad = _event(3, "FG1-1790000000003", a)
        bad["stt"]["audio"] = base64.b64encode(b"not flac").decode()
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": [bad]})[1]["acked"] == 3
        assert any(t["id"] == "FG1-1790000000003" for t in all_traffic(hub.app.state()))
        assert hub.app.read_stt_audio("FG1-1790000000003").input == "listening-8k"
    finally:
        hub.close()


def test_a_page_read_from_its_clip_starts_at_its_voice(tmp_path):
    db = AudioDb(":memory:")
    a = np.concatenate([np.full(8000, 1000, np.int16), np.arange(8000, dtype=np.int16)])
    db.put("SOUTH2-1", wav_bytes(a, 8000), "clip", at=1.0, channel="South 2")
    got = sttinput.read(db, "SOUTH2-1", voice_offset_s=1.0)
    assert got.input == "listening-8k" and got.voice_offset_s == 1.0 and np.array_equal(got.pcm, a[8000:])
    pcm = _speechlike(RATE)
    sttinput.store(db, "SOUTH2-1", flac.encode(pcm, 16000), {"input": "stt-src", "rate": 16000, "voice_offset_s": 4.25},
                   1.0, channel="South 2")
    got = sttinput.read(db, "SOUTH2-1", voice_offset_s=1.0)
    assert got.input == "stt-src" and got.voice_offset_s == 4.25 and np.array_equal(got.pcm, pcm)


def test_the_stt_input_follows_its_clip_into_the_store_and_out_with_its_retention(tmp_path):
    store = FsAudioStore(tmp_path / "bucket")
    db = AudioDb(tmp_path / "audio.sqlite", store=store, may_delete=lambda: True)
    day = 86400.0
    now = time.time()
    for cid, at in (("FG1-1", now - 40 * day), ("FG1-2", now - 40 * day), ("FG1-3", now - day)):
        db.put(cid, wav_bytes(np.zeros(800, np.int16), 8000), "clip", at=at, channel="FG1")
        sttinput.store(db, cid, flac.encode(_speechlike(1600), 16000), {"input": "stt-src", "rate": 16000}, at, "FG1")
    assert db.upload_pending(limit=50) == 6
    keys = sorted(r[0] for r in db.db.execute("SELECT key FROM audio WHERE id LIKE '%.src'"))
    assert all(k.endswith(".src.flac") and k.startswith("audio/clip/") for k in keys)
    assert {key_id(k) for k in keys} == {"FG1-1.src", "FG1-2.src", "FG1-3.src"}
    assert key_id(object_key("clip", now, "FG1-1")) == "FG1-1"
    assert sttinput.read(db, "FG1-3").input == "stt-src"             # read back from the store
    assert [c["clips"] for c in db.channels(max_age_s=0)] == [3]      # (its bytes count, not as a clip)
    # 30 days: FG1-1 and its STT input go; FG1-2 is a record (kept), and so is its STT input
    n = db.prune_clips(now - 30 * day, kept=lambda ids: {"FG1-2"} & set(ids), kept_older_than=None)
    assert n == 2
    left = sorted(r[0] for r in db.db.execute("SELECT id FROM audio"))
    assert left == ["FG1-2", "FG1-2.src", "FG1-3", "FG1-3.src"]
    assert sorted(p.name for p in (tmp_path / "bucket").rglob("*.*")) == ["FG1-2.src.flac", "FG1-2.wav",
                                                                          "FG1-3.src.flac", "FG1-3.wav"]


# -- end to end: the station's whisper input, on the hub, bit for bit ------------------------------------------------


def test_station_to_hub_the_engines_get_exactly_what_the_station_whisper_got(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    from sizeup.app import make_app
    st = make_app(station_cfg(tmp_path, hub.url))
    st.stt.client = RecordingWhisper({})
    st.stt.start()
    st.link.start()
    try:
        now = time.time()
        a = voice(3.0, seed=21)
        st._segment(Channel(453.0375, "FG1"), Segment(now, now + 3.0, a, 20.0, []))
        tid = f"FG1-{int(now * 1000)}"
        assert wait(lambda: any(t["id"] == tid for t in all_traffic(hub.get("/api/state"))), 15)
        got = hub.app.read_stt_audio(tid)
        assert got.input == "stt-src"
        assert np.array_equal(got.pcm, whisper_input(st.stt.client.got[0]))
        # the station's .src.wav is the audio before radio_voice; its whisper input, made again from it, matches
        # the hub's to within the .src.wav's own int16 rounding
        with wave.open(str(tmp_path / "st" / "clips" / f"{tid}.src.wav")) as w:
            src = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32767
        again = sttinput.stt_pcm(src, fm=True).astype(np.int32)
        assert len(again) == len(got.pcm) and np.abs(again - got.pcm).max() <= 2
        # and the WAV a GPU worker gets is the very body WhisperClient posted
        posted = wav_bytes(np.concatenate([np.zeros(8000, np.float32), st.stt.client.got[0], np.zeros(8000, np.float32)]))
        assert got.whisper_wav() == posted
        assert WhisperClient  # (the client whose body this is: sizeup/stt.py)
    finally:
        st.link.stop()
        hub.close()
