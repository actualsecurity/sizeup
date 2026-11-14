import pytest


@pytest.fixture(autouse=True)
def _private_claims_dir(tmp_path, monkeypatch):
    """Tests never touch the real ~/.local/state/rtl-claims shared with shortfinal."""
    import sizeup.radio.claims as claims
    monkeypatch.setattr(claims, "DIR", tmp_path / "rtl-claims")
    monkeypatch.setattr(claims, "helpers", lambda: set())     # (a unit-mode test's trunk-recorder service: its own)
    monkeypatch.delenv("SIZEUP_TRUNK_RECORDER_UNIT", raising=False)


@pytest.fixture(autouse=True)
def _no_real_dongles(monkeypatch):
    """Tests never enumerate the real RTL-SDRs on this machine (librtlsdr opens each to read its serial): no librtlsdr,
    so no dongle, unless a test hands in its own fake device list."""
    import sizeup.radio.adsbd as adsbd
    import sizeup.radio.sdr as sdr
    monkeypatch.setattr(sdr, "_load", lambda rtl_fm="": None)
    monkeypatch.setattr(sdr, "_cache", (0.0, []))
    monkeypatch.setattr(adsbd, "_load", lambda: None)


@pytest.fixture(autouse=True)
def _no_real_keychain(monkeypatch):
    """Tests never read the real macOS Keychain (sizeup/cloud/stationmode.py): every lookup finds
    nothing unless a test hands in its own fake `security`."""
    import subprocess

    import sizeup.cloud.stationmode as sm
    monkeypatch.setattr(sm, "RUN", lambda args, **kw: subprocess.CompletedProcess(args, 44, "", "not found"))
    for k in ("SIZEUP_STATION_TOKEN", "SIZEUP_ACCESS_CLIENT_ID", "SIZEUP_ACCESS_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def _prealert_stays_local(monkeypatch):
    """The phone pre-alert (sizeup/prealert.py) reads ntfy: in tests only ever a local fake server,
    and only ever GET. Never ntfy.sh: its ring topic is the fire crew's real alert channel."""
    from urllib.parse import urlsplit

    import sizeup.prealert as prealert
    real = prealert.urlopen

    def local_only(req, *a, **kw):
        host = urlsplit(req.full_url if hasattr(req, "full_url") else str(req)).hostname
        assert host in ("127.0.0.1", "localhost", "::1"), f"a test tried to reach {host} (pre-alert: local fake ntfy only)"
        assert getattr(req, "get_method", lambda: "GET")() == "GET" and getattr(req, "data", None) is None
        return real(req, *a, **kw)
    monkeypatch.setattr(prealert, "urlopen", local_only)


@pytest.fixture(autouse=True)
def _roads_stay_local(monkeypatch):
    """The highway layers (sizeup/roads.py) never reach 511NY or a camera from a test: no credentials from the
    environment, none of 511NY's own addresses, and the real fetch only to this machine (tests hand in fixtures)."""
    from urllib.parse import urlsplit

    import sizeup.roads as roads
    for name in ("SIZEUP_511NY_USER", "SIZEUP_511NY_PASS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(roads, "NY511_URLS", {})        # 511NY's signs are on by default: not in tests
    monkeypatch.setattr(roads, "THRUWAY_URLS", {})      # the Thruway provider is on by default: not in tests
    monkeypatch.setattr(roads, "COUNTY_URLS", {})       # the county's too
    real = roads.urlopen

    def local_only(req, *a, **kw):
        host = urlsplit(req.full_url if hasattr(req, "full_url") else str(req)).hostname
        assert host in ("127.0.0.1", "localhost", "::1"), f"a test tried to reach {host} (roads: fixtures only)"
        return real(req, *a, **kw)
    monkeypatch.setattr(roads, "urlopen", local_only)


@pytest.fixture(autouse=True)
def _roadwork_stays_local(monkeypatch):
    """Road projects & closures (sizeup/roadprojects.py) never reach a source from a test: the default addresses off
    (the Thruway's, NYSDOT's and CTDOT's capital programs, the villages' RSS), the real fetch only to this machine."""
    from urllib.parse import urlsplit

    import sizeup.roadprojects as rw
    monkeypatch.setattr(rw, "THRUWAY_CAPITAL_URL", "")
    monkeypatch.setattr(rw, "NYSDOT_URL", "")
    monkeypatch.setattr(rw, "CTDOT_URL", "")
    monkeypatch.setattr(rw, "NOTICE_FEEDS", ())
    real = rw.urlopen

    def local_only(req, *a, **kw):
        host = urlsplit(req.full_url if hasattr(req, "full_url") else str(req)).hostname
        assert host in ("127.0.0.1", "localhost", "::1"), f"a test tried to reach {host} (roadwork: fixtures only)"
        return real(req, *a, **kw)
    monkeypatch.setattr(rw, "urlopen", local_only)


@pytest.fixture(autouse=True)
def _gis_stays_local(monkeypatch):
    """The county GIS layers (sizeup/gis.py) never reach the county from a test: the real fetch only to
    this machine (tests hand in recorded fixtures)."""
    from urllib.parse import urlsplit

    import sizeup.gis as gis
    real = gis.urlopen

    def local_only(req, *a, **kw):
        host = urlsplit(req.full_url if hasattr(req, "full_url") else str(req)).hostname
        assert host in ("127.0.0.1", "localhost", "::1"), f"a test tried to reach {host} (gis: fixtures only)"
        return real(req, *a, **kw)
    monkeypatch.setattr(gis, "urlopen", local_only)


@pytest.fixture(autouse=True)
def _roadgraph_stays_local(monkeypatch):
    """The road graph (sizeup/roadgraph.py, #360) never asks Overpass from a test: the real fetch only to this
    machine (tests build graphs from synthetic ways or tests/fixtures/osm/)."""
    from urllib.parse import urlsplit

    import sizeup.roadgraph as rg
    real = rg.urlopen

    def local_only(req, *a, **kw):
        host = urlsplit(req.full_url if hasattr(req, "full_url") else str(req)).hostname
        assert host in ("127.0.0.1", "localhost", "::1"), f"a test tried to reach {host} (roadgraph: fixtures only)"
        return real(req, *a, **kw)
    monkeypatch.setattr(rg, "urlopen", local_only)


@pytest.fixture(autouse=True)
def _trains_stay_local(monkeypatch):
    """Metro-North's trains (sizeup/trains.py) never reach the MTA from a test: its default addresses are
    off (a Trains built with none says not configured), no key from the environment, and the real fetch
    only to this machine (tests hand in recorded fixtures, tests/fixtures/mta/)."""
    from urllib.parse import urlsplit

    import sizeup.trains as trains
    monkeypatch.delenv("SIZEUP_MTA_KEY", raising=False)
    monkeypatch.setattr(trains, "RT_URL", "")
    monkeypatch.setattr(trains, "GTFS_URL", "")
    real = trains.urlopen

    def local_only(req, *a, **kw):
        host = urlsplit(req.full_url if hasattr(req, "full_url") else str(req)).hostname
        assert host in ("127.0.0.1", "localhost", "::1"), f"a test tried to reach {host} (trains: fixtures only)"
        return real(req, *a, **kw)
    monkeypatch.setattr(trains, "urlopen", local_only)
    import sizeup.traintime as traintime                 # MTA TrainTime (#362): the same, its address off
    monkeypatch.setattr(traintime, "BASE_URL", "")
    monkeypatch.setattr(traintime, "urlopen", local_only)


@pytest.fixture(autouse=True)
def _weather_stays_local(monkeypatch):
    """Weather and water (sizeup/weather.py) never reach NOAA from a test: the real fetch only to this
    machine, and an app's Weather (the real fetch) is never started; a test hands in recorded fixtures."""
    from urllib.parse import urlsplit

    import sizeup.weather as weather
    real, start = weather.urlopen, weather.Weather.start

    def local_only(req, *a, **kw):
        host = urlsplit(req.full_url if hasattr(req, "full_url") else str(req)).hostname
        assert host in ("127.0.0.1", "localhost", "::1"), f"a test tried to reach {host} (weather: fixtures only)"
        return real(req, *a, **kw)

    def fixtures_only(self):
        return self if self.fetch is weather.http_get else start(self)
    monkeypatch.setattr(weather, "urlopen", local_only)
    monkeypatch.setattr(weather.Weather, "start", fixtures_only)


@pytest.fixture(autouse=True)
def _notify_stays_local(monkeypatch):
    """Push notifications (sizeup/notify.py) only ever reach a local fake ntfy from a test: never ntfy.sh, and
    never a topic from the environment (SIZEUP_ALERT_TOPIC / SIZEUP_STATUS_TOPIC on a developer's machine)."""
    from urllib.parse import urlsplit

    import sizeup.notify as notify
    monkeypatch.delenv("SIZEUP_ALERT_TOPIC", raising=False)
    monkeypatch.delenv("SIZEUP_STATUS_TOPIC", raising=False)
    monkeypatch.delenv("SIZEUP_STATUS_TOKEN", raising=False)
    real = notify.urlopen

    def local_only(req, *a, **kw):
        host = urlsplit(req.full_url if hasattr(req, "full_url") else str(req)).hostname
        assert host in ("127.0.0.1", "localhost", "::1"), f"a test tried to reach {host} (notify: local fake ntfy only)"
        return real(req, *a, **kw)
    monkeypatch.setattr(notify, "urlopen", local_only)


@pytest.fixture(autouse=True)
def _aircraft_stays_local(monkeypatch):
    """Aircraft near HPN (sizeup/aircraft.py) never reach adsb.lol, adsb.fi or the FAA from a test: their default
    addresses are off (an AircraftLayer built with none says not configured, and fetches no registry), and the real
    fetch only to this machine (tests hand in recorded fixtures, tests/fixtures/aircraft/)."""
    from urllib.parse import urlsplit

    import sizeup.aircraft as aircraft
    monkeypatch.setattr(aircraft, "ADSBLOL_URL", "")
    monkeypatch.setattr(aircraft, "ADSBFI_URL", "")
    monkeypatch.setattr(aircraft, "FAA_URL", "")
    real = aircraft.urlopen

    def local_only(req, *a, **kw):
        host = urlsplit(req.full_url if hasattr(req, "full_url") else str(req)).hostname
        assert host in ("127.0.0.1", "localhost", "::1"), f"a test tried to reach {host} (aircraft: fixtures only)"
        return real(req, *a, **kw)
    monkeypatch.setattr(aircraft, "urlopen", local_only)


@pytest.fixture(autouse=True)
def _cache_copies_stay_local(monkeypatch):
    """The map caches' S3 copies (sizeup/cachesync.py) are off in tests (no bucket from the environment), and the
    real S3 call refuses: a test hands in its own fake S3."""
    import sizeup.cachesync as cachesync
    for k in ("LITESTREAM_BUCKET", "LITESTREAM_PATH", "LITESTREAM_REGION", "SIZEUP_CACHE_S3"):
        monkeypatch.delenv(k, raising=False)

    def refuse(method, url, *a, **kw):
        raise AssertionError(f"a test tried to reach S3 ({method} {url.split('?')[0]}): fakes only")
    monkeypatch.setattr(cachesync, "_send", refuse)


@pytest.fixture(autouse=True)
def _module_rng_per_test(request):
    """A test module's `rng` (np.random.default_rng(seed), the synthetic tones' phases and noise) starts afresh for
    every test, seeded from the module's seed and the test's own id: the same numbers whichever tests ran before it,
    serially or spread over pytest-xdist's workers (-n auto, #251)."""
    import zlib

    import numpy as np
    mod = request.module
    rng = getattr(mod, "rng", None)
    if not isinstance(rng, np.random.Generator):
        yield
        return
    seed = getattr(mod, "_RNG_SEED", None)
    if seed is None:
        seed = mod._RNG_SEED = int(rng.bit_generator.seed_seq.entropy)
    mod.rng = np.random.default_rng([seed, zlib.crc32(request.node.nodeid.encode())])
    yield
    mod.rng = rng


@pytest.fixture(autouse=True)
def _cads_shut_down(monkeypatch):
    """Every Cad a test makes (its own, an app's, a hub's and a station's) is shut down when the test ends
    (Cad.shutdown(), #497): its workers joined and its database closed. Left as they were, each kept its threads
    (the radio log's, the dispatch voice's, the address history's) and its database's descriptors for the rest of the
    run: a process that ran the whole suite ended with about 1000 threads and 2000 descriptors (#457), and another
    test's CPU and descriptor numbers were at their mercy. A Cad made by a module's fixture is made before this one
    and is not touched. The workers get a moment, not seconds: a dispatch voice still being rendered (the real `say`
    on a Mac) ends by itself, finds the Cad shut and writes nothing."""
    from sizeup.cad.service import Cad
    made, init = [], Cad.__init__

    def tracked(self, *a, **kw):
        made.append(self)
        init(self, *a, **kw)
    monkeypatch.setattr(Cad, "__init__", tracked)
    yield
    for cad in reversed(made):
        try:
            if hasattr(cad, "store"):            # (one whose __init__ raised has nothing to shut down)
                cad.shutdown(wait_s=0.05)
        except Exception:  # noqa: BLE001 - a teardown: the test's own result stands
            pass


@pytest.fixture(params=[3, 6, 12, 22, 23], ids=lambda h: f"wall-{h:02d}h")
def any_hour(request, monkeypatch):
    """The real clock moved to hh:30 America/New_York (still ticking), as if the suite ran then. #346: an airband test
    failed only after 23:00, when NY Approach's schedule is on 120.8; a test that takes this must not depend on it."""
    import time
    from datetime import datetime

    from sizeup.tz import TZ
    real = time.time
    shift = datetime(2026, 11, 7, request.param, 30, tzinfo=TZ).timestamp() - real()
    monkeypatch.setattr(time, "time", lambda: real() + shift)
    return request.param


# Private real-data replay (#283, tests/replay.py): the "replay" marker and the replay_sets fixture; a replay test is
# skipped, with the reason, when the local cache (~/.cache/sizeup-replay or $SIZEUP_REPLAY_DIR) has no data of its kind.
from tests.replay import pytest_collection_modifyitems, pytest_configure, replay_sets  # noqa: E402,F401
