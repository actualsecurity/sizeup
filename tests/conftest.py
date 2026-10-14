import pytest


@pytest.fixture(autouse=True)
def _private_claims_dir(tmp_path, monkeypatch):
    """Tests never touch the real ~/.local/state/rtl-claims shared with shortfinal."""
    import sizeup.radio.claims as claims
    monkeypatch.setattr(claims, "DIR", tmp_path / "rtl-claims")


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
    """The highway layers (sizeup/roads.py) never reach 511NY or a camera from a test: no key from the
    environment, and the real fetch only to this machine (tests hand in recorded fixtures)."""
    from urllib.parse import urlsplit

    import sizeup.roads as roads
    monkeypatch.delenv("SIZEUP_511NY_KEY", raising=False)
    monkeypatch.setattr(roads, "THRUWAY_URLS", {})      # the Thruway provider is on by default: not in tests
    monkeypatch.setattr(roads, "COUNTY_URLS", {})       # the county's too
    real = roads.urlopen

    def local_only(req, *a, **kw):
        host = urlsplit(req.full_url if hasattr(req, "full_url") else str(req)).hostname
        assert host in ("127.0.0.1", "localhost", "::1"), f"a test tried to reach {host} (roads: fixtures only)"
        return real(req, *a, **kw)
    monkeypatch.setattr(roads, "urlopen", local_only)


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
