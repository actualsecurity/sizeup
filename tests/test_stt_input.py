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
from sizeup.cloud.outbox import Outbox
from sizeup.incidents import Traffic
from sizeup.radio.fm import RATE, Channel, Segment
from sizeup.stt import radio_voice, wav_bytes
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


def _craft(total: int, frames: int, n: int = 65536, md5: bytes = b"\x01" * 16) -> bytes:
    """A stream of `frames` CONSTANT frames of n samples each whose STREAMINFO says `total`: a few KB that would
    decode to frames * n samples (the shape of a decompression bomb)."""
    out = []
    for fi in range(frames):
        fields = [(0b11111111111110, 14), (0, 1), (0, 1), (0b0111, 4), (5, 4), (0, 4), (0b100, 3), (0, 1)]
        fields += flac._utf8(fi) + [(n - 1, 16)]
        head = flac._pack(np.array([v for v, _ in fields], np.uint64), np.array([w for _, w in fields]))
        head += bytes([flac.crc8(head)])
        frame = head + flac._pack(np.array([0, 0, 0, 1000], np.uint64), np.array([1, 6, 1, 16]))
        out.append(frame + flac.crc16(frame).to_bytes(2, "big"))
    info = (b"\x10\x00\x10\x00" + bytes(6) + ((16000 << 44) | (15 << 36) | total).to_bytes(8, "big") + md5)
    return b"fLaC" + bytes([0x80, 0, 0, 34]) + info + b"".join(out)


def test_flac_refuses_a_stream_that_would_decode_to_gigabytes():
    bomb = _craft(2000 * 65536, 2000)                  # ~20 KB saying (and holding) 131 M samples: 2.6 GB as int64
    assert len(bomb) < 30_000
    t0 = time.monotonic()
    with pytest.raises(flac.FlacError, match="over the"):
        flac.decode(bomb)
    with pytest.raises(flac.FlacError, match="only"):    # the header lies small: the first frame is longer than it
        flac.decode(_craft(1000, 2000))
    assert time.monotonic() - t0 < 1.0
    with pytest.raises(flac.FlacError, match="MD5"):     # no MD5: not one encode() writes
        flac.decode(_craft(65536, 1, md5=bytes(16)))
    out, _ = flac.decode(_craft(2 * 65536, 2, md5=__import__("hashlib").md5(np.full(2 * 65536, 1000, "<i2").tobytes()).digest()),
                         max_samples=1 << 20)
    assert len(out) == 2 * 65536 and (out == 1000).all()      # (the crafted shape is valid FLAC otherwise)
    assert flac.info(bomb)[:2] == (16000, 2000 * 65536)
    with pytest.raises(ValueError):                    # and the hub refuses it before decoding anything
        sttinput.from_wire({"format": "flac", "input": "stt-src", "rate": 16000, "samples": 2000 * 65536,
                            "audio": base64.b64encode(bomb).decode()})


def _fixed0(us: list[int], k: int, tail: bytes = b"", n: int | None = None) -> bytes:
    """One FIXED order-0 frame of n (default len(us)) samples whose residuals are Rice codes (parameter k) of the
    folded values `us`, then `tail`; STREAMINFO says n samples, with the MD5 of what `us` decode to (never zero)."""
    import hashlib
    n = n or len(us)
    fields = [(0b11111111111110, 14), (0, 1), (0, 1), (0b0111, 4), (5, 4), (0, 4), (0b100, 3), (0, 1)]
    fields += flac._utf8(0) + [(n - 1, 16)]
    head = flac._pack(np.array([v for v, _ in fields], np.uint64), np.array([w for _, w in fields]))
    head += bytes([flac.crc8(head)])
    vals = [0, 0b001000, 0, 0, 0, k] + [(1 << k) | (u & ((1 << k) - 1)) for u in us]
    widths = [1, 6, 1, 2, 4, 4] + [(u >> k) + 1 + k for u in us]
    frame = head + flac._pack(np.array(vals, np.uint64), np.array(widths))
    frame += flac.crc16(frame).to_bytes(2, "big")
    x = np.array([-((u + 1) >> 1) if u & 1 else u >> 1 for u in us], "<i2")
    info = (b"\x10\x00\x10\x00" + bytes(6) + ((16000 << 44) | (15 << 36) | n).to_bytes(8, "big")
            + hashlib.md5(x.tobytes()).digest())
    return b"fLaC" + bytes([0x80, 0, 0, 34]) + info + frame + tail


def test_flac_refuses_a_run_of_zero_bits_fast_and_in_bounded_memory():
    """8 MB of zero bits after one Rice code start: a unary run no 16-bit residual needs. Refused at once, without a
    window (or arrays) the size of the blob."""
    import tracemalloc
    bomb = _fixed0([], 0, tail=bytes(8 << 20), n=1024)          # header totals match, MD5 non-zero, then zeros
    small = _fixed0([], 0, tail=bytes(1 << 20), n=1024)
    assert flac.info(bomb)[1] == 1024 and flac.info(bomb)[2] != bytes(16)
    # its header checks out, so the hub's ingest keeps it: decoding it (read_stt_audio) must defend itself
    data, _ = sttinput.from_wire({"format": "flac", "input": "stt-src", "rate": 16000, "samples": 1024,
                                  "audio": base64.b64encode(small).decode()})
    assert data == small
    for data in (small, bomb):
        tracemalloc.start()
        t0 = time.monotonic()
        with pytest.raises(flac.FlacError):
            flac.decode(data)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert time.monotonic() - t0 < 2.0 and peak < 160 * 2 ** 20      # (64 MB of it: the blob's own bits)


def test_flac_decodes_a_long_but_valid_rice_code_in_a_bigger_window():
    """A residual near the 16-bit limit with k = 0 is a unary run of ~60000 bits: longer than the first window."""
    us = [0, 60000, 3, 0, 65535, 1] + [2] * 200
    out, _ = flac.decode(_fixed0(us, 0))
    assert out.tolist() == [-((u + 1) >> 1) if u & 1 else u >> 1 for u in us]
    out, _ = flac.decode(_fixed0([7, 65000, 9], 3))
    assert out.tolist() == [-4, 32500, -5]
    long_run = _fixed0([0, 2], 0)                       # then the second code's run made longer than any residual
    head = long_run[:-4]                                 # (its frame without the last code bits / CRC)
    with pytest.raises(flac.FlacError, match="longer than any"):
        flac.decode(head + bytes((flac.MAX_QUOTIENT >> 3) + 16))


# -- the station: made off the radio path, sent on its own lane ------------------------------------------------------


class RecordingWhisper:
    """whisper-server stand-in that keeps every array it was given (Transcriber._run -> client.transcribe)."""

    def __init__(self, texts):
        self.texts, self.got = texts, []

    def transcribe(self, audio, prompt="", **kw):
        self.got.append(np.array(audio, copy=True))
        return self.texts.get(round(len(audio) / RATE), "Engine 60 responding"), 0.9


def offline_station(tmp_path, texts=None, client=None, caps=("p25_systems", "stt_input")):
    """A station whose hub isn't there (or is `client`): everything waits in its outbox, where the test reads it.
    caps: what the hub last said it takes (a station encodes STT inputs only for a hub that takes them)."""
    from sizeup.app import make_app
    app = make_app(station_cfg(tmp_path, f"http://127.0.0.1:{free_port()}"))
    app.link._set_caps(caps)
    if client is not None:
        app.link._client = client
    app.stt.client = RecordingWhisper(texts or {})
    app.stt.start()
    app.link.start()
    return app


def queued(app, kind, cid):
    rows = [r for r in app.link.outbox.batch(max_bytes=1 << 30) if r["type"] == kind and r["data"].get("id") == cid]
    return rows[0] if rows else None


def stt_rows(app, cid):
    return [r for r in app.link.outbox.stt_batch(max_bytes=1 << 30, max_items=1000) if r["data"]["id"] == cid]


def whisper_input(arr) -> np.ndarray:
    """What whisper-server reads from WhisperClient's WAV: the float array as int16 (stt.wav_bytes)."""
    return (np.clip(arr, -1, 1) * 32767).astype("<i2")


def test_fm_traffic_goes_up_as_before_and_its_exact_whisper_input_on_its_own_lane(tmp_path):
    st = offline_station(tmp_path)
    try:
        now = time.time()
        a = voice(3.0, seed=7)
        st._segment(Channel(453.0375, "FG1"), Segment(now, now + 3.0, a, 20.0, []))
        tid = f"FG1-{int(now * 1000)}"
        assert wait(lambda: queued(st, "traffic", tid) is not None and stt_rows(st, tid), 10)
        ev = queued(st, "traffic", tid)
        assert "stt" not in ev and set(ev) == {"seq", "at", "type", "data", "audio", "audio_dropped"}
        with wave.open(io.BytesIO(ev["audio"])) as w:                   # the listening clip: as before
            assert w.getframerate() == 8000
        [row] = stt_rows(st, tid)
        assert row["meta"] == {"v": 1, "format": "flac", "input": "stt-src", "kind": "traffic", "rate": 16000,
                               "samples": len(a)}
        assert row["data"] == {"id": tid, "kind": "traffic", "source": "FG1", "talkgroup": None, "system": "",
                               "end": pytest.approx(now + 3.0)}
        pcm, rate = flac.decode(row["flac"])
        assert rate == 16000 and len(st.stt.client.got) == 1
        assert np.array_equal(pcm, whisper_input(st.stt.client.got[0]))   # byte for byte what whisper transcribed
        assert np.array_equal(pcm, sttinput.stt_pcm(a, fm=True))           # FM: de-emphasised, as whisper's
        assert not np.array_equal(pcm, sttinput.stt_pcm(a, fm=False))
        # WhisperClient's own WAV body (its 0.5 s pads) is what the hub can rebuild from it
        assert sttinput.SttAudio(pcm, 16000, "stt-src").whisper_wav() == wav_bytes(
            np.concatenate([np.zeros(8000, np.float32), st.stt.client.got[0].astype(np.float32), np.zeros(8000, np.float32)]))
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
        assert wait(lambda: stt_rows(st, t.id) and st.stt.client.got, 10)
        [row] = stt_rows(st, t.id)
        assert row["meta"]["rate"] == 16000 and row["meta"]["input"] == "stt-src" and "voice_offset_s" not in row["meta"]
        assert row["data"]["talkgroup"] == (307 if kind == "p25" else None)
        pcm, _ = flac.decode(row["flac"])
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
        assert wait(lambda: stt_rows(st, t.id), 10)
        pcm, _ = flac.decode(stt_rows(st, t.id)[0]["flac"])
        assert np.array_equal(pcm, sttinput.stt_pcm(a, fm=True))
    finally:
        st.link.stop()


def test_a_page_goes_up_at_once_and_its_voice_after_the_tones_on_its_own(tmp_path):
    st = offline_station(tmp_path, {6: PAGE_TEXT})
    try:
        a = np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0), np.zeros(int(0.2 * RATE)), voice(6.0)]).astype(np.float32)
        now = time.time()
        st._segment(Channel(470.2, "South 2"), Segment(now, now + len(a) / RATE, a, 25.0))
        assert wait(lambda: st.pages and st.pages[0].status == "done" and stt_rows(st, st.pages[0].id), 15)
        page = st.pages[0]
        ev = queued(st, "page", page.id)
        assert ev is not None and ev["audio"] and "stt" not in ev
        [row] = stt_rows(st, page.id)                                       # made once
        meta = row["meta"]
        assert page.voice_offset_s > 3.5 and meta["voice_offset_s"] == round(page.voice_offset_s, 4)
        assert meta["kind"] == "page" and row["data"]["label"] == "South 2"
        pcm, rate = flac.decode(row["flac"])
        assert rate == 16000 and len(pcm) == len(page.voice) == meta["samples"]
        assert np.array_equal(pcm, whisper_input(st.stt.client.got[0]))      # the voice whisper got, tones cut off
    finally:
        st.link.stop()


def test_the_radio_path_never_waits_on_the_encoding(tmp_path, monkeypatch):
    """App._stt only queues a reference: a slow encoder (a 60 s page on a busy station) holds no radio thread, and the
    transmission's event is out before its STT input exists."""
    st = offline_station(tmp_path)
    gate, seen = __import__("threading").Event(), []
    real = sttinput._blob

    def slow(*a):
        seen.append(__import__("threading").current_thread().name)
        gate.wait(10)
        return real(*a)
    monkeypatch.setattr(sttinput, "_blob", slow)
    try:
        t0 = time.monotonic()
        t = _transmission(st, "FG1-1790000000777", "FG1", voice(2.0), True, eng="aws")
        assert time.monotonic() - t0 < 0.5
        assert queued(st, "traffic", t.id) is not None and stt_rows(st, t.id) == []
        assert wait(lambda: seen == ["hub-stt-input"], 5)
        gate.set()
        assert wait(lambda: stt_rows(st, t.id), 10)
    finally:
        gate.set()
        st.link.stop()


def test_the_encoder_is_bounded_and_counts_every_drop_by_reason():
    made, gate = [], __import__("threading").Event()

    def sink(d, data, meta, **kw):
        gate.wait(10)
        if d["id"] == "FG1-bad":
            raise OSError("disk full")
        made.append(d["id"])
    enc = sttinput.Encoder(sink, log=lambda m: None, max_items=2, max_s=100.0)
    items = [Traffic(f"FG1-{k}", "FG1", 0.0, 1.0, "") for k in range(5)]
    a = voice(1.0)
    assert enc.submit(items[0], a)
    assert wait(lambda: enc.stats()["queued"] == 0, 5)                     # taken: the worker waits in the sink
    assert enc.submit(items[1], a) and enc.submit(items[2], a)
    assert not enc.submit(items[3], a)                                      # full: dropped at once, never blocks
    assert not enc.submit(items[4], np.zeros(sttinput.MAX_SECONDS * RATE + 1, np.float32))
    assert enc.submit(Traffic("FG1-bad", "FG1", 0.0, 1.0, ""), a) is False  # (still full)
    gate.set()
    assert wait(lambda: made == ["FG1-0", "FG1-1", "FG1-2"], 10)
    assert enc.submit(Traffic("FG1-bad", "FG1", 0.0, 1.0, ""), a)
    assert wait(lambda: enc.stats()["dropped"]["failed"] == 1, 5)
    assert enc.stats()["dropped"] == {"queue": 2, "too_big": 1, "failed": 1} and enc.stats()["made"] == 3
    enc.stop()
    assert not enc.submit(items[0], a)


def test_the_station_status_shows_dropped_stt_inputs_by_reason(tmp_path):
    st = offline_station(tmp_path)
    try:
        st.link.outbox.dropped_stt = 3
        st.link.stt_encoder.dropped["queue"] = 2
        assert "5 STT input(s) dropped" in st.link.status_row()["detail"]
        si = st.link.status(talkgroups=False)["stt_input"]
        assert si["dropped"] == {"queue": 2, "too_big": 0, "failed": 0, "outbox": 3, "refused": 0}
    finally:
        st.link.stop()


# -- the sender: its own lane, under the events, only to a hub that takes them ------------------------------------


class FakeHub:
    """A hub link stand-in: the state answer says what it takes; events fail while `down`; STT posts recorded."""

    def __init__(self, caps):
        self.caps, self.down, self.stt_posts, self.lock = caps, True, [], __import__("threading").Lock()

    def __call__(self, timeout):
        return self

    def close(self):
        pass

    def request(self, method, path, body=None, timeout=None, gzip_body=False, raw_gzip=None):
        from sizeup.cloud.client import HubError
        if path == "/api/station/state":
            return {"ok": True, "build": "", "caps": self.caps}
        if path == "/api/station/events":
            if self.down:
                raise HubError("events: hub busy", 503)
            return {"acked": max(e["seq"] for e in body["events"])}
        if path == "/api/station/stt":
            with self.lock:
                self.stt_posts.append((body, gzip_body))
            return {"ok": True, "stored": [it["n"] for it in body["items"]], "refused": []}
        if path.startswith("/api/station/jobs"):
            time.sleep(0.2)
            return {"jobs": []}
        return {}


def test_stt_inputs_wait_while_any_event_waits_and_go_gzipped_after(tmp_path):
    hub = FakeHub(["p25_systems", "stt_input"])
    st = offline_station(tmp_path, client=hub)
    try:
        t = _transmission(st, "FG1-1790000000555", "FG1", voice(2.0), True, eng="aws")
        assert wait(lambda: stt_rows(st, t.id), 10)
        time.sleep(1.0)
        assert hub.stt_posts == [] and st.link.outbox.pending()[0] >= 1     # the event can't go: neither may its blob
        st.link.adopt("st-renamed")                     # the hub renamed it (#660): its STT posts say the new id too
        hub.down = False
        assert wait(lambda: hub.stt_posts and not st.link.outbox.stt_pending()[0], 15)
        assert hub.stt_posts[0][0]["station"] == "st-renamed"
        assert st.link.outbox.pending()[0] == 0
        body, gz = hub.stt_posts[0]
        assert gz and body["items"][0]["id"] == t.id and body["items"][0]["stt"]["format"] == "flac"
        assert st.link.stt_stats()["sent"] == 1
    finally:
        st.link.stop()


def test_an_older_hub_never_gets_them_and_nothing_is_encoded_for_it(tmp_path):
    hub = FakeHub(["p25_systems"])
    hub.down = False
    st = offline_station(tmp_path, client=hub, caps=("p25_systems",))
    try:
        t = _transmission(st, "FG1-1790000000556", "FG1", voice(2.0), True, eng="aws")
        assert wait(lambda: not st.link.outbox.pending()[0], 10)
        time.sleep(1.0)
        assert hub.stt_posts == [] and stt_rows(st, t.id) == [] and st.link.stt_stats()["hub_takes"] is False
        assert st.link.stt_encoder.stats() == {"queued": 0, "made": 0, "dropped": {"queue": 0, "too_big": 0, "failed": 0}}
    finally:
        st.link.stop()


# -- the outbox: its own lane, first to go ----------------------------------------------------------------------------


def test_the_outbox_drops_stt_inputs_before_any_clip_and_counts_them(tmp_path):
    ob = Outbox(tmp_path / "o.sqlite", max_audio_bytes=6000)                 # STT inputs' own budget: 1800
    for k in range(3):
        ob.add("traffic", {"id": f"FG1-{k}"}, b"a" * 1000)
        ob.add_stt({"id": f"FG1-{k}"}, b"s" * 500, {"rate": 16000})
    assert [r["data"]["id"] for r in ob.stt_batch()] == ["FG1-0", "FG1-1", "FG1-2"]
    ob.add_stt({"id": "FG1-3"}, b"s" * 500, {"rate": 16000})             # over their budget: the oldest
    assert [r["data"]["id"] for r in ob.stt_batch()] == ["FG1-1", "FG1-2", "FG1-3"] and ob.dropped_stt == 1
    ob.add("traffic", {"id": "FG1-4"}, b"a" * 2000)            # the whole over the limit: STT inputs go, no clip
    assert ob.stt_pending() == (2, 1000) and ob.dropped_stt == 2 and ob.dropped_audio == 0     # (5000 + 1000)
    assert all(r["audio"] is not None for r in ob.batch())
    ob.add("traffic", {"id": "FG1-5"}, b"a" * 1500)            # only then a clip: the last STT inputs first
    assert ob.stt_pending() == (0, 0) and ob.dropped_stt == 4 and ob.dropped_audio == 1
    assert ob.audio_bytes + ob.stt_bytes <= 6000
    # the events never wait for them: their own rows, their own acks
    assert [r["type"] for r in ob.batch()] == ["traffic"] * 5


def test_the_stt_lane_batches_acks_and_survives_a_restart(tmp_path):
    ob = Outbox(tmp_path / "o.sqlite", max_audio_bytes=100_000)
    for k in range(4):
        ob.add_stt({"id": f"FG1-{k}", "kind": "traffic"}, bytes([k]) * 1000, {"rate": 16000, "samples": 10})
    rows = ob.stt_batch(max_bytes=2500)
    assert [r["data"]["id"] for r in rows] == ["FG1-0", "FG1-1"]                # up to the size, at least one
    assert len(ob.stt_batch(max_bytes=10)) == 1
    assert ob.stt_ack([rows[1]["n"]]) == 1                                      # any of them, by its row
    ob.close()
    again = Outbox(tmp_path / "o.sqlite", max_audio_bytes=100_000)
    assert again.stt_pending() == (3, 3000) and again.stt_bytes == 3000
    assert [r["data"]["id"] for r in again.stt_batch()] == ["FG1-0", "FG1-2", "FG1-3"]
    assert again.stt_batch()[0]["meta"] == {"rate": 16000, "samples": 10} and again.pending() == (0, None)


def test_an_outbox_from_before_the_stt_lane_opens_and_sends_its_rows(tmp_path):
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
    assert len(rows) == 1 and rows[0]["audio"] == b"\x01\x02" and ob.epoch == "e0" and ob.stt_pending() == (0, 0)


# -- the hub: stored beside the clip, read for its engines ----------------------------------------------------------


def _event(seq, tid, a, fm=True, end=None):
    end = end or time.time()
    data = {"id": tid, "source": "FG1", "start": end - len(a) / RATE, "end": end, "text": "Engine 60 responding",
            "status": "done", "unit": "", "talkgroup": None, "rig": "", "radio_id": "", "raw": "Engine 60 responding",
            "confidence": 0.9, "model": "m.bin", "attach": True, "pairs": None}
    clip = wav_bytes(radio_voice(a, deemph=fm)[::2], 8000)
    return {"seq": seq, "at": end, "type": "traffic", "data": data, "audio": base64.b64encode(clip).decode()}


def _item(n, tid, a, fm=True, kind="traffic", end=None, **more):
    pcm = sttinput.stt_pcm(a, fm)
    meta = {"v": 1, "format": "flac", "input": "stt-src", "kind": kind, "rate": 16000, "samples": len(pcm)} | more
    where = {"source": "FG1", "talkgroup": None, "system": ""} if kind == "traffic" else {"label": "South 2"}
    return {"n": n, "id": tid, "kind": kind, **where, "end": end or time.time(),
            "stt": sttinput.to_wire(flac.encode(pcm, 16000), meta)}


def test_the_hub_stores_the_stt_input_whenever_it_comes_and_reads_it_for_its_engines(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        a, b = voice(2.0, seed=1), voice(1.5, seed=2)
        # the STT input first (its own lane), its event after: either order
        code, r = call(hub.url + "/api/station/stt", "POST", {"v": 1, "items": [_item(7, "FG1-1790000000001", a)]})
        assert code == 200 and r == {"ok": True, "stored": [7], "refused": []}
        batch = {"epoch": "e1", "events": [_event(1, "FG1-1790000000001", a), _event(2, "FG1-1790000000002", b)]}
        assert call(hub.url + "/api/station/events", "POST", batch)[1]["acked"] == 2
        got = hub.app.read_stt_audio("FG1-1790000000001", voice_offset_s=3.0)
        assert got.input == "stt-src" and got.rate == 16000
        assert got.voice_offset_s == 0.0                    # a transmission's: never the caller's page offset
        assert np.array_equal(got.pcm, sttinput.stt_pcm(a, True)) and got.flac[:4] == b"fLaC"
        assert np.array_equal(got.pcm8k(), got.pcm[::2])
        assert json.loads(hub.app.audio.note("FG1-1790000000001.src"))["kind"] == "traffic"
        # its STT input not here (an older station, dropped, still on its way): the listening clip, said so
        old = hub.app.read_stt_audio("FG1-1790000000002")
        assert old.input == "listening-8k" and old.rate == 8000 and old.flac is None
        assert np.array_equal(old.pcm8k(), old.pcm) and len(old.whisper_wav()) == 44 + 2 * (2 * len(old.pcm) + 16000)
        assert hub.app.read_stt_audio("FG1-0") is None and hub.app.read_stt_audio("../x") is None
        # ... or wait a little for it
        import threading
        threading.Timer(0.3, lambda: call(hub.url + "/api/station/stt", "POST",
                                          {"items": [_item(8, "FG1-1790000000002", b)]})).start()
        t0 = time.monotonic()
        late = hub.app.read_stt_audio("FG1-1790000000002", wait_s=5.0)
        assert late.input == "stt-src" and time.monotonic() - t0 < 4.0
        # never a clip anyone plays: /api/audio refuses it, the clip itself is as before
        assert call(hub.url + "/api/audio/FG1-1790000000001.src.wav", token=None)[0] == 404
        assert hub.get("/api/audio/FG1-1790000000001.wav")[:4] == b"RIFF"
        assert not hub.app.has_clip("FG1-1790000000001.src") and hub.app.has_clip("FG1-1790000000001")
        # a station's token only: as every station route
        assert call(hub.url + "/api/station/stt", "POST", {"items": []}, token=None)[0] == 401
        assert "stt_input" in call(hub.url + "/api/station/state", "POST", {"at": time.time()})[1]["caps"]
    finally:
        hub.close()


def test_the_hub_refuses_a_bad_or_crafted_stt_input_and_keeps_the_rest(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        a = voice(1.0, seed=4)
        good = _item(1, "FG1-1790000000011", a)
        not_flac = _item(2, "FG1-1790000000012", a)
        not_flac["stt"]["audio"] = base64.b64encode(b"not flac").decode()
        lies = _item(3, "FG1-1790000000013", a)
        lies["stt"]["samples"] = 5                                 # its header says otherwise
        bomb = _item(4, "FG1-1790000000014", a)
        bomb["stt"]["audio"] = base64.b64encode(_craft(2000 * 65536, 2000)).decode()
        bomb["stt"]["samples"] = 2000 * 65536
        bad_id = _item(5, "../etc", a)
        bad_kind = _item(6, "FG1-1790000000016", a, kind="alarm")
        items = [good, not_flac, lies, bomb, bad_id, bad_kind, {"n": "x"}]
        code, r = call(hub.url + "/api/station/stt", "POST", {"items": items})
        assert code == 200 and r["stored"] == [1] and r["refused"] == [2, 3, 4, 5, 6]
        assert hub.app.read_stt_audio("FG1-1790000000011").input == "stt-src"
        assert not hub.app.audio.has("FG1-1790000000014.src")
        assert call(hub.url + "/api/station/stt", "POST", {"nope": 1})[0] == 400
    finally:
        hub.close()


def test_a_failed_store_never_blocks_and_a_fenced_one_is_not_acked(tmp_path, monkeypatch):
    from sizeup import fence as fence_mod
    hub = HubRun(hub_cfg(tmp_path))
    try:
        a = voice(1.0)
        monkeypatch.setattr(sttinput, "store", lambda *x, **k: (_ for _ in ()).throw(OSError("disk")))
        code, r = call(hub.url + "/api/station/stt", "POST", {"items": [_item(1, "FG1-1790000000021", a)]})
        assert code == 200 and r["refused"] == [1]
        monkeypatch.setattr(sttinput, "store", lambda *x, **k: (_ for _ in ()).throw(fence_mod.FencedWrite("not the leader")))
        with pytest.raises(fence_mod.FencedWrite):
            hub.app.put_stt_input(_item(2, "FG1-1790000000022", a))
    finally:
        hub.close()


def test_a_page_read_from_its_clip_starts_at_its_voice(tmp_path):
    db = AudioDb(":memory:")
    a = np.concatenate([np.full(8000, 1000, np.int16), np.arange(8000, dtype=np.int16)])
    db.put("SOUTH2-1", wav_bytes(a, 8000), "clip", at=1.0, channel="South 2")
    got = sttinput.read(db, "SOUTH2-1", voice_offset_s=1.0)
    assert got.input == "listening-8k" and got.voice_offset_s == 1.0 and np.array_equal(got.pcm, a[8000:])
    pcm = _speechlike(RATE)
    sttinput.store(db, "SOUTH2-1", flac.encode(pcm, 16000),
                   {"input": "stt-src", "kind": "page", "rate": 16000, "voice_offset_s": 4.25}, 1.0, channel="South 2")
    got = sttinput.read(db, "SOUTH2-1", voice_offset_s=1.0)
    assert got.input == "stt-src" and got.voice_offset_s == 4.25 and np.array_equal(got.pcm, pcm)


def test_an_unreadable_stt_input_falls_back_to_the_clip(tmp_path, monkeypatch):
    db = AudioDb(":memory:")
    db.put("FG1-1", wav_bytes(np.zeros(800, np.int16), 8000), "clip", at=1.0, channel="FG1")
    sttinput.store(db, "FG1-1", flac.encode(_speechlike(1600), 16000), {"kind": "traffic"}, 1.0, "FG1")
    said = []
    monkeypatch.setattr(flac, "decode", lambda *a, **k: (_ for _ in ()).throw(MemoryError()))
    assert sttinput.read(db, "FG1-1", log=said.append).input == "listening-8k" and "MemoryError" in said[0]


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


def test_the_orphan_sweep_restores_an_stt_input_with_its_clip(tmp_path):
    """A restore a little behind lost both rows: the clip the hub still knows comes back, and its STT input with it."""
    store = FsAudioStore(tmp_path / "bucket")
    db = AudioDb(tmp_path / "audio.sqlite", store=store, may_delete=lambda: True)
    now = time.time()
    old = now - 10 * 86400
    for cid in ("FG1-1", "FG1-2"):
        db.put(cid, wav_bytes(np.zeros(800, np.int16), 8000), "clip", at=old, channel="FG1")
        sttinput.store(db, cid, flac.encode(_speechlike(1600), 16000), {"kind": "traffic"}, old, "FG1")
    db.put("FG1-9", wav_bytes(np.zeros(800, np.int16), 8000), "clip", at=now + 2 * 3600, channel="FG1")   # the newest
    assert db.upload_pending(limit=50) == 5
    for p in (tmp_path / "bucket").rglob("*.*"):
        __import__("os").utime(p, (old, old))
    db.db.execute("DELETE FROM audio WHERE id IN ('FG1-2', 'FG1-2.src')")
    db._forget(["FG1-2", "FG1-2.src"])
    n = db.sweep_orphans(min_age_s=0, now=now, known=lambda ids: {i: ("FG1", None, old) for i in ids if i == "FG1-2"})
    assert n == 0 and db.sweep_restored == 2
    assert {"FG1-2", "FG1-2.src"} <= {r[0] for r in db.db.execute("SELECT id FROM audio")}
    assert sttinput.read(db, "FG1-2").input == "stt-src"


def test_the_hub_health_shows_dropped_stt_inputs_by_reason(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        status = {"id": "t", "name": "Test", "radios": [], "outbox": {"pending": 0, "oldest": None, "dropped_audio": 0},
                  "stt_input": {"dropped": {"queue": 2, "outbox": 3, "too_big": 0, "failed": 0, "refused": 0}}}
        assert call(hub.url + "/api/station/state", "POST", {"at": time.time(), "status": status})[0] == 200
        ob = hub.app.hub.health_.model()["outbox"]
        assert ob["dropped_stt"] == 5 and ob["dropped_stt_why"] == {"queue": 2, "outbox": 3}
    finally:
        hub.close()


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
        got = hub.app.read_stt_audio(tid, wait_s=15.0)
        assert got.input == "stt-src"
        assert np.array_equal(got.pcm, whisper_input(st.stt.client.got[0]))
        assert wait(lambda: st.link.stt_stats()["sent"] == 1 and not st.link.outbox.stt_pending()[0], 10)
        # the station's .src.wav is the audio before radio_voice; its whisper input, made again from it, matches
        # the hub's to within the .src.wav's own int16 rounding
        with wave.open(str(tmp_path / "st" / "clips" / f"{tid}.src.wav")) as w:
            src = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32767
        again = sttinput.stt_pcm(src, fm=True).astype(np.int32)
        assert len(again) == len(got.pcm) and np.abs(again - got.pcm).max() <= 2
        # and the WAV a GPU worker gets is the very body WhisperClient posted
        posted = wav_bytes(np.concatenate([np.zeros(8000, np.float32), st.stt.client.got[0], np.zeros(8000, np.float32)]))
        assert got.whisper_wav() == posted
    finally:
        st.link.stop()
        hub.close()


def test_the_stt_post_claims_its_station_and_one_bad_item_never_fails_it(tmp_path, monkeypatch):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        a = voice(1.0, seed=6)
        assert call(hub.url + "/api/station/state", "POST", {"at": time.time(), "status": {"id": "t"}})[0] == 200
        code, r = call(hub.url + "/api/station/stt", "POST", {"station": "st-elsewhere", "items": [_item(1, "FG1-1790000000031", a)]})
        assert code in (401, 409) and not hub.app.audio.has("FG1-1790000000031.src")      # another station's id: refused
        real, woke = hub.app.put_stt_input, []

        def flaky(it, **kw):
            if it["id"] == "FG1-1790000000032":
                raise KeyError("boom")
            return real(it, **kw)
        monkeypatch.setattr(hub.app, "put_stt_input", flaky)
        monkeypatch.setattr(hub.app, "stt_stored", lambda: woke.append(1))
        items = [_item(1, "FG1-1790000000031", a), _item(2, "FG1-1790000000032", a), _item(3, "FG1-1790000000033", a)]
        code, r = call(hub.url + "/api/station/stt", "POST", {"station": "t", "items": items})
        assert code == 200 and r["stored"] == [1, 3] and r["refused"] == [2]
        assert woke == [1]                                                    # the waiters: woken once per post
    finally:
        hub.close()


def test_read_stt_audio_waits_at_most_its_cap(tmp_path, monkeypatch):
    from sizeup.cloud import hub as hub_mod
    hub = HubRun(hub_cfg(tmp_path))
    try:
        monkeypatch.setattr(hub_mod, "STT_WAIT_MAX_S", 0.3)
        t0 = time.monotonic()
        assert hub.app.read_stt_audio("FG1-1790000000041", wait_s=1e9) is None
        assert time.monotonic() - t0 < 2.0
    finally:
        hub.close()
