"""Private real-data replay fixtures (GitHub #283): the cache, its manifests, and the pytest plugin.

Synthetic fixtures only test what their author assumed. These tests replay data captured from the real sources
(ADS-B around HPN, the CARS-Hub's camera streams, the hub's own audio clips) so a bug the real world brings out
(a departure read as an arrival, a feed down for hours, a truncated segment) is caught before it ships.

The data is PRIVATE and never in git (this repo is public). It lives in a private S3 location and is pulled into a
local cache outside the repo:

    $SIZEUP_REPLAY_DIR, else ~/.cache/sizeup-replay

    <cache>/<kind>/<set>/manifest.json      what the set is, when and how it was captured, each file's sha256
    <cache>/<kind>/<set>/<files...>         the captured data

    kind    adsb     ADS-B snapshots around HPN over a window (tools/capture_fixture.py adsb)
            camera   511NY CARS-Hub HLS playlists and segments (tools/capture_fixture.py camera)
            audio    airband clips restored read-only from the hub's Litestream backup (tools/capture_fixture.py audio)
            mrrs-intel  radio log rows and county-CAD auto-labels for the intel replay (tools/intel_replay.py --capture):
                     LOCAL ONLY, never pushed (they hold police transcripts; tools/replay_sync.py LOCAL_ONLY)

Get it:      .venv/bin/python tools/replay_sync.py pull          (read-only S3 download)
Capture:     .venv/bin/python tools/capture_fixture.py adsb|camera|audio ...
Share it:    .venv/bin/python tools/replay_sync.py push --write-to-s3   (refuses without the flag)

A test asks for data with the marker and the fixture:

    @pytest.mark.replay("adsb")
    def test_something(replay_sets):
        for s in replay_sets("adsb"): ...

and is skipped, with the reason, when the cache has no set of that kind (public CI, a fresh clone). Nothing here
reaches the network.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

KINDS = ("adsb", "camera", "audio", "mrrs-intel")
MANIFEST = "manifest.json"
MANIFEST_VERSION = 1


def cache_dir() -> Path:
    """$SIZEUP_REPLAY_DIR, else ~/.cache/sizeup-replay (outside the repo: never committed)."""
    env = os.environ.get("SIZEUP_REPLAY_DIR", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".cache" / "sizeup-replay"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def set_id(prefix: str, when: float | None = None) -> str:
    """A set's name: prefix and its UTC capture time, e.g. hpn-20261107T153000Z."""
    return f"{prefix}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(time.time() if when is None else when))}"


def write_manifest(set_dir: Path, kind: str, *, source: str, how: str, captured_from: float, captured_to: float,
                   files: dict[str, str], notes: str = "", extra: dict | None = None) -> dict:
    """The set's manifest: what it is (kind, source, each file's meaning), when (UTC, from / to) and how (the
    command), each file's size and sha256 (replay_sync checks them). files: relative path -> what it is."""
    assert kind in KINDS, kind
    entries = []
    for rel, what in sorted(files.items()):
        p = set_dir / rel
        entries.append({"path": rel, "what": what, "bytes": p.stat().st_size, "sha256": sha256(p)})
    doc = {"version": MANIFEST_VERSION, "kind": kind, "set": set_dir.name, "source": source, "how": how,
           "captured_from": _iso(captured_from), "captured_to": _iso(captured_to), "files": entries, "notes": notes,
           **(extra or {})}
    (set_dir / MANIFEST).write_text(json.dumps(doc, indent=2) + "\n")
    return doc


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


@dataclass
class ReplaySet:
    kind: str
    path: Path
    manifest: dict

    @property
    def name(self) -> str:
        return f"{self.kind}/{self.path.name}"

    def file(self, rel: str) -> Path:
        return self.path / rel

    def files(self, what_prefix: str = "") -> list[Path]:
        return [self.path / f["path"] for f in self.manifest.get("files", []) if f.get("what", "").startswith(what_prefix)]

    def jsonl(self, rel: str):
        """Each line of a (gzipped) JSON-lines file, parsed."""
        p = self.path / rel
        opener = gzip.open if p.suffix == ".gz" else open
        with opener(p, "rt", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)


def find_sets(kind: str, root: Path | None = None) -> list[ReplaySet]:
    """Every complete set of this kind in the cache (one with a manifest whose files are all there), oldest first."""
    root = cache_dir() if root is None else root
    out = []
    base = root / kind
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        m = d / MANIFEST
        if not m.is_file():
            continue
        try:
            doc = json.loads(m.read_text())
        except (OSError, ValueError):
            continue
        if doc.get("kind") != kind or not all((d / f["path"]).is_file() for f in doc.get("files", [])):
            continue
        out.append(ReplaySet(kind, d, doc))
    return out


def why_missing(kind: str) -> str:
    how = ("tools/intel_replay.py --capture, local only" if kind == "mrrs-intel"
           else f"tools/replay_sync.py pull, or tools/capture_fixture.py {kind}")
    return f"no '{kind}' replay data in {cache_dir()} (private, never in git: {how}; set SIZEUP_REPLAY_DIR to use another folder)"


# ---- the pytest plugin (loaded by tests/conftest.py) ---------------------------------------------------------------


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", "replay(kind, ...): replays private real data from the local replay cache "
                                       "(tests/replay.py); skipped when the cache has none of that kind")


def pytest_collection_modifyitems(config, items) -> None:
    have: dict[str, bool] = {}
    for item in items:
        for mark in item.iter_markers("replay"):
            for kind in mark.args or ():
                if kind not in have:
                    have[kind] = bool(find_sets(kind))
                if not have[kind]:
                    item.add_marker(pytest.mark.skip(reason=why_missing(kind)))
                    break


@pytest.fixture
def replay_sets():
    """replay_sets(kind) -> [ReplaySet, ...]: the cache's sets of that kind (skips the test when there are none)."""
    def get(kind: str) -> list[ReplaySet]:
        sets = find_sets(kind)
        if not sets:
            pytest.skip(why_missing(kind))
        return sets
    return get
