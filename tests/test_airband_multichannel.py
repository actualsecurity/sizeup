"""Several airband channels per dongle, clearer names, HPN's tower hours (GitHub #275). Synthetic IQ (AM carriers at the
real offsets, with noise, a DC spike and an IQ-imbalance image), a fake rtl_sdr and fake dongles only: no radio, no
network."""

from __future__ import annotations

import json
import stat
import sys
import threading
import time
import tomllib
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from sizeup import app as appmod
from sizeup import atc_hours
from sizeup.app import App, plan_text
from sizeup.cloud import configupdate
from sizeup.config import ChannelCfg, ConfigError, airband_owner, load
from sizeup.radio import am, channelize
from sizeup.radio.am import AmReceiver, CarrierSquelch, Extra
from sizeup.radio.channelize import Channelizer, iq_u8, plan
from sizeup.radio.fm import FRAME, RATE, Channel
from sizeup.tz import TZ
from tests.test_atc import FIVE, envelope
from tests.test_sdr_auto_assign import DONGLES, rig  # noqa: F401 (rig: the fixture)

ROOT = Path(__file__).resolve().parent.parent
STATION = ROOT / "deploy" / "station"
DAYNIGHT = ((7 * 60, 126.4), (23 * 60, 120.8))
APP = "NY App 126.4/120.8"
APP3 = "NY App 124.65"
TWR = ChannelCfg("HPN Tower", 118.575, "airband", "hpn-twr", 36.4, 3.5, also=((120.8, 23 * 60, 7 * 60, APP),))
NYAPP = ChannelCfg(APP, 126.4, "airband", "nyapp", 36.4, 3.5, schedule=DAYNIGHT)
NYAPP3 = ChannelCfg(APP3, 124.65, "airband", "nyapp2", 36.4, 3.5)
GND = ChannelCfg("HPN Ground", 121.825, "airband", "hpn-gnd", 36.4, 3.5, schedule=((360, 121.825), (1380, 124.65)))


def local(y, mo, d, h, mi=0, s=0) -> float:
    return datetime(y, mo, d, h, mi, s, tzinfo=TZ).timestamp()


# -- the capture: rate and centre ----------------------------------------------------------------------------------------


def test_each_pair_gets_a_rate_and_a_centre_clear_of_the_dc_spike_and_each_others_image():
    got = {pair: plan(pair) for pair in ((118.575, 120.8), (126.4, 124.65), (120.8, 121.825))}
    assert got[(118.575, 120.8)] == (119.7025, 2_560_000)          # 2.225 MHz apart: the widest, at the edge
    assert got[(126.4, 124.65)][1] == 2_048_000 and got[(120.8, 121.825)][1] == 1_600_000
    for pair, (c, rate) in got.items():
        off = [m * 1e6 - c * 1e6 for m in pair]
        assert all(abs(o) <= channelize.EDGE_FRAC * rate for o in off)               # inside the usable band
        assert all(abs(o) >= channelize.DC_GUARD for o in off)                       # off the DC spike
        assert abs(off[0] + off[1]) >= channelize.IMAGE_GUARD                        # not on the other's mirror image
        assert rate % channelize.OUT_RATE == 0
    with pytest.raises(ValueError, match="don't fit one dongle"):
        plan((118.575, 121.825))                                                       # 3.25 MHz: two dongles
    assert plan((118.575,))[0] != 118.575                                              # one alone: still off DC


def synth(rate, offsets, secs=2.0, cnr_db=20.0, seed=1, dc=0.02, image_db=-30.0, keyed=None):
    """8-bit IQ as rtl_sdr gives it: an AM carrier (a tone, 1000 Hz + 150 Hz per channel) at each offset, noise at cnr_db
    in a 10 kHz channel, a DC offset and the tuner's IQ-imbalance image. keyed: [(channel, start s, end s)] or all on."""
    rng = np.random.default_rng(seed)
    n = int(rate * secs)
    t = np.arange(n) / rate
    amp = 0.15
    x = np.zeros(n, np.complex128)
    for k, o in enumerate(offsets):
        gate = np.ones(n) if keyed is None else np.zeros(n)
        for ch, a, b in keyed or ():
            if ch == k:
                gate[int(a * rate):int(b * rate)] = 1
        x += gate * amp * (1 + 0.8 * np.sin(2 * np.pi * (1000 + 150 * k) * t)) * np.exp(2j * np.pi * o * t + 1j * k)
    p = amp ** 2 / 10 ** (cnr_db / 10) * rate / 10_000
    x += np.sqrt(p / 2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    x += 10 ** (image_db / 20) * np.conj(x) + dc * (1 + 1j)
    u = np.empty(2 * n, np.uint8)
    u[0::2] = np.clip(np.round(x.real * 128 + 127.4), 0, 255)
    u[1::2] = np.clip(np.round(x.imag * 128 + 127.4), 0, 255)
    return u


def tone_snr(env, tone) -> float:
    """The tone over the noise in the voice band (dB, the same width), from the 16 kHz envelope."""
    e = env[4000:] - env[4000:].mean()
    f = np.fft.rfftfreq(len(e), 1 / RATE)
    S = np.abs(np.fft.rfft(e * np.hanning(len(e)))) ** 2
    on = np.abs(f - tone) < 20
    band = (f > 300) & (f < 3000) & (np.abs(f - tone) >= 40)
    return float(10 * np.log10(S[on].sum() / (S[band].mean() * on.sum())))


@pytest.mark.parametrize("pair", [(118.575, 120.8), (124.65, 126.4), (120.8, 121.825)])
def test_each_channel_comes_out_as_clean_as_it_would_alone(pair):
    c, rate = plan(pair)
    off = [m * 1e6 - c * 1e6 for m in pair]
    both = Channelizer(rate, off).feed(iq_u8(synth(rate, off).tobytes()))
    for k in range(2):
        alone = Channelizer(rate, [off[k]]).feed(iq_u8(synth(rate, [off[k]], dc=0, image_db=-200).tobytes()))[0]
        if k:                                       # (the tone of channel 1 is 1150 Hz: its own synthetic)
            alone = Channelizer(rate, off).feed(iq_u8(synth(rate, off, dc=0, image_db=-200).tobytes()))[1]
        assert len(both[k]) >= 1.9 * RATE
        assert tone_snr(both[k], 1000 + 150 * k) >= tone_snr(alone, 1000 + 150 * k) - 1.0   # the other carrier, DC, image
        assert tone_snr(both[k], 1000 + 150 * k) > 30                                         # 20 dB CNR, 0.8 depth
        assert abs(float(np.mean(both[k])) - 0.15) < 0.01                                    # the carrier's level: its DC


def test_the_chain_is_flat_to_the_edge_and_a_planned_centre_keeps_out_the_image():
    rate = 2_560_000
    t = np.arange(rate // 2) / rate
    for o in (30e3, 600e3, 1.0e6, 1.1275e6):                       # the channelizer itself: flat out to the widest pair's
        y = Channelizer(rate, [o]).feed((0.2 * np.exp(2j * np.pi * o * t)).astype(np.complex64))[0]
        assert abs(20 * np.log10(y[2000:].mean() / 0.2)) < 0.05
    c, _ = plan((118.575, 120.8))
    planned = [118.575e6 - c * 1e6, 120.8e6 - c * 1e6]
    for offs, worst in (([-1112500.0, 1112500.0], -35.0), (planned, -70.0)):
        x = 0.5 * np.exp(2j * np.pi * offs[0] * t)                 # a strong tower carrier, nothing on 120.8 ...
        x = x + 10 ** (-30 / 20) * np.conj(x) + 0.0005 * (np.random.default_rng(2).standard_normal(len(t)) + 0j)
        y = Channelizer(rate, offs).feed(x.astype(np.complex64))   # ... but the tuner's image of it at minus its offset
        leak = 20 * np.log10(y[1][2000:].mean() / y[0][2000:].mean())
        assert (leak > worst) if worst > -50 else (leak < worst)   # centred between them: the image lands on 120.8


def test_a_dongle_with_two_channels_costs_little_more_than_one():
    rate = 2_560_000
    iq = iq_u8(synth(rate, [-1127500, 1097500], secs=2.0).tobytes())
    took = {}
    for n in (1, 2, 4):
        ch = Channelizer(rate, list(np.linspace(-1.1e6, 1.1e6, n) + 12_345))
        t0 = time.process_time()
        ch.feed(iq)
        took[n] = (time.process_time() - t0) / 2.0                 # CPU seconds per second of capture
    assert took[2] < 0.25 and took[4] < took[1] * 1.6 + 0.01       # the FFT is shared: each channel adds a little


# -- the receiver: rtl_fm for one, the channelizer for several ----------------------------------------------------------


def tower(clock, covered=None, **kw):
    events = []
    r = AmReceiver("/opt/sizeup/rtl-sdr/bin/rtl_fm", "", 36.4, Channel(118.575, "HPN Tower"), lambda c, s: None,
                   events.append, open_db=3.5, also=[Extra(120.8, APP, 23 * 60, 7 * 60)], covered=covered, **kw)
    r.clock = lambda: clock[0]
    r._tune()
    return r, events


def test_the_tower_dongle_takes_approach_overnight_and_keeps_118575_all_day():
    day = [local(2026, 7, 1, 12)]
    r, _ = tower(day)
    assert r.active == (118.575,) and r.capture is None
    assert r.argv("3")[:5] == ["/opt/sizeup/rtl-sdr/bin/rtl_fm", "-M", "am", "-f", "118.5750M"]   # one: rtl_fm as before
    night = [local(2026, 7, 1, 23, 30)]
    r, events = tower(night)
    assert r.active == (118.575, 120.8) and r.capture == (119.7025, 2_560_000)
    cmd = r.argv("3")
    assert cmd[:3] == [sys.executable, "-m", "sizeup.radio.channelize"]
    assert cmd[cmd.index("--rtl-sdr") + 1] == "/opt/sizeup/rtl-sdr/bin/rtl_sdr"                  # the same rtl-sdr build
    assert [float(cmd[i + 1]) for i, x in enumerate(cmd) if x == "--offset"] == [-1127500.0, 1097500.0]
    assert cmd[cmd.index("-d") + 1] == "3" and cmd[cmd.index("-g") + 1] == "36.4"
    assert r.plan_at(local(2026, 7, 2, 6, 59)) == (118.575, 120.8)       # through the night ...
    assert r.plan_at(local(2026, 7, 2, 7, 0)) == (118.575,)              # ... until Approach's 07:00
    assert r.plan_at(local(2026, 1, 2, 23, 0)) == (118.575, 120.8)       # local time, winter too
    assert r.label_for(120.8) == APP and r.label_for(118.575) == "HPN Tower"
    assert r.describe_plan() == "118.575 + 120.8 23:00-07:00"


def test_each_frequency_has_its_own_squelch_and_goes_out_as_its_channel():
    rng = np.random.default_rng(3)
    clock = [local(2026, 7, 1, 23, 30)]
    got = []
    r, _ = tower(clock)
    r.on_segment = lambda ch, seg: got.append((ch.label, ch.mhz, round(seg.end - seg.start, 1)))
    r.squelch = CarrierSquelch(3.5, clock=lambda: clock[0])
    r.subs = r._make_subs(r.squelch)
    for s in r.subs[1:]:
        s.squelch.clock = lambda: clock[0]
    quiet, talk = envelope([(3.0, 0.01, False)], rng), envelope([(1.5, 0.05, True), (1.0, 0.01, False)], rng)
    idle = envelope([(2.5, 0.01, False)], rng)
    for a, b in zip(quiet + talk + idle, quiet + idle + talk):     # 118.575 keys up, then 120.8
        clock[0] += FRAME / RATE
        r._deliver(r.squelch.feed(a), r.subs[0].channel)
        r._deliver(r.subs[1].squelch.feed(b), r.subs[1].channel)
    assert [(lab, mhz) for lab, mhz, _d in got] == [("HPN Tower", 118.575), (APP, 120.8)]
    st = r.status()
    assert [f["label"] for f in st["freqs"]] == ["HPN Tower", APP] and st["freqs"][0]["name"] == "CTAF"
    assert st["now"] == "118.575 CTAF + 120.8" and st["tower"] == {"open": False, "hours": "06:00-23:00", "next": "06:00"}


def test_the_set_changes_only_between_transmissions_on_all_of_its_frequencies():
    rng = np.random.default_rng(5)
    clock = [local(2026, 7, 2, 6, 59, 56)]
    r, _ = tower(clock)
    r.squelch = CarrierSquelch(3.5, clock=lambda: clock[0])
    r.subs = r._make_subs(r.squelch)
    for s in r.subs[1:]:
        s.squelch.clock = lambda: clock[0]
    frames = envelope([(3.0, 0.01, False)], rng)
    for f in frames:
        clock[0] += FRAME / RATE
        r.squelch.feed(f)
        r.subs[1].squelch.feed(f)
    busy = envelope([(2.0, 0.05, True)], rng)                      # 120.8 is talking at 07:00
    for f in busy:
        clock[0] += FRAME / RATE
        r.squelch.feed(frames[0])
        r.subs[1].squelch.feed(f)
    assert clock[0] > local(2026, 7, 2, 7) and r.subs[1].squelch.busy_since is not None
    assert r.retune_due() is None                                   # never mid-clip on the other frequency either
    for f in envelope([(3.0, 0.01, False)], rng):
        clock[0] += FRAME / RATE
        r.squelch.feed(f)
        r._deliver(r.subs[1].squelch.feed(f), r.subs[1].channel)
    assert r.retune_due() == 118.575                                # quiet: back to the tower alone (rtl_fm)


def test_a_frequency_another_dongle_keeps_is_not_delivered_twice():
    clock = [local(2026, 7, 1, 23, 30)]
    got, asked = [], []
    r, events = tower(clock, covered=lambda mhz, me: asked.append(mhz) or True)
    r.on_segment = lambda ch, seg: got.append(ch.label)
    seg = SimpleNamespace(start=clock[0], end=clock[0] + 2)
    r._deliver(seg, Channel(120.8, APP))                            # Approach's dongle has it: kept there
    r._deliver(seg, Channel(118.575, "HPN Tower"))                  # its own: never asked
    assert got == ["HPN Tower"] and asked == [120.8]
    assert events[-1] == "HPN Tower: 120.8 is heard on another dongle: kept there, not here"
    assert sum("another dongle" in e for e in events) == 1


def heard_elsewhere(channels, running):
    """App._heard_elsewhere on a fake app: these receivers, the ones in `running` up."""
    rx = [SimpleNamespace(channel=Channel(c.mhz, c.label), active=act) for c, act in channels]
    fake = SimpleNamespace(cfg=SimpleNamespace(channels=tuple(c for c, _a in channels)), receivers=rx,
                           _running=lambda r: r.channel.label in running)
    return lambda mhz, label: App._heard_elsewhere(fake, mhz, next(r for r in rx if r.channel.label == label))


def test_the_channels_own_dongle_keeps_a_shared_frequency_else_the_first_one():
    night = [(TWR, (118.575, 120.8)), (NYAPP, (120.8,)), (NYAPP3, (124.65,)), (GND, (124.65,))]
    f = heard_elsewhere(night, {"HPN Tower", APP, APP3, "HPN Ground"})
    assert f(120.8, "HPN Tower") and not f(120.8, APP)              # Approach's own dongle keeps 120.8
    assert f(124.65, "HPN Ground") and not f(124.65, APP3)          # ... and 124.65
    f = heard_elsewhere(night, {"HPN Tower", "HPN Ground"})         # Approach's dongles Off (or another app's)
    assert not f(120.8, "HPN Tower") and not f(124.65, "HPN Ground")   # nothing lost
    assert airband_owner((TWR, NYAPP, NYAPP3, GND), 120.8) == APP and airband_owner((GND,), 124.65) == "HPN Ground"


class FakeRtlSdr:
    """An "rtl_sdr" that writes its arguments to a file, then synthetic IQ: 118.575 keyed 3-5 s, 120.8 keyed 6-8 s."""

    def __init__(self, tmp_path):
        self.log = tmp_path / "rtl_sdr.txt"
        iq = tmp_path / "iq.u8"
        c, rate = plan((118.575, 120.8))
        synth(rate, [118.575e6 - c * 1e6, 120.8e6 - c * 1e6], secs=10.0, cnr_db=25,
              keyed=[(0, 3.0, 5.0), (1, 6.0, 8.0)]).tofile(iq)
        self.exe = tmp_path / "rtl_sdr"
        self.exe.write_text(f'#!/bin/sh\necho "$@" >> "{self.log}"\ncat "{iq}"\nexec sleep 30\n')
        self.exe.chmod(self.exe.stat().st_mode | stat.S_IEXEC)


def test_a_fake_rtl_sdr_through_the_channelizer_process_gives_each_channel_its_transmission(tmp_path, monkeypatch):
    fake = FakeRtlSdr(tmp_path)
    clock = [local(2026, 7, 1, 23, 30)]
    got: list[tuple[str, float, bool]] = []
    done = threading.Event()

    def seg(ch, s):
        got.append((ch.label, ch.mhz, bool(getattr(s, "weak", False))))
        if len(got) >= 2:
            done.set()
    monkeypatch.chdir(ROOT)                                          # python -m sizeup.radio.channelize finds this tree
    r = AmReceiver(str(tmp_path / "rtl_fm"), "", 36.4, Channel(118.575, "HPN Tower"), seg, lambda m: None, open_db=3.5,
                   also=[Extra(120.8, APP, 23 * 60, 7 * 60)], rtl_sdr=str(fake.exe))
    r.clock = lambda: clock[0]
    r.start()
    try:
        assert done.wait(60), f"{got} {r.status()}"
        st = r.status()
    finally:
        r.stop()
    assert sorted(got) == [("HPN Tower", 118.575, False), (APP, 120.8, False)]
    assert st["state"] == "ok" and st["capture"] == {"centre_mhz": 119.7025, "rate": 2560000}
    assert st["detail"] == "118.575 + 120.8 · 2.56 MS/s @ 119.7025"
    assert fake.log.read_text().split()[:4] == ["-f", "119702500", "-s", "2560000"]


# -- config: "also", and what fits one dongle --------------------------------------------------------------------------


def write(tmp_path, body: str) -> Path:
    p = tmp_path / "c.toml"
    p.write_text('[radio]\nsdrs = "auto"\n' + body)
    return p


CHANS = ('[[radio.channels]]\nlabel = "NY App 126.4/120.8"\nmhz = 126.4\nkind = "airband"\n'
         'schedule = [{ at = "07:00", mhz = 126.4 }, { at = "23:00", mhz = 120.8 }]\n'
         '[[radio.channels]]\nlabel = "NY App 124.65"\nmhz = 124.65\nkind = "airband"\n')


def test_also_is_read_and_each_frequency_is_its_channels(tmp_path):
    c = load(write(tmp_path, CHANS + '[[radio.channels]]\nlabel = "HPN Tower"\nmhz = 118.575\nkind = "airband"\n'
                                    'also = [{ mhz = 120.8, from = "23:00", until = "07:00" }]\n'))
    t = next(x for x in c.channels if x.label == "HPN Tower")
    assert t.also == ((120.8, 1380, 420, APP),) and plan_text(t) == "118.575 + 120.8 23:00-07:00"
    for bad, why in (('also = [{ mhz = 119.1 }]', "isn't on any airband channel"),          # nobody's frequency
                     ('also = [{ mhz = 124.65 }]', "don't fit one dongle"),                  # 6 MHz from 118.575
                     ('also = [{ mhz = 120.8, from = "23:00" }]', "both or neither"),
                     ('also = [{ mhz = 120.8, from = "25:00", until = "07:00" }]', "a time of day"),
                     ('also = [{ mhz = 150.0 }]', "airband voice")):
        with pytest.raises(ConfigError, match=why):
            load(write(tmp_path, CHANS + f'[[radio.channels]]\nlabel = "HPN Tower"\nmhz = 118.575\nkind = "airband"\n{bad}\n'))
    with pytest.raises(ConfigError, match="airband channels only"):
        load(write(tmp_path, '[[radio.channels]]\nlabel = "FG1"\nmhz = 453.0375\nalso = [{ mhz = 453.1 }]\n'))


def test_the_templates_have_the_names_and_the_sets(tmp_path, monkeypatch):
    from tests.test_station_install import station_config
    for c in (station_config(tmp_path, monkeypatch), load(ROOT / "deploy" / "hub" / "config.hub.toml"),
              load(ROOT / "config.example.toml")):
        ch = {x.label: x for x in c.channels}
        assert ch["HPN Tower"].also == ((120.8, 1380, 420, APP),)
        assert ch[APP].schedule == DAYNIGHT and ch[APP].also == ((124.65, 420, 1380, APP3),)
        assert ch["HPN Ground"].schedule == ((360, 121.825), (1380, 124.65)) and APP3 in ch
        assert not {"NY Approach", "NY Approach 124.65"} & set(ch)
    hub = load(ROOT / "deploy" / "hub" / "config.hub.toml")
    assert {APP, APP3, "HPN Ground", "HPN Tower"} <= set(hub.stt_aws_channels)
    assert appmod.RENAMED == configupdate.RENAMES                    # a saved job follows the update's rename


# -- the live station's config: renamed and given its sets once (configupdate "airband-sets") ----------------------------


def as_260() -> str:
    from tests.test_fg2 import before_275
    return before_275((STATION / "config.station.toml").read_text())


def new_release(tmp_path) -> Path:
    rel = tmp_path / "rel-275"
    (rel / "sizeup").mkdir(parents=True)
    (rel / "sizeup" / "config.py").write_text((ROOT / "sizeup" / "config.py").read_text())
    return rel


def test_an_installed_station_is_renamed_and_given_its_sets_once(tmp_path, monkeypatch):
    from tests.test_fg2 import installed
    conf = installed(tmp_path, monkeypatch, as_260())
    marks = tmp_path / "etc" / "config-updates"
    marks.write_text("fg2\ntones-47\nhpn-atc\nadsb-receiver\napproach-ground\nsouth-end\n")
    before = load(conf)
    said = configupdate.run(conf, marks, leaving=new_release(tmp_path))
    assert len(said) == 1 and said[0].startswith(
        'airband-sets: HPN Tower: also 120.8 23:00-07:00; "NY Approach" is now "NY App 126.4/120.8"; NY App 126.4/120.8: '
        'also 124.65 07:00-23:00; "NY Approach 124.65" is now "NY App 124.65"; HPN Ground: schedule 121.825 06:00-23:00, '
        "124.65 23:00-06:00 (the old file: ")
    kept = conf.with_name("config.toml.before-airband-sets").read_text()
    text = conf.read_text()
    assert len(text.splitlines()) == len(kept.splitlines()) + 3       # 2 labels changed, 3 lines added
    after = load(conf)
    (tmp_path / "t").mkdir()
    tpl = {x.label: x for x in load(installed(tmp_path / "t", monkeypatch, (STATION / "config.station.toml").read_text())).channels}
    for c in after.channels:                                          # what a new install has
        assert (c.label, c.mhz, c.schedule, c.also, c.device) == \
            (tpl[c.label].label, tpl[c.label].mhz, tpl[c.label].schedule, tpl[c.label].also, tpl[c.label].device)
    assert replace(after, channels=before.channels, stt_aws_channels=before.stt_aws_channels) == before   # nothing else
    assert marks.read_text().endswith("south-end\nairband-sets\n")
    assert configupdate.run(conf, marks) == []                         # once
    conf.write_text(kept)
    assert configupdate.run(conf, marks) == []                         # put back by hand: stays so


def test_it_waits_while_the_release_to_go_back_to_cant_read_also(tmp_path, monkeypatch):
    from tests.test_fg2 import installed
    conf = installed(tmp_path, monkeypatch, as_260())
    marks = tmp_path / "etc" / "config-updates"
    marks.write_text("fg2\ntones-47\nhpn-atc\nadsb-receiver\napproach-ground\nsouth-end\n")
    same = conf.read_text()
    old = tmp_path / "rel-260"
    (old / "sizeup").mkdir(parents=True)
    (old / "sizeup" / "config.py").write_text('_take(c, "x", {"label", "schedule"})\n')   # reads a schedule, not "also"
    assert configupdate.reads_schedule(old) and not configupdate.reads_also(old) and configupdate.reads_also(None)
    said = configupdate.run(conf, marks, leaving=old)
    assert said == ["airband-sets: deferred: the release this switch would go back to (rel-260) can't read an airband "
                    'channel\'s "also": the #275 names and frequencies are changed at the next update']
    assert conf.read_text() == same and "airband-sets" not in marks.read_text()
    said = configupdate.run(conf, marks, leaving=new_release(tmp_path))  # the next update
    assert said[0].startswith("airband-sets: HPN Tower") and load(conf).channels[4].label == APP


def test_channels_set_by_hand_are_left_alone():
    hand = ('[radio]\nsdrs = "auto"\n'
            '[[radio.channels]]\nlabel = "HPN Tower"\nmhz = 118.575\nkind = "airband"\n'      # no 120.8 channel here
            '[[radio.channels]]\nlabel = "NY Approach"\nmhz = 126.4\nkind = "airband"\n'      # no schedule: not #260's
            '[[radio.channels]]\nlabel = "HPN Ground"\nmhz = 121.825\nkind = "airband"\n'     # no 124.65 channel here
            '[[radio.channels]]\nlabel = "NY App 124.65"\nmhz = 124.65\nkind = "airband"\nalso = []\n')
    text, said = configupdate.airband_sets(hand.replace("also = []\n", ""))
    d = tomllib.loads(text)["radio"]["channels"]
    assert [c["label"] for c in d] == ["HPN Tower", "NY Approach", "HPN Ground", "NY App 124.65"]
    assert "also" not in d[0] and d[2]["schedule"] == [{"at": "06:00", "mhz": 121.825}, {"at": "23:00", "mhz": 124.65}]
    assert said == "airband-sets: HPN Ground: schedule 121.825 06:00-23:00, 124.65 23:00-06:00"
    assert configupdate.airband_sets("[web]\nport = 1\n")[1].startswith("airband-sets: no [[radio.channels]]")
    with pytest.raises(ValueError):                                    # a label it can't edit as one plain line
        configupdate.airband_sets(as_260().replace('label = "NY Approach 124.65"', "label = 'NY Approach 124.65'"))


def test_a_dongles_saved_job_follows_the_rename(rig, tmp_path):  # noqa: F811
    plugged, _logs, make = rig
    plugged[:] = DONGLES + [(4, "RTL2838UHIDIR", "nyapp")]
    (tmp_path / "sdr_modes.json").write_text(json.dumps({"nyapp": "NY Approach", "_set_by": {"nyapp": "person"}}))
    app = make(channels=FIVE + (NYAPP, NYAPP3, GND))
    assert app.assign["nyapp"] == APP and app.set_by["nyapp"] == "person"
    sdr = app.state()["sdr"]
    assert APP in sdr["modes"] and sdr["now"].get(APP) in ("126.4", "120.8")   # the button's "· 126.4"


# -- the tower's hours ----------------------------------------------------------------------------------------------------


def test_the_tower_hours_are_the_chart_supplements_in_local_time():
    for t, is_open in ((local(2026, 7, 1, 5, 59), False), (local(2026, 7, 1, 6, 0), True), (local(2026, 7, 1, 22, 59), True),
                       (local(2026, 7, 1, 23, 0), False), (local(2026, 1, 15, 6, 30), True), (local(2026, 3, 8, 5, 30), False)):
        assert atc_hours.tower_open(t) is is_open
    night, day = local(2026, 11, 7, 2), local(2026, 11, 7, 14)
    assert atc_hours.freq_name(118.575, night) == "CTAF" and atc_hours.freq_name(118.575, day) == ""
    assert atc_hours.freq_name(121.825, night) == "closed" and atc_hours.freq_name(120.8, night) == ""
    assert atc_hours.tower(day) == {"open": True, "hours": "06:00-23:00", "next": "23:00"}


def test_the_pages_show_the_frequency_ctaf_and_the_tower_state():
    static = ROOT / "sizeup" / "web" / "static"
    rc = (static / "radio-controls.js").read_text()
    assert "sdr.now && sdr.now[m]" in rc and '"Tower open" : "Tower closed"' in rc   # the button and the row (#278: short)
    cad = (static / "cad.js").read_text()
    assert "HPN_TOWER_OPEN = [6 * 60, 23 * 60]" in cad and "`${+t.mhz} CTAF`" in cad
    assert atc_hours.TOWER_OPEN == (6 * 60, 23 * 60)                  # the same hours as the Python side
    js = (static / "aircraft.js").read_text()
    assert 'tw.open === false ? "CTAF" : "TWR"' in js


def test_the_hub_takes_a_not_yet_renamed_stations_transmissions_as_the_renamed_channels():
    from sizeup.cloud.hub import HubApp
    got = []
    hub = SimpleNamespace(_kind={APP: "airband", APP3: "airband", "HPN Tower": "airband"},
                          _apply_traffic=lambda d, audio: got.append((d["id"], d["source"])))
    for src in ("NY Approach", "NY Approach 124.65", "HPN Tower"):
        HubApp.apply(hub, "traffic", {"id": f"{src.replace(' ', '')}-1", "source": src}, None)
    assert got == [("NYApproach-1", APP), ("NYApproach124.65-1", APP3), ("HPNTower-1", "HPN Tower")]   # ids kept
    hub._kind["NY Approach"] = "airband"                                # a hub that still has the old one: as sent
    HubApp.apply(hub, "traffic", {"id": "NYApproach-2", "source": "NY Approach"}, None)
    assert got[-1] == ("NYApproach-2", "NY Approach")
