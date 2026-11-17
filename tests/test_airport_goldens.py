"""Today's airport pre-alerts, pinned end to end (the pipeline migration's PR 1; tests/airport_golden.py says how).

Each invented scenario in tests/fixtures/pipeline/airband/ runs through a real App (App._transcribed / _retext ->
App._atc -> AtcWatch, AtcTalkers, AirportWatch -> AirportAlerts; the fire radio through Cad._on_text; the Aircraft
layer's pictures through App._atc_view; the console's accept / dismiss / read) on an injected clock, with ntfy
replaced at the wire. Two comparisons, byte for byte:

    outcomes    <name>.golden.json: stored rows, incidents, CAD log lines, what was sent to ntfy, poll-now reasons,
                the console's answers. What the migration must keep.
    internals   <name>.trace.json: the decisions behind them and the watches' log lines. The migration may regenerate
                these on purpose (--write-trace) as the code moves; the outcomes stay.

Change a golden only on purpose, never from a test run:

    .venv/bin/python -m tests.airport_golden --write <name>          (then read the diff before committing it)
    .venv/bin/python -m tests.airport_golden --write-trace <name>    (internals only)
"""

from __future__ import annotations

import json

import pytest

from sizeup import atc
from sizeup.atc_normalize import normalize as written
from tests import airport_golden as G

_runs: dict[str, tuple[dict, dict]] = {}


def _run(name: str, tmp_path) -> tuple[dict, dict]:
    if name not in _runs:                                 # one run feeds both comparisons (in this worker)
        _runs[name] = G.run(name, tmp_path)
    return _runs[name]


@pytest.mark.parametrize("name", G.names())
def test_the_outcomes_are_what_the_golden_says(name, tmp_path):
    d = G.diff(G.golden_path(name), _run(name, tmp_path)[0])
    assert not d, f"{name}: today's airport pre-alert OUTCOMES changed (regenerate only on purpose):\n{d}"


@pytest.mark.parametrize("name", G.names())
def test_the_internals_are_what_the_trace_says(name, tmp_path):
    """The decisions and log lines behind the outcomes. A migration step that moves them on purpose regenerates the
    traces (--write-trace) with the outcome goldens untouched."""
    d = G.diff(G.trace_path(name), _run(name, tmp_path)[1])
    assert not d, f"{name}: the internals changed (outcomes are checked apart; --write-trace if on purpose):\n{d}"


def test_the_wiring_is_what_its_golden_says(tmp_path):
    """The numbers every decision uses (dedupe, group, cap, expiry, age gate, context, the rule table) as the App sets
    them, the config file's defaults for them, and where each kind of push goes: the airport pre-alerts on the ALERTS
    kind (the alerts topic, by role)."""
    d = G.diff(G.golden_path("wiring"), G.wiring(tmp_path))
    assert not d, f"wiring: changed (regenerate only on purpose):\n{d}"


def test_the_config_file_defaults_are_what_the_goldens_ran_on(tmp_path):
    """The goldens' App is built from the Config dataclass's defaults; a config file that sets none of the airport's
    settings must load the same (config.load), or the goldens pin something production doesn't run."""
    assert G.config_file_defaults(tmp_path) == G.dataclass_defaults()


def test_every_golden_has_its_scenario_and_back():
    goldens = {p.name.removesuffix(".golden.json") for p in G.FIXTURES.glob("*.golden.json")}
    traces = {p.name.removesuffix(".trace.json") for p in G.FIXTURES.glob("*.trace.json")}
    assert goldens == {*G.names(), "wiring"} and traces == set(G.names())


def _golden(name: str) -> dict:
    return json.loads(G.golden_path(name).read_text())


def _norm(text: str) -> str:
    """The words as AtcWatch reads them: the written form (App._atc), then atc.normalize."""
    return atc.normalize(written(text))


RULES = atc.Rules.load()


def test_every_shipped_rule_has_a_golden_that_fires_and_one_that_does_not():
    """Structure-driven: a rule added to data/atc/rules.json without its goldens fails here (add a "fires" and a
    "quiet" case to rules.jsonl, then regenerate). From the outcomes only:

        fires   the case stored a pre-alert whose snippet of the case's transmission lists the case's "words" among
                what it heard, and those words are the rule's (its pattern matches them whole)
        quiet   the case stored nothing; "via" says why, checked against the rule table: "not" (the pattern AND its
                'not' match), "pattern" (the pattern doesn't), "readout" (inside an ATIS / NOTAM, a rule that isn't
                'always'), "threshold" (the pattern matches, the weight alone is under the threshold), "routine"
                (normal sequencing in the same words, a 'routine' rule)"""
    by_id = {r.id: r for r in RULES.rules}
    cases = _golden("rules")["cases"]
    assert [(c["rule"], c["expect"]) for c in G.load("rules")] == [(c["rule"], c["expect"]) for c in cases]
    assert {c["rule"] for c in cases} <= set(by_id), "a case for a rule that isn't shipped"
    for rid, rule in by_id.items():
        fires = [c for c in cases if c["rule"] == rid and c["expect"] == "fires"]
        quiet = [c for c in cases if c["rule"] == rid and c["expect"] == "quiet"]
        assert fires and quiet, f"rule {rid}: no 'fires' / 'quiet' case in tests/fixtures/pipeline/airband/rules.jsonl"
        for c in fires:
            tid = c["steps"][-1]["step"]["id"]
            snips = [s for st in c["steps"] for a in st.get("alerts", []) for s in a["data"].get("snippets", [])
                     if s.get("id") == tid]
            assert snips and any(c["words"] in s.get("hits", []) for s in snips), \
                f"rule {rid}: its 'fires' case stored no pre-alert hearing {c['words']!r} ({c['why']})"
            assert rule.rx.fullmatch(c["words"]), f"rule {rid}: {c['words']!r} aren't its words"
        for c in quiet:
            assert not any("alerts" in st for st in c["steps"]), f"rule {rid}: its 'quiet' case stored one ({c['why']})"
            norm, via = _norm(c["steps"][-1]["step"]["text"]), c["via"]
            hit = bool(rule.rx.search(norm))
            ok = {"not": hit and rule.never is not None and bool(rule.never.search(norm)),
                  "pattern": not hit,
                  "readout": hit and not rule.always and bool(RULES.suppress and RULES.suppress.search(norm)),
                  "threshold": hit and rule.weight < RULES.threshold,
                  "routine": hit and rule.routine and RULES.is_routine(norm)}.get(via, False)
            assert ok, f"rule {rid}: its quiet case isn't quiet by {via!r} ({c['why']}): {norm!r}"


def test_every_not_clause_has_a_case_it_excludes():
    """Each rule with a 'not' has a quiet case that hits its pattern and is excluded by the 'not' (checked above)."""
    vias = {(c["rule"], c.get("via")) for c in G.load("rules")}
    missing = [r.id for r in RULES.rules if r.never is not None and (r.id, "not") not in vias]
    assert not missing, f"no quiet case excluded by the 'not' of {missing}"


def test_every_category_is_stored_in_some_golden():
    """Each category of the rule table (and the three fire categories the code derives) is the kind of a stored
    pre-alert in at least one golden."""
    kinds = set()
    for name in G.names():
        g = _golden(name)
        for s in [s for c in g.get("cases", []) for s in c["steps"]] + g.get("steps", []):
            kinds |= {a["kind"] for a in s.get("alerts", [])}
    want = set(RULES.categories) | {"fire_off", "fire_acft", "fire_field"}
    assert want <= kinds, f"no golden stores a pre-alert of kind {sorted(want - kinds)}"


def test_the_outcome_goldens_hold_only_outcomes():
    """The strict goldens never carry the internals (those are the traces')."""
    for name in G.names():
        g = _golden(name)
        for s in [s for c in g.get("cases", []) for s in c["steps"]] + g.get("steps", []):
            assert set(s) <= {"step", *G.OUTCOMES}, f"{name}: {sorted(set(s) - {'step', *G.OUTCOMES})}"
