"""A job's arguments and results of the wrong shape (sizeup/cloud/station.py call_args / _call / _run_job,
sizeup/cloud/hub.py render_voice / HubApp._catchup_whisper). contracts/README.md J4, J5, J6. Invented values only."""

from __future__ import annotations

import base64
import threading

import pytest

from sizeup.cad.dispatcher import AudioUnavailable
from sizeup.cloud.hub import Hub, HubApp
from sizeup.cloud.station import CALLS, StationLink, call_args


class FakeApp:
    """The App's signatures for the calls used here (the real ones: sizeup/app.py)."""

    def __init__(self):
        self.ran: list[tuple] = []

    def set_adsb_receiver(self, serial: str | None) -> str | None:
        self.ran.append(("set_adsb_receiver", serial))
        return None

    def retranscribe(self, ids: list[str] | None = None, as_alt: bool = False) -> int:
        self.ran.append(("retranscribe", ids, as_alt))
        return 7 if ids is None else len(ids)

    def set_stt_alert_keys(self, keys, rev) -> dict:
        self.ran.append(("set_stt_alert_keys", keys, rev))
        return {"ok": True, "rev": rev}

    def sources_poll_now(self, reason: str = "") -> int:
        self.ran.append(("sources_poll_now", reason))
        return 1

    def emergency_all_clear(self, tg=None, radio_ids=None) -> bool:
        self.ran.append(("emergency_all_clear", tg, radio_ids))
        return False


def a_link(app=None) -> StationLink:
    """A StationLink with only what _run_job / _call use; its results land in .posted."""
    link = StationLink.__new__(StationLink)
    link.app = app or FakeApp()
    link.log = lambda m: None
    link._stop, link._live_wake = threading.Event(), threading.Event()
    link.posted = []

    class Client:
        def request(self, method, path, body=None, timeout=None):
            link.posted.append((path, body))

        def close(self):
            pass
    link._client = lambda timeout: Client()
    return link


def run(link, kind, args) -> dict:
    job = {"id": "0123456789abcdef", "type": kind}
    if args is not ...:
        job["args"] = args
    link._run_job(job)
    return link.posted[-1][1]


# -- J4: by name, and what the station does not know is ignored -------------------------------------------------------

def test_arguments_by_position_or_by_name_and_one_it_does_not_know_is_ignored():
    link = a_link()
    assert run(link, "call", {"fn": "set_adsb_receiver", "args": [""]}) == {"ok": True, "result": None}
    assert run(link, "call", {"fn": "set_adsb_receiver", "args": ["", "from_the_future"]}) == {"ok": True, "result": None}
    assert run(link, "call", {"fn": "set_adsb_receiver", "args": {"serial": "00000101", "from_the_future": 1}}) \
        == {"ok": True, "result": None}
    assert run(link, "call", {"fn": "retranscribe", "args": {"ids": ["FG1-1"], "as_alt": True}}) == {"ok": True, "result": 1}
    assert run(link, "call", {"fn": "set_stt_alert_keys", "args": [["fm:FG1"], 3]})["result"] == {"ok": True, "rev": 3}
    assert link.app.ran == [("set_adsb_receiver", ""), ("set_adsb_receiver", ""), ("set_adsb_receiver", "00000101"),
                            ("retranscribe", ["FG1-1"], True), ("set_stt_alert_keys", ["fm:FG1"], 3)]


def test_an_argument_it_must_have_left_out_is_refused_and_nothing_runs():
    link = a_link()
    for args in ([], {}, {"rev": 3}, ["only-keys"]):
        res = run(link, "call", {"fn": "set_stt_alert_keys", "args": args})
        assert res["ok"] is False and res["error"].startswith("TypeError: set_stt_alert_keys() missing argument"), args
    assert run(link, "call", {"fn": "set_adsb_receiver", "args": "00000101"}) == {"ok": False,
                                                                                  "error": "args: a list or an object"}
    assert link.app.ran == []


def test_a_stand_in_without_a_signature_to_read_takes_the_args_as_given():
    class Any:
        def __init__(self):
            self.got = []

        def set_mode(self, *a, **k):
            self.got.append((a, k))
            return "ok"
    app = Any()
    link = a_link(app)
    assert run(link, "call", {"fn": "set_mode", "args": ["Fireground 1", None]})["result"] == "ok"
    assert run(link, "call", {"fn": "set_mode", "args": {"mode": "Off"}})["result"] == "ok"
    assert app.got == [(("Fireground 1", None), {}), ((), {"mode": "Off"})]


# -- J5: a call without its arguments never runs on the defaults -------------------------------------------------------

def test_a_call_without_its_arguments_is_refused_and_nothing_runs():
    link = a_link()
    for args in ({"fn": "retranscribe"}, {"fn": "retranscribe", "args": None}, {"fn": "retranscribe", "args": []},
                 {"fn": "retranscribe", "args": {}}, {"fn": "retranscribe", "args": {"as_alt": True}},
                 {"fn": "sources_poll_now"}, {"fn": "emergency_all_clear", "args": None}):
        res = run(link, "call", args)
        assert res["ok"] is False and res["error"], args
    assert link.app.ran == []
    # every recording, asked for by name (null), and the calls whose defaults are harmless, with an empty list
    assert run(link, "call", {"fn": "retranscribe", "args": [None]}) == {"ok": True, "result": 7}
    assert run(link, "call", {"fn": "retranscribe", "args": {"ids": None}}) == {"ok": True, "result": 7}
    assert run(link, "call", {"fn": "sources_poll_now", "args": []}) == {"ok": True, "result": 1}
    assert run(link, "call", {"fn": "emergency_all_clear", "args": []}) == {"ok": True, "result": False}


def test_a_sources_reset_that_does_not_name_its_source_is_refused():
    link = a_link()
    asked = []
    link.app.sources_runner = None
    import sizeup.cloud.station as station_mod
    real = station_mod.request_reset
    station_mod.request_reset = lambda runner, source, by, log, origin: asked.append(source) or {"ok": True, "result": "x"}
    try:
        for args in (["east-cad"], None, ..., "east-cad", {}, {"by": "admin@example.org"}, {"source": 5}):
            res = run(link, "sources_reset", args)
            assert res["ok"] is False and "source" in res["error"], args
        assert asked == []
        assert run(link, "sources_reset", {"source": "", "by": "admin@example.org"}) == {"ok": True, "result": "x"}
        assert run(link, "sources_reset", {"source": "east-cad", "by": "admin@example.org"})["ok"] is True
        assert asked == ["", "east-cad"]
    finally:
        station_mod.request_reset = real


def test_every_call_has_a_signature_the_station_can_read():
    """call_args reads each App method's parameters: none of CALLS takes *args, so a name it does not know is
    ignored for each of them, never passed on."""
    from sizeup.app import App
    for fn in CALLS:
        given = call_args(fn, getattr(App, fn).__get__(object()), {"from_the_future": 1})
        assert given is not None, fn                      # (a dict, or why not: never "as given")


# -- J6: a wrong type in a result is a station that sent nothing there -------------------------------------------------

def a_hub(result: dict) -> tuple[Hub, list[str]]:
    h = Hub.__new__(Hub)
    logs: list[str] = []
    h.log = logs.append
    h._wrong_results = set()
    h.call = lambda kind, args, timeout: result
    return h, logs


@pytest.mark.parametrize("audio", [5, ["UklGRg=="], {"x": 1}, True])
def test_a_tts_result_whose_audio_is_no_string_is_text_only_said_once(audio):
    h, logs = a_hub({"ok": True, "label": "Voice: x", "audio": audio})
    for _ in range(2):
        with pytest.raises(AudioUnavailable, match="the station sent no audio: text only"):
            h.render_voice({"text": "Engine 1 respond"})
    assert len(logs) == 1 and "'audio'" in logs[0]


def test_a_tts_result_with_a_label_of_another_type_keeps_its_audio():
    wav = b"RIFF" + b"\x00" * 40
    h, logs = a_hub({"ok": True, "label": 5, "audio": base64.b64encode(wav).decode()})
    assert h.render_voice({"text": "Engine 1 respond"}) == (wav, "Voice: rendered by the station")
    assert len(logs) == 1 and "'label'" in logs[0]


@pytest.mark.parametrize("queued", [5, "FG1-1", {"FG1-1": 1}, True])
def test_a_catch_up_result_whose_queued_is_no_list_is_nothing_queued(queued):
    h, logs = a_hub({})
    h.remote = lambda fn, *a, timeout=None: {"queued": queued, "refused": {}, "hold": ""}
    app = HubApp.__new__(HubApp)
    app.hub = h
    app._catchup_refused_until = 0.0
    assert app._catchup_whisper([{"id": "FG1-1"}]) is None
    assert app._catchup_whisper([{"id": "FG1-1"}]) is None
    assert len(logs) == 1 and "'queued'" in logs[0]
    h.remote = lambda fn, *a, timeout=None: {"queued": ["FG1-1", 5], "refused": {"FG1-2": "busy"}, "hold": ""}
    assert app._catchup_whisper([{"id": "FG1-1"}]) == {"queued": ["FG1-1"], "refused": {"FG1-2": "busy"}, "hold": ""}
