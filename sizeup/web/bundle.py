"""The /cad page's scripts and stylesheets as one file each (static/bundle/), so a first load (and the first load after
every deploy) asks for a handful of files instead of ~60, one round trip through Cloudflare each.

A page marks what may be bundled:

    <!-- bundle css cad -->
    <link rel="stylesheet" href="/static/style.css">
    ...
    <!-- /bundle -->

and the same with `bundle js cad` around plain `<script src="/static/...">` tags. Built when the hub image is
(Dockerfile: `python -m sizeup.web.bundle`), never at run time and never committed: static/bundle/ holds cad.js
(+ cad.js.map), cad.css and manifest.json (each bundle's inputs and their digest).

As the page is served (server.py Assets), a block is replaced by its bundle only when the manifest names exactly the
block's files with exactly the bytes on disk now; otherwise (a checkout with no build, a file edited after it) the
page keeps the individual tags. /cad?bundle=0 always gives the individual tags (debugging). The bundle's URL is
versioned by its content (?v=, like every static file), so it is kept a year and a deploy names a new one.

Same semantics as the separate tags: the scripts are concatenated in the page's order, unwrapped (their top-level
names stay global), each one starting with a "use strict" directive, so the bundle (which starts with one) is
strict throughout just as each file was; a file without the directive is refused at build. Each file is marked
(/* file: x.js */) and the source map points every line back at its file. One difference remains: an exception
thrown at the top level of one file stops the files after it in the bundle (separate tags would run them).

During a hot/cold handover the page and its bundle may come from different tasks: a bundle version the task doesn't
have gets its own (not kept: no-cache, as for any static file), and a task with no bundle at all (404) makes the page
load the individual files instead (the script's fallback below, the stylesheet's onerror)."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

DIR = "bundle"
MANIFEST = f"{DIR}/manifest.json"
BLOCK = re.compile(r"<!-- bundle (js|css) ([a-z][a-z0-9-]*) -->\n(.*?)<!-- /bundle -->\n?", re.S)
TAG = {"js": re.compile(r'<script src="/static/([\w./-]+\.js)"></script>'),
       "css": re.compile(r'<link rel="stylesheet" href="/static/([\w./-]+\.css)">')}
STRICT = re.compile(r'\A(?:\s|//[^\n]*\n|/\*.*?\*/)*"use strict";', re.S)
IMPORT = re.compile(r'@import\s+url\("(/static/[\w./-]+\.css)"\)')
FLAG = "SIZEUP_BUNDLE"          # set by the bundle's first line: the page's fallback loads the files when it's missing


@dataclass
class Block:
    kind: str           # "js" | "css"
    name: str           # the bundle's name: static/bundle/<name>.<kind>
    files: list[str]    # under static/, in the page's order
    span: tuple[int, int]


def blocks(html: str) -> list[Block]:
    """The page's bundle blocks. A block holds only its kind's plain tags (no inline script, no defer/async/module)."""
    out = []
    for m in BLOCK.finditer(html):
        kind, name, inner = m.group(1), m.group(2), m.group(3)
        rest = TAG[kind].sub("", inner)
        if rest.strip():
            raise ValueError(f"bundle {kind} {name}: only plain tags may be bundled, not {rest.strip()[:80]!r}")
        out.append(Block(kind, name, TAG[kind].findall(inner), m.span()))
    return out


def digest(static: Path, files: list[str]) -> str:
    """sha256 over the inputs' names and bytes, in order."""
    h = hashlib.sha256()
    for rel in files:
        b = (static / rel).read_bytes()
        h.update(f"{rel}\0{len(b)}\0".encode())
        h.update(b)
    return h.hexdigest()


# -- building -------------------------------------------------------------------------------------------------------

B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def vlq(n: int) -> str:
    n = (-n << 1) | 1 if n < 0 else n << 1
    out = ""
    while True:
        d, n = n & 31, n >> 5
        out += B64[d | (32 if n else 0)]
        if not n:
            return out


def join_js(static: Path, files: list[str], name: str) -> tuple[bytes, bytes]:
    """(the bundle, its source map). Every line of a file maps to that line of the file (column 0)."""
    out = ['"use strict";', f'window.{FLAG} = "{name}";']
    maps = ["", ""]
    prev_src = prev_line = 0
    for i, rel in enumerate(files):
        text = (static / rel).read_text("utf-8")
        if not STRICT.match(text):
            raise ValueError(f"{rel}: start it with a \"use strict\" directive (the bundle is strict throughout)")
        if "sourceMappingURL" in text:
            raise ValueError(f"{rel}: has its own source map; leave it out of the bundle")
        out.append(f"/* file: {rel} */")
        maps.append("")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        for j, line in enumerate(lines):
            out.append(line)
            maps.append("A" + vlq(i - prev_src) + vlq(j - prev_line) + "A")
            prev_src, prev_line = i, j
        out.append(";")             # the next file never continues this one's last statement
        maps.append("")
    out.append(f"//# sourceMappingURL={name}.js.map")
    maps.append("")
    smap = {"version": 3, "file": f"{name}.js", "sources": [f"/static/{r}" for r in files], "names": [],
            "mappings": ";".join(maps)}
    return ("\n".join(out) + "\n").encode("utf-8"), json.dumps(smap, separators=(",", ":")).encode()


def join_css(static: Path, files: list[str]) -> bytes:
    out = []
    for i, rel in enumerate(files):
        text = (static / rel).read_text("utf-8")
        if "@charset" in text or (i and "@import" in text):
            raise ValueError(f"{rel}: @charset, or an @import outside the first file, can't be bundled")
        out.append(f"/* file: {rel} */\n{text.rstrip()}\n")
    return "".join(out).encode("utf-8")


def build(static: Path) -> dict:
    """Write static/bundle/ for every block of every page; returns the manifest."""
    static = static.resolve()
    manifest: dict[str, dict] = {}
    built: dict[str, bytes] = {}
    for page in sorted(static.glob("*.html")):
        for b in blocks(page.read_text("utf-8")):
            key = f"{b.kind}:{b.name}"
            if key in manifest:
                if manifest[key]["files"] != b.files:
                    raise ValueError(f"{page.name}: bundle {b.kind} {b.name} differs from another page's")
                continue
            if not b.files:
                raise ValueError(f"{page.name}: bundle {b.kind} {b.name} is empty")
            if b.kind == "js":
                body, smap = join_js(static, b.files, b.name)
                built[f"{DIR}/{b.name}.js.map"] = smap
            else:
                body = join_css(static, b.files)
            rel = f"{DIR}/{b.name}.{b.kind}"
            built[rel] = body
            manifest[key] = {"file": rel, "files": b.files, "digest": digest(static, b.files)}
    out = static / DIR
    out.mkdir(exist_ok=True)
    for old in out.iterdir():
        if old.is_file() and old.name not in {Path(r).name for r in built} | {"manifest.json"}:
            old.unlink()
    for rel, body in built.items():
        (static / rel).write_bytes(body)
    (static / MANIFEST).write_text(json.dumps(manifest, indent=1) + "\n")
    return manifest


# -- serving --------------------------------------------------------------------------------------------------------


class Bundles:
    """What static/bundle/ holds, checked against the files on disk (by their stat, rehashed when one changes)."""

    def __init__(self, static: Path):
        self.static = static.resolve()
        self._lock = threading.Lock()
        self._manifest: tuple[tuple | None, dict] = (None, {})
        self._fresh: dict[str, tuple[tuple, bool]] = {}      # key -> (the inputs' stat stamps, digest matched)

    def _stamp(self, rel: str) -> tuple | None:
        try:
            st = (self.static / rel).stat()
        except OSError:
            return None
        return (rel, st.st_mtime_ns, st.st_size)

    def manifest(self) -> dict:
        stamp = self._stamp(MANIFEST)
        with self._lock:
            if self._manifest[0] == stamp:
                return self._manifest[1]
        try:
            m = json.loads((self.static / MANIFEST).read_text()) if stamp else {}
        except (OSError, ValueError):
            m = {}
        with self._lock:
            self._manifest, self._fresh = (stamp, m), {}
        return m

    def current(self, b: Block) -> str | None:
        """The bundle (its path under static/) to serve for this block, or None: no build, or a stale one."""
        key = f"{b.kind}:{b.name}"
        ent = self.manifest().get(key)
        if not ent or ent.get("files") != b.files or self._stamp(ent["file"]) is None:
            return None
        stamps = tuple(self._stamp(r) for r in b.files) + (self._stamp(MANIFEST),)
        with self._lock:
            hit = self._fresh.get(key)
        if hit and hit[0] == stamps:
            return ent["file"] if hit[1] else None
        try:
            ok = digest(self.static, b.files) == ent.get("digest")
        except OSError:
            ok = False
        with self._lock:
            self._fresh[key] = (stamps, ok)
        return ent["file"] if ok else None


def render(html: str, bundles: Bundles | None) -> str:
    """The page with each block whose bundle is current replaced by it (and a fallback to the files should the
    bundle not load); other blocks as they are. The server versions every /static/... reference afterwards."""
    if bundles is None or "<!-- bundle " not in html:
        return html
    parts, at = [], 0
    for b in blocks(html):
        rel = bundles.current(b)
        if rel is None:
            continue
        parts.append(html[at:b.span[0]])
        at = b.span[1]
        if b.kind == "css":
            files = ",".join(f"'/static/{f}'" for f in b.files)
            parts.append(f'<link rel="stylesheet" href="/static/{rel}" onerror="this.onerror=null;this.after(...[{files}]'
                         f'.map(h=>Object.assign(document.createElement(\'link\'),{{rel:\'stylesheet\',href:h}})))">\n')
            first = (bundles.static / b.files[0]).read_text("utf-8")
            for imp in IMPORT.findall(first):          # what the first stylesheet imports: fetched alongside, not after
                parts.append(f'<link rel="preload" as="style" href="{imp}">\n')
        else:
            tags = "".join(f"<script src='/static/{f}'><\\/script>" for f in b.files)
            parts.append(f'<script src="/static/{rel}"></script>\n'
                         f'<script>window.{FLAG} || document.write("{tags}")</script>\n')
    return "".join(parts) + html[at:] if parts else html


def main(argv: list[str]) -> int:
    static = Path(__file__).resolve().parent / "static"
    if argv[1:] == ["--check"]:         # the image's bundles are there and current (CI)
        bs = Bundles(static)
        bad = [f"{p.name}: {b.kind} {b.name}" for p in sorted(static.glob("*.html"))
               for b in blocks(p.read_text("utf-8")) if bs.current(b) is None]
        print("stale or missing: " + ", ".join(bad) if bad else "bundles current")
        return 1 if bad else 0
    for key, ent in build(static).items():
        print(f"{key}: {len(ent['files'])} files -> {ent['file']} ({(static / ent['file']).stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
