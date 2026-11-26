"""One name per radio source (admin, 2026-11-25: a talkgroup read one way on the Radio rail and another, ambiguous way on
every chip): the hub's resolver (sizeup/radionames.py) and the page's (static/radio-names.js), both from the one admin
store (radio_folders' names, set from the rail's Edit and Settings > Radios > Channels) plus a short label for chips
(the settings section radio_shorts, beside the name in the same Settings row: admins, audited, exported and checked on
import). Every label a person reads goes through them: the chips (short, with "System · Name (TG n)" on hover), the
talking-now lines, alarms and their pushes, the pre-alert window's channels, the operating channels; the keys, the
radio log's own label and every filter keep the real one.

Every talkgroup number, label and name here is invented."""

import json
import re
import subprocess
from pathlib import Path

import pytest

from sizeup import radionames, sourcekeys
from sizeup.cad.api import CadApi, needs
from sizeup.cloud.access import Identity
from tests.test_cad import make_cad
from tests.test_radio_emergency import NODE, STATIC

ROOT = Path(__file__).resolve().parent.parent
NAMES = {"p25:metro:9001": "Town PD Dispatch North", "fm:FG1": "Fireground 1", "tg:4401": "County Fire Ops"}


@pytest.fixture
def fresh():
    radionames.use(names={}, shorts={}, systems={}, freqs={})
    yield
    radionames.use(names={}, shorts={}, systems={}, freqs={})


# ---- the hub's resolver ----------------------------------------------------------------------------------------------

def test_one_name_short_and_hover_for_every_shape_of_row(fresh):
    radionames.use(names=NAMES, shorts={"p25:metro:9001": "PD North"}, systems={"westchester": "County", "metro": "Metro"},
                   freqs={"FG1": "154.1"})
    log_row = {"source": "p25:metro:9001", "channel": "PD Dispatch", "talkgroup": 9001, "system": "metro"}
    talking = {"tg": 9001, "p25_system": "metro", "channel": "PD Dispatch"}            # a talking-now entry
    alarm = {"talkgroup": 4401, "channel": "Fire Ops"}                                # the county's, by its number
    fm = {"channel": "FG1", "system": "fm"}
    for r in (log_row, talking):
        assert radionames.key_of(r) == "p25:metro:9001"
        assert (radionames.short(r), radionames.name(r)) == ("PD North", "Town PD Dispatch North")
        assert radionames.title(r) == "Metro · Town PD Dispatch North (PD Dispatch, TG 9001)"
    assert radionames.key_of(alarm) == "p25:westchester:4401"          # a name kept under the older "tg:" key still counts
    assert (radionames.short(alarm), radionames.title(alarm)) == ("County Fire Ops", "County · County Fire Ops (Fire Ops, TG 4401)")
    assert (radionames.short(fm), radionames.title(fm)) == ("Fireground 1", "Analog · Fireground 1 (FG1, 154.1 MHz)")
    air = {"channel": "Tower", "system": "airband", "mhz": 118.6}
    assert (radionames.short(air), radionames.title(air)) == ("Tower", "Airband · Tower (118.6 MHz)")
    # nothing set: the real label, as before
    radionames.use(names={}, shorts={})
    assert radionames.short(log_row) == "PD Dispatch" and radionames.title(log_row) == "Metro · PD Dispatch (TG 9001)"
    assert radionames.rev() == ""


def test_shorts_are_checked_and_kept_qualified(fresh):
    assert radionames.clean_shorts({"tg:4401": " Ops  1 ", "fm:FG1": "", "junk": "x", "air:Tower": 5}) == \
        {"p25:westchester:4401": "Ops 1"}
    with pytest.raises(ValueError):
        radionames.clean_short("x" * (radionames.SHORT_MAX + 1))
    with pytest.raises(ValueError):
        radionames.clean_short(["PD"])


# ---- the setting ------------------------------------------------------------------------------------------------------

def test_a_short_label_is_an_admin_setting_audited_exported_and_on_every_page(tmp_path, fresh):
    cad = make_cad(tmp_path)
    cad.comms.context_broadcast = True
    cad.source_systems = lambda: {"westchester": "County", "metro": "Metro"}
    assert needs("/api/cad/settings/radio_folders") == "admin"        # the same admin-only setting as the names
    assert cad.state()["radio_names"] == ""
    cad.settings_post("radio_folders", {"action": "name", "key": "p25:metro:9001", "name": "Town PD Dispatch North",
                                        "operator": "Lt. Test"})
    r1 = cad.state()["radio_names"]
    assert r1
    out = cad.settings_post("radio_folders", {"action": "short", "key": "p25:metro:9001", "short": " PD  North ",
                                              "operator": "Lt. Test"})
    assert out["radio_shorts"] == {"p25:metro:9001": "PD North"}
    assert cad.state()["radio_names"] not in ("", r1)                 # every page asks the names again
    (a,) = [r for r in cad.settings.audit_log("radios") if r["key"] == "radio_shorts/p25:metro:9001"]
    assert (a["operator"], a["old"], a["new"]) == ("Lt. Test", "default", "PD North")
    # the same again: nothing written; too long: refused
    cad.settings_post("radio_folders", {"action": "short", "key": "p25:metro:9001", "short": "PD North", "operator": "Lt. Test"})
    assert len([r for r in cad.settings.audit_log("radios") if r["key"].startswith("radio_shorts/")]) == 1
    with pytest.raises(Exception, match="at most"):
        cad.settings_post("radio_folders", {"action": "short", "key": "fm:FG1", "short": "x" * 30, "operator": "Lt. Test"})
    # the hub's own text says it (an alarm's push), the page is given it
    who = cad.alarms.who_of({"talkgroup": 9001, "channel": "PD Dispatch", "source": "p25:metro:9001"})
    assert who["channel"] == "Town PD Dispatch North (TG 9001)"
    page = cad.radio_names()
    assert page["names"] == {"p25:metro:9001": "Town PD Dispatch North"} and page["shorts"] == {"p25:metro:9001": "PD North"}
    assert page["systems"]["metro"] == "Metro"
    # exported, checked on the way in, and back after a reset
    exp = cad.settings_export()
    assert exp["overrides"]["radio_shorts"] == {"p25:metro:9001": "PD North"}
    assert cad.settings_post("import", {"action": "dry_run", "settings": exp, "operator": "Lt. Test"})["ok"]
    bad = json.loads(json.dumps(exp))
    bad["overrides"]["radio_shorts"] = {"tg:4401": "Ops"}             # not qualified
    with pytest.raises(Exception, match="radio_shorts"):
        cad.settings_post("import", {"action": "dry_run", "settings": bad, "operator": "Lt. Test"})
    bad["overrides"]["radio_shorts"] = {"p25:westchester:4401": "x" * 30}
    with pytest.raises(Exception, match="radio_shorts"):
        cad.settings_post("import", {"action": "dry_run", "settings": bad, "operator": "Lt. Test"})
    # blank: its name again (nothing kept)
    cad.settings_post("radio_folders", {"action": "short", "key": "p25:metro:9001", "short": "", "operator": "Lt. Test"})
    assert cad.radio_names()["shorts"] == {} and "radio_shorts" not in cad.settings_export()["overrides"]
    assert radionames.short({"source": "p25:metro:9001", "channel": "PD Dispatch"}) == "Town PD Dispatch North"


def test_a_reader_who_may_not_see_another_system_gets_none_of_its_names(tmp_path, fresh):
    cad = make_cad(tmp_path)
    cad.comms.context_broadcast = True
    cad.source_systems = lambda: {"westchester": "County", "metro": "Metro"}
    for key, name in (("p25:metro:9001", "Town PD Dispatch North"), ("fm:FG1", "Fireground 1")):
        cad.settings_post("radio_folders", {"action": "name", "key": key, "name": name, "operator": "Lt. Test"})
        cad.settings_post("radio_folders", {"action": "short", "key": key, "short": name[:6], "operator": "Lt. Test"})
    api = CadApi(cad)

    def names(who=None):
        code, raw, _ = api.get("/api/cad/radio-names", "", who)
        assert code == 200
        return json.loads(raw)
    signed = names(Identity(email="someone@example.org"))
    assert set(signed["names"]) == set(signed["shorts"]) == {"p25:metro:9001", "fm:FG1"}
    anon = names()
    assert anon["names"] == {"fm:FG1": "Fireground 1"} and anon["shorts"] == {"fm:FG1": "Firegr"}
    assert "metro" not in json.dumps(anon).lower()
    # nor may such an admin label another system's talkgroup
    with sourcekeys.request_context(None), pytest.raises(Exception, match="another system"):
        cad.settings_post("radio_folders", {"action": "short", "key": "p25:metro:9003", "short": "X", "operator": "Lt. Test"})


# ---- every label path goes through the one resolver ------------------------------------------------------------------

# A `.channel` inside a label that is not a display: what it is for
NOT_DISPLAY = {
    ("cad-feed.js", "encodeURIComponent(f.channel)"): "a link's filter: the real label",
    ("health.js", "x.channel"): "a station health hint's subject (a receiver, the station), not a radio source",
    ("radio-nav.js", "e.channel"): "a source key being built (fm:<label>, air:<label>)",
}
TEMPLATE = re.compile(r"\$\{((?:[^{}]|\{[^{}]*\})*?\.channel\b(?:[^{}]|\{[^{}]*\})*?)\}")
EL_TEXT = re.compile(r'\bel\(\s*"[a-z0-9]+",\s*[`"][^`"]*[`"],\s*([^()]*\.channel)\s*\)')
ASSIGN = re.compile(r"(?:textContent|\.title|\.label)\s*=\s*([^;]*\.channel\b[^;]*);|setText\(([^;]*\.channel\b[^;]*)\)")


def test_every_channel_label_on_the_page_asks_the_resolver():
    bad = []
    for f in sorted(STATIC.glob("*.js")):
        if f.name in ("radio-names.js", "radio-controls.js", "station.js", "squelch-panel.js", "signal-panel.js",
                      "signal-chart.js"):
            continue      # the resolver itself; the station's own receivers and their charts (no CAD, no names)
        for i, line in enumerate(f.read_text().splitlines(), 1):
            for m in [*TEMPLATE.finditer(line), *EL_TEXT.finditer(line), *ASSIGN.finditer(line)]:
                e = next(g for g in m.groups() if g).strip()
                if "chName" in e or "RadioNames" in e or re.search(r"\bch\(", e):
                    continue
                if (f.name, e) in NOT_DISPLAY:
                    continue
                bad.append(f"{f.name}:{i}: {e}")
    assert not bad, "a channel label not from radio-names.js (chName):\n" + "\n".join(bad)
    # the chip of every transmission row (the Radio views, the incident's Radio card and Heard, the pre-alert window,
    # the feed): radioRow's, the short label with the system and full name on hover
    cad = (STATIC / "cad.js").read_text()
    assert 'const src = el("span", "chip src", chName(t));' in cad and "src.title = chTitle(t);" in cad
    assert "typeof RadioNames !== \"undefined\" ? RadioNames.short(t)" in cad
    # the rail keeps no name of its own over the row's chip
    nav = (STATIC / "radio-nav.js").read_text()
    assert "shownName" not in nav and "srcChip" not in nav
    # each helper a module keeps asks chName (cad.js), which asks RadioNames
    for f in ("radio.js", "alarm.js", "cad-feed.js"):
        assert re.search(r"const ch = \([a-z]\) => \(typeof chName === \"function\"", (STATIC / f).read_text()), f
    html = (STATIC / "cad.html").read_text()
    assert html.index('src="/static/cad.js"') < html.index('src="/static/radio-names.js"')


def test_the_hubs_label_text_asks_the_resolver():
    """Text the hub writes for a person (an alarm's push, the pre-alert window's channel list, the operating channels)
    names a source through sizeup/radionames.py; the matchers beside it keep the real label."""
    src = {p: (ROOT / "sizeup" / p).read_text() for p in ("cad/alarms.py", "context_bundle.py", "cad/service.py")}
    assert 'channel = radionames.name(a) if' in src["cad/alarms.py"]
    assert "shown = list(dict.fromkeys(radionames.short(r)" in src["context_bundle.py"]
    assert 'f"t.channel IN (' in src["context_bundle.py"]               # (the window is still found by the real label)
    assert '"name": radionames.name({"talkgroup": tg, "channel": n})' in src["cad/service.py"]


# ---- the page's resolver (node) --------------------------------------------------------------------------------------

HARNESS = r"""
global.window = global;
const hooks = [];
let fetched = 0, rendered = 0;
global.cad = { hooks, state: { radio_names: "r1" }, txRev: 0 };
global.render = () => { rendered++; };
global.fetch = async (url) => { fetched++; return { ok: true, json: async () => ({ rev: "r1",
  names: { "p25:metro:9001": "Town PD Dispatch North", "tg:4401": "County Fire Ops", "fm:FG1": "Fireground 1" },
  shorts: { "p25:metro:9001": "PD North" }, systems: { westchester: "County", metro: "Metro" }, freqs: { FG1: "154.1" } }) }; };
const [FILE] = process.argv.slice(-1);
require(FILE);
const N = window.RadioNames;
(async () => {
  const before = [N.short({ source: "p25:metro:9001", channel: "PD Dispatch" }), N.title({ source: "p25:metro:9001", channel: "PD Dispatch", talkgroup: 9001 })];
  hooks.forEach((h) => h()); hooks.forEach((h) => h());
  await new Promise((r) => setTimeout(r, 10));
  hooks.forEach((h) => h());
  const row = { source: "p25:metro:9001", channel: "PD Dispatch", talkgroup: 9001, system: "metro" };
  const node = { textContent: "", title: "" };
  N.label(node, row);
  process.stdout.write(JSON.stringify({ before, fetched, rendered, txRev: cad.txRev,
    short: N.short(row), name: N.name(row), title: N.title(row), node,
    talking: N.short({ tg: 9001, p25_system: "metro", channel: "PD Dispatch" }),
    county: [N.short({ talkgroup: 4401, channel: "Fire Ops" }), N.title({ talkgroup: 4401, channel: "Fire Ops" })],
    fm: [N.short({ channel: "FG1", system: "fm" }), N.title({ channel: "FG1" })],
    unnamed: [N.short({ source: "p25:metro:9002", channel: "PD Tac" }), N.title({ source: "p25:metro:9002", channel: "PD Tac" })],
    key: N.short("p25:metro:9001") }));
})();
"""


def test_the_pages_resolver():
    if NODE is None:
        pytest.skip("node not installed")
    p = subprocess.run([NODE, "-e", HARNESS, "--", str(STATIC / "radio-names.js")], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    o = json.loads(p.stdout)
    assert o["before"] == ["PD Dispatch", "metro · PD Dispatch (TG 9001)"]       # before the names come: the real label
    assert (o["fetched"], o["rendered"], o["txRev"]) == (1, 1, 1)                  # asked once for that rev, rows drawn again
    assert (o["short"], o["name"]) == ("PD North", "Town PD Dispatch North")
    assert o["title"] == "Metro · Town PD Dispatch North (PD Dispatch, TG 9001)"
    assert o["node"] == {"textContent": "PD North", "title": o["title"]}
    assert o["talking"] == "PD North"
    assert o["county"] == ["County Fire Ops", "County · County Fire Ops (Fire Ops, TG 4401)"]
    assert o["fm"] == ["Fireground 1", "Analog · Fireground 1 (FG1, 154.1 MHz)"]
    assert o["unnamed"] == ["PD Tac", "Metro · PD Tac (TG 9002)"]
    assert o["key"] == "PD North"
