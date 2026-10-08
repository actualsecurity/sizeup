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
