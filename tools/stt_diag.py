#!/usr/bin/env python3
"""Why is the station's whisper behind? (GitHub #300). Read-only, stdlib only: run it on the station.

    sizeup-station stt [--hours 6] [--sample 10]       (or: python3 -I /opt/sizeup/current/tools/stt_diag.py)
    sizeup-station stt --bench clip.wav [--runs 3]      also time whisper-server on one clip, a few ways

What it reports, each a line or a short table:

    machine     CPUs, load, CPU pressure (/proc/pressure/cpu: the share of time runnable work waited for a CPU)
    whisper     the command whisper-server runs (model, threads), the model file, threads against the CPUs
    processes   whisper-server, trunk-recorder, rtl_fm, readsb, sizeup: their nice value and CPU use over --sample s
    pace        whisper-server's requests per hour from its journal, and the seconds between back-to-back ones (its
                time per clip while it is busy), with the clips' lengths
    queue       the station's log: clips not transcribed (dropped, skipped, shed) and the hub's AWS fallbacks
    bench       (--bench only) one clip through the running whisper-server: as sizeup sends it, without temperature
                fallback, and with a shorter audio context. It adds a few requests to whisper's queue: run it when
                the station is quiet.

Nothing here changes the station. journalctl may need sudo (or the systemd-journal group) to read the units' logs.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import statistics
import subprocess
import sys
import time
import tomllib
import urllib.request
import uuid
import wave
from collections import Counter, defaultdict

CONF = "/etc/sizeup/config.toml"
ARGV_FILE = "/run/sizeup-whisper/argv"
MODELS = "/opt/sizeup/models/whisper.bin"
WATCH = ("whisper-server", "trunk-recorder", "rtl_fm", "readsb", "python3", "python")
BUSY_GAP_S = 120.0          # back-to-back requests closer than this: whisper was busy between them


def run(cmd: list[str], timeout: float = 60.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def config() -> dict:
    try:
        with open(CONF, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def machine() -> list[str]:
    cpus = os.cpu_count() or 0
    out = [f"machine:   {cpus} CPUs, load {' '.join(open('/proc/loadavg').read().split()[:3]) if os.path.exists('/proc/loadavg') else '?'}"
           + (", WSL2" if "microsoft" in run(["uname", "-r"]).lower() else "")]
    try:
        some = open("/proc/pressure/cpu").readline()
        m = dict(kv.split("=") for kv in some.split()[1:])
        out.append(f"pressure:  CPU waited for {m.get('avg10')}% of the last 10 s, {m.get('avg60')}% of 1 min, "
                   f"{m.get('avg300')}% of 5 min (over ~20%: the box is short of CPU)")
    except (OSError, ValueError):
        out.append("pressure:  /proc/pressure/cpu not available")
    return out


def whisper(cfg: dict) -> list[str]:
    stt = cfg.get("stt", {})
    argv = []
    try:
        argv = open(ARGV_FILE).read().split("\n")
    except OSError:
        line = run(["pgrep", "-a", "whisper-server"]).strip().split("\n")[0]
        argv = line.split()[1:] if line else []
    argv = [a for a in argv if a]
    out = [f"whisper:   {' '.join(argv) if argv else 'whisper-server not found'}"]
    model = os.path.realpath(MODELS) if os.path.exists(MODELS) else stt.get("model", "?")
    size = os.path.getsize(model) / 1e6 if os.path.exists(model) else 0
    out.append(f"model:     {os.path.basename(model)} ({size:.0f} MB)"
               + ("  <- large: several times slower than small.en on a CPU" if "large" in model else ""))
    threads = int(argv[argv.index("-t") + 1]) if "-t" in argv[:-1] else None
    cpus = os.cpu_count() or 0
    if threads is not None:
        note = "ok" if threads <= max(1, cpus - 1) else f"too many: at most {max(1, cpus - 1)} (one fewer than the CPUs)"
        out.append(f"threads:   {threads} on {cpus} CPUs ({note}); [stt] threads = {stt.get('threads', '(unset)')}")
    return out


def _procs() -> dict[int, tuple[str, int, int]]:
    """pid -> (name, nice, utime+stime ticks) for the processes we watch."""
    got = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            stat = open(f"/proc/{d}/stat").read()
            name = stat[stat.index("(") + 1:stat.rindex(")")]
            rest = stat[stat.rindex(")") + 2:].split()
            if name in WATCH:
                if name.startswith("python"):
                    cmd = open(f"/proc/{d}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
                    if "sizeup" not in cmd and "adsbd" not in cmd:
                        continue
                    name = "adsbd (python)" if "adsbd" in cmd else "sizeup (python)"
                got[int(d)] = (name, int(rest[16]), int(rest[11]) + int(rest[12]))
        except (OSError, ValueError, IndexError):
            continue
    return got


def processes(sample_s: float) -> list[str]:
    if not os.path.isdir("/proc"):
        return ["processes: no /proc here (run it on the station)"]
    tick = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    a = _procs()
    time.sleep(sample_s)
    b = _procs()
    rows: dict[str, list] = defaultdict(lambda: [0, set(), 0.0])
    for pid, (name, nice, t1) in b.items():
        t0 = a.get(pid, (name, nice, t1))[2]
        r = rows[name]
        r[0] += 1
        r[1].add(nice)
        r[2] += (t1 - t0) / tick / sample_s * 100
    out = [f"processes: CPU over {sample_s:.0f} s (100% = one core), nice"]
    for name, (n, nices, pct) in sorted(rows.items(), key=lambda kv: -kv[1][2]):
        out.append(f"  {name:18s} x{n:<2d} {pct:6.0f}%   nice {','.join(str(x) for x in sorted(nices))}")
    if not rows:
        out.append("  (none of whisper-server, trunk-recorder, rtl_fm, readsb, sizeup running)")
    return out


def pace(hours: float) -> list[str]:
    """whisper-server's requests from its journal: per hour, and the gap between back-to-back ones."""
    text = run(["journalctl", "-u", "sizeup-whisper", "--since", f"-{int(hours * 60)}min", "-o", "short-unix",
                "--no-pager"], timeout=120)
    if not text.strip():
        return ["pace:      no whisper-server journal (run it with sudo?)"]
    reqs: list[float] = []
    clip_s: list[tuple[float, float]] = []
    for line in text.splitlines():
        parts = line.split(" ", 1)
        try:
            t = float(parts[0])
        except (ValueError, IndexError):
            continue
        if "Received request" in line:
            reqs.append(t)
        m = re.search(r"\(\d+ samples, ([\d.]+) sec\)", line)
        if m:
            clip_s.append((t, float(m.group(1))))
    if not reqs:
        return ["pace:      no requests in whisper-server's journal"]
    out = [f"pace:      whisper-server, the last {hours:g} h (gap = seconds between back-to-back requests = its time per clip)"]
    byhour: dict[str, list] = defaultdict(lambda: [0, [], []])
    for i, t in enumerate(reqs):
        h = time.strftime("%m-%d %H:00", time.localtime(t))
        byhour[h][0] += 1
        if i and t - reqs[i - 1] < BUSY_GAP_S:
            byhour[h][1].append(t - reqs[i - 1])
    for t, s in clip_s:
        byhour[time.strftime("%m-%d %H:00", time.localtime(t))][2].append(s)
    for h in sorted(byhour):
        n, gaps, lens = byhour[h]
        busy = sum(1 for g in gaps if g < 30)
        out.append(f"  {h}  {n:4d} clips   gap median {statistics.median(gaps) if gaps else 0:5.1f} s"
                   f"   clip median {statistics.median(lens) if lens else 0:4.1f} s"
                   + ("   (busy all hour)" if busy >= 0.8 * n and n > 20 else ""))
    return out


def queue_log(hours: float) -> list[str]:
    text = run(["journalctl", "-u", "sizeup-station", "--since", f"-{int(hours * 60)}min", "-o", "cat", "--no-pager"],
               timeout=120)
    if not text.strip():
        return ["queue:     no sizeup-station journal (run it with sudo?)"]
    why: Counter = Counter()
    chan: Counter = Counter()
    fallback = 0
    for line in text.splitlines():
        m = re.match(r"speech-to-text: (\S+) not transcribed: (.*)", line)
        if m:
            w = m.group(2)
            why["shed (context talkgroup)" if "context only" in w else "skipped (waited too long)" if "skipped" in w
                else "dropped (queue full)" if "full" in w or "already waiting" in w else w[:40]] += 1
            chan[m.group(1).split("-")[0]] += 1
        m = re.match(r"speech-to-text re-run: (\d+) recording\(s\) queued", line)
        if m:
            fallback += int(m.group(1))
    out = [f"queue:     the last {hours:g} h: {sum(why.values())} clips not transcribed; {fallback} re-runs queued "
           "(the hub's AWS fallback and people's Re-transcribe)"]
    for w, n in why.most_common():
        out.append(f"  {n:5d}  {w}")
    if chan:
        out.append("  most from talkgroups: " + ", ".join(f"{c} ({n})" for c, n in chan.most_common(5)))
    return out


def _wav16k(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit(f"{path}: want 16-bit mono WAV")
        rate, frames = w.getframerate(), w.readframes(w.getnframes())
    buf = io.BytesIO()
    with wave.open(buf, "wb") as o:
        o.setnchannels(1)
        o.setsampwidth(2)
        o.setframerate(rate)
        o.writeframes(frames)
    return buf.getvalue()


def _post(port: int, wav: bytes, fields: list[tuple[str, str]]) -> float:
    b = uuid.uuid4().hex
    body = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields)
    body += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="x.wav"\r\n'
             "Content-Type: audio/wav\r\n\r\n").encode() + wav + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/inference", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=300) as r:
        json.load(r)
    return time.monotonic() - t0


def bench(cfg: dict, path: str, runs: int) -> list[str]:
    port = int(cfg.get("stt", {}).get("port", 8179))
    wav = _wav16k(path)
    with wave.open(path, "rb") as w:
        secs = w.getnframes() / w.getframerate()
    ctx = str(min(1500, int((secs + 1.0) / 30.0 * 1500) + 64))
    base = [("response_format", "verbose_json"), ("temperature", "0.0")]
    out = [f"bench:     {os.path.basename(path)} ({secs:.1f} s) through whisper-server :{port}, {runs} runs each"]
    for label, extra in (("as sizeup sends it", []), ("no temperature fallback", [("temperature_inc", "0.0")]),
                         (f"audio_ctx {ctx}", [("audio_ctx", ctx)])):
        try:
            took = [_post(port, wav, base + extra) for _ in range(runs)]
            out.append(f"  {label:26s} median {statistics.median(took):5.1f} s  (min {min(took):.1f}, max {max(took):.1f})")
        except OSError as e:
            out.append(f"  {label:26s} failed: {e}")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hours", type=float, default=6.0, help="how far back the journals are read (6)")
    p.add_argument("--sample", type=float, default=10.0, help="seconds of CPU use sampled (10)")
    p.add_argument("--bench", metavar="WAV", help="also time whisper-server on this 16-bit mono WAV")
    p.add_argument("--runs", type=int, default=3)
    a = p.parse_args(argv)
    cfg = config()
    for section in (machine(), whisper(cfg), processes(a.sample), pace(a.hours), queue_log(a.hours)):
        print("\n".join(section))
    if a.bench:
        print("\n".join(bench(cfg, a.bench, max(1, a.runs))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
