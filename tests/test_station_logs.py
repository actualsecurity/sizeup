"""Station logs to CloudWatch: the scrub, the station's shipper (journal -> spool -> hub), the hub's sink and the
admins' Logs view (sizeup/logscrub.py, sizeup/cloud/logship.py, sizeup/cloud/cwlogs.py). Synthetic data only: the
journal fixture has journalctl -o json's real shape with invented lines."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sizeup import aws, logscrub
from sizeup.cloud import cwlogs, logship
from sizeup.cloud.client import HubError
from sizeup.cloud.cwlogs import LogsError, StationLogs
from sizeup.cloud.logship import LogShipper

FIXTURE = Path(__file__).parent / "fixtures" / "journal-sizeup.jsonl"
JOURNAL = [json.loads(x) for x in FIXTURE.read_text().splitlines()]
T0 = 1762950000.0                   # the fixture's first entry
TOKEN = "st-Tok3n-0123456789abcdefXYZ"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    logscrub.forget()
    monkeypatch.delenv("SIZEUP_LOG_SHIP", raising=False)
    yield
    logscrub.forget()


# -- the scrub ---------------------------------------------------------------------------------------------------

def test_scrub_drops_a_registered_secret_and_redacts_credential_shapes():
    logscrub.secret(TOKEN, "Corr3ct-Horse-9")
    logscrub.secret_name("FEEDX_USER", "FEEDX_PASS")
    s = logscrub.scrub
    assert s(f"hub link: bearer {TOKEN} refused") is None                  # a known value: the whole line dropped
    assert s("feed login as Corr3ct-Horse-9") is None
    assert s("FEEDX_USER read from the credstore") == "[credential] [redacted] from the credstore"   # (a name + a space: its value)
    assert s("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345") == "Authorization: [redacted]"
    assert s("GET https://u:pw1@example.org/a?token=abc&page=2") == "GET https://[redacted]@example.org/a?token=[redacted]&page=2"
    assert s("aws AKIAABCDEFGHIJKLMNOP wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY") == "aws [aws-key] [redacted]"
    assert s('{"password": "x1", "user": "u"}') == '{"password": [redacted], "user": "u"}'
    assert s("cookie=Zq81LmXpTRe4vB2nW9yKs0Ha; path=/") == "cookie=[redacted] path=/"
    assert s("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.c2lnbmF0dXJlLXZhbHVl") == "jwt [redacted]"
    assert s("-----BEGIN OPENSSH PRIVATE KEY-----") is None
    assert s("the password is hunter7") is None and s("client secret abc123") is None
    sha = "3f2a9c1d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c"                       # kept: a sha, numbers, times
    assert s(f"auto-update: on {sha}") == f"auto-update: on {sha}"
    assert s("FG1: carrier 4.2 s, quieting 18 dB at 12:30:01") == "FG1: carrier 4.2 s, quieting 18 dB at 12:30:01"
    assert s("hub link: token missing") == "hub link: token missing"


# The review's repros (#494): each one line.
def test_repro_a_name_redaction_no_longer_hides_the_value():
    logscrub.secret_name("FEEDX_PASS")
    assert logscrub.scrub("FEEDX_PASS=Xq7v") == "[credential]=[redacted]"


def test_repro_registered_values_in_other_forms():
    logscrub.secret("p@ss w;rd&\"1'")
    v = "p@ss w;rd&\"1'"
    import base64
    import html
    import urllib.parse
    for form in (urllib.parse.quote(v, safe=""), urllib.parse.quote_plus(v), json.dumps(v)[1:-1], html.escape(v),
                 repr(v)[1:-1], base64.b64encode(v.encode()).decode()):
        assert logscrub.scrub(f"sent {form} ok") is None, form


def test_repro_basic_auth_of_a_registered_login():
    import base64
    logscrub.secret_pair("bob", "Hunt3r-2x")
    b = base64.b64encode(b"bob:Hunt3r-2x").decode()
    assert logscrub.scrub(f"Authorization: Basic {b}") is None and logscrub.scrub(f"hdr {b}") is None


@pytest.mark.parametrize("line,want", [
    ("pw=hunter2", "pw=[redacted]"), ("p: hunter2", "p: [redacted]"), ("pin=1234", "pin=[redacted]"),
    ("pass hunter2", None), ("mysql -p hunter2 db", "mysql -p [redacted] db"), ("mysql -phunter2", "mysql -p [redacted]"),
    ("curl -u bob:hunter2 https://x.example", "curl -u [redacted] https://x.example"),
    ("--user bob:hunter2", "--user [redacted]"), ("connect bob:hunter2@host.example", "connect [redacted]@host.example"),
    ("credentials for bob:hunter2", "credentials for bob:[redacted]"),
    ("login with ('bob', 'hunter2')", "login with ([redacted])"),
    ("{'Name': '/x', 'Value': 'hunter2', 'Type': 'SecureString'}", "{'Name': '/x', 'Value': [redacted], 'Type': 'SecureString'}"),
    ("Value=hunter2 Type=SecureString", "Value=[redacted] Type=SecureString"),
    ('{"Value": "hunter2"}', '{"Value": [redacted]}'),
    ("ｐａｓｓｗｏｒｄ=ｈｕｎｔｅｒ２", "password=[redacted]"), ("pass​word=hunter2", "password=[redacted]"),
    ("password= a;b,c&d'e", "password= [redacted]"), ('{"password": "a\\"b;c", "u": 1}', '{"password": [redacted], "u": 1}'),
    ("Authorization: Bearer abc", "Authorization: [redacted]"), ("sent Bearer abc", "sent Bearer [redacted]"),
])
def test_repro_unregistered_formats(line, want):
    assert logscrub.scrub(line) == want


def test_repro_no_benign_phrase_bypass():
    assert logscrub.scrub("adapter: password version 3 accepted") is None
    assert logscrub.scrub("password is set hunter2") is None
    assert logscrub.scrub("password: [redacted]") == "password: [redacted]" and logscrub.scrub("no password.") == "no password."


def test_repro_a_value_past_the_cut_still_drops_the_line():
    logscrub.secret(TOKEN)
    assert logscrub.scrub("x" * (logscrub.MAX_LINE - 5) + TOKEN) is None


def test_repro_short_credstore_values_match_as_words(tmp_path, monkeypatch):
    from sizeup import procenv
    (tmp_path / "sizeup-adapter.FEEDX_PIN").write_text("pc7\n")
    monkeypatch.setattr(procenv, "_dir", tmp_path)
    procenv.register_secrets()
    assert logscrub.scrub("feed: entered pc7 ok") is None and logscrub.scrub("FEEDX_PIN is missing") == "[credential] [redacted] missing"
    assert logscrub.scrub("rpc7x ok") == "rpc7x ok"


# Re-review repros (#494)
def test_repro2_base64_of_user_password_at_any_alignment(tmp_path, monkeypatch):
    import base64
    from sizeup import procenv
    (tmp_path / "sizeup-adapter.FEEDX_USER").write_text("bob\n")
    (tmp_path / "sizeup-adapter.FEEDX_PASS").write_text("Tr0ub&dor3x\n")
    monkeypatch.setattr(procenv, "_dir", tmp_path)
    procenv.register_secrets()
    for user in ("bob", "ab12", "x", "someone.else"):
        b = base64.b64encode(f"{user}:Tr0ub&dor3x".encode()).decode()
        assert logscrub.scrub(f"Authorization: Basic {b}") is None and logscrub.scrub(f"hdr {b} ok") is None, user


def test_repro2_usernames_and_regions_are_not_secrets(tmp_path, monkeypatch):
    from sizeup import procenv
    for n, v in (("FEEDX_USER", "dispatch"), ("SIZEUP_SSM_REGION", "us-east-1"), ("FEEDX_URL", "https://cad.example.net/app"),
                 ("FEEDX_PASS", "Tr0ub&dor3x")):
        (tmp_path / f"sizeup-adapter.{n}").write_text(v + "\n")
    monkeypatch.setattr(procenv, "_dir", tmp_path)
    procenv.register_secrets()
    assert logscrub.scrub("dispatch mode: 60 Control") == "dispatch mode: 60 Control"
    assert logscrub.scrub("aws region us-east-1") == "aws region us-east-1"
    assert logscrub.scrub("x Tr0ub&dor3x") is None and "cad.example.net" in procenv.adapter_hosts()


@pytest.mark.parametrize("line,want", [
    ("FEEDX_PASS hunter2", "[credential] [redacted]"), ("FEEDX_PASS -> hunter2", "[credential] -> [redacted]"),
    ("password => hunter2", "password => [redacted]"), ("password=correct horse battery", "password=[redacted]"),
    ("passcode: 1234", "passcode: [redacted]"), ("api key: abc", "api key: [redacted]"), ("apikey=abc", "apikey=[redacted]"),
    ("key=abc", "key=[redacted]"), ("token=abc", "token=[redacted]"), ("otp: 123456", "otp: [redacted]"),
    ("login code: 123456", "login code: [redacted]"), ("Authorization: Token abc123", "Authorization: [redacted]"),
    ("Authorization: NTLM TlRMTVNTUAAB", "Authorization: [redacted]"),
    ("ftp://bob:p@ss@host.example/x", "ftp://[redacted]@host.example/x"),
    ("p\u0430ssword=x", "password=[redacted]"), ("\u03c1assword=x", "password=[redacted]"), ("p\\x61ssword=x", "password=[redacted]"),
])
def test_repro2_scrub_additions(line, want):
    logscrub.secret_name("FEEDX_PASS")
    assert logscrub.scrub(line) == want


def test_repro2_upper_and_hex_forms_of_a_value():
    logscrub.secret("Tr0ub&dor3x")
    assert logscrub.scrub("x TR0UB&DOR3X") is None and logscrub.scrub("x " + b"Tr0ub&dor3x".hex()) is None
    assert logscrub.scrub("x " + b"Tr0ub&dor3x".hex().upper()) is None


def test_repro2_the_reader_never_follows_a_planted_link(tmp_path):
    from sizeup.cloud import logread
    target = tmp_path / "elsewhere"
    target.write_text("keep")
    d = tmp_path / "ho"
    d.mkdir()
    (d / "status.json").symlink_to(target)
    (d / "cursor").symlink_to(target)
    r = logread.Reader(d, read=lambda c, s, n: [dict(JOURNAL[0])], clock=lambda: T0)
    assert r.once() == "ok"
    assert target.read_text() == "keep" and not (d / "status.json").is_symlink()


def test_repro_the_station_registers_its_config_tokens(tmp_path):
    s, _ = shipper(tmp_path)
    s.cfg.cloud_token, s.cfg.cloud_status_token = "cfg-Tok3n-from-the-file", "st4tus-Tok3n-xyz"
    s.start()
    s.stop()
    assert logscrub.scrub("x cfg-Tok3n-from-the-file") is None and logscrub.scrub("x st4tus-Tok3n-xyz") is None


def test_repro_the_hub_registers_its_own_tokens(tmp_path):
    from sizeup.cloud.hub import HubApp
    from tests.test_cloud import TOKEN as HUB_TOKEN
    from tests.test_cloud import hub_cfg
    HubApp(hub_cfg(tmp_path), log=lambda m: None)
    assert logscrub.scrub(f"hub: {HUB_TOKEN}") is None


def test_levels_and_repeats():
    assert logscrub.level(6, "hub link: connected") == "info"
    assert logscrub.level(6, "Traceback (most recent call last):") == "error"
    assert logscrub.level(6, "control channel lost, retrying") == "warning"
    assert logscrub.level(6, "14:00:10 debug: rx buffer 4096") == "debug"
    assert logscrub.level(3, "anything") == "error" and logscrub.level(7, "x") == "debug"
    assert logscrub.at_least("warning", "info") and not logscrub.at_least("debug", "info")
    r = logscrub.Repeats(burst=2, window_s=600)
    sent = [x for t in range(6) for x in r.allow("sizeup-station", f"FG1: carrier {t}.1 s", "info", T0 + t)]
    assert [m for _, _, _, m in sent] == ["FG1: carrier 0.1 s", "FG1: carrier 1.1 s"] and r.suppressed == 4
    assert r.flush(T0 + 100) == []
    out = r.flush(T0 + 700)
    assert [m for _, _, _, m in out] == ["[repeated 4× in 10 min] FG1: carrier 5.1 s"]


# -- the station -------------------------------------------------------------------------------------------------

class FakeClient:
    def __init__(self, answers):
        self.answers, self.sent = list(answers), []

    def request(self, method, path, body=None, timeout=None, gzip_body=False, raw_gzip=None):
        self.sent.append((path, json.loads(gzip.decompress(raw_gzip))))
        a = self.answers.pop(0) if self.answers else {"ok": True, "accepted": True}
        if isinstance(a, Exception):
            raise a
        return a

    def close(self):
        pass


def handoff(d: Path, rows, name="000000000000001.jsonl") -> Path:
    """What sizeup-logread writes: one file of journal entries."""
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


def shipper(tmp_path, answers=(), rows=None, clock=None, markers=()):
    c = FakeClient(answers)
    cfg = SimpleNamespace(station_id="pcfd", cloud_outbox=str(tmp_path / "outbox.sqlite"), cloud_token="", cloud_access_id="",
                          cloud_access_secret="", cloud_status_token="")
    now = clock or [T0 + 100]
    ho = tmp_path / "handoff"
    handoff(ho, list(JOURNAL) if rows is None else rows)
    s = LogShipper(cfg, lambda t: c, log=lambda m: None, clock=lambda: now[0], handoff=ho, markers=lambda: set(markers))
    s.client_, s.now = c, now
    return s, c


def test_the_station_reads_filters_scrubs_spools_and_sends(tmp_path):
    s, c = shipper(tmp_path)
    assert s.step() == "ok"                       # read: the batch is open, nothing due yet; the hand-off file kept
    assert s.pending and not s._batches() and not c.sent and s._handoff_files()
    s.now[0] += logship.BATCH_S
    assert s.step() == "ok"
    assert len(c.sent) == 1 and not s._batches() and not s._handoff_files()    # spooled: the hand-off file gone
    path, body = c.sent[0]
    assert path == "/api/station/logs" and body["station"] == "pcfd"
    lines = body["lines"]
    msgs = [m for _, _, _, m in lines]
    assert lines == sorted(lines, key=lambda r: r[0])
    assert not any("debug" in m for m in msgs) and s.dropped_level == 1
    assert sum(m.startswith("14:00:0") and "FG1" in m for m in msgs) == 5 and s.repeats.suppressed == 2
    # the runner's lines about the incident source stay on the station by default
    assert not any("incident sources" in m for m in msgs) and s.dropped_adapter == 2
    units = {u for _, u, _, _ in lines}
    assert units == {"sizeup-station", "sizeup-whisper", "sizeup-trunk-recorder", "sizeup-update", "sizeup-health"}
    lv = {m: v for _, _, v, m in lines}
    assert lv["Traceback (most recent call last):"] == "error"
    assert "bad byte � here" in msgs
    st = s.stats()
    assert st["state"] == "ok" and st["sent_lines"] == len(lines) and st["spool_batches"] == 0


def test_repro_adapter_lines_stay_unless_settings_say(tmp_path):
    rows = [dict(JOURNAL[0], MESSAGE="14:00:00 feedx: polled 12 calls"),
            dict(JOURNAL[0], MESSAGE="[log feedx_client] warning: slow answer"),
            dict(JOURNAL[0], MESSAGE='  File "/opt/x/venv/lib/python3.12/site-packages/feedx/poll.py", line 9, in poll'),
            dict(JOURNAL[0], MESSAGE="    raise Err(f'bad {page}')"),
            dict(JOURNAL[0], MESSAGE="14:00:01 hub link: connected")]
    s, c = shipper(tmp_path, rows=rows, markers=("feedx",))
    s.step()
    assert [m for _, _, _, m in s.pending] == ["14:00:01 hub link: connected"] and s.dropped_adapter == 4
    s2, _ = shipper(tmp_path / "on", rows=rows, markers=("feedx",))
    s2._set_config({"adapter": True})
    s2.step()
    assert len(s2.pending) == 5


def test_adapter_markers_come_from_the_installed_entry_points_and_the_runner():
    ep = SimpleNamespace(name="feedx", value="feedx_pkg.adapter:make", module="feedx_pkg.adapter",
                         dist=SimpleNamespace(name="feedx-adapter"))
    runner = SimpleNamespace(runs=[SimpleNamespace(a=SimpleNamespace(source="feedx", label="Feed X",
                                                                     base_url="https://CAD.example.net/x"))])
    m = logship.adapter_markers(SimpleNamespace(sources_runner=runner), entry_points=lambda group: [ep],
                                hosts={"feed-host.example.org"})
    assert {"feedx", "feedx_pkg", "feedx-adapter", "feedx_adapter", "feed x", "cad.example.net", "feed-host.example.org"} <= m


def test_repro2_adapter_withholding_gaps(tmp_path):
    st = dict(JOURNAL[0])
    rows = [dict(st, MESSAGE="14:00:00 FEEDX: polled"),                                     # case
            dict(st, MESSAGE="GET https://cad.example.net/api failed"),                     # its host
            dict(st, MESSAGE="Traceback (most recent call last):"),
            dict(st, MESSAGE='  File "/opt/x/venv/lib/python3.12/site-packages/feedx/poll.py", line 9, in poll'),
            dict(st, MESSAGE="    raise Err(page)"),
            dict(st, MESSAGE="feedx.Err: the county said no for unit E63"),                 # the exception line
            dict(st, MESSAGE="[log sizeup_feedx] warning: slow"),                            # sizeup_<adapter>'s logger
            dict(st, MESSAGE="WARNING:urllib3.connectionpool:Retrying (Retry(total=2)) /api"),   # basicConfig's format
            dict(st, _SYSTEMD_UNIT="sizeup-update.service", MESSAGE="adapter: staged sizeup-feedx 1a2b3c4"),
            dict(st, MESSAGE="14:00:01 hub link: connected")]
    s, c = shipper(tmp_path, rows=rows, markers=("feedx", "sizeup-feedx", "cad.example.net"))
    s.step()
    assert [m for _, _, _, m in s.pending] == ["Traceback (most recent call last):", "14:00:01 hub link: connected"]
    assert s.dropped_adapter == 8


def test_repro2_basicconfig_handlers_get_the_tag():
    import logging
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        for h in saved:
            root.removeHandler(h)
        h = logging.StreamHandler()
        root.addHandler(h)
        logship.install_log_tags()
        assert isinstance(h.formatter, logship._Tag)
    finally:
        root.removeHandler(h)
        for x in saved:
            root.addHandler(x)


def test_a_known_secret_never_leaves_the_station(tmp_path):
    rows = [dict(JOURNAL[0], MESSAGE=f"hub link: token {TOKEN}"), dict(JOURNAL[0], MESSAGE="feed: user FEEDX_USER ok")]
    logscrub.secret(TOKEN)
    logscrub.secret_name("FEEDX_USER")
    s, c = shipper(tmp_path, rows=rows)
    s.step()
    s.spool(force=True)
    s.send()
    sent = json.dumps(c.sent)
    assert TOKEN not in sent and "FEEDX_USER" not in sent and "[credential]" in sent and s.dropped_secret == 1


def test_repro_a_scrub_that_raises_skips_the_line_and_goes_on(tmp_path, monkeypatch):
    s, c = shipper(tmp_path, rows=[dict(JOURNAL[0], MESSAGE="boom"), dict(JOURNAL[0], MESSAGE="fine")])
    real = logscrub.scrub
    monkeypatch.setattr(logscrub, "scrub", lambda m: (_ for _ in ()).throw(ValueError()) if m == "boom" else real(m))
    s.step()
    assert s.dropped_error == 1 and [m for _, _, _, m in s.pending] == ["fine"]


def test_repro_a_batch_is_cut_at_its_size(tmp_path, monkeypatch):
    monkeypatch.setattr(logship, "MAX_BATCH_LINES", 3)
    rows = [dict(JOURNAL[0], MESSAGE=f"line {chr(97 + i)}") for i in range(7)]
    s, c = shipper(tmp_path, rows=rows)
    s.step()
    assert [len(b["lines"]) for _, b in c.sent] == [3, 3] and len(s.pending) == 1


def test_repro_pending_is_kept_when_the_spool_write_fails(tmp_path, monkeypatch):
    s, c = shipper(tmp_path)
    s.step()
    n = len(s.pending)
    monkeypatch.setattr(logship.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("disk full")))
    assert s.spool(force=True) is None and len(s.pending) == n and s._handoff_files()


def test_repro_a_batch_the_hub_refuses_is_set_aside_not_retried(tmp_path):
    s, c = shipper(tmp_path, answers=[HubError("hub said 413: too large", 413)])
    s.step()
    s.spool(force=True)
    assert s.send() == "rejected" and not s._batches() and s.rejected == 1
    assert len(list((tmp_path / "logspool" / "rejected").iterdir())) == 1 and s.send() == "empty"


def test_the_hub_away_keeps_the_spool_capped_and_sends_it_later(tmp_path, monkeypatch):
    s, c = shipper(tmp_path, answers=[HubError("can't reach the hub", 0)] * 3)
    s.step()
    s.spool(force=True)
    now = s.now
    assert s.send() == "failed" and len(s._batches()) == 1 and s.state == "offline"
    assert s.send() == "waiting"
    now[0] = s.next_send
    assert s.send() == "failed"
    c.answers = [{"ok": True, "accepted": False, "retry_s": 600, "reason": "hub: not configured (no log group)",
                  "config": {"on": True, "level": "warning", "include": [], "exclude": ["sizeup-whisper"]}}]
    now[0] = s.next_send
    assert s.send() == "not configured" and len(s._batches()) == 1 and s.next_send == now[0] + 600
    assert s.config["level"] == "warning" and s.config["exclude"] == ["sizeup-whisper"]
    assert json.loads((tmp_path / "logspool" / "config.json").read_text())["level"] == "warning"
    now[0] = s.next_send
    assert s.send() == "sent" and not s._batches()
    monkeypatch.setattr(logship, "SPOOL_MAX_BYTES", 1)
    s2, _ = shipper(tmp_path / "b", rows=[])
    for i in range(3):
        s2.pending.append([int(T0 * 1000) + i, "sizeup-station", "info", f"line {i}"])
        s2.spool(force=True)
    assert s2.dropped_spool >= 2 and len(s2._batches()) <= 1


def test_off_reads_nothing(tmp_path, monkeypatch):
    s, c = shipper(tmp_path)
    monkeypatch.setenv("SIZEUP_LOG_SHIP", "off")
    assert s.step() == "off" and s.stats()["on"] is False and not s._handoff_files() and s.dropped_off == 1
    monkeypatch.delenv("SIZEUP_LOG_SHIP")
    s._set_config({"on": False})
    assert s.step() == "off" and s.reason == "off in Settings"


def test_no_reader_is_said_not_raised(tmp_path):
    s, c = shipper(tmp_path, rows=[])
    s.handoff = tmp_path / "nowhere"
    s.step()
    assert s.stats()["state"] == "no journal"


def test_the_reader_hands_over_only_its_units_and_keeps_its_cursor(tmp_path):
    from sizeup.cloud import logread
    asked = []

    def read(cursor, since, limit):
        asked.append(cursor)
        return [] if cursor else [dict(r, _EXTRA="x") for r in JOURNAL[:3]]
    r = logread.Reader(tmp_path, read=lambda c, s, n: [{k: v for k, v in x.items() if k in logread.FIELDS}
                                                        for x in read(c, s, n)], clock=lambda: T0)
    assert r.once() == "ok" and r.once() == "ok"
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1 and asked == ["", JOURNAL[2]["__CURSOR"]]
    assert all("_EXTRA" not in json.loads(x) for x in files[0].read_text().splitlines())
    assert json.loads((tmp_path / "status.json").read_text())["written"] == 3
    unit = (Path(__file__).parents[1] / "deploy/station/systemd/sizeup-logread.service").read_text()
    for want in ("User=sizeup-logs", "SupplementaryGroups=systemd-journal", "PrivateNetwork=yes", "ProtectSystem=strict"):
        assert want in unit
    inst = (Path(__file__).parents[1] / "deploy/station/install-linux.sh").read_text()
    assert "usermod -g sizeup-logs -a -G systemd-journal sizeup-logs" in inst and 'usermod -a -G systemd-journal "$SVC_USER"' not in inst
    assert "Group=sizeup-logs" in unit and "--gid sizeup-logs" in inst


# -- the hub -----------------------------------------------------------------------------------------------------

class FakeLogs:
    """CloudWatch Logs' JSON API, enough of it."""

    def __init__(self, group=cwlogs.STATION_GROUP, retention=30, deny=False):
        self.calls, self.group, self.retention, self.deny = [], group, retention, deny
        self.streams: dict[str, list] = {}
        self.queries = 0

    def __call__(self, target, body, reg, timeout=15.0):
        self.calls.append((target, body))
        if self.deny:
            raise aws.AwsError("AccessDeniedException: not authorized", "AccessDeniedException", 400)
        if target == "DescribeLogGroups":
            return {"logGroups": [{"logGroupName": self.group, "retentionInDays": self.retention, "storedBytes": 1234}]
                    if self.group else []}
        if target == "PutRetentionPolicy":
            self.retention = body["retentionInDays"]
            return {}
        if target == "CreateLogStream":
            if body["logStreamName"] in self.streams:
                raise aws.AwsError("ResourceAlreadyExistsException", "ResourceAlreadyExistsException", 400)
            self.streams[body["logStreamName"]] = []
            return {}
        if target == "PutLogEvents":
            assert len(body["logEvents"]) <= cwlogs.MAX_PUT_EVENTS
            ts = [e["timestamp"] for e in body["logEvents"]]
            assert ts == sorted(ts)
            self.streams[body["logStreamName"]].extend(body["logEvents"])
            return {}
        if target == "StartQuery":
            self.queries += 1
            return {"queryId": "q1"}
        if target == "GetQueryResults":
            st = cwlogs.STATION_GROUP
            return {"status": "Complete", "statistics": {"bytesScanned": 2048.0, "recordsMatched": 2.0},
                    "results": [
                        [{"field": "@timestamp", "value": "2025-11-12 12:20:11.000"},
                         {"field": "@message", "value": '{"unit":"sizeup-station","level":"error","msg":"Traceback"}'},
                         {"field": "@log", "value": f"000000000000:{st}"}, {"field": "@logStream", "value": "pcfd-2025-11-12"}],
                        [{"field": "@timestamp", "value": "2025-11-12 12:20:10.000"},
                         {"field": "@message", "value": "12:20:10 audio: s3 put failed (password: x9)"},
                         {"field": "@log", "value": "000000000000:/ecs/sizeup-hub"},
                         {"field": "@logStream", "value": "hub/hub/0123abcd"}]]}
        raise AssertionError(target)


def sink(fake, settings=None, status=None, clock=T0 + 60):
    store = {} if settings is None else settings
    return StationLogs(settings=lambda: store, station_status=lambda: status, log=lambda m: None, call=fake,
                       clock=lambda: clock)


def batch(lines, station="pcfd"):
    return gzip.compress(json.dumps({"v": 1, "station": station, "batch": "b1", "lines": lines}).encode())


GZ = {"Content-Encoding": "gzip"}


def test_not_configured_until_the_aws_step_has_run_and_the_station_keeps_its_batch():
    for fake, why in ((FakeLogs(group=""), "no log group"), (FakeLogs(deny=True), "AccessDeniedException")):
        sl = sink(fake)
        code, out = sl.ingest(batch([[int(T0 * 1000), "sizeup-station", "info", "x"]]), GZ)
        assert code == 200 and out["accepted"] is False and out["retry_s"] == 600 and why in out["reason"]
        assert "25-station-logs.sh" in out["reason"] and sl.status()["configured"] is False
        assert not any(t == "PutLogEvents" for t, _ in fake.calls)


def test_the_hub_filters_scrubs_again_and_writes_one_stream_a_day():
    fake = FakeLogs(retention=None)
    sl = sink(fake, settings={"exclude": ["sizeup-ais"], "level": "info"})
    ms = int(T0 * 1000)
    lines = [[ms + 2, "sizeup-station", "info", "FG1: carrier 4.2 s"],
             [ms + 1, "sizeup-station", "info", "GET https://u:pw1@example.org/x"],
             [ms + 3, "sizeup-ais", "info", "ais: 4 vessels"],                     # excluded
             [ms + 4, "sizeup-station", "debug", "rx buffer"],                    # below the level
             [ms + 5, "sizeup-station", "info", "the password is hunter7"],       # fail closed
             [ms - 20 * 86400 * 1000, "sizeup-station", "info", "too old"],
             ["x", "bad", "info", "malformed"]]
    code, out = sl.ingest(batch(lines), GZ)
    assert code == 200 and out["accepted"] is True and out["n"] == 2 and out["config"]["exclude"] == ["sizeup-ais"]
    assert fake.retention == 30                                 # the group's retention brought to the setting
    (stream, evs), = fake.streams.items()
    assert stream == "pcfd-2025-11-12"
    assert [json.loads(e["message"]) for e in evs] == [
        {"unit": "sizeup-station", "level": "info", "msg": "GET https://[redacted]@example.org/x"},
        {"unit": "sizeup-station", "level": "info", "msg": "FG1: carrier 4.2 s"}]
    assert sl.today["lines"] == 2 and sl.today["dropped"] == 5
    # a second batch: the stream is not made again
    sl.ingest(batch([[ms + 9, "sizeup-station", "info", "again"]]), GZ)
    assert sum(t == "CreateLogStream" for t, _ in fake.calls) == 1
    # off in Settings: accepted and dropped (the station stops reading)
    off = sink(FakeLogs(), settings={"on": False})
    code, out = off.ingest(batch(lines), GZ)
    assert out["accepted"] is True and out["config"]["on"] is False


def test_a_big_batch_is_split_within_cloudwatchs_limits(monkeypatch):
    monkeypatch.setattr(cwlogs, "MAX_PUT_EVENTS", 100)
    fake = FakeLogs()
    sl = sink(fake)
    ms = int(T0 * 1000)
    code, out = sl.ingest(batch([[ms + i, "sizeup-station", "info", f"line {i}"] for i in range(250)]), GZ)
    assert out["n"] == 250 and sum(t == "PutLogEvents" for t, _ in fake.calls) == 3


def test_bad_bodies():
    sl = sink(FakeLogs())
    assert sl.ingest(None, {})[0] == 413
    assert sl.ingest(b"nope", GZ)[0] == 400
    assert sl.ingest(json.dumps({"lines": "x"}).encode(), {})[0] == 400


def test_queries_are_bounded_and_built_safely():
    sl = sink(FakeLogs(), clock=T0)
    b = sl.build({"source": ["station"], "unit": ["sizeup-station"], "level": ["warning"], "q": ['a"b/c|d']})
    assert b["groups"] == [cwlogs.STATION_GROUP] and b["end"] - b["start"] == cwlogs.DEFAULT_WINDOW_S + 1
    assert 'unit = "sizeup-station"' in b["query"] and 'level in ["warning", "error"]' in b["query"]
    assert '@message like /(?i)a\\"b\\/c\\|d/' in b["query"] and b["query"].rstrip().endswith("limit 200")
    h = sl.build({"unit": ["hub"], "level": ["error"]})
    assert h["groups"] == [cwlogs.HUB_GROUP] and "@logStream like /^hub\\//" in h["query"] and "traceback" in h["query"]
    a = sl.build({"source": ["all"], "level": ["warning"], "limit": ["9999"]})
    assert len(a["groups"]) == 2 and "ispresent(level)" in a["query"] and a["limit"] == cwlogs.MAX_ROWS
    for bad in ({"source": ["x"]}, {"unit": ["a b"]}, {"level": ["loud"]}, {"q": ["x" * 200]}, {"q": ["a\nb"]},
                {"start": [str(T0 - 8 * 86400)], "end": [str(T0)]}, {"start": [str(T0)], "end": [str(T0 - 1)]}):
        with pytest.raises(LogsError):
            sl.build(bad)


def test_a_query_answers_rows_with_the_hubs_lines_scrubbed_too():
    fake = FakeLogs()
    sl = sink(fake)
    out = sl.query({"source": ["all"]})
    assert out["scanned_bytes"] == 2048.0 and len(out["rows"]) == 2
    st, hub = out["rows"]
    assert st == {"t": pytest.approx(1762950011.0), "source": "station", "unit": "sizeup-station", "level": "error",
                  "msg": "Traceback"}
    assert hub["source"] == "hub" and hub["unit"] == "hub" and hub["level"] == "error"
    assert "x9" not in hub["msg"] and "[redacted]" in hub["msg"]
    assert "000000000000" not in json.dumps(out)              # never the account


# -- the API and Settings ----------------------------------------------------------------------------------------

def test_logs_are_a_signed_in_admins_and_settings_set_reset_and_audit(tmp_path):
    from sizeup.cloud.access import Identity
    from tests.test_push_settings import Rig
    from tests.test_roles import COREY, JULIAN, call
    rig = Rig(tmp_path)
    get = lambda p, q="", who=COREY: rig.api.get(p, q, Identity(email=who) if who else None)   # noqa: E731
    assert get("/api/cad/logs/status")[0] == 404                      # not the hub: nothing there
    fake = FakeLogs()
    rig.cad.station_logs = sink(fake, status={"logship": {"state": "ok", "spool_bytes": 0}})
    rig.cad.station_logs.settings = lambda: rig.cad.settings.section(cwlogs.SECTION)
    assert get("/api/cad/logs/status", who=None)[0] == 403             # never without a verified sign-in
    assert get("/api/cad/logs/status", who=JULIAN)[0] == 403           # a dispatcher
    code, raw, _ = get("/api/cad/logs/status")
    st = json.loads(raw)
    assert code == 200 and st["configured"] is True and st["station"]["state"] == "ok" and st["settings"]["level"] == "info"
    code, raw, _ = get("/api/cad/logs", "source=station&level=bogus")
    assert code == 400
    code, raw, _ = get("/api/cad/logs", "source=all")
    assert code == 200 and len(json.loads(raw)["rows"]) == 2
    post = lambda body, who=COREY: call(rig.api, "/api/cad/settings/station_logs", body, who)   # noqa: E731
    assert post({"action": "set", "key": "level", "value": "warning"})[0] == 200
    code, out = post({"action": "set", "key": "exclude", "value": "sizeup-ais, sizeup-adsb.service"})
    assert code == 200 and out["station_logs"]["exclude"]["value"] == ["sizeup-adsb", "sizeup-ais"]
    assert post({"action": "set", "key": "retention_days", "value": 90})[0] == 200
    for body in ({"action": "set", "key": "level", "value": "loud"}, {"action": "set", "key": "retention_days", "value": 31},
                 {"action": "set", "key": "retention_days", "value": 3},                       # (#494 review: at least 7)
                 {"action": "set", "key": "on", "value": 1}, {"action": "set", "key": "nope", "value": 1},
                 {"action": "set", "key": "include", "value": ["a b"]}):
        assert post(body)[0] == 400, body
    assert post({"action": "set", "key": "level", "value": "error"}, who=JULIAN)[0] == 403
    assert rig.cad.station_logs.cfg() == {"on": True, "level": "warning", "include": [],
                                          "exclude": ["sizeup-adsb", "sizeup-ais"], "adapter": False, "retention_days": 90}
    rig.cad.station_logs.check()
    assert fake.retention == 90
    assert post({"action": "reset", "key": "level"})[0] == 200 and rig.cad.station_logs.cfg()["level"] == "info"
    audit = rig.cad.settings_audit_log({"section": ["system"]})["audit"]
    assert [a["key"] for a in reversed(audit) if a["key"].startswith("station_logs/")] == [
        "station_logs/level", "station_logs/exclude", "station_logs/retention_days", "station_logs/level"]
    assert rig.cad.settings_get()["station_logs"]["retention_days"]["value"] == 90


def test_the_hub_takes_a_batch_over_the_station_link(tmp_path, monkeypatch):
    from sizeup.cloud.hub import HubApp
    from tests.test_cloud import TOKEN as HUB_TOKEN
    from tests.test_cloud import hub_cfg
    app = HubApp(hub_cfg(tmp_path), log=lambda m: None)
    try:
        fake = FakeLogs()
        app.station_logs._call = fake
        app.station_logs.clock = lambda: T0 + 60
        body = batch([[int(T0 * 1000), "sizeup-station", "info", "hub link: connected"]])
        hdrs = {"Authorization": f"Bearer {HUB_TOKEN}", "Content-Encoding": "gzip"}
        code, raw, _ = app.hub.handle("POST", "/api/station/logs", "", hdrs, lambda n: body)
        assert code == 200 and json.loads(raw)["accepted"] is True
        assert app.cad.station_logs is app.station_logs and sum(map(len, fake.streams.values())) == 1
        code, _, _ = app.hub.handle("POST", "/api/station/logs", "", {"Authorization": "Bearer nope"}, lambda n: body)
        assert code == 401
    finally:
        app.close() if hasattr(app, "close") else None
