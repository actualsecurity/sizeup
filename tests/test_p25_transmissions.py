"""One row per transmission when several radios key up on one trunk-recorder call (#638).

trunk-recorder writes one WAV + JSON per call; srcList / freqList place each transmission in it ("pos": the sum of
the earlier transmissions' lengths). The station's build is patched (deploy/station/install-linux.sh tr_patch) so a
new radio ID on the voice channel ends the transmission even when no terminator was decoded (Phase 2 TDMA ends one
only on MAC_HANGTIME), so each talker is its own srcList entry. These calls are written the way that build writes
them: synthetic audio (one tone per transmission, so a clip shows which transmission it holds), invented radio IDs.
"""

from __future__ import annotations

import json
import re
import subprocess
import wave
from pathlib import Path

import numpy as np

from sizeup.radio.live import LiveTalk
from sizeup.radio.p25 import CallIngest, apply_grants, parse_call, read_wav_16k, split_call
from tests.test_app import wait
from tests.test_p25_systems import PD, two  # noqa: F401 (fixture)

ROOT = Path(__file__).resolve().parent.parent
LABELS = {307: "Fire/EMS 15", 301: "60 Control", PD: "PD Dispatch"}
T0 = 1_790_954_731.97
A, B, C = 9900101, 9900202, 9900303            # invented radio IDs
TONES = (500.0, 900.0, 1300.0, 1700.0, 2100.0)


def tdma_call(d: Path, name: str, txs, *, tg=PD, system="mrrs", start=T0, rate=8000, tdma=True, encrypted=False,
              wav=True) -> list[float]:
    """txs: (src, seconds, gap after) per transmission, as trunk-recorder 5.2.0 writes them (pos = the earlier ones'
    lengths, srcList "time" whole seconds, freqList "len" per transmission). Each transmission a tone of its own.
    Returns the wall-clock start of each."""
    src_list, freq_list, pcm, starts = [], [], [], []
    at, pos = start, 0.0
    for k, (src, sec, gap) in enumerate(txs):
        starts.append(at)
        src_list.append({"src": src, "time": int(at), "pos": round(pos, 2), "emergency": 0, "signal_system": "",
                         "tag": "", "tag_ota": ""})
        freq_list.append({"freq": 770181250, "time": int(at), "pos": round(pos, 2), "len": sec, "error_count": 0,
                          "spike_count": 0})
        n = int(round(sec * rate))
        pcm.append(0.3 * np.sin(2 * np.pi * TONES[k % len(TONES)] * np.arange(n) / rate))
        pos += sec
        at += sec + gap
    stop = starts[-1] + txs[-1][1]
    meta = {"freq": 770181250, "freq_error": 0, "signal": 0, "noise": 0, "source_num": 0, "recorder_num": 1,
            "tdma_slot": 1 if tdma else 0, "phase2_tdma": int(tdma), "start_time": int(start), "stop_time": int(stop),
            "start_time_ms": int(round(start * 1000)), "stop_time_ms": int(round(stop * 1000)), "emergency": 0,
            "priority": 0, "mode": 0, "duplex": 0, "encrypted": int(encrypted), "call_length": int(round(pos)),
            "call_length_ms": int(round(pos * 1000)), "talkgroup": tg, "talkgroup_tag": "", "talkgroup_description": "",
            "talkgroup_group_tag": "", "talkgroup_group": "", "color_code": -1,
            "audio_type": "digital tdma" if tdma else "digital", "short_name": system,
            "freqList": freq_list, "srcList": src_list}
    (d / f"{name}.json").write_text(json.dumps(meta, indent=2))
    if wav:
        with wave.open(str(d / f"{name}.wav"), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
            w.writeframes((np.concatenate(pcm) * 32767).astype("<i2").tobytes())
    return starts


def day(tmp_path: Path, system="mrrs") -> Path:
    d = tmp_path / system / "2026" / "11" / "16"
    d.mkdir(parents=True)
    return d


def tone(x: np.ndarray, rate: int = 16000) -> float:
    """The clip's loudest frequency (Hz)."""
    f = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / rate)[int(f.argmax())])


def near(f: float, want: float) -> bool:
    return abs(f - want) < 20


def test_three_talkers_in_one_tdma_call_are_three_transmissions_with_their_own_ids_and_audio(tmp_path):
    d = day(tmp_path)
    starts = tdma_call(d, f"{PD}-1790954731.970_770181250.1-call_4", [(A, 2.4, 0.2), (B, 1.6, 0.1), (A, 2.0, 0.0)])
    c = parse_call(d / f"{PD}-1790954731.970_770181250.1-call_4.json", LABELS)
    c.audio = read_wav_16k(Path(c.wav_path))
    pieces = split_call(c)
    assert [(p.id[-3:], p.pairs[0][0], round(p.end - p.start, 2)) for p in pieces] == [
        ("-t0", str(A), 2.4), ("-t1", str(B), 1.6), ("-t2", str(A), 2.0)]
    assert [p.id.rsplit("-t", 1)[0] for p in pieces] == [c.id] * 3          # the whole call, linked by its id
    # each clip holds its own transmission's audio (pos lines up with the WAV), and starts when it was said
    assert [near(tone(p.audio), f) for p, f in zip(pieces, TONES)] == [True] * 3
    assert all(abs(p.start - s) < 1.0 for p, s in zip(pieces, starts))
    assert pieces[0].start < pieces[1].start < pieces[2].start


def test_a_loudnormed_call_at_192_khz_cuts_at_the_same_places(tmp_path):
    # 5.2.0's single-pass loudnorm (calls under 1.5 s, or a failed analysis) writes the WAV at 192 kHz
    d = day(tmp_path)
    tdma_call(d, "k-call_1", [(A, 0.6, 0.0), (B, 0.7, 0.0)], rate=192000)
    c = parse_call(d / "k-call_1.json", LABELS)
    c.audio = read_wav_16k(Path(c.wav_path))
    pieces = split_call(c)
    assert [(p.pairs[0][0], len(p.audio)) for p in pieces] == [(str(A), 9600), (str(B), 11200)]
    assert [near(tone(p.audio), f) for p, f in zip(pieces, TONES)] == [True, True]


def test_the_same_radio_keying_twice_stays_two_transmissions(tmp_path):
    # issue #62's decision: never merged for being the same radio (each key-up its own row)
    d = day(tmp_path)
    tdma_call(d, "s-call_2", [(A, 1.5, 0.3), (A, 1.0, 0.0)])
    c = parse_call(d / "s-call_2.json", LABELS)
    c.audio = read_wav_16k(Path(c.wav_path))
    assert [(p.talker, round(p.end - p.start, 2)) for p in split_call(c)] == [(str(A), 1.5), (str(A), 1.0)]


def test_unknown_radio_ids_stay_unknown_and_short_silent_fragments_join_their_neighbour(tmp_path):
    d = day(tmp_path)
    tdma_call(d, "u-call_3", [(A, 1.2, 0.1), (-1, 1.1, 0.1), (0, 0.9, 0.0)])
    c = parse_call(d / "u-call_3.json", LABELS)
    c.audio = read_wav_16k(Path(c.wav_path))
    assert [(p.talker, p.pairs) for p in split_call(c)] == [(str(A), [(str(A), str(A))]), ("", []), ("", [])]


def test_an_encrypted_call_never_becomes_a_transmission(tmp_path):
    # trunk-recorder writes the JSON of an encrypted call and removes its audio: nothing to ingest
    d = day(tmp_path)
    got: list = []
    ing = CallIngest(tmp_path, LABELS, on_call=got.append).prime()
    tdma_call(d, "e-call_5", [(A, 1.0, 0.1), (B, 1.0, 0.0)], encrypted=True, wav=False)
    assert ing.scan() == [] and ing.scan() == []
    assert parse_call(d / "e-call_5.json", LABELS).encrypted


def test_a_stock_builds_run_on_tdma_transmission_is_still_cut_at_the_grant(tmp_path):
    # Unpatched 5.2.0 on Phase 2: no MAC_HANGTIME decoded between A and B, so one transmission credited to B (the
    # last ID it heard). The unit script saw B granted: the cut is there.
    d = day(tmp_path)
    starts = tdma_call(d, "r-call_6", [(B, 4.0, 0.0)])
    c = parse_call(d / "r-call_6.json", LABELS)
    assert apply_grants(c, [(starts[0] - 0.4, str(A)), (starts[0] + 2.0, str(B))])
    assert [(t.src, t.via, round(t.pos, 2)) for t in c.turns] == [(str(A), "grant", 0.0), (str(B), "srclist", 2.5)]


def test_the_voice_channel_naming_a_new_talker_shows_it_live():
    now = [1000.0]
    lt = LiveTalk(clock=lambda: now[0], resolve=lambda s, r, a: {"unit": "E99"} if r == str(B) else None)
    head = "[2026-11-16 10:00:00.000] [mrrs]\t5C\tTG:   9211\tFreq: 770.181250 MHz\t"
    lt.feed_line(head + "Starting P25 Recorder Num [0]\tTDMA: true\tSlot: 1\tQPSK: true")
    lt.feed_line(f"SIZEUP-UNIT mrrs {A} call 9211")
    assert [e["radio_id"] for e in lt.state()] == [str(A)]
    now[0] += 3.0
    assert lt.feed_line(head + f"Unit ID changed on Voice Channel, ext: {B}\tprevious: {A}\t samples: 24000")
    assert [(e["radio_id"], e["rig"]) for e in lt.state() if not e["ended"]] == [(str(B), "E99")]
    assert lt.grants("mrrs", 9211, 0, 2000) == [(1000.0, str(A))]     # not a grant: the call JSON has it already
    assert not lt.feed_line(head + "Unit ID changed on Voice Channel, ext: -1\tprevious: 1\t samples: 1")


def test_end_to_end_a_call_json_and_wav_become_rows_per_transmission(two, tmp_path):
    app, said, _logs = two
    rt = app.systems["mrrs"]
    d = day(tmp_path / "in")
    ing = CallIngest(tmp_path / "in", LABELS, on_call=lambda c: None).prime()
    name = f"{PD}-1790954731.970_770181250.1-call_9"
    starts = tdma_call(d, name, [(A, 2.0, 0.3), (B, 1.4, 0.2), (C, 1.8, 0.0)])
    got = ing.scan() + ing.scan()
    assert len(got) == 1
    app._call_system(rt, got[0])
    ids = [f"{name}-t{k}" for k in range(3)]
    assert wait(lambda: all(r["status"] != "pending" for r in rows_by_id(app, ids).values()) and
                len(rows_by_id(app, ids)) == 3)
    rows = rows_by_id(app, ids)
    assert [(rows[i]["radio_id"], rows[i]["call_id"], rows[i]["turn"], rows[i]["system"], rows[i]["talkgroup"])
            for i in ids] == [(str(A), name, 0, "mrrs", PD), (str(B), name, 1, "mrrs", PD), (str(C), name, 2, "mrrs", PD)]
    assert [round(rows[i]["end"] - rows[i]["start"], 2) for i in ids] == [2.0, 1.4, 1.8]
    assert all(abs(rows[i]["start"] - s) < 1.0 for i, s in zip(ids, starts))
    assert len(said) == 3                                              # each transcribed on its own
    loose = {t.id: t for t in app.store.loose}
    assert [(loose[i].call_id, loose[i].turn) for i in ids] == [(name, 0), (name, 1), (name, 2)]
    for i, f in zip(ids, TONES):                                        # the audio speech-to-text got
        with wave.open(str(app.clips_dir / f"{i}.src.wav")) as w:
            x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
        assert near(tone(x), f), i


def rows_by_id(app, ids):
    return {r["id"]: r for r in app.cad.store.q("SELECT * FROM radio_traffic") if r["id"] in ids}


# -- the station's trunk-recorder patch ----------------------------------------------------------------------------------


def the_patch() -> str:
    sh = (ROOT / "deploy" / "station" / "install-linux.sh").read_text()
    m = re.search(r"^tr_patch\(\) \{\n  cat <<'PATCH'\n(.*?)^PATCH\n\}\n", sh, re.S | re.M)
    assert m, "tr_patch() not found"
    return m.group(1)


def test_the_patch_is_a_well_formed_diff_of_the_transmission_sink():
    patch = the_patch()
    assert re.findall(r"^diff --git a/(\S+)", patch, re.M) == ["trunk-recorder/gr_blocks/transmission_sink.cc",
                                                              "trunk-recorder/gr_blocks/transmission_sink.h"]
    lines = patch.splitlines()
    k = 0
    hunks = 0
    while k < len(lines):
        m = re.match(r"@@ -\d+,(\d+) \+\d+,(\d+) @@", lines[k])
        k += 1
        if not m:
            continue
        hunks += 1
        old, new = int(m[1]), int(m[2])
        while old or new:
            line = lines[k] if k < len(lines) else "?"
            k += 1
            tag = line[:1] if line else " "
            assert tag in " +-", line
            old -= tag in " -"
            new -= tag in " +"
            assert old >= 0 and new >= 0
    assert hunks == 9
    assert not re.search(r"^-", patch.replace("--- a/", ""), re.M)       # it only adds: stock code paths stay as they are
    for want in ('std::getenv("SIZEUP_TR_SPLIT")', "Unit ID changed on Voice Channel, ext: ",
                 "would split: prev ", "if ((d_split_mode == 2) && !d_conventional)", "#638"):
        assert want in patch, want
    assert "#397" not in patch


def test_the_installer_applies_the_patch_before_the_build_and_names_the_build_by_it():
    sh = (ROOT / "deploy" / "station" / "install-linux.sh").read_text()
    build = sh[sh.index('step "trunk-recorder $TR_TAG"'):sh.index('step "whisper.cpp $WHISPER_TAG"')]
    assert build.index("fetch_source trunk-recorder") < build.index('tr_patch | git -C "$src" apply -') < \
        build.index("cmake -S")
    assert 'want="trunk-recorder $TR_TAG $TR_COMMIT${TR_PATCH_ID:+ patch $TR_PATCH_ID}"' in build
    assert '"${low[@]}" cmake --build "$src/build"' in build and "low=(nice -n 10)" in build
    assert 'TR_B="$TR_PLAIN_B${TR_PATCH_ID:+-p$TR_PATCH_ID}"' in sh
    out = subprocess.run(["bash", "-c", 'eval "$(awk \'/^tr_patch\\(\\) \\{/{on=1} on{print} on&&/^}$/{exit}\' "$1")";'
                          ' tr_patch', "-", str(ROOT / "deploy" / "station" / "install-linux.sh")],
                         capture_output=True, text=True, check=True).stdout
    assert out == the_patch()


def test_ci_tests_the_patch_on_the_real_sink_and_switches_between_builds():
    ci = (ROOT / ".github" / "scripts" / "station-update.sh").read_text()
    assert ".github/scripts/tr-split-test.sh" in ci or "tr-split-test.sh" in ci
    harness = (ROOT / ".github" / "scripts" / "tr_split_test.cc").read_text()
    for case in ("split mid-window", "new ID then terminate, one window", "terminate then new ID, one window",
                 "same ID again", "grant-only ID replaced", "ID flip back", "group mismatch", "conventional: stock",
                 "log: stock, logged", "off: stock", "terminate and new ID at one offset", "log = off: terminators",
                 "log = off: group mismatch"):
        assert f'"{case}"' in harness, case


# -- which systems cut: SIZEUP_TR_SPLIT from each system's config.json ------------------------------------------------------


def test_each_system_starts_its_trunk_recorder_with_its_own_split_mode(tmp_path):
    from sizeup.radio import trunkrecd
    from tests.test_p25_systems import ask_county, ask_system, service
    county, mrrs, pop, _logs, d = service(tmp_path)
    ask_county(tmp_path)
    county.step(100.0)
    ask_system(tmp_path, {0: "pd-a"})
    mrrs.step(101.0)
    assert [p.kw["env"][trunkrecd.SPLIT_ENV] for p in pop.started] == ["log", "on"]        # the repo's: MRRS cuts
    assert "sizeup" not in json.loads((d.parent.parent / "config.runtime.json").read_text())
    assert "sizeup" not in json.loads((d / "config.runtime.json").read_text())
    c = json.loads((d / "config.json").read_text())
    c["sizeup"]["split"] = "log"
    (d / "config.json").write_text(json.dumps(c))
    assert trunkrecd.split_mode(["t", "--config=config.json"], d) == "log"
    assert trunkrecd.split_mode(["t", "--config=config.json"], d.parent.parent) == "log"
    c["sizeup"]["split"] = "yes"
    (d / "config.json").write_text(json.dumps(c))
    assert trunkrecd.split_mode(["t"], d) == "log"                                           # not a mode: the default
    assert trunkrecd.split_mode(["t"], tmp_path / "nowhere") == "log"


def test_the_repo_cuts_mrrs_and_only_logs_on_the_county():
    """#647's review: MRRS "on" (cut where the next talker's ID comes in), the county still "log"."""
    tr = ROOT / "deploy" / "trunk-recorder"
    want = {"config.json": "log", "mrrs": "on"}
    for conf in [tr / "config.json", *sorted((tr / "systems").glob("*/config.json"))]:
        name = conf.name if conf.parent == tr else conf.parent.name
        assert json.loads(conf.read_text())["sizeup"]["split"] == want.get(name, "log"), conf
        assert trunkrecd_mode(conf) == want.get(name, "log")


def trunkrecd_mode(conf: Path) -> str:
    from sizeup.radio import trunkrecd
    return trunkrecd.split_mode(["trunk-recorder", "--config=config.json"], conf.parent)
