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


def test_callsigns_named_and_belong_to_their_department():
    c = load(str(Path(__file__).resolve().parent.parent / "config.example.toml"))
    assert c.unit_callsigns["2398"] == "2398 Duty Officer"
    assert c.unit_departments["2391 Chief of Department"] == "Port Chester FD"


def test_paging_hang_and_continuation_defaults_and_limits(tmp_path):
    import pytest
    from sizeup.config import ConfigError
    c = load(ROOT / "config.example.toml")
    s2, fg1 = (next(ch for ch in c.channels if ch.label == n) for n in ("South 2", "FG1"))
    assert (s2.hang_s, s2.continue_s, s2.drop_db) == (1.5, 3.0, None)     # GitHub #61
    assert (fg1.hang_s, fg1.continue_s, fg1.drop_db) == (None, 0.0, 10.0)  # traffic: as before
    p = tmp_path / "c.toml"

    def chan(kind, extra):
        p.write_text(f'[radio]\ntones_file = ""\n[[radio.channels]]\nlabel = "X"\nmhz = 470.2\nkind = "{kind}"\n{extra}\n')
        return load(p).channels[0]
    assert (chan("paging", "hang_s = 2.0\ncontinue_s = 4").hang_s, chan("paging", "continue_s = 4").continue_s) == (2.0, 4.0)
    assert (chan("traffic", "hang_s = 1.0").hang_s, chan("traffic", "").continue_s) == (1.0, 0.0)
    for kind, extra in (("traffic", "continue_s = 3"), ("paging", "hang_s = 0"), ("paging", "continue_s = 60")):
        with pytest.raises(ConfigError, match="hang_s|continue_s"):
            chan(kind, extra)
