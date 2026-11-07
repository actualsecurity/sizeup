"""The station is at the South End firehouse, not Headquarters (GitHub #271): its name, the one-time config update
that renames an installed station, and the receiver's position (readsb --lat/--lon) from [station] lat / lon."""

import json
import os
import re
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from sizeup.cloud import configupdate
from sizeup.config import STATION_LL, ConfigError, load
from sizeup.radio import adsbd

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "deploy" / "station" / "config.station.toml"
OLD = "Port Chester FD Headquarters"
NEW = "Port Chester FD South End"


def south_end() -> tuple[float, float]:
    f = json.loads((ROOT / "data" / "firehouses.json").read_text())
    rows = next(v for v in f.values() if isinstance(v, list)) if isinstance(f, dict) else f
    ll = next(r["ll"] for r in rows if r["address"] == "51-53 Grace Church St")
    return ll[1], ll[0]


def before_271() -> str:
    """The station template as it was before #271 (what /etc/sizeup/config.toml was made from)."""
    t = TEMPLATE.read_text()
    t = re.sub(r'^name = "Port Chester FD South End".*$', f'name = "{OLD}"', t, count=1, flags=re.M)
    t = re.sub(r"^# Where the station PC.*?\nlon = [-0-9.]+\n", "", t, count=1, flags=re.M | re.S)
    t = re.sub(r'^(id = "pcfd-hq").*$', r"\1", t, count=1, flags=re.M)
    st = tomllib.loads(t)["station"]
    assert st["name"] == OLD and "lat" not in st and "lon" not in st
    return t


def installed(tmp_path, monkeypatch, text: str) -> Path:
    monkeypatch.setenv("SIZEUP_STATE_DIR", str(tmp_path / "state"))
    p = tmp_path / "etc" / "config.toml"
    p.parent.mkdir(exist_ok=True)
    p.write_text(text.replace("/var/lib/sizeup/trunk-recorder/talkgroups.csv", f"{ROOT}/deploy/trunk-recorder/talkgroups.csv"))
    os.chmod(p, 0o640)
    return p


# -- the name and the place ------------------------------------------------------------------------------------------


def test_the_default_station_position_is_the_south_end_firehouse():
    assert STATION_LL == pytest.approx(south_end(), abs=1e-5) and adsbd.REF == pytest.approx(STATION_LL, abs=1e-5)


@pytest.mark.parametrize("path", ["config.example.toml", "deploy/station/config.station.toml"])
def test_the_templates_say_south_end_and_keep_the_id(path, tmp_path, monkeypatch):
    monkeypatch.setenv("SIZEUP_STATE_DIR", str(tmp_path / "state"))
    text = (ROOT / path).read_text().replace("/var/lib/sizeup/trunk-recorder/talkgroups.csv",
                                            f"{ROOT}/deploy/trunk-recorder/talkgroups.csv")
    c = load(ROOT / path) if path == "config.example.toml" else load(installed(tmp_path, monkeypatch, text))
    assert (c.station_id, c.station_name) == ("pcfd-hq", NEW) and c.station_ll == pytest.approx(south_end(), abs=1e-5)
    assert OLD not in text


def test_station_lat_lon_are_both_or_neither_and_in_the_area(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[station]\nid = "x"\n')
    assert load(p).station_ll == STATION_LL
    p.write_text('[station]\nid = "x"\nlat = 41.0\nlon = -73.7\n')
    assert load(p).station_ll == (41.0, -73.7)
    for bad in ("lat = 41.0\n", "lat = 41.0\nlon = 'x'\n", "lat = 10.0\nlon = -73.7\n"):
        p.write_text(f'[station]\nid = "x"\n{bad}')
        with pytest.raises(ConfigError, match="station.lat"):
            load(p)


def test_readsb_gets_the_stations_position_from_the_config(tmp_path):
    conf = tmp_path / "config.toml"
    conf.write_text('[station]\nlat = 41.01\nlon = -73.68\n[aircraft.receiver]\nsdr = "adsb1090"\n')
    s = adsbd.settings(str(conf), str(tmp_path / "none.json"))
    assert (s["lat"], s["lon"]) == (41.01, -73.68)
    argv = adsbd.readsb_argv("readsb", 1, s["gain"], s["bias_tee"], adsbd.JSON_DIR, s["lat"], s["lon"])
    assert argv[argv.index("--lat") + 1] == "41.01000" and argv[argv.index("--lon") + 1] == "-73.68000"
    for odd in ("", "[station]\nlat = 'x'\nlon = -73.68\n", "[station]\nlat = 10.0\nlon = -73.68\n", "[station]\nlat = 41.0\n"):
        conf.write_text(odd)
        s = adsbd.settings(str(conf), str(tmp_path / "none.json"))
        assert (s["lat"], s["lon"]) == adsbd.REF                          # the South End default
    s = adsbd.settings(str(tmp_path / "missing.toml"), str(tmp_path / "none.json"))
    assert (s["lat"], s["lon"]) == adsbd.REF


# -- the one-time config update --------------------------------------------------------------------------------------


def test_an_installed_station_is_renamed_once_and_keeps_its_id(tmp_path, monkeypatch):
    conf = installed(tmp_path, monkeypatch, before_271())
    marks = tmp_path / "etc" / "config-updates"
    marks.write_text("fg2\ntones-47\nhpn-atc\nadsb-receiver\napproach-ground\nairband-sets\n")
    before = load(conf)
    assert (before.station_id, before.station_name) == ("pcfd-hq", OLD)
    said = configupdate.run(conf, marks)
    assert said == [f'south-end: [station] name is now "{NEW}" (was "{OLD}"; the id \'pcfd-hq\' is kept) '
                    f"(the old file: {conf.with_name('config.toml.before-south-end')})"]
    kept = conf.with_name("config.toml.before-south-end").read_text()
    assert kept == before_271().replace("/var/lib/sizeup/trunk-recorder/talkgroups.csv",
                                        f"{ROOT}/deploy/trunk-recorder/talkgroups.csv")
    assert conf.read_text() == kept.replace(f'name = "{OLD}"', f'name = "{NEW}"', 1)      # that one line
    assert oct(conf.stat().st_mode & 0o777) == "0o640"
    after = load(conf)
    assert (after.station_id, after.station_name) == ("pcfd-hq", NEW) and replace(after, station_name=OLD) == before
    assert after.station_ll == STATION_LL                                  # the code's default: South End
    assert marks.read_text().endswith("approach-ground\nsouth-end\nairband-sets\n")
    conf.write_text(kept)                                                  # put back by hand: stays so
    assert configupdate.run(conf, marks) == []


def test_a_name_set_by_hand_or_already_new_is_left_alone():
    for name in ("Port Chester FD", "Port Chester FD Headquarters (test)", "PCFD HQ"):
        text = f'[station]\nid = "pcfd-hq"\nname = "{name}"\n'
        assert configupdate.rename_south_end(text) == (text, f"south-end: the station is called {name!r}, not the old "
                                                             "default: nothing changed")
    text = f'[station]\nid = "pcfd-hq"\nname = "{NEW}"\n'
    assert configupdate.rename_south_end(text)[0] == text
    assert configupdate.rename_south_end("[web]\nport = 1\n")[0] == "[web]\nport = 1\n"
    # only [station]'s name: a [[tones]] entry by the same words stays, and a comment on the line is kept
    text = f'[station]\nid = "pcfd-hq"\nname = "{OLD}"   # ours\n\n[[tones]]\nname = "{OLD}"\na_hz = 1.0\nb_hz = 2.0\n'
    new, said = configupdate.rename_south_end(text)
    assert new == text.replace(f'name = "{OLD}"   # ours', f'name = "{NEW}"   # ours') and said.startswith("south-end: [station]")


def test_a_name_it_cant_change_safely_is_not_changed():
    for text in (f'station = {{ id = "pcfd-hq", name = "{OLD}" }}\n', f"[station]\nid = 'pcfd-hq'\nname = '{OLD}'\n",
                 f'[station]\nname = """{OLD}"""\n'):
        with pytest.raises(ValueError):
            configupdate.rename_south_end(text)


def test_the_update_needs_no_release_to_go_back_to():
    assert "south-end" not in configupdate.NEEDS_LEAVING                   # every release reads [station] name
    assert [n for n, _f in configupdate.UPDATES][-2:] == ["south-end", "airband-sets"]   # (#275's after it)
