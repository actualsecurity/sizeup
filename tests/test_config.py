from pathlib import Path

from sizeup.config import load

ROOT = Path(__file__).parent.parent


def test_example_config_loads_county_data():
    c = load(ROOT / "config.example.toml")
    # Tone list (RadioReference + learned on air) and the 2021 mutual aid plan apparatus roster.
    assert len(c.tones) > 80
    u = c.unit_departments
    assert u["Engine 61"] == u["Ladder 31"] == u["Tower Ladder 2"] == u["Rescue 40"] == "Port Chester FD"
    assert u["Engine 14"] == u["Ladder 30"] == "Rye Brook FD"     # not Ladder 54 (unassigned)
    assert "Ladder 54" not in u
    assert u["Engine 25"] == "New Rochelle"                          # the 15:32 South 2 page
    assert u["Engine 174"] == u["Ladder 1"] == "Fairview"            # the 15:43 South 2 page
