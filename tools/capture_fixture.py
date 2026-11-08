#!/usr/bin/env python3
"""Capture real data into the private replay cache (GitHub #283; tests/replay.py says where and why).

    .venv/bin/python tools/capture_fixture.py adsb   [--minutes 40] [--every 5] [--feeds adsblol,adsbfi] [--radius 15]
    .venv/bin/python tools/capture_fixture.py camera [--cameras 3] [--segments 4] [--id CAMERA_ID ...]
    .venv/bin/python tools/capture_fixture.py audio  [--clips 10] [--channel HPNTower-] [--ltx-cache DIR]

Each run writes one set, <cache>/<kind>/<set>/, with a manifest.json saying what each file is, when it was captured
and the command that captured it. The cache is $SIZEUP_REPLAY_DIR, else ~/.cache/sizeup-replay: outside the repo.
Nothing captured ever goes into git (the repo is public); tools/replay_sync.py push shares it, privately.

  adsb    the public ADS-B feeds sizeup reads (adsb.lol's point query, and adsb.fi's with --feeds) around HPN,
          every --every s (at least 1) for --minutes: snapshots.jsonl.gz, one line per answer {t: when fetched
          (unix s), feed, status, body: the JSON as served}. Within the feeds' limits (adsb.fi: 1/s); a feed that
          fails (adsb.lol answers 429 at 5 s at times) rests, doubling, up to 2 minutes.
  camera  the 511NY CARS-Hub's camera streams in our area: the CCTV inventory (data/cctv.xml, HTTP Basic: the
          username and password from the macOS Keychain, account pcfd, services sizeup-511ny-carshub-user / -pass;
          they are only ever in the request's Authorization header, never printed or written), trimmed to our
          area's cameras; the Thruway's and the county's public camera lists (what Roads merges them with); then
          for --cameras of the CARS-Hub cameras with video the master playlist, its chunklist and the newest
          --segments segments, exactly as served; with --stills every area CARS-Hub camera's still too, once, a
          second apart (stills/<id>.<png|jpg>, stills.json: their addresses; the overlay match, #320). One inventory request: 511NY allows one every 5 minutes, so a
          stamp outside the cache (~/.local/state/sizeup/ny511-last-request) refuses a second capture within 5
          minutes. The hub polls the same inventory with the same account (hourly): a capture can still land
          within 5 minutes of the hub's own poll; capture rarely. Names from the source (camera ids, chunklist and segment names) must be plain
          file names: '/', '..' or a leading '.' is refused, so nothing is written outside the set's folder.
  audio   clips restored read-only from the hub's Litestream backup of audio.sqlite
          (s3://sizeup-hub-backups-504804195561/sizeup-hub/audio.sqlite/, tools/ltx.py), restored to a temporary
          file deleted afterwards: only the airband channels' clips (HPN Tower, HPN Ground, NY Approach: public air
          traffic control, no caller or patient data), the newest --clips of each. Never dispatch, fireground or
          P25 audio, never a transcript, never anything from the CAD. Each clip's WAV as stored, its id and time.

Data minimisation: only what the replay tests need. No CAD app data, ever; no dispatch / caller / patient audio.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sizeup import aircraft as ac  # noqa: E402
from sizeup import roads as r  # noqa: E402
from tests.replay import cache_dir, set_id, write_manifest  # noqa: E402

ACCOUNT = "pcfd"
KC_USER, KC_PASS = "sizeup-511ny-carshub-user", "sizeup-511ny-carshub-pass"
BACKUP = "s3://sizeup-hub-backups-504804195561/sizeup-hub"


def how() -> str:
    return "tools/capture_fixture.py " + " ".join(shlex.quote(a) for a in sys.argv[1:])


SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}$")


def safe_name(name: str) -> str:
    """A file or folder name from a source (a camera id, a segment's name): one plain name, never a path. Refuses
    '/', '..', a leading '.', and anything else outside SAFE_NAME, so nothing lands outside the set's folder."""
    if not SAFE_NAME.fullmatch(name) or ".." in name:
        raise ValueError(f"not a plain file name: {name[:80]!r}")
    return name


def new_set(kind: str, prefix: str) -> Path:
    d = cache_dir() / kind / set_id(prefix)
    d.mkdir(parents=True, exist_ok=False)
    return d


# ---- adsb --------------------------------------------------------------------------------------------------------


def adsb(args) -> None:
    feeds = [f.strip() for f in args.feeds.split(",") if f.strip()]
    urls = {"adsblol": ac.ADSBLOL_URL, "adsbfi": ac.ADSBFI_URL}
    for f in feeds:
        if f not in urls:
            raise SystemExit(f"unknown feed {f} (adsblol, adsbfi)")
    every = max(1.0, float(args.every))            # never faster than one round a second
    d = new_set("adsb", "hpn")
    raw = d / "snapshots.jsonl"
    start = time.time()
    end = start + args.minutes * 60
    n = {f: 0 for f in feeds}
    rest = {f: 0.0 for f in feeds}                 # a feed that says 429 rests (doubling, at most 2 min)
    fails = {f: 0 for f in feeds}
    errors = 0
    said = time.time()
    with open(raw, "w", encoding="utf-8") as out:
        while time.time() < end:
            tick = time.time()
            for f in feeds:
                if tick < rest[f]:
                    continue
                url = urls[f].format(lat=f"{ac.ARP[0]:.4f}", lon=f"{ac.ARP[1]:.4f}", radius=args.radius)
                try:
                    code, _, body = ac.http_get(url, ac.TIMEOUT_S, ac.MAX_FEED_BYTES, {"Accept": "application/json"})
                    doc = json.loads(body)
                except (HTTPError, URLError, OSError, ValueError) as e:
                    errors += 1
                    fails[f] += 1
                    rest[f] = time.time() + min(120.0, every * 2 ** min(fails[f], 5))
                    print(f"{f}: {type(e).__name__}: {getattr(e, 'reason', e)}; resting {rest[f] - time.time():.0f} s",
                          flush=True)
                    continue
                fails[f] = 0
                out.write(json.dumps({"t": round(time.time(), 3), "feed": f, "status": code, "body": doc},
                                     separators=(",", ":")) + "\n")
                out.flush()
                n[f] += 1
            if time.time() - said >= 60:                   # progress once a minute
                said = time.time()
                print(f"{time.strftime('%H:%M:%S')} {n} answers, {errors} errors", flush=True)
            time.sleep(max(0.0, every - (time.time() - tick)))
    with open(raw, "rb") as fi, gzip.open(d / "snapshots.jsonl.gz", "wb") as fo:
        shutil.copyfileobj(fi, fo)
    raw.unlink()
    write_manifest(d, "adsb", source=", ".join(feeds) + f" point query, {args.radius} nm around HPN",
                   how=how(), captured_from=start, captured_to=time.time(),
                   files={"snapshots.jsonl.gz": "snapshots: one JSON line per feed answer {t, feed, status, body}"},
                   extra={"every_s": every, "answers": n, "errors": errors})
    print(f"{d}: {n} answers, {errors} errors")


# ---- camera ------------------------------------------------------------------------------------------------------


def keychain(service: str) -> str:
    """A generic password from the login keychain; "" when there is none. Its value never reaches the terminal."""
    try:
        out = subprocess.run(["security", "find-generic-password", "-a", ACCOUNT, "-s", service, "-w"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _trim_cctv(body: bytes, keep: set[str]) -> bytes:
    """The inventory with only the cameras kept (their <inventory-item> whole, with the document's own wrapping),
    so the fixture is the real XML, small."""
    import re
    out, pos = [], 0
    for m in re.finditer(rb"<(?:\w+:)?inventory-item\b.*?</(?:\w+:)?inventory-item>", body, re.S):
        out.append(body[pos:m.start()])
        dev = re.search(rb"<(?:\w+:)?device-id>\s*([^<]*?)\s*</", m.group(0))
        cid = re.sub(r"[^A-Za-z0-9_.:-]", "-", dev.group(1).decode(errors="replace"))[:64] if dev else ""
        if cid in keep:
            out.append(m.group(0))
        pos = m.end()
    out.append(body[pos:])
    text = b"".join(out)
    # (cctvInventory blocks left empty are harmless: parse_carshub_cctv reads inventory items only)
    return text


NY511_STAMP = Path.home() / ".local" / "state" / "sizeup" / "ny511-last-request"   # when this tool last asked 511NY
                                                                                    # (not in the replay cache)
NY511_GAP_S = 300.0                            # 511NY: once every 5 minutes at most


def ny511_wait(stamp: Path, now: float | None = None) -> float:
    """Seconds still to wait before the next 511NY inventory request (0: go), from the stamp file's time.
    Note the hub polls the same inventory with the same account (hourly, never under 5 min apart): a capture right
    after one of its polls still makes two requests within 5 minutes; this only keeps the tool's own requests apart."""
    now = time.time() if now is None else now
    try:
        last = float(stamp.read_text().strip())
    except (OSError, ValueError):
        return 0.0
    return max(0.0, NY511_GAP_S - (now - last))


def camera(args) -> None:
    stamp = NY511_STAMP
    wait = ny511_wait(stamp)
    if wait > 0:
        raise SystemExit(f"511NY asks for at most one request every 5 minutes: the last capture asked "
                         f"{NY511_GAP_S - wait:.0f} s ago; try again in {wait:.0f} s")
    auth = r.BasicAuth(keychain(KC_USER), keychain(KC_PASS))
    if not auth:
        raise SystemExit(f"no 511NY CARS-Hub credentials in the Keychain (account {ACCOUNT}, {KC_USER} / {KC_PASS})")
    secrets = auth.secrets()

    def say(text: str) -> None:
        print(r.redact(text, secrets), flush=True)

    start = time.time()
    url = r.hub_cctv()
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(f"{start:.0f}\n")                # before the request: a failed one counts too
    try:
        code, _, body = r.http_get(url, r.TIMEOUT_S * 3, r.MAX_FEED_BYTES, auth.header())
    except (HTTPError, URLError, OSError, ValueError) as e:
        say(f"CCTV inventory: {type(e).__name__}: {getattr(e, 'reason', e)}")
        raise SystemExit(1)
    del auth                                                      # nothing below needs it: the rest is public
    area = r.parse_carshub_cctv(body, r.DEFAULT_BBOX)
    cams = [c for c in area if c.get("video")]
    say(f"CCTV inventory: {len(body)} bytes, {len(area)} cameras in our area, {len(cams)} with video")
    if args.id:
        chosen = [c for c in cams if c["id"] in set(args.id)]
    else:
        chosen = cams[:: max(1, len(cams) // max(1, args.cameras))][: args.cameras]
    d = new_set("camera", "carshub")
    files: dict[str, str] = {}
    (d / "cctv_area.xml").write_bytes(_trim_cctv(body, {c["id"] for c in area}))
    files["cctv_area.xml"] = "CARS-Hub CCTV inventory (data/cctv.xml) trimmed to our area's cameras, as served"
    # ours, which the CARS-Hub's are merged with (Roads.merged): the Thruway's and the county's lists, public, one each
    for name, url, what in (("thruway_cameras.xml", r.THRUWAY_URLS["cameras"], "the Thruway's camera list, as served"),
                            ("county_cameras.json", r.COUNTY_URLS["cameras"], "the county DPW's camera layer, as served")):
        try:
            code, _, ours = r.http_get(url, r.TIMEOUT_S * 3, r.MAX_FEED_BYTES)
        except (HTTPError, URLError, OSError, ValueError) as e:
            say(f"{name}: {type(e).__name__}: {getattr(e, 'reason', e)}")
            continue
        (d / name).write_bytes(ours)
        files[name] = what
    streams = []
    for c in chosen:
        try:
            cdir = Path("streams") / safe_name(c["id"])
        except ValueError as e:
            say(f"skipped a camera: {e}")
            continue
        (d / cdir).mkdir(parents=True, exist_ok=True)
        rec = {"id": c["id"], "video": c["video"], "files": []}
        try:
            got = _stream(c["video"], args.segments, d / cdir)
        except (HTTPError, URLError, OSError, ValueError) as e:
            say(f"{c['id']}: {type(e).__name__}: {getattr(e, 'reason', e)}")
            rec["error"] = f"{type(e).__name__}: {getattr(e, 'reason', e)}"[:200]
            streams.append(rec)
            continue
        for rel, what in got:
            files[str(cdir / rel)] = what
            rec["files"].append({"name": rel, "what": what})
        streams.append(rec)
        say(f"{c['id']}: {len(got)} files")
    if args.stills:
        files.update(_stills(area, d, say))
    (d / "streams.json").write_text(json.dumps(streams, indent=2) + "\n")
    files["streams.json"] = "per camera: its video URL and the files fetched, in order (master, chunklist, segments)"
    write_manifest(d, "camera", source="511NY CARS-Hub (data/cctv.xml) and the cameras' HLS on *.skyvdn.com",
                   how=how(), captured_from=start, captured_to=time.time(), files=files,
                   extra={"cameras_in_area": len(area), "cameras_in_area_with_video": len(cams),
                          "streams": len(chosen)})
    print(f"{d}: {len(chosen)} streams, {len(files)} files")


def _stills(area: list[dict], d: Path, say) -> dict[str, str]:
    """Every area camera's still as served (public, no credentials), one request a second: stills/<id>.<ext> and
    stills.json [{id, url, file, ctype}]."""
    files, index = {}, []
    (d / "stills").mkdir(exist_ok=True)
    for c in area:
        if not c.get("url"):
            continue
        try:
            code, h, body = r.http_get(c["url"], r.IMAGE_TIMEOUT_S, r.IMAGE_MAX_BYTES)
            name = f"stills/{safe_name(c['id'])}.{'png' if body[:4] == b'\x89PNG' else 'jpg'}"
        except (HTTPError, URLError, OSError, ValueError) as e:
            say(f"{c['id']} still: {type(e).__name__}: {getattr(e, 'reason', e)}")
            continue
        (d / name).write_bytes(body)
        ctype = h.get("content-type", "")
        files[name] = f"still, HTTP {code}, {ctype}"
        index.append({"id": c["id"], "url": c["url"], "file": name, "ctype": ctype})
        time.sleep(1.0)
    (d / "stills.json").write_text(json.dumps(index, indent=2) + "\n")
    files["stills.json"] = "per camera: its still's address and the file it was saved as"
    say(f"stills: {len(index)}")
    return files


def _stream(video: str, segments: int, out: Path) -> list[tuple[str, str]]:
    """The master playlist, its chunklist and its newest `segments` segments, as served (no transcoding, no trimming).
    The chunklist's segments are fetched right after it, oldest first, like a player starting live."""
    base = video.rsplit("/", 1)[0] + "/"
    got = []
    code, h, master = r.http_get(video, r.STREAM_OPEN_TIMEOUT_S * 2, r.STREAM_MAX_BYTES)
    (out / "playlist.m3u8").write_bytes(master)
    got.append(("playlist.m3u8", f"master playlist, HTTP {code}, {h.get('content-type', '')}"))
    chunk = next((t.strip() for t in master.decode(errors="replace").splitlines() if t.strip() and not t.startswith("#")), "")
    if not chunk:
        return got
    cname = safe_name(chunk.split("?")[0])               # a plain name in the stream's folder, never a path
    code, h, chunklist = r.http_get(base + chunk, r.STREAM_TIMEOUT_S, r.STREAM_MAX_BYTES)
    (out / cname).write_bytes(chunklist)
    got.append((cname, f"chunklist, HTTP {code}, {h.get('content-type', '')}"))
    names = [t.strip() for t in chunklist.decode(errors="replace").splitlines() if t.strip() and not t.startswith("#")]
    for name in names[-segments:]:
        sname = safe_name(name.split("?")[0])
        code, h, seg = r.http_get(base + name, r.STREAM_TIMEOUT_S, r.STREAM_MAX_BYTES)
        (out / sname).write_bytes(seg)
        got.append((sname, f"segment, HTTP {code}, {h.get('content-type', '')}, content-length "
                           f"{h.get('content-length', '?')}"))
    return got


# ---- audio -------------------------------------------------------------------------------------------------------


AIRBAND = ("HPNTower-", "HPNGround-", "NYApproach-")     # the airband channels' clip ids (label without spaces, -ms)


def audio(args) -> None:
    """Airband clips only (public air traffic control): never dispatch, fireground or P25 audio, which may carry
    caller or patient details. The restored database is a temporary file, deleted when this ends."""
    sys.path.insert(0, str(ROOT / "tools"))
    import ltx
    prefixes = tuple(args.channel or AIRBAND)
    bad = [x for x in prefixes if x not in AIRBAND]
    if bad:
        raise SystemExit(f"only the airband channels' clips are captured ({', '.join(AIRBAND)}), not {bad}")
    start = time.time()
    with tempfile.TemporaryDirectory(prefix="sizeup-ltx-") as tmp:
        db_path = Path(tmp) / "audio.sqlite"
        work = Path(args.ltx_cache).expanduser().resolve() if args.ltx_cache else Path(tmp) / "ltx"
        if args.ltx_cache and (work == ROOT or ROOT in work.parents):
            raise SystemExit(f"--ltx-cache {work} is inside the repo: the LTX files hold the whole audio database "
                             "(dispatch audio too); keep them outside it")
        info = ltx.restore(f"{BACKUP}/audio.sqlite", db_path, work=work, log=print)
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = []
        for prefix in prefixes:
            rows += con.execute("SELECT id, kind, at, LENGTH(wav) FROM audio WHERE kind = 'clip' AND substr(id, 1, ?) = ? "
                                "ORDER BY at DESC LIMIT ?", (len(prefix), prefix, args.clips)).fetchall()
        if not rows:
            raise SystemExit(f"no airband clips ({', '.join(prefixes)}) in the backup")
        d = new_set("audio", "airband")
        files, clips = {}, []
        for aid, kind, at, size in sorted(rows, key=lambda x: x[2]):
            wav = con.execute("SELECT wav FROM audio WHERE id = ?", (aid,)).fetchone()[0]
            name = "clips/" + "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in aid)[:100] + ".wav"
            (d / name).parent.mkdir(parents=True, exist_ok=True)
            (d / name).write_bytes(bytes(wav))
            files[name] = f"WAV as stored in audio.sqlite (kind {kind})"
            clips.append({"id": aid, "channel": aid.rsplit("-", 1)[0], "at": at, "bytes": size, "file": name})
        con.close()
    (d / "clips.json").write_text(json.dumps(clips, indent=2) + "\n")
    files["clips.json"] = "each clip's id, channel, time (unix s) and file (no notes, no transcripts)"
    write_manifest(d, "audio", source=f"{BACKUP}/audio.sqlite (Litestream LTX, read-only; restored to TXID "
                                      f"{info['txid']:#x})", how=how(), captured_from=start, captured_to=time.time(),
                   files=files, extra={"clips": len(clips), "restored_at_ms": info["timestamp_ms"]})
    print(f"{d}: {len(clips)} clips")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="what", required=True)
    a = sub.add_parser("adsb")
    a.add_argument("--minutes", type=float, default=40.0)
    a.add_argument("--every", type=float, default=5.0, help="seconds between rounds (at least 1)")
    a.add_argument("--feeds", default="adsblol")
    a.add_argument("--radius", type=int, default=int(ac.RADIUS_NM))
    c = sub.add_parser("camera")
    c.add_argument("--cameras", type=int, default=3)
    c.add_argument("--segments", type=int, default=4)
    c.add_argument("--id", action="append", default=[])
    c.add_argument("--stills", action="store_true", help="also every area camera's still, once each, 1 s apart")
    u = sub.add_parser("audio")
    u.add_argument("--clips", type=int, default=10, help="per channel, the newest")
    u.add_argument("--channel", action="append", default=[], help=f"one of {', '.join(AIRBAND)} (default: all)")
    u.add_argument("--ltx-cache", default="", help="keep the downloaded LTX files here (else a temporary folder)")
    args = p.parse_args(argv)
    {"adsb": adsb, "camera": camera, "audio": audio}[args.what](args)


if __name__ == "__main__":
    main()
