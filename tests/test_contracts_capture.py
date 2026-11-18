"""contracts/ (ADR 0004): the DESIGNED contract of the radio capture split (ADR 0002; the design is
docs/native/capture-split.md, #773): the spool, the urgent lane, the live socket, the wish and status files, the two
mode files. Nothing of it is implemented, so these tests hold the schemas and vectors to the design's own tables and
run the rules a schema cannot say on the reference aids of tests/contracts_capture.py (specification aids, not
production code). The harness that every schema passes is tests/test_contracts_schemas.py."""
from __future__ import annotations

import copy
import fcntl
import io
import json
import re
import sqlite3
from pathlib import Path

import pytest

from tests import contracts as c
from tests import contracts_capture as cc

ROOT = Path(__file__).resolve().parent.parent
DESIGN = ROOT / "docs" / "native" / "capture-split.md"        # arrives with #773; until then its tables are copied here
README = (c.ROOT / "README.md").read_text(encoding="utf-8")
FUTURE = re.compile(r"from[_-]the[_-]future")
BOOT, OTHER_BOOT = "b1946ac9-2f0e-4c1d-9a77-0d1e2f3a4b5c", "0c7d2e4a-61b1-4946-ac92-f0e4c1d9a770"


def valid(name: str) -> dict:
    return dict(c.vectors(name, "valid"))


# -- the schemas as a set ----------------------------------------------------------------------------------------------

def test_the_capture_schemas_are_these_and_all_designed():
    assert {n for n in c.names() if n.startswith("capture-")} == set(cc.NAMES)
    for name in cc.NAMES:
        doc = c.load(name)
        assert doc["x-status"] == "designed" and doc["description"].startswith("Designed (docs/native/capture-split.md")
        assert "#773" in doc["description"] and "not implemented" in doc["description"]


# What docs/native/capture-split.md 10 "Contract artifacts" says a reader may rely on, row by row (copied from the
# design at 7e657add; test_the_design_documents_table_is_this_one compares it with the document once that is here).
REQUIRED = {
    "Spool sidecar, `kind: \"segment\"`": [
        "v", "kind", "id", "seq", "spool_epoch", "channel.label", "channel.kind", "start", "end", "closed_boot", "boot",
        "published", "reason", "levels", "ids", "alarm_key", "audio", "audio", "file", "format", "encoding", "rate_hz",
        "channels", "samples", "bytes"],
    "Spool sidecar, `kind: \"call\"`": ["system", "talkgroup", "turns", "emergency", "encrypted", "tr"],
    "Spool sidecar, `kind: \"metrics\"`": ["v", "kind", "id", "seq", "spool_epoch", "published", "records"],
    "Spool cursor": ["v", "spool_epoch", "seq"],
    "Urgent item (a row's `data`, and an item of the request)": [
        "key", "rev", "kind", "at", "event_at", "age_s", "age_src", "boot", "wall", "data", "data", "source", "start",
        "radio_id", "emergency", "emergency_via", "data", "channel", "started", "pairs", "phase", "ended"],
    "`POST /api/station/urgent`, request": ["v", "station", "items"],
    "`POST /api/station/urgent`, answers": ["ok", "gen", "results", "key", "rev", "status", "error", "reason"],
    "Socket, capture to client": ["v", "t", "at", "t"],
    "Socket, client to capture": ["v", "t", "v", "t: \"request\"", "id", "op", "args", "expires_boot"],
    "Socket, replies": ["v", "t: \"reply\"", "id", "ok", "ok: false", "error", "confirm"],
    "Wish file": ["v", "seq", "mode", "paths", "channels", "dongles"],
    "Status file": ["v", "at", "at_boot", "boot", "pid", "contract", "wish_seq", "channels", "sdrs", "refusals", "spool",
                    "urgent"],
}


def required_when(doc: dict, key: str, word: str) -> list[str]:
    """What a schema requires once `key` is `word`: its own `required` and that word's `then`."""
    out = list(doc.get("required", []))
    for b in doc.get("allOf", []):
        if b.get("if", {}).get("properties", {}).get(key, {}).get("const") == word:
            then = b["then"]
            then = c.registry().resolver(doc["$id"]).lookup(then["$ref"]).contents if "$ref" in then else then
            out += then.get("required", [])
    return out


def test_what_a_reader_may_rely_on_is_the_designs_table():
    side = c.load("capture-spool-sidecar")
    row = [x for x in REQUIRED["Spool sidecar, `kind: \"segment\"`"] if "." not in x][:15]
    assert set(required_when(side, "kind", "segment")) == set(row) | {"channel"}
    assert side["properties"]["channel"]["required"] == ["label", "kind"]
    assert side["$defs"]["audio"]["required"] == REQUIRED["Spool sidecar, `kind: \"segment\"`"][-7:]
    assert side["$defs"]["audio"]["type"] == ["object", "null"]                 # `audio` (object or null)
    common = ["v", "kind", "id", "seq", "spool_epoch", "published"]              # "the common ones": see the README
    assert side["required"] == common == REQUIRED["Spool sidecar, `kind: \"metrics\"`"][:-1]
    assert set(REQUIRED["Spool sidecar, `kind: \"call\"`"]) <= set(required_when(side, "kind", "call"))
    assert required_when(side, "kind", "metrics") == REQUIRED["Spool sidecar, `kind: \"metrics\"`"]
    assert c.load("capture-spool-cursor")["required"] == REQUIRED["Spool cursor"]
    item = c.load("capture-urgent-item")
    assert item["required"] == ["key", "rev", "kind", "at", "event_at", "age_s", "age_src", "data"]
    for key in item["required"]:                                                 # "all required, none nullable"
        prop = item["properties"][key]
        prop = item["$defs"]["data"] if "$ref" in prop else prop
        assert "null" not in json.dumps(prop.get("type")), key
    assert item["properties"]["age_s"]["minimum"] == 0 and item["properties"]["age_src"]["x-known"] == ["boot", "wall"]
    by_kind = {b["if"]["properties"]["kind"]["const"]: b["then"]["properties"]["data"]["required"]
               for b in item["$defs"]["by-kind"]["allOf"]}
    assert by_kind == {"alarm": ["source", "start", "radio_id", "emergency", "emergency_via"],
                       "page": ["channel", "started", "pairs", "phase", "ended"]}
    req = c.load("capture-urgent-request")
    assert req["required"] == REQUIRED["`POST /api/station/urgent`, request"]
    assert (req["properties"]["items"]["minItems"], req["properties"]["items"]["maxItems"]) == (1, 32)
    ans = c.load("capture-urgent-response")
    assert ans["else"]["required"] == ["ok", "gen", "results"]
    assert ans["$defs"]["result"]["required"] == ["key", "rev", "status"]
    assert ans["$defs"]["result"]["properties"]["status"]["x-known"] == ["applied", "duplicate", "stale", "rejected"]
    assert c.load("capture-socket-event")["required"] == ["v", "t", "at"]
    ask = c.load("capture-socket-request")
    assert ask["required"] == ["v", "t"] and ask["$defs"]["request"]["required"] == ["id", "op", "args", "expires_boot"]
    reply = c.load("capture-socket-reply")
    assert set(reply["required"]) == {"v", "t", "id", "ok", "at"} and reply["then"]["required"] == ["error", "confirm"]
    assert reply["properties"]["t"] == {"const": "reply", "description": "Always `reply`."}
    assert c.load("capture-wish")["required"] == REQUIRED["Wish file"]
    assert c.load("capture-status")["required"] == REQUIRED["Status file"]


def design_rows() -> dict[str, list[str]]:
    """{artifact: the names in backticks of its "Required" cell} from the design's section 10."""
    body = DESIGN.read_text(encoding="utf-8").split("## 10. Contract artifacts", 1)[1]
    body = body.split("Outside `contracts/capture/`", 1)[0]
    rows = [[x.strip() for x in re.split(r"(?<!\\)\|", line)[1:-1]]             # (a cell may hold an escaped \|)
            for line in body.splitlines() if line.startswith("|")][2:]
    return {r[0]: re.findall(r"`([^`]+)`", r[3]) for r in rows if r[3] != "-"}


def test_the_design_documents_table_is_this_one():
    """The copy above against the document itself, once #773 has put it in the tree: a change to the design's table
    fails here, and the schemas follow."""
    if not DESIGN.exists():
        pytest.skip("docs/native/capture-split.md is not in this tree yet (#773)")
    assert design_rows() == REQUIRED


def test_the_designs_example_bodies_are_vectors():
    """docs/native/capture-split.md 10 "Vectors to start from": each of its five bodies is a valid vector, as written
    there. (Compared with the document once it is in the tree; until then the vectors are the copy.)"""
    mine = [valid("capture-spool-sidecar")["segment-with-an-emergency-id.json"],
            valid("capture-urgent-request")["an-alarm-and-a-page.json"],
            valid("capture-urgent-response")["both-applied.json"],
            valid("capture-socket-request")["pick.json"], valid("capture-socket-reply")["refused-parked.json"]]
    assert mine[0]["alarm_key"] == mine[1]["items"][0]["key"] == mine[2]["results"][0]["key"] == "CHA-live-1900000000123"
    assert mine[3]["id"] == mine[4]["id"]
    if DESIGN.exists():
        text = DESIGN.read_text(encoding="utf-8").split("Vectors to start from", 1)[1].split("## 11.", 1)[0]
        assert [c.loads_strict(m) for m in re.findall(r"```json\n(.*?)```", text, re.S)] == mine


@pytest.mark.parametrize("name", cc.NAMES)
def test_every_valid_vector_names_only_keys_its_part_of_the_schema_names(name):
    """A schema that chooses by a word lists a frame's keys under that word's `then`, where the harness's own check
    does not look: this one does. A key in a valid vector that no part of the schema names is a typo in the contract."""
    for file, inst in c.vectors(name, "valid"):
        assert [p for p in cc.undeclared(name, inst) if not FUTURE.search(p)] == [], file


@pytest.mark.parametrize("name", cc.NAMES)
def test_every_valid_vector_is_still_valid_with_unknown_keys_in_every_object(name):
    """The one rule for every file and message: unknown keys are ignored by every reader. So a key from a newer build,
    in every object a schema describes, leaves every valid vector valid."""
    added = 0
    for file, inst in c.vectors(name, "valid"):
        out, n = cc.with_unknown_keys(name, inst)
        added += n
        assert c.errors(name, out) == [], file
    assert added >= 3


def test_the_check_on_keys_looks_inside_a_frame_and_a_requests_arguments():
    """The check on itself: a key added inside a frame, inside an entry of its list and inside a request's `args`
    is found; a word from a newer build needs no schema and is found nowhere."""
    pick = copy.deepcopy(valid("capture-socket-request")["pick.json"])
    pick["args"]["new_flag"], pick["new_top"] = True, 1
    assert sorted(cc.undeclared("capture-socket-request", pick)) == ["$.args.new_flag", "$.new_top"]
    talking = copy.deepcopy(valid("capture-socket-event")["talking.json"])
    talking["entries"][0]["new_flag"] = True
    assert cc.undeclared("capture-socket-event", talking) == ["$.entries[0].new_flag"]
    urgent = copy.deepcopy(valid("capture-socket-event")["urgent-alarm.json"])
    urgent["data"]["new_flag"] = urgent["data"]["ids"][0]["new_flag"] = True
    assert sorted(cc.undeclared("capture-socket-event", urgent)) == ["$.data.ids[0].new_flag", "$.data.new_flag"]
    assert cc.undeclared("capture-socket-event", valid("capture-socket-event")["a-frame-from-the-future.json"]) == []
    assert c.errors("capture-socket-event", urgent) == []                      # ... while it still validates


def test_a_word_from_a_newer_build_is_valid_and_a_wrong_type_is_not():
    """A kind, a frame, an op, a state, a mode a reader does not know is taken as unknown, never as an error."""
    seg = valid("capture-spool-sidecar")["segment-with-an-emergency-id.json"]
    status, wish = valid("capture-status")["running.json"], valid("capture-wish")["service.json"]
    item = valid("capture-urgent-item")["alarm-raised-live.json"]

    def put(doc, path, value):
        out = copy.deepcopy(doc)
        node = out
        for part in path[:-1]:
            node = node[part]
        node[path[-1]] = value
        return out
    cases = [("capture-spool-sidecar", seg, ("reason",)), ("capture-spool-sidecar", seg, ("channel", "kind")),
             ("capture-spool-sidecar", seg, ("ids", 0, "from")), ("capture-urgent-item", item, ("age_src",)),
             ("capture-urgent-item", item, ("data", "emergency_via")), ("capture-status", status, ("channels", 0, "run")),
             ("capture-status", status, ("channels", 0, "state")), ("capture-status", status, ("urgent", "state")),
             ("capture-status", status, ("urgent", "mode")), ("capture-wish", wish, ("mode",)),
             ("capture-wish", wish, ("channels", 0, "kind")),
             ("capture-urgent-response", valid("capture-urgent-response")["both-applied.json"], ("results", 0, "status")),
             ("capture-socket-event", valid("capture-socket-event")["carrier-up.json"], ("state",)),
             ("capture-socket-request", valid("capture-socket-request")["hello-reader.json"], ("role",))]
    for name, doc, path in cases:
        assert c.errors(name, put(doc, path, "from_the_future")) == [], (name, path)
        for bad in (1, None, True, ["x"], {"x": 1}):
            assert c.errors(name, put(doc, path, bad)) != [], (name, path, bad)


def test_a_boot_clock_value_is_never_a_wall_clock_time_and_the_reverse():
    """Which clock a field is on is in its name, and in its bounds: a wall-clock time (Unix seconds) does not validate
    where seconds since boot belong, nor milliseconds where seconds do."""
    boot_fields = wall_fields = 0
    for name in cc.NAMES:
        for path, prop in c.properties(c.load(name)):
            key = path.rsplit(".", 1)[-1]
            if key.endswith("_boot"):
                boot_fields += 1
                assert prop.get("exclusiveMaximum") == 1000000000 and prop.get("minimum") == 0, (name, path)
            elif prop.get("exclusiveMaximum") == c.TIME_LIMIT:
                wall_fields += 1
                assert not key.endswith(("_s", "_ms", "_boot")), (name, path)
    assert boot_fields == 6 and wall_fields >= 40          # closed_, published_, event_, expires_, at_, creds_at_boot
    assert 1_900_000_000 > 1000000000 > 20 * 365 * 86400             # no uptime reaches it; every date today is over it


def test_no_vector_of_the_contract_carries_a_secret_but_the_root_helpers_request():
    """docs/native/capture-split.md 4.3 "Credentials", "Never": a token is in no wish, no status, no spool item, no
    urgent item and no frame but the root reload helper's `credentials` request. A wish names no URL either."""
    named = re.compile(r"token|secret|password|access_(id|secret)|authorization|bearer|credential", re.I)

    def secrets(node) -> bool:
        """A key named like a secret, or a value shaped like a station's or a worker's token."""
        if isinstance(node, dict):
            return any(named.fullmatch(k) or secrets(v) for k, v in node.items())
        if isinstance(node, list):
            return any(secrets(v) for v in node)
        return isinstance(node, str) and bool(re.match(r"s[zw]1\.", node) or "not-a-real-secret" in node)
    carriers = []
    for name in cc.NAMES:
        for file, inst in c.vectors(name, "valid"):
            text = json.dumps(inst)
            if secrets(inst):
                carriers.append((name, file))
            if name == "capture-wish":
                assert not re.search(r"https?://|hub_url", text), file
    assert carriers == [("capture-socket-request", "credentials-from-the-root-helper.json")]
    hub = c.load("capture-wish")["properties"]["hub"]
    assert {"url", "token"} <= set(hub["propertyNames"]["not"]["enum"])
    assert "path" not in json.dumps(c.load("capture-socket-request")["$defs"]["args-pick"])
    for file in ("the-hubs-url.json", "the-stations-token.json"):
        assert c.errors("capture-wish", dict(c.vectors("capture-wish", "invalid"))[file]) != []


# -- 1. the spool ------------------------------------------------------------------------------------------------------

def test_a_segments_id_file_names_and_audio_size_follow_from_its_fields():
    for file, side in valid("capture-spool-sidecar").items():
        if side["kind"] == "segment":
            assert side["id"] == cc.segment_id(side["channel"]["label"], side["start"]), file
            if side["keyup"]:
                assert side["keyup"]["parent"] == cc.segment_id(side["channel"]["label"], side["keyup"]["first_start"])
        audio = side.get("audio")
        if audio:
            assert audio["file"] == cc.item_name(side["seq"], side["id"], ".wav"), file
            assert audio["bytes"] == cc.WAV_HEADER + 2 * audio["samples"] * audio["channels"], file
            assert audio["samples"] / audio["rate_hz"] <= side["end"] - side["start"] + 0.05, file
        if side.get("bursts_audio"):
            assert side["bursts_audio"] == cc.item_name(side["seq"], side["id"], ".mdc.wav"), file
        if side.get("tr"):
            assert side["tr"] == cc.item_name(side["seq"], side["id"], ".tr") and not side["tr"].endswith(".json")
        if side.get("audio_dropped"):
            assert side["audio"] is None and not any(i["emergency"] for i in side["ids"]), file
        if side.get("published_boot") is not None and "closed_boot" in side:
            assert side["published_boot"] >= side["closed_boot"]
    assert cc.segment_id("CH A", 1900000000.123) == "CHA-1900000000123"
    cursor = valid("capture-spool-cursor")["channels-in-parallel.json"]
    assert c.errors("capture-spool-cursor", dict(cursor, done=list(range(12346, 12346 + 256)))) == []
    assert len(c.errors("capture-spool-cursor", dict(cursor, done=list(range(12346, 12346 + 257))))) == 1
    assert len(cc.wav_s16(101600)) == 203244                                  # the design's own figures


SEG = {"v": 1, "kind": "segment", "published": 1900000006.52, "closed_boot": 5120.44, "boot": BOOT, "reason": "quiet",
       "levels": {"quieting_db": 31.2}, "ids": [], "alarm_key": "", "bursts_audio": None,
       "audio": {"format": "wav", "encoding": "s16le", "rate_hz": 16000, "channels": 1, "samples": 800}}


def seg(label: str, start: float, length: float = 0.05, **more) -> dict:
    return dict(copy.deepcopy(SEG), id=cc.segment_id(label, start), channel={"label": label, "kind": "traffic"},
                start=start, end=start + length, published=start + length + 0.05, **more)


def rules(found: list[str]) -> list[str]:
    return sorted({line.split(":", 1)[0] for line in found})


@pytest.fixture
def spool(tmp_path):
    sp = cc.Spool(tmp_path / "spool")
    sp.publish(seg("CH A", 1900000000.123), cc.wav_s16(800), bursts=cc.wav_s16(100))
    sp.publish(seg("PAGE 1", 1900000001.0), cc.wav_s16(800))
    sp.publish(seg("CH A", 1900000004.2), cc.wav_s16(800))
    return sp


def test_a_spool_written_in_order_keeps_every_rule(spool):
    assert cc.check_spool(spool.root) == []
    names = sorted(p.name for p in (spool.root / "items").iterdir())
    assert names == ["000000000001-CHA-1900000000123.json", "000000000001-CHA-1900000000123.mdc.wav",
                     "000000000001-CHA-1900000000123.wav", "000000000002-PAGE1-1900000001000.json",
                     "000000000002-PAGE1-1900000001000.wav", "000000000003-CHA-1900000004200.json",
                     "000000000003-CHA-1900000004200.wav"]
    assert (spool.root / "SEQ").read_text() == "3\n" and (spool.root / "EPOCH").read_text() == "3f9a1c0b7d2e4a61\n"
    assert not list((spool.root / "tmp").iterdir())
    for p in (spool.root / "items").glob("*.json"):                            # what a reader lists: sidecars only
        assert c.errors("capture-spool-sidecar", c.loads_strict(p.read_text())) == []
    # an item with no audio (a channel that is not recorded) and a metrics item are complete with their sidecar alone
    spool.publish(dict(seg("CH A", 1900000010.0), audio=None))
    spool.publish({"v": 1, "kind": "metrics", "id": "metrics-1900000260000", "published": 1900000320.0, "records": []})
    assert cc.check_spool(spool.root) == []


@pytest.mark.parametrize("stop,live,settled", [("tmp", [], ["tmp"]), ("audio", [], ["debris"]), ("sidecar", [], [])])
def test_a_writer_killed_at_any_point_leaves_no_half_item_a_reader_would_take(spool, stop, live, settled):
    """Atomicity (tmp + rename, audio first, the sidecar last): wherever the writer dies, a reader that lists `*.json`
    sees only complete items. What a crash leaves (a write under tmp/, audio with no sidecar) is debris that capture
    removes at its next start; the sequence number it used is never given out again."""
    before = sorted(p.name for p in (spool.root / "items").glob("*.json"))
    with pytest.raises(cc.Crash):
        spool.publish(seg("CH A", 1900000020.0), cc.wav_s16(800), stop=stop)
    after = sorted(p.name for p in (spool.root / "items").glob("*.json"))
    assert after == before + (["000000000004-CHA-1900000020000.json"] if stop == "sidecar" else [])
    assert rules(cc.check_spool(spool.root, settled=False)) == live            # while capture runs: nothing wrong
    assert rules(cc.check_spool(spool.root, settled=True)) == settled          # after a start it must be gone
    for p in (spool.root / "items").glob("*.json"):
        side = json.loads(p.read_text())
        assert (spool.root / "items" / side["audio"]["file"]).stat().st_size == side["audio"]["bytes"]
    again = cc.Spool(spool.root)                                               # capture starts again
    assert cc.check_spool(spool.root) == [] and (spool.root / "EPOCH").read_text() == "3f9a1c0b7d2e4a61\n"
    assert again.next == (4 if stop == "tmp" else 5)                           # max(SEQ, the highest in items/) + 1
    side = again.publish(seg("CH A", 1900000030.0), cc.wav_s16(800))
    assert side["seq"] == again.next - 1 and cc.check_spool(spool.root) == []


def test_seq_is_recovered_from_the_file_and_from_the_items(spool):
    assert cc.recover_seq(spool.root) == 4
    (spool.root / "SEQ").write_text("1\n")                                     # an old SEQ (a crash before its rewrite)
    assert cc.recover_seq(spool.root) == 4
    (spool.root / "SEQ").unlink()
    assert cc.recover_seq(spool.root) == 4
    (spool.root / "SEQ").write_text("41\n")                                    # items pruned behind the cursor
    assert cc.recover_seq(spool.root) == 42
    (spool.root / "SEQ").write_text("not a number")
    assert cc.recover_seq(spool.root) == 4


def break_spool(root: Path, how: str) -> None:
    items = root / "items"
    one, two, three = (items / n for n in ("000000000001-CHA-1900000000123.json", "000000000002-PAGE1-1900000001000.json",
                                           "000000000003-CHA-1900000004200.json"))

    def edit(path, **change):
        path.write_text(json.dumps(dict(json.loads(path.read_text()), **change)))
    if how == "sidecar before its audio":
        (items / "000000000002-PAGE1-1900000001000.wav").unlink()
    elif how == "audio cut short":
        (items / "000000000003-CHA-1900000004200.wav").write_bytes(cc.wav_s16(800)[:900])
    elif how == "audio empty":
        (items / "000000000003-CHA-1900000004200.wav").write_bytes(b"")
    elif how == "audio that is not what the sidecar says":
        (items / "000000000003-CHA-1900000004200.wav").write_bytes(cc.wav_s16(800, rate_hz=8000))
    elif how == "audio with no sidecar":
        (items / "000000000002-PAGE1-1900000001000.json").unlink()
    elif how == "a sidecar written in place":
        three.write_text(three.read_text()[:120])
    elif how == "a later seq that ended earlier on its channel":
        edit(three, end=1900000000.1)
    elif how == "the name and the sidecar disagree":
        edit(two, seq=7)
    elif how == "an id twice":
        edit(three, id="CHA-1900000000123")
    elif how == "an item of another spool":
        edit(two, spool_epoch="0000000000000000")
    elif how == "the bursts file gone":
        (items / "000000000001-CHA-1900000000123.mdc.wav").unlink()
    elif how == "a sidecar missing what a segment needs":
        doc = json.loads(one.read_text())
        del doc["alarm_key"]
        one.write_text(json.dumps(doc))
    elif how == "a file that is no item":
        (items / "notes.txt").write_text("x")
    elif how == "a second file under a complete item's seq":
        (items / "000000000002-PAGE1-1900000001000.flac").write_bytes(b"x")
    elif how == "no epoch":
        (root / "EPOCH").unlink()
    elif how == "no items folder":
        for p in items.iterdir():
            p.unlink()
        items.rmdir()
    elif how == "a cursor that counts a done item":
        (root / "cursor.app.json").write_text(json.dumps(
            {"v": 1, "spool_epoch": "3f9a1c0b7d2e4a61", "seq": 2, "done": [2]}))
    elif how == "a cursor that is not one":
        (root / "cursor.app.json").write_text(json.dumps({"v": 1, "seq": 2}))
    else:
        raise AssertionError(how)


@pytest.mark.parametrize("how,rule", [
    ("sidecar before its audio", "audio"), ("audio cut short", "audio-size"), ("audio empty", "audio-size"),
    ("audio that is not what the sidecar says", "audio-format"), ("audio with no sidecar", "debris"),
    ("a sidecar written in place", "sidecar"), ("a later seq that ended earlier on its channel", "order"),
    ("the name and the sidecar disagree", "name"), ("an id twice", "id"), ("an item of another spool", "epoch"),
    ("the bursts file gone", "audio"), ("a sidecar missing what a segment needs", "sidecar"),
    ("a file that is no item", "name"), ("a second file under a complete item's seq", "debris"), ("no epoch", "epoch"),
    ("no items folder", "layout"), ("a cursor that counts a done item", "cursor"), ("a cursor that is not one", "cursor")])
def test_the_checker_names_the_rule_a_spool_breaks(spool, how, rule):
    """Each fixture breaks one rule of contracts/capture/spool.md, and the reference check names that rule."""
    break_spool(spool.root, how)
    found = cc.check_spool(spool.root)
    assert rule in rules(found) and len(rules(found)) <= 2, found


def test_order_is_per_channel_and_nothing_is_promised_across_channels(tmp_path):
    sp = cc.Spool(tmp_path / "spool")
    sp.publish(seg("CH A", 1900000000.0, 30.0), cc.wav_s16(800))               # a long one on CH A ...
    sp.publish(seg("PAGE 1", 1900000002.0, 1.0), cc.wav_s16(800))             # ... ended after this one began and ended
    sp.publish(seg("PAGE 1", 1900000001.0, 0.5), cc.wav_s16(800))              # an earlier end under a later seq
    assert rules(cc.check_spool(sp.root)) == ["order"]
    assert "seq 3 on PAGE 1" in cc.check_spool(sp.root)[0]


def test_capture_deletes_behind_the_cursor_and_drops_audio_sidecar_first(spool):
    """Who deletes: capture only, what the reader's cursor says is done. At a bound the audio goes and the sidecar
    stays, rewritten first: at no moment does a sidecar name audio that is gone."""
    spool.drop_audio(2)
    assert cc.check_spool(spool.root) == []
    side = json.loads((spool.root / "items" / "000000000002-PAGE1-1900000001000.json").read_text())
    assert side["audio"] is None and side["audio_dropped"] is True and c.errors("capture-spool-sidecar", side) == []
    assert spool.prune({"v": 1, "spool_epoch": "0000000000000000", "seq": 3}) == []    # another spool's cursor: nothing
    cursor = {"v": 1, "spool_epoch": spool.epoch, "seq": 1, "done": [3], "tries": {"2": 1}, "at": 1900000010.0}
    (spool.root / "cursor.app.json").write_text(json.dumps(cursor))
    assert cc.check_spool(spool.root) == []
    assert spool.prune(cursor) == [1, 3]
    assert sorted(p.name for p in (spool.root / "items").iterdir()) == ["000000000002-PAGE1-1900000001000.json"]
    assert cc.check_spool(spool.root) == [] and cc.recover_seq(spool.root) == 4          # SEQ outlives the items


def test_late_is_the_larger_of_two_ages_and_never_gates_by_one_clock():
    """docs/native/capture-split.md 4.1 "Late" and 6 step 5: under 60 s is live, older is late; the larger of the
    boot-clock and the wall-clock age decides, so a clock that under-counts makes a row late, never stale traffic live."""
    side = {"end": 1900000006.473, "closed_boot": 5120.44, "boot": BOOT}

    def late(wall_age, boot_age, boot=BOOT):
        return cc.is_late(side, wall=side["end"] + wall_age, boottime=side["closed_boot"] + boot_age, boot_now=boot)
    assert not late(59, 59) and late(61, 61) and late(60, 60)
    assert not late(-3600 + 4, 4)              # the wall clock stepped back an hour: nothing changes
    assert late(3600 + 4, 4)                   # stepped forward: late (and an emergency in it still attaches)
    assert late(3600, 0.5)                     # the boot clock stood still for an hour: late by the wall clock
    assert late(4, 61)
    assert late(61, 0, OTHER_BOOT) and not late(4, 99999, OTHER_BOOT)          # across boots: the wall age alone
    assert late(-5, 0, OTHER_BOOT)                                             # ... and a negative one counts as old


# -- 2. the urgent lane ----------------------------------------------------------------------------------------------------

def test_an_items_age_is_on_the_boot_clock_and_after_a_reboot_on_the_wall_clock():
    """docs/native/capture-split.md 4.3 "Clocks": within a boot an alarm's age is its boot-clock age, never the larger
    of two; an item from before a reboot is sent all the same with the wall-clock age, clamped at 0."""
    at, at_boot = 1900000000.123, 5114.09
    assert cc.age(at, at_boot, BOOT, wall=at + 12.0, boottime=at_boot + 12.0, boot_now=BOOT) == (12.0, "boot")
    assert cc.age(at, at_boot, BOOT, wall=at + 3612.0, boottime=at_boot + 12.0, boot_now=BOOT) == (12.0, "boot")
    assert cc.age(at, at_boot, BOOT, wall=at - 3588.0, boottime=at_boot + 12.0, boot_now=BOOT) == (12.0, "boot")
    assert cc.age(at, at_boot, BOOT, wall=at + 412.6, boottime=31.0, boot_now=OTHER_BOOT) == (pytest.approx(412.6), "wall")
    assert cc.age(at, at_boot, BOOT, wall=at - 50.0, boottime=31.0, boot_now=OTHER_BOOT) == (0.0, "wall")
    for file in ("alarm-from-before-a-reboot.json", "alarm-sent-by-a-clock-that-stepped-back.json"):
        assert valid("capture-urgent-item")[file]["age_src"] == "wall"
    # a page item's 120 s is the larger of the two ages: here the safe side is "old"
    assert cc.larger_age(at, at_boot, BOOT, wall=at + 3600.0, boottime=at_boot + 5.0, boot_now=BOOT) == 3600.0
    assert cc.larger_age(at, at_boot, BOOT, wall=at - 3600.0, boottime=at_boot + 5.0, boot_now=BOOT) == 5.0


def test_the_keys_of_the_vectors_are_the_designs_forms():
    items = valid("capture-urgent-item")
    live, close = items["alarm-raised-live.json"], items["alarm-found-at-the-close.json"]
    label, start = live["data"]["source"], live["data"]["start"]
    assert live["key"] == f"{label.replace(' ', '')}-live-{int(start * 1000)}" and live["rev"] == 0
    assert close["key"] == cc.segment_id(label, start) \
        == valid("capture-spool-sidecar")["segment-with-an-emergency-id.json"]["id"]
    p25 = items["alarm-p25-live.json"]
    d = p25["data"]
    assert p25["key"] == f"p25-{d['system']}-{d['talkgroup']}-{d['radio_id']}-{int(p25['event_at'] * 1000)}"
    page = items["page-voice.json"]
    assert page["key"] == f"page:{page['data']['channel']}:{page['data']['started']:.2f}" and page["rev"] == 2
    assert items["page-first-pair.json"]["rev"] == 0 and items["page-first-pair.json"]["key"] == page["key"]
    for file, item in items.items():
        if item["kind"] == "alarm":
            assert item["rev"] == 0 and item["data"]["emergency"] is True, file
        assert item["at"] >= item["event_at"], file


def test_a_request_is_at_most_32_items_and_64_kib():
    req = valid("capture-urgent-request")["an-alarm-and-a-page.json"]
    alarm = req["items"][0]
    many = [dict(alarm, key=f"CHA-live-{1900000000123 + i}") for i in range(33)]
    assert c.errors("capture-urgent-request", dict(req, items=many[:32])) == []
    assert len(c.errors("capture-urgent-request", dict(req, items=many))) == 1
    assert len(json.dumps(dict(req, items=many[:32])).encode()) < cc.REQUEST_MAX_BYTES   # 32 alarms fit in the body cap
    for file, body in valid("capture-urgent-request").items():
        assert len(json.dumps(body).encode()) <= cc.REQUEST_MAX_BYTES, file
    hub = cc.Hub()
    assert hub.urgent(dict(req, items=many))[0] == 422 and hub.alarms == []
    fat = dict(req, items=[dict(alarm, data=dict(alarm["data"], unit="U" * 70000))])
    assert hub.urgent(fat) == (413, {"error": "body over 65536 bytes", "reason": "too_large"}) and hub.alarms == []
    for code, body in (hub.urgent(dict(req, items=many)), hub.urgent(fat)):
        assert c.errors("capture-urgent-response", body) == [] and code in (413, 422)


SCENARIOS = cc.data("urgent-scenarios")


def scenario_item(spec: dict) -> dict:
    item = copy.deepcopy(SCENARIOS["items"][spec["item"]])
    for k, v in spec.items():
        if k == "data":
            item["data"].update(v)
        elif k != "item":
            item[k] = v
    return item


def run(sc: dict, **params) -> cc.Hub:
    """One scenario of contracts/capture/urgent-scenarios.json on the reference model: every answer must be the
    table's, every 200 a valid answer, and what holds at the end the table's `then`."""
    p = SCENARIOS["params"]
    hub = cc.Hub(**{"max_age_s": p["max_age_s"], "p25_slack_s": p["p25_slack_s"], "repeat_s": p["repeat_s"],
                    **sc.get("hub", {}), **params})
    gen, last = hub.gen, -1.0
    for step in sc["steps"]:
        assert step["at"] > last, "steps are in the hub's time order"
        hub.now = last = step["at"]
        if "urgent" in step:
            body = {"v": 1, "station": step.get("station", "st-north"), "urgent_epoch": "9c2d7e10a4b35f68",
                    "sent_at": 1900000000.0 + step["at"], "items": [scenario_item(s) for s in step["urgent"]]}
            code, answer = hub.urgent(body)
            assert code == step.get("expect_http", 200), (sc["name"], step, answer)
            assert c.errors("capture-urgent-response", answer) == [], answer
            if code != 200:
                assert answer.get("reason") == step.get("reason") and step["expect"] == [], (sc["name"], step, answer)
                continue
            if not step.get("off_contract"):                   # (a body a conforming sender never makes)
                assert c.errors("capture-urgent-request", body) == [], (sc["name"], step)
            assert [r["status"] for r in answer["results"]] == step["expect"], (sc["name"], step, answer)
            assert [(r["key"], r["rev"]) for r in answer["results"]] == [(i["key"], i["rev"]) for i in body["items"]]
            if "reasons" in step:
                assert [r.get("reason") for r in answer["results"]] == step["reasons"]
            assert (answer["gen"] != gen) == step.get("gen_changed", False), (sc["name"], step)
            gen = answer["gen"]
        elif "event" in step:
            assert hub.event(step.get("station", "st-north"), step["event"]) == 200
        elif "ack" in step:
            hub.ack(step["ack"])
        elif "state_paging" in step:
            hub.state("st-north", step["state_paging"])
        elif step.get("hub") in ("handoff", "lead"):
            getattr(hub, step["hub"])()
        else:
            hub.restore(step["hub"]["restore_s"])
    return hub


def holds(hub: cc.Hub, then: dict) -> None:
    got = {"alarms": len(hub.alarms), "pushes": len(hub.pushes), "notices": len(hub.notices), "pages": len(hub.pages),
           "traffic_ids": [a["traffic_id"] for a in hub.alarms],
           "push_age_s": hub.pushes[0]["age_s"] if hub.pushes else None}
    for key, want in then.items():
        if key == "page":
            page = hub.pages[("st-north", want["key"])]
            flat = {"key": want["key"], "rev": page["rev"], "phase": page["data"].get("phase"), "ended": page["ended"]}
            assert {k: flat[k] for k in want} == want
        else:
            assert got[key] == want, (key, got)


@pytest.mark.parametrize("sc", SCENARIOS["scenarios"], ids=lambda sc: sc["name"][:60])
def test_the_hubs_idempotency_table(sc):
    holds(run(sc), sc["then"])


def test_the_table_covers_the_designs_rows_and_says_what_is_not_decided():
    names = " | ".join(sc["design"] for sc in SCENARIOS["scenarios"])
    for row in range(1, 16):                                   # the 15 rows of "Idempotency, on the hub"
        assert re.search(rf"rows? (\d+, )*{row}\b", names), f"no scenario for row {row} of the idempotency table"
    assert "H1k" in names and "handoff" in names
    assert SCENARIOS["params"]["max_age_s"] == 1800 and SCENARIOS["params"]["max_age_s_decided"] is False
    assert "NOT DECIDED" in SCENARIOS["description"] and "A7" in README
    for sc in SCENARIOS["scenarios"]:
        assert set(sc) <= {"name", "design", "steps", "then", "hub", "depends_on"}, sc["name"]
        for step in sc["steps"]:
            assert len(set(step) & {"urgent", "event", "ack", "state_paging", "hub"}) == 1, step
    for kind in ("alarm", "p25", "page", "future"):
        assert c.errors("capture-urgent-item", SCENARIOS["items"][kind]) == []


def test_the_age_limit_is_a_parameter_until_the_admin_decides():
    """A7 is open: what a late emergency becomes on the hub. 30 min is the recommended rule, not a decision, so the
    limit is a parameter of the model and of the table. The scenarios that depend on it change with it, and no other
    scenario does."""
    depends = [sc for sc in SCENARIOS["scenarios"] if sc.get("depends_on") == "max_age_s"]
    assert len(depends) == 3
    late = next(sc for sc in depends if sc["name"].startswith("late, under"))
    older = next(sc for sc in depends if sc["name"].startswith("older than"))
    holds(run(late), late["then"])
    with pytest.raises(AssertionError):
        run(late, max_age_s=900.0)                             # with a 15 min limit, 29 min is stale
    with pytest.raises(AssertionError):
        run(older, max_age_s=3600.0)                           # with a 60 min limit, 31 min is still an alarm
    hub = cc.Hub(max_age_s=3600.0)
    hub.now = 1860.0
    assert hub.urgent({"v": 1, "station": "st-north", "items": [scenario_item({"item": "alarm", "age_s": 1860.0})]})[1][
        "results"][0]["status"] == "applied" and hub.pushes[0]["age_s"] == 1860.0
    for sc in SCENARIOS["scenarios"]:
        if "depends_on" not in sc:
            for limit in (900.0, 3600.0):
                holds(run(sc, max_age_s=limit), sc["then"])


# What can go wrong in the lane (docs/native/capture-split.md 4.3), row by row, on the two reference models together.

ALARM_DATA = SCENARIOS["items"]["alarm"]["data"]
KEY = SCENARIOS["items"]["alarm"]["key"]


def send(lane: cc.Lane, hub: cc.Hub, kind: str = "alarm", *, wall: float, boottime: float, lost: bool = False):
    """One request of capture's sender, and the hub's answer taken (lost: the answer never arrives)."""
    body = lane.request(kind, wall=wall, boottime=boottime)
    if body is None:
        return None
    assert c.errors("capture-urgent-request", body) == [], c.errors("capture-urgent-request", body)
    code, answer = hub.urgent(body)
    lane.answer(0 if lost else code, None if lost else answer, boottime=boottime)
    return code, answer


def statuses(got) -> list[str]:
    return [r["status"] for r in got[1]["results"]]


def test_lane_capture_killed_between_the_decode_and_the_commit():
    """Row 1. If the request had not gone out either, the alarm is lost: it existed only in memory. Stated, not hidden."""
    lane, hub = cc.Lane(), cc.Hub()
    lane.disk = "stalled"                                      # the commit has not returned
    lane.press(KEY, "alarm", ALARM_DATA, wall=1900000000.39, boottime=5114.36)
    assert lane.unsent() == [KEY] and lane.rows == {}
    lane.crash()
    lane.disk = "ok"
    lane.start(wall=1900000005.4, boottime=5119.4)
    assert lane.request("alarm", wall=1900000005.4, boottime=5119.4) is None and hub.alarms == []


def test_lane_capture_killed_after_the_request_went_out_before_the_commit():
    """Row 2. The hub has it; capture does not know. Nothing is sent twice and the alarm stands alone."""
    lane, hub = cc.Lane(), cc.Hub()
    lane.disk = "stalled"
    lane.press(KEY, "alarm", ALARM_DATA, wall=1900000000.39, boottime=5114.36)
    hub.now = 0.4
    assert statuses(send(lane, hub, wall=1900000000.44, boottime=5114.41, lost=True)) == ["applied"]
    lane.crash()
    lane.disk = "ok"
    lane.start(wall=1900000005.4, boottime=5119.4)
    assert lane.request("alarm", wall=1900000005.4, boottime=5119.4) is None
    assert len(hub.alarms) == len(hub.pushes) == 1 and hub.alarms[0]["traffic_id"] == KEY


def test_lane_the_disk_is_full_and_the_item_is_sent_from_memory():
    """Row 3. A failed insert never stops the send: the item is `durable` false, in the frame to the app and in the
    request alike. It is lost only if capture also dies before the hub answers. The app takes it over once, and when
    its row appears the second frame only moves the cursor."""
    lane, hub = cc.Lane(), cc.Hub()
    lane.disk = "full"
    lane.press(KEY, "alarm", ALARM_DATA, wall=1900000000.39, boottime=5114.36)
    frame = lane.frames()[0]
    assert (frame["n"], frame["durable"]) == (None, False)
    head = {"v": 1, "t": "urgent", "urgent_epoch": lane.epoch, **frame}
    assert c.errors("capture-socket-event", head) == []
    hub.now = 0.4
    assert statuses(send(lane, hub, wall=1900000000.44, boottime=5114.41)) == ["applied"]
    assert lane.unsent() == [] and lane.rows == {}             # answered for, though never written
    taken = {(frame["key"], frame["rev"])}                     # the app's set of (key, rev) it has taken
    lane.disk = "ok"
    lane.tick()                                                # the writer tries again: the row appears, already sent
    row = lane.rows[(KEY, 0)]
    assert (row["state"], row["hub"], row["n"]) == ("sent", "applied", 1)
    second = lane.frames()[0]
    assert (second["n"], second["durable"]) == (1, True) and (second["key"], second["rev"]) in taken
    assert c.errors("capture-socket-event", {"v": 1, "t": "urgent", "urgent_epoch": lane.epoch, **second}) == []
    # ... and the other way: full, sent, then killed before any row: gone from capture, but the hub has it
    lane2, hub2 = cc.Lane(), cc.Hub()
    lane2.disk = "full"
    lane2.press(KEY, "alarm", ALARM_DATA, wall=1900000000.39, boottime=5114.36)
    lane2.crash()
    lane2.start(wall=1900000005.4, boottime=5119.4)
    assert lane2.request("alarm", wall=1900000005.4, boottime=5119.4) is None and hub2.alarms == []


def test_lane_an_fsync_stalls_and_the_row_follows_the_answer():
    """Row 4. The send goes without the row; a commit that lands after the hub's answer is written as sent, never
    left `new`: no second send, no false "not delivered"."""
    lane, hub = cc.Lane(), cc.Hub()
    lane.disk = "stalled"
    lane.press(KEY, "alarm", ALARM_DATA, wall=1900000000.39, boottime=5114.36)
    hub.now = 0.45
    assert statuses(send(lane, hub, wall=1900000000.44, boottime=5114.41)) == ["applied"]
    lane.disk = "ok"
    lane.tick()
    assert lane.rows[(KEY, 0)]["state"] == "sent" and lane.unsent() == []
    assert lane.request("alarm", wall=1900000001.0, boottime=5115.0) is None
    lane.crash()
    lane.start(wall=1900000005.4, boottime=5119.4)             # a restart loads `new` rows only: this one is not
    assert lane.request("alarm", wall=1900000005.4, boottime=5119.4) is None and len(hub.pushes) == 1


def test_lane_the_app_is_down_and_takes_over_later():
    """Row 5. Nothing changes for the send. The take-over waits, and makes no second alarm when it comes."""
    lane, hub = cc.Lane(), cc.Hub()
    lane.press(KEY, "alarm", ALARM_DATA, wall=1900000000.39, boottime=5114.36)
    hub.now = 0.4
    assert statuses(send(lane, hub, wall=1900000000.44, boottime=5114.41)) == ["applied"]
    hub.now = 40.0                                         # the app is back: its take-over event, then the transmission
    assert hub.event("st-north", {"type": "alarm", "id": KEY, "radio_id": "7001", "source": "CH A", "start": 0.0,
                                  "end": 0.3}) == 200
    assert hub.event("st-north", {"type": "traffic", "id": "CHA-1900000000123", "alarm_key": KEY, "emergency": True,
                                  "radio_id": "7001", "source": "CH A", "start": 0.0, "end": 6.4}) == 200
    assert len(hub.alarms) == len(hub.pushes) == 1 and hub.alarms[0]["traffic_id"] == "CHA-1900000000123"


def test_lane_the_hub_answers_applied_then_comes_back_from_an_older_replica():
    """Row 6. The `gen` rule: every answer carries the hub's generation; when it changes, capture sends again every
    alarm it marked sent in the last 120 s, and no older one."""
    lane, hub = cc.Lane(), cc.Hub()
    lane.press("CHA-live-1899999000000", "alarm", dict(ALARM_DATA, radio_id="7009"), wall=1899999000.0, boottime=4114.0)
    hub.now = -1000.0
    assert statuses(send(lane, hub, wall=1899999000.1, boottime=4114.1)) == ["applied"]
    lane.press(KEY, "alarm", ALARM_DATA, wall=1900000000.39, boottime=5114.36)
    hub.now = 0.4
    assert statuses(send(lane, hub, wall=1900000000.44, boottime=5114.41)) == ["applied"]
    hub.now = 10.0
    hub.restore(30.0)                                          # the alarm of 10 s ago is gone from the hub
    assert [a["traffic_id"] for a in hub.alarms] == ["CHA-live-1899999000000"]
    assert lane.request("alarm", wall=1900000010.0, boottime=5124.0) is None           # capture has nothing to send ...
    lane.press("page:PAGE 1:1900000010.00", "page", SCENARIOS["items"]["page"]["data"], wall=1900000010.0, boottime=5124.0)
    assert statuses(send(lane, hub, "page", wall=1900000010.1, boottime=5124.1)) == ["applied"]    # ... until an answer
    assert lane.unsent() == [KEY]                              # says the generation changed: the last 120 s again
    hub.now = 10.5
    got = send(lane, hub, wall=1900000010.5, boottime=5124.5)
    assert statuses(got) == ["applied"] and got[1]["results"][0]["key"] == KEY
    assert sorted(a["traffic_id"] for a in hub.alarms) == ["CHA-live-1899999000000", KEY]
    assert lane.request("alarm", wall=1900000011.0, boottime=5125.0) is None


@pytest.mark.parametrize("minutes,status,alarms,pushes,notices", [(29, "applied", 1, 1, 0), (31, "stale", 0, 0, 1)])
def test_lane_the_uplink_is_down_for_hours(minutes, status, alarms, pushes, notices):
    """Row 7. The alarm is not given up: it stays `new`, is retried, and shows as not delivered. When the hub
    answers, A7's recommended rule (a parameter, not decided): under 30 min an alarm and a push with its age, older
    a late notice and no push. Either answer makes it sent."""
    lane, hub = cc.Lane(), cc.Hub(max_age_s=SCENARIOS["params"]["max_age_s"])
    lane.press(KEY, "alarm", ALARM_DATA, wall=1900000000.39, boottime=5114.36)
    for t in range(15, minutes * 60, 15):                      # every 15 s: no answer
        body = lane.request("alarm", wall=1900000000.39 + t, boottime=5114.36 + t)
        assert body["items"][0]["key"] == KEY and body["items"][0]["age_s"] == pytest.approx(t, abs=0.01)
        lane.answer(0, None, boottime=5114.36 + t)
        assert lane.unsent() == [KEY] and lane.rows[(KEY, 0)]["state"] == "new"
    t = minutes * 60
    hub.now = float(t)
    got = send(lane, hub, wall=1900000000.39 + t, boottime=5114.36 + t)
    assert statuses(got) == [status] and lane.unsent() == [] and lane.rows[(KEY, 0)]["state"] == "sent"
    assert (len(hub.alarms), len(hub.pushes), len(hub.notices)) == (alarms, pushes, notices)
    if pushes:
        assert hub.pushes[0]["age_s"] == pytest.approx(t) and hub.alarms[0]["at"] == pytest.approx(0.0)


@pytest.mark.parametrize("step", [3600.0, -3600.0])
def test_lane_the_stations_wall_clock_steps(step):
    """Row 8. Nothing: an alarm's age is on the boot clock, and the hub places it at its own now minus that age."""
    lane, hub = cc.Lane(), cc.Hub()
    lane.press(KEY, "alarm", ALARM_DATA, wall=1900000000.39, boottime=5114.36)
    body = lane.request("alarm", wall=1900000000.89 + step, boottime=5114.86)
    assert (body["items"][0]["age_s"], body["items"][0]["age_src"]) == (0.5, "boot")
    hub.now = 0.9
    code, answer = hub.urgent(body)
    assert statuses((code, answer)) == ["applied"] and hub.alarms[0]["at"] == pytest.approx(0.4)
    assert hub.pushes[0]["age_s"] == 0.5


def test_lane_an_alarm_from_before_a_reboot_is_sent_with_its_wall_clock_age():
    """4.3 "Clocks", an item sent after a reboot: sent all the same, `age_src` `wall`, never held back."""
    lane, hub = cc.Lane(boot=BOOT), cc.Hub()
    lane.press(KEY, "alarm", ALARM_DATA, wall=1900000000.39, boottime=5114.36)
    lane.crash()
    lane.start(wall=1900000400.0, boottime=12.0, boot=OTHER_BOOT)
    body = lane.request("alarm", wall=1900000413.0, boottime=25.0)
    assert (body["items"][0]["age_s"], body["items"][0]["age_src"]) == (pytest.approx(412.61), "wall")
    hub.now = 413.0
    assert statuses(hub.urgent(body)) == ["applied"] and hub.pushes[0]["age_s"] == pytest.approx(412.61)
    lane.crash()                                             # and with the wall clock behind the event: 0, not negative
    lane.start(wall=1899999000.0, boottime=40.0, boot="a-third-boot")
    assert lane.request("alarm", wall=1899999000.0, boottime=40.0)["items"][0]["age_s"] == 0.0


def test_lane_two_owners_cannot_both_hold_the_lock(tmp_path):
    """Row 9. `radio.lock` is a lifetime flock: a second capture, or capture beside an inline app, does not get it,
    and gets it the moment the holder is gone."""
    path = tmp_path / "radio.lock"
    first, second = open(path, "w"), open(path, "w")
    try:
        fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
        first.close()                                          # the holder exits: nothing to clean up
        fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        first.close()
        second.close()


def test_a_page_item_expires_and_only_its_highest_rev_is_sent_while_an_alarm_never_expires():
    lane, hub = cc.Lane(), cc.Hub()
    page = SCENARIOS["items"]["page"]
    for rev in (0, 1, 2):
        lane.press(page["key"], "page", dict(page["data"], phase="voice" if rev == 2 else "tones"), rev=rev,
                   wall=1900000101.9 + rev, boottime=5216.0 + rev, event_at=1900000100.25, event_boot=5214.35)
    body = lane.request("page", wall=1900000104.9, boottime=5219.0)
    assert [(i["key"], i["rev"]) for i in body["items"]] == [(page["key"], 2)]
    assert [lane.rows[(page["key"], r)]["state"] for r in (0, 1, 2)] == ["expired", "expired", "new"]
    assert body["items"][0]["age_s"] == pytest.approx(4.65)
    lane.answer(0, None, boottime=5219.0)                      # no answer; 119 s after the event it is still sent ...
    assert lane.request("page", wall=1900000219.2, boottime=5333.3) is not None
    assert lane.request("page", wall=1900000220.3, boottime=5334.4) is None           # ... at 120 s it is not
    assert lane.rows[(page["key"], 2)]["state"] == "expired"
    # the larger of two ages: a wall clock stepped forward expires a page item (the safe side is "old") ...
    lane.press("page:PAGE 1:1900000300.00", "page", page["data"], wall=1900000300.0, boottime=5414.0)
    assert lane.request("page", wall=1900000300.5 + 3600.0, boottime=5414.5) is None
    # ... and never an alarm, by any clock
    lane.press(KEY, "alarm", ALARM_DATA, wall=1900000000.39, boottime=5114.36)
    body = lane.request("alarm", wall=1900000000.39 + 90000.0 + 3600.0, boottime=5114.36 + 90000.0)
    assert body["items"][0]["age_s"] == pytest.approx(90000.0) and c.errors("capture-urgent-request", body) == []
    hub.now = 90000.0
    assert statuses(hub.urgent(body)) == ["stale"]             # clamped to 24 h on the hub, and past any limit


def test_an_answer_makes_an_item_sent_only_when_it_names_it():
    """4.3 "The sender": sent is a 200 whose results name the item applied, duplicate or stale. Nothing else counts."""
    def lane_with(n=2):
        lane = cc.Lane()
        for i in range(n):
            lane.press(f"CHA-live-{1900000000123 + i}", "alarm", ALARM_DATA, wall=1900000000.39 + i, boottime=5114.36 + i)
        return lane
    ok = {"ok": True, "v": 1, "station": "st-north", "gen": 5}
    for code, body, sent in (
            (200, dict(ok, results=[{"key": "CHA-live-1900000000123", "rev": 0, "status": "applied"}]), 1),
            (200, dict(ok, results=[]), 0), (200, dict(ok), 0), (204, None, 0), (0, None, 0),
            (503, {"error": "updating", "updating": True}, 0), (401, {"error": "no", "reason": "token_revoked"}, 0),
            (200, dict(ok, results=[{"key": "CHA-live-1900000000123", "rev": 0, "status": "from_the_future"}]), 0),
            (200, dict(ok, results=[{"key": "CHA-live-1900000000123", "rev": 1, "status": "applied"}]), 0),
            (200, dict(ok, results=[{"key": "CHA-live-1900000000123", "rev": 0, "status": "duplicate"},
                                    {"key": "CHA-live-1900000000124", "rev": 0, "status": "stale"}]), 2)):
        lane = lane_with()
        assert len(lane.request("alarm", wall=1900000002.0, boottime=5116.0)["items"]) == 2
        lane.answer(code, body, boottime=5116.1)
        assert 2 - len(lane.unsent()) == sent, (code, body)
    lane = lane_with(1)
    lane.answer(200, dict(ok, results=[{"key": "CHA-live-1900000000123", "rev": 0, "status": "rejected",
                                        "reason": "kind"}]), boottime=5116.1)
    assert lane.rows[("CHA-live-1900000000123", 0)]["state"] == "rejected" and lane.unsent() == []
    assert lane.request("alarm", wall=1900000003.0, boottime=5117.0) is None           # never sent again
    lane = lane_with(40)                                       # at most 32 items a request, the oldest first
    body = lane.request("alarm", wall=1900000050.0, boottime=5164.0)
    assert len(body["items"]) == 32 and body["items"][0]["key"] == "CHA-live-1900000000123"


def test_new_rows_are_loaded_at_start_oldest_alarm_first_and_a_page_only_if_fresh():
    lane = cc.Lane()
    page = SCENARIOS["items"]["page"]
    lane.press(page["key"], "page", page["data"], wall=1900000000.0, boottime=5114.0)
    lane.press("CHA-live-1900000001000", "alarm", ALARM_DATA, wall=1900000001.0, boottime=5115.0)
    lane.press("page:PAGE 1:1900000090.00", "page", page["data"], wall=1900000090.0, boottime=5204.0)
    lane.press("CHA-live-1900000002000", "alarm", ALARM_DATA, wall=1900000002.0, boottime=5116.0)
    lane.crash()
    lane.start(wall=1900000130.0, boottime=5244.0)
    assert list(lane.memory) == [("CHA-live-1900000001000", 0), ("CHA-live-1900000002000", 0),
                                 ("page:PAGE 1:1900000090.00", 0)]
    assert lane.rows[(page["key"], 0)]["state"] == "expired"   # 130 s old: live state, not sent
    assert [f["durable"] for f in lane.frames()] == [True, True, True]


def test_a_p25_emergency_bit_alarms_on_a_transition_only():
    """#775 (Q-B), capture's side: one alarm per press; a stuck bit never re-alarms; a restart of capture does not
    re-alarm a bit that was already set; a counter of transitions an hour."""
    bits = cc.P25Bits()
    assert bits.seen("examplesys", "7001", 1000.0) is True
    assert [bits.seen("examplesys", "7001", 1000.0 + t) for t in (2, 30, 300, 890, 1480)] == [False] * 5    # still set
    assert bits.seen("examplesys", "7002", 1010.0) is True                             # another radio
    assert bits.seen("othersys", "7001", 1020.0) is True                               # the same id on another system
    restarted = cc.P25Bits()
    restarted.last_set = dict(bits.last_set)                                        # the p25_bit table outlives capture
    assert restarted.seen("examplesys", "7001", 2500.0) is False
    assert restarted.seen("examplesys", "7001", 2500.0 + 601.0) is True           # not set for over 10 min: a new press
    assert bits.per_hour(2500.0) == 3 and bits.per_hour(1000.0 + 3601.0) == 2


def test_the_urgent_file_is_the_designs_ddl_and_keeps_its_promises(tmp_path):
    """contracts/capture/urgent.sql is the DDL of docs/native/capture-split.md 4.3, byte for byte (compared with the
    document once it is in the tree). It runs; (key, rev) is the row's identity; a row's `data` is the item's."""
    ddl = (cc.CAPTURE / "urgent.sql").read_text(encoding="utf-8")
    if DESIGN.exists():
        assert f"```sql\n{ddl}```" in DESIGN.read_text(encoding="utf-8")
    db = sqlite3.connect(tmp_path / "urgent.sqlite")
    try:
        db.executescript(ddl)
        db.executescript(ddl)                                                          # IF NOT EXISTS: a second start
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2                     # FULL
        cols = [r[1] for r in db.execute("PRAGMA table_info(urgent)")]
        assert cols == ["n", "key", "rev", "kind", "at", "event_at", "event_boot", "boot", "data", "state", "sent_at",
                        "hub", "hub_gen", "tries", "last_error"]
        held = valid("capture-socket-event")["urgent-alarm.json"]
        row = (held["key"], held["rev"], held["kind"], held["at"], held["event_at"], held["event_boot"], held["boot"],
               json.dumps(held["data"]))
        insert = ("INSERT OR IGNORE INTO urgent (key, rev, kind, at, event_at, event_boot, boot, data) "
                  "VALUES (?,?,?,?,?,?,?,?)")
        assert db.execute(insert, row).rowcount == 1 and db.execute(insert, row).rowcount == 0     # (key, rev) once
        assert db.execute(insert, (row[0], 1, *row[2:])).rowcount == 1                 # another rev: another row
        n, state, data_ = db.execute("SELECT n, state, data FROM urgent WHERE key = ? AND rev = 0",
                                     (held["key"],)).fetchone()
        assert (n, state) == (1, "new") and json.loads(data_) == held["data"]
        back = dict(zip(("n", "key", "rev", "kind", "at", "event_at", "event_boot", "boot"), db.execute(
            "SELECT n, key, rev, kind, at, event_at, event_boot, boot FROM urgent WHERE n = 1").fetchone()),
            data=json.loads(data_), durable=True)
        assert c.subschema("capture-urgent-item", "#/$defs/held").is_valid(back)
        db.execute("INSERT INTO meta VALUES ('v', '1'), ('urgent_epoch', '9c2d7e10a4b35f68')")
        db.execute("INSERT INTO p25_bit VALUES ('examplesys', '7001', 1900000300.0)")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO p25_bit VALUES ('examplesys', '7001', 1900000301.0)")
    finally:
        db.close()
    held_keys = set(c.load("capture-urgent-item")["$defs"]["held"]["properties"])
    assert held_keys - {"durable"} <= set(cols)                                     # the held item is the row's columns


# -- 3. the socket ---------------------------------------------------------------------------------------------------------

FRAMES = cc.data("socket-frames")


def test_the_framing_is_the_stt_encoders_and_the_byte_vectors_decode():
    """docs/native/capture-split.md 4.2: "Framing is #709's". The byte vectors are what today's writer of that framing
    (sizeup/cloud/sttencode.py) produces and what its reader takes."""
    from sizeup.cloud import sttencode
    assert FRAMES["limits"] == {"header_max_bytes": cc.MAX_HEADER, "payload_max_bytes": cc.MAX_PAYLOAD}
    assert cc.MAX_HEADER == sttencode.MAX_HEADER and cc.MAX_PAYLOAD <= sttencode.MAX_FRAME
    assert len(FRAMES["frames"]) >= 5
    for f in FRAMES["frames"]:
        raw, payload = bytes.fromhex(f["hex"]), bytes.fromhex(f["payload_hex"])
        assert cc.frame(f["header"], payload) == raw, f["name"]
        assert cc.unframe(raw) == (f["header"], payload, b""), f["name"]
        out = io.BytesIO()
        if json.dumps(f["header"]).isascii() and "\\u" not in json.dumps(f["header"]):
            sttencode.write_frame(out, f["header"], payload)                           # (it escapes what is not ASCII)
            assert out.getvalue() == raw, f["name"]
        assert sttencode.read_frame(io.BytesIO(raw)) == (f["header"], payload), f["name"]
        assert int.from_bytes(raw[:4], "big") == len(raw) - 8 - len(payload)
        assert int.from_bytes(raw[4:8], "big") == len(payload)
    assert any(f["payload_hex"] for f in FRAMES["frames"]) and any(not f["hex"].isascii() or "e2809c" in f["hex"]
                                                                   for f in FRAMES["frames"])


def test_a_stream_is_cut_anywhere_and_a_bad_frame_closes_the_connection():
    for s in FRAMES["streams"]:
        buf, seen = b"", []
        for chunk, want in zip(s["chunks_hex"], s["frames_after_each"], strict=True):
            got, buf = cc.unframe_all(buf + bytes.fromhex(chunk))
            seen += got
            assert len(seen) == want, s["name"]
        assert buf == b"" and [h["t"] for h, _ in seen] == ["hello", "poke"]
    for bad in FRAMES["refused"]:
        with pytest.raises(cc.FrameError) as e:
            cc.unframe(bytes.fromhex(bad["hex"]))
        assert str(e.value) == bad["why"], bad["name"]
    assert cc.unframe(b"") is None and cc.unframe(bytes.fromhex(FRAMES["frames"][0]["hex"])[:-1]) is None
    with pytest.raises(cc.FrameError):
        cc.frame({"x": "y" * cc.MAX_HEADER})


def test_every_frame_vector_travels_in_a_frame_and_a_connection_starts_as_the_design_says():
    """Every header of the two socket schemas fits the framing; and the first exchange: capture's `hello` (versions,
    the two epochs, the boot id), the client's `hello` (its role), the snapshot."""
    for name in ("capture-socket-event", "capture-socket-request", "capture-socket-reply"):
        for file, head in valid(name).items():
            assert cc.unframe(cc.frame(head)) == (head, b"", b""), (name, file)
    ev, ask = valid("capture-socket-event"), valid("capture-socket-request")
    hello = ev["hello.json"]
    assert {"contract", "impl", "build", "pid", "started", "spool_epoch", "urgent_epoch", "boot"} <= set(hello)
    wire = cc.frame(hello) + cc.frame(ev["snapshot.json"])
    assert [h["t"] for h, _ in cc.unframe_all(wire)[0]] == ["hello", "snapshot"]
    assert ask["hello-reader.json"]["role"] == "reader" and ask["hello-reader.json"]["contract"] == hello["contract"] == 1
    assert hello["spool_epoch"] == valid("capture-spool-sidecar")["segment-with-an-emergency-id.json"]["spool_epoch"]
    assert hello["urgent_epoch"] == valid("capture-urgent-request")["an-alarm-and-a-page.json"]["urgent_epoch"]
    frames = c.load("capture-socket-event")
    assert frames["properties"]["t"]["x-known"] == ["hello", "snapshot", "carrier", "ids", "urgent", "reply", "talking",
                                                    "paging", "ours", "grant", "spool", "status", "level"]
    assert {v["t"] for v in ev.values()} >= set(frames["properties"]["t"]["x-known"])  # a vector for every frame
    ops = c.load("capture-socket-request")["$defs"]["request"]["properties"]["op"]["x-known"]
    assert ops == ["pick", "squelch_apply", "reception_start", "reception_stop", "probe", "airband_ab", "check",
                   "urgent_since", "credentials", "snapshot"]
    assert {v.get("op") for v in ask.values() if v["t"] == "request"} >= set(ops)      # ... and for every request
    assert {v["t"] for v in ask.values()} >= {"hello", "poke", "cursor", "urgent_taken", "paging_finished", "request"}


def test_an_urgent_frame_is_the_item_whole_and_urgent_since_pages_it_with_the_memory():
    ev = valid("capture-socket-event")
    durable, not_yet = ev["urgent-alarm.json"], ev["urgent-alarm-not-durable.json"]
    assert (durable["n"], durable["durable"], not_yet["n"], not_yet["durable"]) == (57, True, None, False)
    assert (durable["key"], durable["rev"]) == (not_yet["key"], not_yet["rev"])        # identity is (key, rev), never n
    item = valid("capture-urgent-item")["alarm-raised-live.json"]
    assert {k: durable[k] for k in ("key", "rev", "kind", "at", "event_at", "data")} == \
        {k: item[k] for k in ("key", "rev", "kind", "at", "event_at", "data")}
    reply = valid("capture-socket-reply")["urgent-since-page.json"]
    since = c.subschema("capture-socket-reply", "#/$defs/urgent-since-result")
    assert list(since.iter_errors(reply["result"])) == []
    result = reply["result"]
    assert all(r["durable"] and r["n"] > 56 for r in result["rows"]) and result["more"] is False
    assert all(m["n"] is None and not m["durable"] for m in result["memory"]) and result["memory"]
    for bad in ({k: v for k, v in result.items() if k != "memory"}, dict(result, rows=[{"n": 57}]),
                dict(result, more="no")):
        assert list(since.iter_errors(bad)) != []
    ask = valid("capture-socket-request")["urgent-since.json"]
    assert ask["id"] == reply["id"] and ask["args"]["n"] == 56 and ask["args"]["urgent_epoch"] == result["urgent_epoch"]
    limit = c.load("capture-socket-request")["$defs"]["args-urgent_since"]["properties"]["limit"]
    assert (limit["default"], limit["maximum"]) == (200, 500)
    snap = c.subschema("capture-socket-event", "#/$defs/snapshot")                     # the `snapshot` request's result
    assert snap.is_valid({k: v for k, v in ev["snapshot.json"].items() if k not in ("v", "t", "at")})


def apply_pick(op, args):
    if op == "pick":
        if args.get("job") == "PAGE 1" and not args.get("confirm"):
            return False, "PAGE 1 is a fire job: move it off sdr-a anyway?", True, None
        return (False, "refused: parked", False, None) if args.get("sdr") == "sdr-b" else (True, "", False, None)
    if op == "squelch_apply":
        running = {"open_db": 11.0, "close_db": 7.0}           # (someone else applied a value in between)
        return (False, "stale", False, None) if args.get("expect", running) != running else (True, "", False, None)
    if op == "probe":
        return True, "", False, "audio"
    return None


def test_a_request_is_applied_once_by_its_id_and_never_after_its_deadline():
    """docs/native/capture-split.md 4.2 "Rule": a repeated id gets the first reply again and changes nothing; a request
    past `expires_boot` is answered `expired` and not applied, whatever the wall clock did; an unknown op is
    `unsupported`; a viewer is `forbidden`; a refusal that needs the person's yes is asked again under a new id."""
    ask = valid("capture-socket-request")
    rq = cc.Requests(apply_pick)

    def go(req, boottime=5329.0, wall=1900000200.0, role="reader", uid=1000):
        reply = rq.handle(req, role=role, uid=uid, boottime=boottime, wall=wall)
        assert c.errors("capture-socket-reply", reply) == [], reply
        assert c.errors("capture-socket-event", reply) == [] and reply["id"] == req["id"]
        return reply
    pick = ask["pick.json"]
    first = go(pick)
    assert (first["ok"], first["error"], first["confirm"]) == (False, "refused: parked", False)
    assert first == dict(valid("capture-socket-reply")["refused-parked.json"], at=first["at"])
    ok = dict(pick, id="r-1", args={"sdr": "sdr-c", "job": "CH A"})
    assert go(ok)["ok"] is True and rq.applied == [("r-1", "pick")]
    assert go(ok, boottime=5329.5)["ok"] is True and rq.applied == [("r-1", "pick")]  # sent again: answered, not applied
    assert go(ok, boottime=5400.0)["ok"] is True and len(rq.applied) == 1              # ... even after its deadline
    late = dict(ok, id="r-2")
    assert go(late, boottime=5330.5)["error"] == "expired" and len(rq.applied) == 1
    assert go(dict(ok, id="r-3"), boottime=5329.9, wall=1900000200.0 + 7200.0)["ok"] is True      # a stepped wall clock
    assert go(dict(ok, id="r-4"), boottime=5329.9, wall=1900000200.0 - 7200.0)["ok"] is True       # expires nothing
    fire = dict(pick, id="r-5", args={"sdr": "sdr-a", "job": "PAGE 1", "confirm": False})
    asked = go(fire)
    assert (asked["ok"], asked["confirm"]) == (False, True) and asked["error"].endswith("?")
    same_id = go(dict(fire, args=dict(fire["args"], confirm=True)))
    assert same_id["ok"] is False                           # the same id again: the first reply again, whatever it says
    assert go(dict(fire, id="r-6", args=dict(fire["args"], confirm=True)))["ok"] is True   # the yes goes under a new id
    assert go(ask["an-op-from-the-future.json"])["error"] == "unsupported"
    assert go(dict(ok, id="r-7"), role="viewer")["error"] == "forbidden" and len(rq.applied) == 4
    stale = go(ask["squelch-apply-with-expect.json"])
    assert (stale["ok"], stale["error"]) == (False, "stale")   # what runs is not `expect`: refused, as #685 and #700
    assert go(dict(ask["squelch-apply-plain-set.json"], id="r-8"))["ok"] is True       # without `expect`: a plain set
    assert go(ask["probe.json"])["result"] == "audio"
    for i in range(cc.REQUEST_IDS):                            # the id memory is bounded: 256 ids, 10 min
        go(dict(ok, id=f"fill-{i}"), boottime=5329.0)
    assert len(rq.seen) <= cc.REQUEST_IDS + 1
    rq2 = cc.Requests(apply_pick)
    rq2.handle(ok, role="reader", uid=1000, boottime=5000.0, wall=1900000000.0)
    rq2.handle(dict(ok, expires_boot=6000.0), role="reader", uid=1000, boottime=5000.0 + cc.REQUEST_IDS_S + 1,
               wall=1900000700.0)
    assert len(rq2.applied) == 2                         # forgotten after 10 min: the deadline is what stops an old one


def test_the_credentials_request_is_roots_alone():
    """docs/native/capture-split.md 4.3 "Rotation without a restart", 7.5: capture accepts `credentials` only from a
    peer whose uid is 0 (SO_PEERCRED), whatever role it declared; anyone else is `forbidden`. The schema says so, and
    it is the one frame that carries a secret."""
    creds = valid("capture-socket-request")["credentials-from-the-root-helper.json"]
    rq = cc.Requests(apply_pick)
    for role in ("reader", "viewer"):
        refused = rq.handle(creds, role=role, uid=1000, boottime=5329.0, wall=1900000200.0)
        assert (refused["ok"], refused["error"]) == (False, "forbidden")
        assert rq.handle(creds, role=role, uid=0, boottime=5329.0, wall=1900000200.0)["ok"] is True
    assert rq.seen == {} and rq.applied == []                  # never kept in the id memory: no reply replays a secret
    doc = c.load("capture-socket-request")["$defs"]["args-credentials"]
    assert "UID IS 0" in doc["description"] and doc["required"] == ["token", "access_id", "access_secret"]
    assert "SO_PEERCRED" in (cc.CAPTURE / "socket.md").read_text(encoding="utf-8")
    for file, reply in valid("capture-socket-reply").items():
        assert "example-not-a-real-secret" not in json.dumps(reply), file


# -- 4. the wish and the status ----------------------------------------------------------------------------------------

def test_the_wish_is_desired_state_only_and_the_status_observed_state_only():
    wish, status = c.load("capture-wish")["properties"], c.load("capture-status")["properties"]
    assert list(wish) == ["v", "seq", "at", "pid", "build", "mode", "paths", "hub", "channels", "airband", "squelch",
                          "tones", "aliases", "dongles", "p25", "metrics", "spool"]    # the design's field table, in order
    assert list(status) == ["v", "at", "at_boot", "boot", "pid", "impl", "build", "contract", "started", "wish_seq",
                            "wish_problem", "refusals", "reader", "channels", "sdrs", "groups", "groups_problem", "no_park",
                            "duplicates", "adsb", "p25", "reception", "spool", "urgent"]
    observed = {"run", "state", "open", "open_s", "last_heard", "restarts", "queued", "refusals", "reader", "urgent"}
    assert not observed & (set(wish) | set(wish["channels"]["items"]["properties"]))
    desired = {"mode", "paths", "hub", "tones", "aliases", "dongles", "metrics"}
    assert not desired & set(status)
    assert list(wish["paths"]["properties"]) == ["spool", "urgent_db", "clips", "captures", "state", "recorder_dir"]
    assert "rtl_fm" not in json.dumps(c.load("capture-wish")["properties"]["paths"])   # nothing capture executes
    assert list(wish["spool"]["properties"]) == ["max_age_h", "max_bytes", "max_items", "sync_s", "min_free_bytes"]
    assert wish["mode"]["x-known"] == ["service", "shadow"]
    running, service = valid("capture-status")["running.json"], valid("capture-wish")["service.json"]
    assert running["wish_seq"] == service["seq"] and running["urgent"]["urgent_epoch"] == "9c2d7e10a4b35f68"
    assert [ch["label"] for ch in running["channels"]] == [ch["label"] for ch in service["channels"]]
    assert running["channels"][0]["squelch"] == service["squelch"]["CH A"]             # the wish's values, as running
    assert running["sdrs"][1]["set_by"] == "auto" and "sdr-b" not in service["dongles"]["jobs"]   # capture's own choice


def test_the_running_state_words_are_the_node_designs():
    """docs/native/capture-split.md 4.5: `channels[].run` is exactly the words of docs/nodes.md, which reports them as
    a node's capability (`capabilities.channels[].state`)."""
    run = c.load("capture-status")["properties"]["channels"]["items"]["properties"]["run"]
    caps = c.load("capabilities")["properties"]["channels"]["items"]["properties"]["state"]
    assert run["x-known"] == caps["x-known"] == ["running", "waiting", "out", "held", "shed", "not_in_set", "local"]
    nodes = (ROOT / "docs" / "nodes.md").read_text(encoding="utf-8")
    for word in run["x-known"]:
        assert f"`{word}`" in nodes, word
    seen = {ch["run"] for inst in valid("capture-status").values() for ch in inst["channels"]}
    assert {"running", "waiting", "held", "shed", "out"} <= seen
    assert c.load("capture-status")["properties"]["channels"]["items"]["required"] == ["label", "kind", "run", "state"]


def test_the_urgent_block_says_why_nothing_is_sent():
    urgent = c.load("capture-status")["properties"]["urgent"]
    assert urgent["properties"]["state"]["x-known"] == ["ok", "retrying", "refused", "no_route", "no_credentials", "off",
                                                        "reset"]
    assert urgent["properties"]["mode"]["x-known"] == ["write", "send"]        # never `off`: service treats it as write
    states = {inst["urgent"]["state"] for inst in valid("capture-status").values()}
    assert {"ok", "retrying", "refused", "no_credentials", "off", "reset"} <= states
    none = valid("capture-status")["no-credentials.json"]["urgent"]
    assert none["creds_at_boot"] is None and none["mode"] == "send"                    # it does everything but send
    unsent = valid("capture-status")["an-alarm-not-delivered.json"]["urgent"]
    assert unsent["unsent"][0]["age_s"] >= 10 and unsent["pending_alarms"] == len(unsent["unsent"]) == 1
    assert valid("capture-status")["an-alarm-in-memory-only.json"]["urgent"]["undurable"] == 1
    refused = valid("capture-status")["a-part-of-the-wish-refused.json"]
    assert {r["what"] for r in refused["refusals"]} <= set(
        c.load("capture-status")["properties"]["refusals"]["items"]["properties"]["what"]["x-known"])
    assert all(r["wish_seq"] == refused["wish_seq"] for r in refused["refusals"])


def test_a_status_file_is_stale_after_15_s_on_the_readers_boot_clock():
    status = valid("capture-status")["running.json"]
    assert not cc.capture_down(status, boottime=status["at_boot"] + 15.0, boot_now=status["boot"])
    assert cc.capture_down(status, boottime=status["at_boot"] + 15.1, boot_now=status["boot"])
    assert cc.capture_down(status, boottime=3.0, boot_now=OTHER_BOOT)               # another boot: that capture is gone
    assert status["at"] > c.TIME_LIMIT / 100 > status["at_boot"]                 # `at` is for people, `at_boot` decides


def test_a_broken_wish_is_never_read_as_everything_off():
    """docs/native/capture-split.md 4.4: missing, unreadable or not an object, capture keeps what it last accepted. The
    vectors a strict reader refuses, and the ones that are JSON and no wish, are the cases."""
    bad = dict(c.vectors("capture-wish", "invalid"))
    assert bad["cut-off-mid-write.json"] is c.NOT_JSON and bad["not-an-object.json"] == []
    assert c.errors("capture-wish", {}) != [] and c.errors("capture-wish", None) != []
    least = valid("capture-wish")["required-keys-only.json"]
    assert least["channels"] == [] and least["dongles"] == {}                      # an EMPTY set is a wish: it is valid


# -- 5. the two mode files ---------------------------------------------------------------------------------------------

MODES = cc.data("modes")


def test_every_combination_of_the_mode_files_is_in_the_table_and_follows_the_rules():
    combos = MODES["combinations"]
    assert [(x["capture_mode"], x["urgent_mode"]) for x in combos] == \
        [(a, b) for a in cc.CAPTURE_MODES for b in cc.URGENT_MODES] and len(combos) == 9
    for file, words in (("capture-mode", cc.CAPTURE_MODES), ("urgent-mode", cc.URGENT_MODES)):
        assert MODES["files"][file]["words"] == list(words) and MODES["files"][file]["default"] == words[0]
    for row in combos:
        assert row == cc.combination(row["capture_mode"], row["urgent_mode"]), (row["capture_mode"], row["urgent_mode"])


def test_no_combination_leaves_a_live_alarm_with_nobody_to_decode_write_or_send_it():
    """docs/native/capture-split.md 4.3: "In every one someone decodes, and someone who is running sends: no
    combination leaves a live alarm with neither." With the app up, and for the three service combinations with it
    down."""
    for row in MODES["combinations"]:
        who = (row["capture_mode"], row["urgent_mode"])
        service = row["capture_mode"] == "service"
        assert row["decodes"] and row["decodes"][0] == ("capture" if service else "app"), who
        assert row["sends"], who                               # the app up: at least one lane sends, ...
        assert {"lane": "outbox", "by": "app"} in row["sends"], who                    # ... the app's own always
        assert all(s["by"] in ("app", "capture") for s in row["sends"]), who           # the shadow instance never sends
        senders = {s["by"] for s in row["sends"]}
        writers = {w["by"] for w in row["writes"] if w["file"] == "urgent.sqlite"}
        if "urgent" in {s["lane"] for s in row["sends"]}:
            assert row["urgent_mode_in_effect"] == "send" and writers, who             # the urgent senders send rows
        assert writers <= {row["decodes"][0]}, who             # who decodes writes: the one holder of radio.lock
        if service:
            assert row["urgent_mode_in_effect"] in ("write", "send") and writers == {"capture"}, who
            assert row["app_down"]["decoded"] and row["app_down"]["written"], who      # never lost with the app down
            assert row["app_down"]["sent"] == ("by capture" if "capture" in senders else "when the app is back"), who
        else:
            assert row["urgent_mode_in_effect"] == row["urgent_mode"] and senders == {"app"}, who
            assert row["app_down"] == {"decoded": False, "written": False, "sent": "nothing to send"}, who
        if row["capture_mode"] == "shadow":
            assert row["decodes"] == ["app", "shadow"] and {"by": "shadow", "file": "urgent-shadow.sqlite"} in row["writes"]
            inline = cc.combination("inline", row["urgent_mode"])              # "as the inline row of the same mode"
            assert row["sends"] == inline["sends"] and row["writes"][:-1] == inline["writes"]
    assert [x["urgent_mode_in_effect"] for x in MODES["combinations"] if x["capture_mode"] == "service"] \
        == ["write", "write", "send"]
    assert {x["urgent_mode_in_effect"] for x in MODES["combinations"]} - {"off"} == \
        set(c.load("capture-status")["properties"]["urgent"]["properties"]["mode"]["x-known"])


def test_a_mode_file_is_one_word_and_anything_else_is_the_default():
    assert cc.read_mode("service\n", cc.CAPTURE_MODES) == "service" and cc.read_mode(" send ", cc.URGENT_MODES) == "send"
    for text in (None, "", "\n", "Service", "service inline", "from_the_future", "on"):
        assert cc.read_mode(text, cc.CAPTURE_MODES) == "inline" and cc.read_mode(text, cc.URGENT_MODES) == "off", text
    # so a capture started while its file does not say `service` does not run (exit 78), and `off` in service is write
    assert cc.combination("service", cc.read_mode(None, cc.URGENT_MODES))["urgent_mode_in_effect"] == "write"


# -- the route, and the README ----------------------------------------------------------------------------------------

def test_the_urgent_route_is_listed_as_designed_and_not_served_yet(tmp_path):
    """contracts/routes.json `designed`: a route that is decided and not built. It has a row's shape (so the row moves
    to `routes` unchanged when the hub serves it) and is in no dispatcher today; the day it is, this test and
    tests/test_contracts_routes.py both fail until the row is moved."""
    table = c.routes()
    rows = table["designed"]
    assert [(r["method"], r["path"]) for r in rows] == [("POST", "/api/station/urgent")]
    row = rows[0]
    assert set(row) - {"note", "designed"} == {"method", "path", "credential", "role", "request", "response"}
    assert row["credential"] == ["sz1"] and row["role"] == {"sz1": ["radio-receive"], "sw1": "never"}
    assert set(row["role"]["sz1"]) <= set(c.load("roles")["properties"])
    assert (row["request"], row["response"]) == ("capture-urgent-request", "capture-urgent-response")
    assert row["designed"] == {"design": "docs/native/capture-split.md 4.3, 7.5, 10, 11 (#773)", "body_max_bytes": 65536,
                               "items_max": 32, "seen": False, "idempotent_by": ["station", "key", "rev"],
                               "not_leader": 503}
    assert (row["designed"]["body_max_bytes"], row["designed"]["items_max"]) == (cc.REQUEST_MAX_BYTES, cc.REQUEST_MAX_ITEMS)
    events = next(r for r in table["routes"] if r["path"] == "/api/station/events")
    assert set(row["role"]["sz1"]) <= set(events["role"]["sz1"])               # "as /api/station/events is"
    listed = {c.example(r["path"]) for r in table["routes"]}
    assert row["path"] not in listed, "the route is in `routes` now: take it out of `designed`"
    assert row["path"] not in c.served_paths(), "the hub serves /api/station/urgent now: move its row to `routes`"
    assert not [p for r in table["reserved"] for p in [row["path"]] if p.startswith(r["prefix"])]
    live = c.Live(tmp_path)
    try:
        body = valid("capture-urgent-request")["one-alarm.json"]
        code, answer = live.call("POST", row["path"], body, token="sz1")
        assert (code, answer) == (404, {"error": "not found"})                 # today's hub: `no_route` for the sender
        assert live.call("POST", row["path"], body, token=None)[0] == 401
    finally:
        live.close()


def test_the_readme_maps_every_schema_to_its_design_section_and_flags_its_choices():
    section = README.split("## Capture split (designed)", 1)[1].split("\n## ", 1)[0]
    for name in cc.NAMES:
        assert re.search(rf"^\| `{name}` \| (designed) \| 1 \|", README, re.M), name
        assert re.search(rf"^\| `{name}`.*\| 4\.\d", section, re.M), f"{name}: its design section"
    files = ["spool.md", "socket.md", "urgent.md", "urgent.sql", "urgent-scenarios.json", "socket-frames.json",
             "modes.json"]
    for file in files:
        assert (cc.CAPTURE / file).is_file() and f"`{file}`" in section, file
    assert sorted(p.name for p in cc.CAPTURE.iterdir()) == sorted(files)
    choices = re.findall(r"^\| (C\d+) \|", section, re.M)
    found = re.findall(r"^\| (X\d+) \|", section, re.M)
    assert choices == [f"C{i}" for i in range(1, len(choices) + 1)] and len(choices) >= 20
    assert found == [f"X{i}" for i in range(1, len(found) + 1)] and len(found) >= 8
    assert "to confirm" in section and "radio maintainers" in section
    assert "A7" in section and "not decided" in section
    for word in ("specification aid", "not production code"):
        assert word in section or word in (ROOT / "tests" / "contracts_capture.py").read_text(encoding="utf-8").lower()
