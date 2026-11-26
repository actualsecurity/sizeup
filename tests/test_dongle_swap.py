"""A dongle swapped for a new one (sizeup/radio/replace.py; App.replace_offers / replace_sdr; Health; the console; the
hub's optional auto-replace, sizeup/cloud/dongleswap.py).

2026-11-25: new dongles (new serials) went in, the jobs stayed on the old serials, coverage was lost for hours. Fakes only,
as tests/test_antenna_groups.py (whose rig this uses): the USB list is a Python list, nothing is ever opened, claims live
in a temp dir. Serials, products and tuners are made up."""

from __future__ import annotations

import json
import threading

import pytest

import sizeup.app as appmod
import sizeup.radio.claims as claims
from sizeup.cloud.dongleswap import AutoReplace
from sizeup.config import ChannelCfg
from sizeup.radio import replace as swap
from tests.test_antenna_groups import GROUPS, P25, grouped, mapped, on, rig  # noqa: F401  (rig: the fixture)
from tests.test_health import Rig, station_status


def gone(app, sd: str, s: float = swap.MISSING_S + 1) -> None:
    """As if `sd` had been gone `s` seconds (in this run), and last seen then."""
    app.sdr()
    app._absent[sd] -= s
    if "at" in app._last_seen.get(sd, {}):
        app._last_seen[sd]["at"] -= s


def unplug(plugged, *serials) -> None:
    plugged[:] = [d for d in plugged if d[2] not in serials]


# -- the matching alone ------------------------------------------------------------------------------------------------

def test_tuners_from_what_rtl_fm_said_or_the_product():
    assert swap.caps("", "Rafael Micro R820T")["tuner"] == "R820T"
    assert swap.caps("Blog V4") == {"tuner": "R828D", "known": True, "hf": True, "bands": swap.TUNERS["R828D"],
                                    "product": "Blog V4"}
    assert swap.caps("RTL2838UHIDIR")["known"] is False                  # generic: taken as an R820T, never for HF
    assert swap.caps("RTL2838UHIDIR", "Elonics E4000 tuner")["tuner"] == "E4000"


@pytest.mark.parametrize("product, tuner, mhz, rate, ok", [
    ("RTL2838UHIDIR", "", [118.575], 0, True),                    # airband on the common R820T
    ("x", "FC2580", [118.575], 0, False),                         # ... never on an FC2580 (146-308, 438-924 MHz)
    ("x", "FC2580", [470.2], 0, True),
    ("x", "E4000", [1200.0], 0, False),                           # the E4000's gap
    ("x", "FC0012", [770.0], 0, True),
    ("RTL2838UHIDIR", "R820T", [7.2], 0, False),                  # HF: direct sampling only
    ("Blog V4", "", [7.2], 0, True),
    ("Blog V4", "", [477.19], 3.2e6, False),                      # more than an RTL2832U takes without drops
])
def test_fits(product, tuner, mhz, rate, ok):
    assert (swap.fits(swap.caps(product, tuner), swap.needs(mhz, rate)) == "") is ok


def test_two_gone_two_new_are_paired_by_capability_then_port():
    m = [{"serial": "old-air", "needs": [swap.needs([118.575])], "caps": swap.caps("x", "R820T"), "port": "1-1"},
         {"serial": "old-uhf", "needs": [swap.needs([470.2])], "caps": swap.caps("x", "FC2580"), "port": "1-2"}]
    n = [{"serial": "new-fc", "caps": swap.caps("x", "FC2580"), "port": "1-4"},
         {"serial": "new-r8", "caps": swap.caps("x", "R820T"), "port": "1-3"}]
    got = {o["missing"]: (o["pick"], o["choices"]) for o in swap.pair(m, n)}
    # the FC2580 can't do airband: the R820T goes to the airband job, the FC2580 to the UHF one (its own tuner)
    assert got == {"old-air": ("new-r8", ["new-r8"]), "old-uhf": ("new-fc", ["new-fc", "new-r8"])}
    # alike in every way: the port each old one was on decides; no port known: asked (no pick, both offered)
    same = [dict(x, caps=swap.caps("x", "R820T")) for x in m]
    n2 = [{"serial": "new-a", "caps": swap.caps("x", "R820T"), "port": "1-2"},
          {"serial": "new-b", "caps": swap.caps("x", "R820T"), "port": "1-1"}]
    assert {o["missing"]: o["pick"] for o in swap.pair(same, n2)} == {"old-air": "new-b", "old-uhf": "new-a"}
    n3 = [dict(x, port="") for x in n2]
    asked = swap.pair(same, n3)
    assert [(o["pick"], sorted(o["choices"]), o["only"]) for o in asked] == [("", ["new-a", "new-b"], False)] * 2


# -- the station -------------------------------------------------------------------------------------------------------

def swapped(rig, **kw):
    """The admin's mapping and groups, then the county's dongle (uhf-a) unplugged and a new one plugged in."""
    plugged, logs, make = rig
    app = grouped(make, sdr_auto_assign=False, **kw)
    app.sdr()                                 # (the station's state post, every 5 s: each dongle's product noted)
    unplug(plugged, "uhf-a")
    plugged.append((6, "RTL2838UHIDIR", "nw-1"))
    app._sdr_arrivals()                       # (its group's plan: a stand-in runs the county's meanwhile)
    return app


def test_a_swap_is_offered_after_60_s_on_health_and_the_console_and_use_it_moves_the_job_and_the_group(rig, tmp_path):
    plugged, logs, make = rig
    app = swapped(rig)
    assert app.sdr()["replace"] == []                                     # gone under 60 s: nothing yet
    gone(app, "uhf-a")
    (o,) = app.sdr()["replace"]
    assert (o["missing"], o["pick"], o["jobs"], o["group"], o["only"], o["critical"]) == (
        "uhf-a", "nw-1", [P25], "UHF", True, True)
    assert [c["serial"] for c in o["choices"]] == ["nw-1"]
    assert any("new SDR nw-1 fits V3 #1 (uhf-a)'s Fire/EMS 15" in x for x in logs)
    # the console's question (the CAD state's dongle_swaps)
    (q,) = app.cad.dongle_swaps()
    assert (q["missing"], q["pick"], q["jobs"]) == ("uhf-a", "nw-1", [P25])
    # Health's card, and the alert says which channel and that a new dongle fits, with its link
    r = Rig()
    r.at(0, status=station_status(sdr=app.sdr()))
    (card,) = r.h.model()["swaps"]
    assert (card["missing"], card["pick"], card["jobs"]) == ("uhf-a", "nw-1", [P25])
    a = next(a for a in r.h.alerts() if a["key"] == "sdr:uhf-a")
    assert a["title"] == "SDR V3 #1 gone" and P25 in a["text"] and a["text"].endswith("· new SDR nw-1 fits")
    assert a["who"]["link"] == "#settings/radios/receivers?sdr=uhf-a"
    # Use it
    assert app.replace_sdr("uhf-a", "nw-1") is None
    g = next(g for g in app.groups if g.name == "UHF")
    assert g.sdrs == ("nw-1", "uhf-b", "uhf-c")                          # in the gone one's place, not beside it
    assert app.assign["nw-1"] == P25 and app.set_by["nw-1"] == "person" and on(app, "FG1") == "uhf-c"
    assert "uhf-a" not in app.sdrs and "uhf-a" not in app.assign
    assert json.loads((tmp_path / "antenna_groups.json").read_text())["groups"][0]["sdrs"] == ["nw-1", "uhf-b", "uhf-c"]
    assert app.sdr()["replace"] == [] and app.cad.dongle_swaps() == []


def test_a_dongle_in_no_group_moves_its_job_and_not_now_puts_the_offer_off(rig):
    plugged, logs, make = rig
    app = grouped(make, sdr_auto_assign=False)
    unplug(plugged, "spare-1")                                            # FG2's dongle (in no group)
    plugged.append((6, "RTL2838UHIDIR", "nw-2"))
    gone(app, "spare-1")
    (o,) = app.sdr()["replace"]
    assert (o["missing"], o["pick"], o["jobs"], o["group"], o["critical"]) == ("spare-1", "nw-2", ["FG2"], "", False)
    assert app.replace_sdr("spare-1", "nw-2", "not_now") is None
    assert app.sdr()["replace"] == []
    app._swap_later.clear()
    assert app.replace_sdr("spare-1", "other") is not None                # never one it doesn't offer
    assert app.replace_sdr("spare-1", "nw-2") is None
    assert app.assign["nw-2"] == "FG2" and on(app, "FG2") == "nw-2" and "spare-1" not in app.sdrs


def test_an_incompatible_new_dongle_is_never_offered(rig):
    plugged, logs, make = rig
    chans = (ChannelCfg("South 2", 470.2, "paging"), ChannelCfg("FG1", 453.0375, "traffic"),
             ChannelCfg("FG2", 458.0375, "traffic"), ChannelCfg("HPN Tower", 118.575, "airband"))
    app = grouped(make, sdr_auto_assign=False, channels=chans)
    plugged.append((6, "RTL2838UHIDIR", "air-1"))
    assert app.set_mode("HPN Tower", "air-1") is None
    unplug(plugged, "air-1")
    plugged.append((7, "NESDR FC2580", "nw-fc"))                          # an FC2580 tuner: no airband
    gone(app, "air-1")
    assert app.sdr()["replace"] == []
    plugged.append((8, "RTL2838UHIDIR", "nw-r8"))
    (o,) = app.sdr()["replace"]
    assert [c["serial"] for c in o["choices"]] == ["nw-r8"]


def test_two_gone_two_new_on_the_station_are_paired(rig):
    plugged, logs, make = rig
    app = grouped(make, sdr_auto_assign=False)
    app.sdr()                                 # (the station's state post, every 5 s: each dongle's product noted)
    unplug(plugged, "uhf-b", "spare-1")                                   # South 2 (a Blog V4) and FG2 (generic)
    plugged += [(6, "RTL2838UHIDIR", "nw-gen"), (7, "Blog V4", "nw-v4")]
    gone(app, "uhf-b")
    gone(app, "spare-1")
    got = {o["missing"]: o["pick"] for o in app.sdr()["replace"]}
    assert got == {"uhf-b": "nw-v4", "spare-1": "nw-gen"}


def test_another_apps_dongle_and_one_a_person_switched_off_are_never_offered(rig):
    plugged, logs, make = rig
    app = swapped(rig)
    plugged.append((7, "RTL2838UHIDIR", "nw-off"))
    assert app.set_mode("Off", "nw-off") is None                          # a person's Off: handed over, or parked
    claims.DIR.mkdir(parents=True, exist_ok=True)
    (claims.DIR / "nw-1.json").write_text(json.dumps({"app": "shortfinal", "pid": 1, "mode": "scan", "since": 0}))
    gone(app, "uhf-a")
    assert app.sdr()["replace"] == []
    (claims.DIR / "nw-1.json").unlink()
    assert [c["serial"] for c in app.sdr()["replace"][0]["choices"]] == ["nw-1"]


def test_a_new_dongle_out_for_no_audio_is_never_offered(rig):
    app = swapped(rig)
    gone(app, "uhf-a")
    app._noaudio["nw-1"] = {"since": 0.0, "job": "FG1"}
    assert app.sdr()["replace"] == []


def test_a_group_member_gone_a_day_is_a_health_hint_with_remove(rig, tmp_path):
    plugged, logs, make = rig
    app = grouped(make, sdr_auto_assign=False)
    app.sdr()                                                             # (each dongle's last sight noted)
    unplug(plugged, "m700-b")
    app.sdr()
    app._last_seen["m700-b"]["at"] -= 26 * 3600
    r = Rig()
    r.hub.now = app._last_seen["m700-b"]["at"] + 26 * 3600
    r.post(station_status(sdr=app.sdr()))
    r.h.tick()
    h = next(x for x in r.h.model()["hints"] if x.get("action"))
    assert h["text"] == "m700-b missing 1 d: remove from group?" and h["action"]["missing"] == "m700-b"
    assert app.replace_sdr("m700-b", action="drop") is None
    assert next(g for g in app.groups if g.name == "700").sdrs == ("m700-a",)
    assert app.replace_sdr("m700-a", action="drop") is not None          # plugged in: never from here


# -- Health: a gone dongle out of the config isn't "Recovered" ---------------------------------------------------------

def test_a_gone_dongle_taken_out_of_the_config_clears_as_removed_never_recovered(rig):
    from sizeup.cloud.health import SETTLE_S
    app = swapped(rig)
    r = Rig()
    r.at(0, status=station_status(sdr=app.sdr()))
    r.at(120, status=station_status(sdr=app.sdr()))
    assert any(m["title"].startswith("SDR V3 #1 gone") for m in r.n.msgs)          # pushed
    gone(app, "uhf-a")
    assert app.replace_sdr("uhf-a", "nw-1") is None
    ch = r.at(130, status=station_status(sdr=app.sdr()))
    (k, a), = [(k, a) for k, a in ch if a["key"] == "sdr:uhf-a"]
    assert k == "clear" and a["title"] == "SDR uhf-a removed from config" and a["text"] == "jobs now on SDR nw-1"
    assert r.n.msgs[-1]["title"].startswith("SDR uhf-a removed from config (jobs now on SDR nw-1)")
    r.at(130 + SETTLE_S + 60, status=station_status(sdr=app.sdr()))
    assert not any(m["title"].startswith("Recovered: SDR") for m in r.n.msgs)
    # ... while one that comes back is a recovery, as before
    r2 = Rig()
    st = station_status()
    st["sdr"]["sdrs"][0]["present"] = False
    r2.at(0, status=st)
    r2.at(120, status=st)
    r2.at(130, status=station_status())
    r2.at(130 + SETTLE_S + 60, status=station_status())
    assert r2.n.msgs[-1]["title"].startswith("Recovered: SDR 101 gone")


def test_the_gone_alert_push_links_to_its_row_in_receivers(rig):
    app = swapped(rig)
    r = Rig()
    r.at(0, status=station_status(sdr=app.sdr()))
    r.at(120, status=station_status(sdr=app.sdr()))
    m = next(m for m in r.n.msgs if m["title"].startswith("SDR V3 #1 gone"))
    assert m["click"] == "https://hub.example/cad#settings/radios/receivers?sdr=uhf-a"
    assert "Fire/EMS 15" in m["title"] and "Fire/EMS 15 on SDR V3 #3 (UHF)" in m["body"]


# -- the hub: a person's answer, audited; auto-replace -----------------------------------------------------------------

def test_use_it_is_an_admins_audited_action(rig, monkeypatch):
    from sizeup.cad.api import CadApi, needs, summarize
    from sizeup.cloud.access import Identity
    from sizeup.cloud.station import CALLS
    from tests.test_p25_systems import _post, _serve
    assert "replace_sdr" in CALLS and needs("/api/sdr-replace") == "admin"
    body = {"missing": "uhf-a", "new": "nw-1", "jobs": [P25], "group": "UHF"}
    assert summarize("/api/sdr-replace", body, body, None)[0] == \
        "Used new SDR nw-1 for gone SDR uhf-a (Fire/EMS 15), group UHF"
    assert summarize("/api/sdr-replace", {"missing": "uhf-a", "action": "not_now"}, {}, None)[0] == \
        "New dongle for gone SDR uhf-a: not now"
    app = swapped(rig)
    CadApi(app.cad).did("/api/sdr-replace", Identity(email="admin@example.org"), body, body)
    (row,) = app.cad.activity.rows(email="admin@example.org")
    assert row["summary"].startswith("Used new SDR nw-1")
    gone(app, "uhf-a")
    server, url = _serve(app, monkeypatch, access=False)
    try:
        assert _post(url + "/api/sdr-replace", {"missing": "uhf-a"})[0] == 400          # no new dongle named
        assert _post(url + "/api/sdr-replace", {"missing": "uhf-a", "new": "nw-1"}) == (200, b'{"ok":true}')
    finally:
        server.shutdown()
    assert app.assign["nw-1"] == P25


class FakeStationHub:
    def __init__(self, app):
        self.app, self.calls = app, []

    def sdr(self):
        return self.app.sdr()

    def remote(self, fn, *args):
        self.calls.append((fn, *args))
        return getattr(self.app, fn)(*args)


def auto(app, on):
    from tests.test_health import FakeNotifier
    hub, n = FakeStationHub(app), FakeNotifier()
    app.cad.dongle_auto_replace = on
    return AutoReplace(hub, app.cad, n, lambda m: None, public_url="https://hub.example"), hub, n


@pytest.fixture
def usb(rig, monkeypatch):
    """Each dongle's USB port (usbreset.ports, as /sys says on Linux): {serial: port}, of those plugged in now. Edit it
    to plug a new dongle into a port."""
    plugged, logs, make = rig
    ports = {s: f"1-{i + 1}" for i, (_i, _p, s) in enumerate(plugged)}
    monkeypatch.setattr(appmod.usbreset, "ports",
                        lambda *a, **k: {s: ports[s] for _i, _p, s in plugged if s in ports})
    return ports


def alone(rig, **kw):
    """The admin's mapping with no antenna groups (no stand-in: a gone dongle's job runs nowhere), auto-assign off,
    each dongle's last sight (and port) noted."""
    plugged, logs, make = rig
    app = make(**{"sdr_auto_assign": False, **kw}).start()
    mapped(app)
    app.sdr()
    return app


def swap_in(rig, usb, old, new, port=None, product="RTL2838UHIDIR"):
    """`old` unplugged, then `new` plugged in: into `old`'s own port (or `port`)."""
    plugged, logs, make = rig
    was = usb[old]
    unplug(plugged, old)
    plugged.append((9 + len(plugged), product, new))
    usb[new] = port or was


def test_auto_replace_off_moves_nothing(rig, usb):
    app = alone(rig)
    swap_in(rig, usb, "uhf-a", "nw-1")
    gone(app, "uhf-a")
    a, hub, n = auto(app, False)
    assert app.sdr()["replace"][0]["auto"] is True
    assert a.tick() == [] and hub.calls == [] and app.assign.get("nw-1", "Off") == "Off"


def test_auto_replace_moves_the_countys_job_to_a_new_dongle_in_its_port_and_says_so(rig, usb):
    app = alone(rig)
    swap_in(rig, usb, "uhf-a", "nw-1")
    a, hub, n = auto(app, True)
    assert a.tick() == []                                                 # gone under 60 s: not yet
    gone(app, "uhf-a")
    (o,) = app.sdr()["replace"]
    assert (o["missing"], o["critical"], o["only"], o["auto"]) == ("uhf-a", True, True, True)
    assert a.tick() == [("uhf-a", "nw-1")]
    assert hub.calls == [("replace_sdr", "uhf-a", "nw-1", "use", "auto")]
    assert app.assign["nw-1"] == P25 and app.set_by["nw-1"] == "auto" and on(app, P25) == "nw-1"   # running there
    assert "uhf-a" not in app.assign
    (m,) = n.msgs
    assert m["title"].startswith("Auto-replaced SDR V3 #1") and "Fire/EMS 15 moved to new SDR nw-1" in m["body"]
    rows = a.cad.store.q("SELECT * FROM settings_audit WHERE key LIKE 'sdr_replace:%'")
    assert [(x["operator"], x["section"], x["new"]) for x in rows] == [("auto-replace", "radios", "SDR nw-1")]


def test_auto_replace_needs_the_gone_dongles_own_usb_port(rig, usb, monkeypatch):
    app = alone(rig)
    swap_in(rig, usb, "uhf-a", "nw-1", port="1-9")                       # another port: a person decides
    gone(app, "uhf-a")
    a, hub, n = auto(app, True)
    (o,) = app.sdr()["replace"]
    assert o["pick"] == "nw-1" and o["only"] and o["auto"] is False
    assert a.tick() == [] and hub.calls == []
    assert app.replace_sdr("uhf-a", "nw-1", "use", "auto") is not None     # the station refuses it too
    usb["nw-1"] = "1-1"
    assert app.sdr()["replace"][0]["auto"] is True
    monkeypatch.setattr(appmod.usbreset, "ports", lambda *a, **k: {})      # no ports known (macOS): never
    assert app.sdr()["replace"][0]["auto"] is False and a.tick() == []


def test_s2_an_old_idle_spare_is_never_auto_replaced_even_in_the_gone_ones_port(rig, usb):
    plugged, logs, make = rig
    plugged.append((7, "RTL2838UHIDIR", "idle"))
    usb["idle"] = "2-1"                                                   # another bus
    app = alone(rig)
    app._last_seen["idle"]["first"] -= 3600                               # there for an hour, Off
    unplug(plugged, "uhf-a")
    gone(app, "uhf-a")
    a, hub, n = auto(app, True)
    (o,) = app.sdr()["replace"]
    assert [c["serial"] for c in o["choices"]] == ["idle"] and o["only"] and o["auto"] is False
    assert a.tick() == [] and app.assign.get("idle", "Off") == "Off"
    usb["idle"] = usb.pop("uhf-a")                                        # moved into the gone one's port: still old
    assert app.sdr()["replace"][0]["auto"] is False and a.tick() == []
    assert app.replace_sdr("uhf-a", "idle") is None and on(app, P25) == "idle"       # a person may


def test_s1_a_stand_in_already_running_the_job_is_never_auto_replaced(rig, usb):
    plugged, logs, make = rig
    app = swapped(rig)                                                    # UHF group: a stand-in runs the county's
    usb["nw-1"] = usb["uhf-a"]
    gone(app, "uhf-a")
    assert on(app, P25) not in (None, "nw-1")
    a, hub, n = auto(app, True)
    (o,) = app.sdr()["replace"]
    assert o["pick"] == "nw-1" and o["only"] and o["auto"] is False      # still asked, never moved by itself
    assert a.tick() == [] and hub.calls == [] and app.assign.get("nw-1", "Off") == "Off"
    assert app.replace_sdr("uhf-a", "nw-1", "use", "auto") is not None


def test_s3_a_job_auto_assign_already_moved_is_never_offered_or_moved_again(rig, usb):
    plugged, logs, make = rig
    plugged.append((7, "RTL2838UHIDIR", "idle"))                          # every job covered: Off by auto
    app = alone(rig, sdr_auto_assign=True)
    app._sdr_arrivals()
    assert app.assign.get("idle", "Off") == "Off"
    app._last_seen["idle"]["first"] -= 3600
    swap_in(rig, usb, "uhf-a", "nw-x")
    app._sdr_arrivals()                                                   # auto-assign: nw-x gets the county's
    assert app.assign["nw-x"] == P25 and on(app, P25) == "nw-x"
    gone(app, "uhf-a")
    a, hub, n = auto(app, True)
    assert app.sdr()["replace"] == [] and app.cad.dongle_swaps() == []    # nothing left to ask (auto off too)
    assert a.tick() == [] and app.assign["nw-x"] == P25 and on(app, P25) == "nw-x"


def test_s7_a_staggered_double_swap_never_takes_the_other_ones_new_dongle(rig, usb):
    plugged, logs, make = rig
    app = alone(rig)
    unplug(plugged, "uhf-a")
    gone(app, "uhf-a")                                                    # the county's: gone a while
    swap_in(rig, usb, "spare-1", "nw-1", port=usb["uhf-a"])               # FG2's swapped just now (its new one, here)
    a, hub, n = auto(app, True)
    (o,) = app.sdr()["replace"]                                           # (FG2's under 60 s: not asked yet)
    assert (o["missing"], o["only"], o["auto"]) == ("uhf-a", True, False)
    assert a.tick() == [] and app.assign.get("nw-1", "Off") == "Off"
    gone(app, "spare-1")
    assert app.replace_sdr("spare-1", "nw-1", "not_now") is None          # put off: still counted
    (o,) = app.sdr()["replace"]
    assert o["missing"] == "uhf-a" and o["auto"] is False and a.tick() == []


def test_auto_replace_needs_the_same_tuner_when_both_are_known(rig, usb):
    plugged, logs, make = rig
    app = alone(rig)
    swap_in(rig, usb, "uhf-b", "nw-fc", product="NESDR FC2580")          # South 2 (paging) was on a Blog V4 (R828D)
    gone(app, "uhf-b")
    (o,) = app.sdr()["replace"]
    assert (o["missing"], o["critical"], o["only"], o["auto"]) == ("uhf-b", True, True, False)
    unplug(plugged, "nw-fc")
    usb.pop("nw-fc")
    swap_in(rig, usb, "uhf-b", "nw-v4", port="1-2", product="Blog V4")
    (o,) = app.sdr()["replace"]
    assert [c["serial"] for c in o["choices"]] == ["nw-v4"] and o["auto"] is True


def test_auto_replace_never_takes_an_ambiguous_fit(rig, usb):
    plugged, logs, make = rig
    app = alone(rig)
    swap_in(rig, usb, "uhf-a", "nw-1")
    plugged.append((8, "RTL2838UHIDIR", "nw-2"))                          # two new ones fit: a person decides
    usb["nw-2"] = "1-8"
    gone(app, "uhf-a")
    a, hub, n = auto(app, True)
    (o,) = app.sdr()["replace"]
    assert len(o["choices"]) == 2 and o["auto"] is False
    assert a.tick() == [] and hub.calls == [] and n.msgs == []


def test_auto_replace_never_takes_a_job_that_isnt_the_countys_or_paging(rig, usb):
    app = alone(rig)
    swap_in(rig, usb, "spare-1", "nw-2")                                  # FG2's: a fireground, never by itself
    gone(app, "spare-1")
    a, hub, n = auto(app, True)
    (o,) = app.sdr()["replace"]
    assert o["only"] and not o["critical"] and o["auto"] is False and a.tick() == [] and hub.calls == []


# -- Use it: what runs, what is kept, what goes back on a failure ------------------------------------------------------

def test_use_it_runs_the_job_on_the_new_dongle_and_survives_a_restart(rig, usb, tmp_path, monkeypatch):
    plugged, logs, make = rig
    app = swapped(rig)
    gone(app, "uhf-a")
    app._noaudio["uhf-a"] = {"since": 0.0, "job": P25, "tries": 0}
    switched = []
    real = app.reception.switched
    monkeypatch.setattr(app.reception, "switched", lambda sd, m: (switched.append((sd, m)), real(sd, m))[1])
    assert app.replace_sdr("uhf-a", "nw-1") is None
    assert on(app, P25) == "nw-1" and switched == [("nw-1", P25)] and "uhf-a" not in app._noaudio
    app.stop()
    again = make(sdr_auto_assign=False).start()
    again._sdr_arrivals()
    assert next(g for g in again.groups if g.name == "UHF").sdrs == ("nw-1", "uhf-b", "uhf-c")
    assert again.assign["nw-1"] == P25 and on(again, P25) == "nw-1" and "uhf-a" not in again.assign
    assert again.sdr()["replace"] == [] and again.missing_rows() == []


def test_use_it_in_no_group_survives_a_restart(rig, usb):
    plugged, logs, make = rig
    app = alone(rig)
    swap_in(rig, usb, "spare-1", "nw-2")
    gone(app, "spare-1")
    assert app.replace_sdr("spare-1", "nw-2") is None and on(app, "FG2") == "nw-2"
    app.stop()
    again = make(sdr_auto_assign=False).start()
    assert again.assign["nw-2"] == "FG2" and on(again, "FG2") == "nw-2" and "spare-1" not in again.assign


def test_s9_a_failed_start_in_no_group_puts_the_job_back_on_the_gone_dongle(rig, usb, monkeypatch):
    plugged, logs, make = rig
    app = alone(rig)
    swap_in(rig, usb, "spare-1", "nw-2")
    gone(app, "spare-1")
    real = app._start_mode
    monkeypatch.setattr(app, "_start_mode", lambda m, s: False if s == "nw-2" else real(m, s))
    assert app.replace_sdr("spare-1", "nw-2") is not None
    assert app.assign["spare-1"] == "FG2" and app.assign.get("nw-2", "Off") == "Off"     # never on no dongle
    assert json.loads(app._mode_file.read_text())["spare-1"] == "FG2"
    assert [o["missing"] for o in app.sdr()["replace"]] == ["spare-1"]   # still asked


def test_s8_a_failed_start_in_a_group_keeps_the_group_and_its_file(rig, usb, tmp_path, monkeypatch):
    plugged, logs, make = rig
    app = swapped(rig)
    gone(app, "uhf-a")
    stand_in = on(app, P25)
    real = app._start_mode
    monkeypatch.setattr(app, "_start_mode", lambda m, s: False if s == "nw-1" else real(m, s))
    assert app.replace_sdr("uhf-a", "nw-1") is not None
    assert next(g for g in app.groups if g.name == "UHF").sdrs == ("uhf-a", "uhf-b", "uhf-c")
    assert json.loads((tmp_path / "antenna_groups.json").read_text())["groups"][0]["sdrs"] == UHF_SAVED
    assert on(app, P25) == stand_in != "nw-1" and app.assign.get("nw-1", "Off") == "Off"
    assert app._own_of("uhf-a") == P25 and [o["missing"] for o in app.sdr()["replace"]] == ["uhf-a"]


UHF_SAVED = ["uhf-a", "uhf-b", "uhf-c"]


def programs(app) -> dict[str, list[str]]:
    """{serial: the jobs whose program runs (or waits) on it now}."""
    out: dict[str, list[str]] = {}
    for m in app.modes:
        if m != "Off" and (sd := on(app, m)):
            out.setdefault(sd, []).append(m)
    return out


@pytest.mark.parametrize("fails", ["start", "save"])
def test_a_failed_grouped_use_it_never_leaves_two_programs_on_one_dongle(rig, usb, tmp_path, monkeypatch, fails):
    """The UHF group runs Fire/EMS 15 on its stand-in (uhf-c) with FG1 short. A Use it whose start fails (or whose
    groups file can't be written) may have moved programs on the way: everything goes back, one program per dongle."""
    plugged, logs, make = rig
    app = swapped(rig)
    gone(app, "uhf-a")
    before = programs(app)
    assert before["uhf-c"] == [P25] and on(app, "FG1") is None
    if fails == "start":
        real = app._start_mode
        monkeypatch.setattr(app, "_start_mode", lambda m, s: False if s == "nw-1" else real(m, s))
    else:
        def no_save(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(appmod.antenna, "save", no_save)
    assert app.replace_sdr("uhf-a", "nw-1") is not None
    now = programs(app)
    assert all(len(jobs) == 1 for jobs in now.values()), now
    assert now == before and on(app, P25) == "uhf-c"
    assert json.loads((tmp_path / "antenna_groups.json").read_text())["groups"][0]["sdrs"] == UHF_SAVED


def test_the_station_state_survives_a_fault_in_the_swap_offers(rig, monkeypatch):
    plugged, logs, make = rig
    app = swapped(rig)

    def boom(*a, **k):
        raise RuntimeError("bad row")
    monkeypatch.setattr(app, "replace_offers", boom)
    for _ in range(3):
        st = app.sdr()
        assert st["replace"] == [] and st["missing"] == [] and st["sdrs"]
    assert len([x for x in logs if "dongle swap offers" in x]) == 1          # said once


def test_a_serial_in_sysfs_that_is_no_utf_8_never_breaks_the_ports(tmp_path):
    from sizeup.radio import usbreset
    for name, serial in (("1-1", b"\xff\xfe\x00bad"), ("1-2", b"00000102\n")):
        d = tmp_path / name
        d.mkdir()
        (d / "idVendor").write_text("0bda\n")
        (d / "serial").write_bytes(serial)
    assert usbreset.ports(tmp_path) == {"00000102": "1-2"}


# -- what is never offered; two threads --------------------------------------------------------------------------------

def test_a_new_serial_on_two_dongles_and_a_lent_dongle_are_never_offered(rig):
    plugged, logs, make = rig
    app = swapped(rig)
    gone(app, "uhf-a")
    plugged.append((8, "RTL2838UHIDIR", "nw-1"))                          # nw-1 twice: which is which can't be told
    assert app.sdr()["replace"] == []
    plugged.pop()
    app.reception.lent = lambda: ("nw-1", "Off", "")                      # lent to a reception test
    try:
        assert app.sdr()["replace"] == []
    finally:
        del app.reception.lent
    assert [o["missing"] for o in app.sdr()["replace"]] == ["uhf-a"]


def test_the_offers_are_safe_from_two_threads(rig):
    app = swapped(rig)
    gone(app, "uhf-a")
    errors = []

    def look():
        try:
            for _ in range(50):
                app.replace_offers()
                app.missing_rows()
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=look) for _ in range(4)]
    for x in ts:
        x.start()
    for x in ts:
        x.join(30)
    assert errors == [] and [o["missing"] for o in app.replace_offers()] == ["uhf-a"]


def test_the_audit_says_the_stations_jobs_and_group_never_the_clients(rig):
    from sizeup.cad.api import CadApi
    from sizeup.web.server import swap_audit
    app = swapped(rig)
    gone(app, "uhf-a")
    app.sdr()
    assert swap_audit(CadApi(app.cad), "uhf-a") == {"jobs": [P25], "group": "UHF"}
    assert swap_audit(CadApi(app.cad), "nobody") == {"jobs": [], "group": ""}
    assert swap_audit(None, "uhf-a") == {"jobs": [], "group": ""}


def test_the_setting_is_off_by_default_and_audited(rig):
    plugged, logs, make = rig
    app = make()
    st = app.cad.settings
    assert st.swap_page()["auto_critical"]["value"] is False and app.cad.dongle_auto_replace is False
    st.post("dongle_swap", {"action": "set", "key": "auto_critical", "value": True, "operator": "admin@example.org"})
    assert app.cad.dongle_auto_replace is True and st.swap_page()["auto_critical"]["edited"] is True
