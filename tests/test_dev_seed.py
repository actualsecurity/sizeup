"""tools/dev_seed.py: the invented calls a local instance is filled with (docs/development.md), against a real hub."""

import importlib.util
from pathlib import Path

import numpy as np

from sizeup.cad import dispatcher
from tests.test_cloud import TOKEN, HubRun, hub_cfg

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("dev_seed", ROOT / "tools" / "dev_seed.py")
dev_seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev_seed)


def test_a_local_hub_gets_the_calls_their_units_and_their_radio_traffic(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dispatcher, "tts", lambda text, voice="", rate_wpm=0: np.full(8000, 0.1, np.float32))
    hub = HubRun(hub_cfg(tmp_path))
    try:
        assert dev_seed.main(["--base", hub.url, "--token", TOKEN]) == 0
        incs = {i["address"]: i for i in hub.app.cad.state()["incidents"]}
        assert {a: i["status"] for a, i in incs.items()} == {
            "100 Example Ave": "active", "25 Sample St": "closed", "Example Ave / Sample St": "pending"}
        rows = hub.get("/api/cad/comms?type=radio")["rows"]
        assert len(rows) == sum(len(c["radio"]) for c in dev_seed.CALLS)
        on = {r["text"]: {e["address"] for e in r["events"]} for r in rows}
        for c in dev_seed.CALLS:                  # every transmission on its own call, none on another
            for _, text in c["radio"]:
                assert on[text] == {c["address"]}, text
        assert "radio traffic: 4 transmission(s)" in capsys.readouterr().out
    finally:
        hub.close()


def test_only_this_machine(capsys):
    assert dev_seed.main(["--base", "https://sizeup.example.org"]) == 2
    assert "not this machine" in capsys.readouterr().err
