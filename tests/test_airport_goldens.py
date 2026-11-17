"""Today's airport pre-alerts, pinned end to end (the pipeline migration's PR 1; tests/airport_golden.py says how).

Each invented scenario in tests/fixtures/pipeline/airband/ runs through a real App (App._transcribed / _retext ->
App._atc -> AtcWatch, AtcTalkers, AirportWatch -> AirportAlerts; the fire radio through Cad._on_text; the Aircraft
layer's pictures through App._atc_view; the console's accept / dismiss / read) on an injected clock, and what it did
(every stored row, push, poll-now, incident, CAD log line and the decisions behind them) must equal its committed
golden byte for byte. A difference fails with the diff. Change a golden only on purpose, never from a test run:

    .venv/bin/python -m tests.airport_golden --write <name>     (then read the diff before committing it)
"""

from __future__ import annotations

import json

import pytest

from sizeup import atc
from tests import airport_golden as G


@pytest.mark.parametrize("name", G.names())
def test_the_scenario_does_what_its_golden_says(name, tmp_path):
    d = G.diff(name, G.run(name, tmp_path))
    assert not d, f"{name}: today's airport pre-alerts changed (regenerate only on purpose):\n{d}"


def test_the_wiring_is_what_its_golden_says(tmp_path):
    """The numbers every decision uses (dedupe, group, cap, expiry, age gate, context, the rule table) as the App sets
    them, and where each kind of push goes: the airport pre-alerts on the ALERTS kind (the alerts topic, by role)."""
    d = G.diff("wiring", G.wiring(tmp_path))
    assert not d, f"wiring: changed (regenerate only on purpose):\n{d}"


def test_every_golden_has_its_scenario_and_back():
    goldens = {p.name.removesuffix(".golden.json") for p in G.FIXTURES.glob("*.golden.json")}
    assert goldens == {*G.names(), "wiring"}


def _golden(name: str) -> dict:
    return json.loads(G.golden_path(name).read_text())


def test_every_shipped_rule_has_a_golden_that_fires_and_one_that_does_not():
    """Structure-driven: a rule added to data/atc/rules.json without its goldens fails here (add a "fires" and a
    "quiet" case to rules.jsonl, then regenerate). A "fires" case's last transmission hits the rule and raises or adds
    to a pre-alert (a weak rule may need a second rule's words beside it: its golden says so); a "quiet" case is a
    near-miss that raises nothing."""
    shipped = [r.id for r in atc.Rules.load().rules]
    cases = _golden("rules")["cases"]
    assert [c["rule"] for c in G.load("rules")] == [c["rule"] for c in cases]
    assert {c["rule"] for c in cases} <= set(shipped), "a case for a rule that isn't shipped"
    for rid in shipped:
        fires = [c for c in cases if c["rule"] == rid and c["expect"] == "fires"]
        quiet = [c for c in cases if c["rule"] == rid and c["expect"] == "quiet"]
        assert fires and quiet, f"rule {rid}: no 'fires' / 'quiet' case in tests/fixtures/pipeline/airband/rules.jsonl"
        for c in fires:
            last = [d for s in c["steps"] for d in s.get("decisions", []) if d.get("watch") == "atc"][-1]
            assert rid in [h[0] for h in last["hits"] or []], f"rule {rid}: its 'fires' case doesn't hit it ({c['why']})"
            assert last["row"] is not None, f"rule {rid}: its 'fires' case raised nothing ({c['why']})"
        for c in quiet:
            assert not any("alerts" in s for s in c["steps"]), f"rule {rid}: its 'quiet' case raised one ({c['why']})"


def test_every_category_is_stored_in_some_golden():
    """Each category of the rule table (and the three fire categories the code derives) is the kind of a stored
    pre-alert in at least one golden."""
    kinds = set()
    for name in G.names():
        g = _golden(name)
        for s in [s for c in g.get("cases", []) for s in c["steps"]] + g.get("steps", []):
            kinds |= {a["kind"] for a in s.get("alerts", [])}
    want = set(atc.Rules.load().categories) | {"fire_off", "fire_acft", "fire_field"}
    assert want <= kinds, f"no golden stores a pre-alert of kind {sorted(want - kinds)}"
