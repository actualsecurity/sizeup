"""The public repo's rule for /api/v1's spec and fixtures: synthetic data only (docs/mobile-api.md section 7). Hosts,
emails and phone numbers are the reserved example ones; a denylist of real names kept OUTSIDE the repo (a CI secret:
the file named by SIZEUP_SPEC_DENYLIST, one term per line) is checked when it is set."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCANNED = [ROOT / "api" / "openapi", ROOT / "tests" / "fixtures" / "api"]
SUFFIXES = {".yaml", ".yml", ".json", ".md", ".txt"}
OK_HOSTS = re.compile(r"(^|\.)(example\.(org|com|net)|spec\.openapis\.org|json-schema\.org)$")
EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
URL = re.compile(r"https?://([\w.-]+)")
PHONE = re.compile(r"(?<!\d)\(?(\d{3})\)?[ .-](\d{3})[ .-]\d{4}(?!\d)")


def problems(text: str, denylist: list[str] = ()) -> list[str]:
    out = []
    for m in EMAIL.finditer(text):
        if not OK_HOSTS.search(m.group(1).lower()):
            out.append(f"email not at example.org: {m.group(0)}")
    for m in URL.finditer(text):
        if not OK_HOSTS.search(m.group(1).lower()):
            out.append(f"host not example.org: {m.group(0)}")
    for m in PHONE.finditer(text):
        if m.group(2) != "555":
            out.append(f"phone number not 555: {m.group(0)}")
    low = text.lower()
    out += [f"denylisted term: {t[:2]}..." for t in denylist if t and t.lower() in low]
    return out


def files():
    for base in SCANNED:
        if base.is_dir():
            yield from (p for p in sorted(base.rglob("*")) if p.is_file() and p.suffix in SUFFIXES)


def denylist() -> list[str]:
    f = os.environ.get("SIZEUP_SPEC_DENYLIST")
    return [t.strip() for t in Path(f).read_text().splitlines() if t.strip()] if f and Path(f).is_file() else []


def test_the_check_catches_what_it_is_for():
    bad = "mail jo@gmail.com, call (914) 123-4567, see https://hub.example.com and https://real.firehouse.org/x"
    got = problems(bad)
    assert any("email" in p for p in got) and any("phone" in p for p in got)
    assert any("real.firehouse.org" in p for p in got) and not any("hub.example.com" in p for p in got)
    assert problems("a@example.org, 555-0100 or 914-555-0199, https://hub.example.org/api") == []
    assert problems("Engine E99 at Elm", ["elm"]) and not problems("Engine E99", ["elm"])


def test_the_spec_and_fixtures_are_synthetic():
    seen = list(files())
    assert seen, "no spec files found"
    deny = denylist()
    bad = {str(p.relative_to(ROOT)): problems(p.read_text(), deny) for p in seen}
    assert {k: v for k, v in bad.items() if v} == {}


@pytest.mark.skipif(not os.environ.get("SIZEUP_SPEC_DENYLIST"), reason="SIZEUP_SPEC_DENYLIST (a CI secret) is not set")
def test_the_denylist_was_read():
    assert denylist(), "SIZEUP_SPEC_DENYLIST names no readable file or an empty one"
