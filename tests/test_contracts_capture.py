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


def test_every_designed_schema_belongs_to_one_design_and_one_list():
    """A designed schema is of docs/nodes.md (the list in tests/test_contracts_nodes.py) or of the capture split (the
    list in tests/contracts_capture.py), by the document its description names. One in neither list, or naming a
    third document, fails here, by its name."""
    from tests.test_contracts_nodes import DESIGNED
    assert not set(DESIGNED) & set(cc.NAMES)
    for name in c.names():
        doc = c.load(name)
        if doc["x-status"] != "designed":
            assert name not in DESIGNED and name not in cc.NAMES, f"{name} is listed as designed and says it is built"
            continue
        nodes = doc["description"].startswith("Designed (docs/nodes.md")
        capture = doc["description"].startswith("Designed (docs/native/capture-split.md")
        assert nodes != capture, f"{name}: a designed schema names its design: docs/nodes.md or the capture split"
        assert name in (DESIGNED if nodes else cc.NAMES), f"{name} is designed and in no list of designed schemas"


def test_the_schemas_the_harness_skips_for_unknown_keys_are_covered_here():
    """tests/test_contracts_schemas.py leaves a schema with a top-level `allOf` out of its "a valid vector carries a
    key from a newer build" test. Of the capture schemas that is exactly the four that choose by a word and the one
    extension; each has such a vector all the same, and so has every other capture schema."""
    skipped = {n for n in cc.NAMES if c.load(n).get("allOf")}
    assert skipped == {"capture-spool-sidecar", "capture-urgent-item", "capture-socket-event", "capture-socket-request",
                       *cc.EXTENSIONS}
    for name in cc.NAMES:
        assert any(FUTURE.search(json.dumps(inst)) for _, inst in c.vectors(name, "valid")), name
    for name in cc.EXTENSIONS:                                 # an extension: the schema it extends, plus optional keys
        doc = c.load(name)
        assert doc["allOf"] == [{"$ref": "station-hello-answer.schema.json"}] and "required" not in doc
        for file, inst in c.vectors("station-hello-answer", "valid"):
            assert c.errors(name, inst) == [], file            # every answer valid today stays valid
        for file, inst in c.vectors("station-hello-answer", "invalid"):
            assert c.errors(name, inst), file


# What docs/native/capture-split.md 10 "Contract artifacts" says a reader may rely on, row by row (copied from the
# design at 225b06ff; test_the_design_documents_table_is_this_one compares it with the document once that is here).
REQUIRED = {
    "Spool sidecar, `kind: \"segment\"`": [
        "v", "kind", "id", "seq", "spool_epoch", "channel.label", "channel.kind", "start", "end", "closed_boot", "boot",
        "published", "reason", "levels", "ids", "alarm_key", "audio", "audio", "file", "format", "encoding", "rate_hz",
        "channels", "samples", "bytes"],
    "Spool sidecar, `kind: \"call\"`": [
        "v", "kind", "id", "seq", "spool_epoch", "published", "start", "end", "closed_boot", "boot", "audio", "system",
        "talkgroup", "turns", "emergency", "encrypted", "tr", "ids", "id", "system", "emergency", "from", "keyup", "split",
        "parent", "prev", "src", "pos_s", "emergency", "via"],
    "Spool sidecar, `kind: \"metrics\"`": ["v", "kind", "id", "seq", "spool_epoch", "published", "records"],
    "Spool cursor": ["v", "spool_epoch", "seq"],
    "Urgent item (a row's `data`, and an item of the request)": [
        "key", "rev", "kind", "at", "event_at", "age_s", "age_src", "boot", "wall", "data", "data", "source", "start",
        "radio_id", "emergency", "emergency_via", "data", "channel", "started", "pairs", "phase", "ended", "trimmed"],
    "`POST /api/station/urgent`, request": ["v", "station", "items"],
    "`POST /api/station/urgent`, answers": ["ok", "gen", "results", "key", "rev", "status", "error", "reason"],
    "Socket, capture to client": ["v", "t", "at", "t"],
    "Socket, client to capture": ["v", "t", "urgent_taken", "urgent_epoch", "n", "v", "t: \"request\"", "id", "op", "args",
                                  "expires_boot"],
    "Socket, replies": ["v", "t: \"reply\"", "id", "ok", "ok: false", "reason", "error", "confirm"],
    "Wish file": ["v", "seq", "mode", "paths", "channels", "dongles"],
    "Status file": ["v", "at", "at_boot", "boot", "pid", "contract", "wish_seq", "channels", "sdrs", "refusals", "spool",
                    "urgent", "label", "kind", "run", "state", "spool", "urgent"],
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
    call = REQUIRED["Spool sidecar, `kind: \"call\"`"]
    assert side["required"] == call[:6] == REQUIRED["Spool sidecar, `kind: \"metrics\"`"][:-1]     # "the common ones"
    assert set(required_when(side, "kind", "call")) == set(call[:17])
    assert side["$defs"]["radio-id"]["required"] == call[18:22] and side["properties"]["keyup"]["required"] == call[23:26]
    assert side["$defs"]["turn"]["required"] == call[26:]
    assert side["$defs"]["turn"]["properties"]["via"]["x-known"] == ["srclist", "grant", "split"]      # the code's words
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
    assert ask["$defs"]["urgent_taken"]["required"] == ["urgent_epoch", "n"]     # both: an old `n` never marks new rows
    reply = c.load("capture-socket-reply")
    assert set(reply["required"]) == {"v", "t", "id", "ok", "at"} and reply["then"]["required"] == ["reason", "error", "confirm"]
    assert reply["properties"]["t"] == {"const": "reply", "description": "Always `reply`."}
    assert reply["properties"]["reason"]["x-known"] == list(cc.REASONS)
    assert c.load("capture-wish")["required"] == REQUIRED["Wish file"]
    status = c.load("capture-status")
    assert status["required"] == REQUIRED["Status file"][:12]
    assert status["properties"]["channels"]["items"]["required"] == REQUIRED["Status file"][12:16]
    for block in ("spool", "urgent"):                          # "the counters a Health row reads"
        assert len(status["properties"][block]["required"]) >= 7


def design_rows() -> dict[str, list[str]]:
    """{artifact: the names in backticks of its "Required" cell} from the first table of the design's section 10."""
    body = DESIGN.read_text(encoding="utf-8").split("## 10. Contract artifacts", 1)[1]
    table: list[str] = []
    for line in body.splitlines():
        if line.startswith("|"):
            table.append(line)
        elif table:
            break
    rows = [[x.strip() for x in re.split(r"(?<!\\)\|", line)[1:-1]] for line in table[2:]]     # (a cell may hold an escaped \|)
    return {r[0]: re.findall(r"`([^`]+)`", r[3]) for r in rows if r[3] != "-"}


def test_the_design_documents_table_is_this_one():
    """The copy above against the document itself, once #773 has put it in the tree: a change to the design's table
    fails here, and the schemas follow."""
    if not DESIGN.exists():
        pytest.skip("docs/native/capture-split.md is not in this tree yet (#773)")
    assert design_rows() == REQUIRED


def test_the_designs_example_bodies_are_vectors():
    """docs/native/capture-split.md 10 "Vectors to start from": each of its six bodies is a valid vector, as written
    there. (Compared with the document once it is in the tree; until then the vectors are the copy.) The sender's two
    example requests are one kind each: an alarm and a page never share a request."""
    mine = [valid("capture-spool-sidecar")["segment-with-an-emergency-id.json"],
            valid("capture-urgent-request")["an-alarm.json"], valid("capture-urgent-request")["a-page.json"],
            valid("capture-urgent-response")["the-alarm-applied.json"],
            valid("capture-socket-request")["pick.json"], valid("capture-socket-reply")["refused-parked.json"]]
    assert mine[0]["alarm_key"] == mine[1]["items"][0]["key"] == mine[3]["results"][0]["key"] == "CHA-live-1900000000123"
    assert [[i["kind"] for i in m["items"]] for m in mine[1:3]] == [["alarm"], ["page"]]
    assert mine[4]["id"] == mine[5]["id"] and (mine[5]["reason"], mine[5]["error"]) == ("parked", "refused: parked")
    mixed = valid("capture-urgent-request")["an-alarm-and-a-page-in-one-as-a-hub-takes-them.json"]
    assert [i["kind"] for i in mixed["items"]] == ["alarm", "page"]              # kept as what a hub must accept
    senders = [f for f, v in valid("capture-urgent-request").items() if len({i["kind"] for i in v["items"]}) > 1]
    assert sorted(senders) == ["an-alarm-and-a-page-in-one-as-a-hub-takes-them.json",
                               "an-item-of-a-kind-from-the-future.json"]
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
    """A kind, a frame, an op, a state, a mode, a reason a reader does not know is taken as unknown, never as an
    error."""
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
             ("capture-urgent-response", valid("capture-urgent-response")["the-alarm-applied.json"], ("results", 0, "status")),
             ("capture-socket-event", valid("capture-socket-event")["carrier-up.json"], ("state",)),
             ("capture-socket-request", valid("capture-socket-request")["hello-reader.json"], ("role",)),
             ("capture-socket-reply", valid("capture-socket-reply")["refused-parked.json"], ("reason",)),
             ("capture-spool-sidecar", valid("capture-spool-sidecar")["call.json"], ("turns", 0, "via"))]
    for name, doc, path in cases:
        assert c.errors(name, put(doc, path, "from_the_future")) == [], (name, path)
        for bad in (1, None, True, ["x"], {"x": 1}):
            assert c.errors(name, put(doc, path, bad)) != [], (name, path, bad)


def test_every_integer_has_a_maximum_and_an_alarm_has_one_revision():
    """No integer of the contract is unbounded (a reader in another language must not overflow on one), and none
    passes 2^53 - 1. An alarm has one revision, 0, and is always an emergency; a held item has a row number exactly
    when it is durable."""
    for name in cc.NAMES:
        for path, prop in c.properties(c.load(name)):
            kinds = prop.get("type") if isinstance(prop.get("type"), list) else [prop.get("type")]
            if "integer" in kinds:
                assert isinstance(prop.get("maximum"), int) and prop["maximum"] <= c.MAX_SAFE, (name, path)
    item = valid("capture-urgent-item")["alarm-raised-live.json"]
    assert c.errors("capture-urgent-item", dict(item, rev=1)) == ["rev: 0 was expected"]
    assert len(c.errors("capture-urgent-item", dict(item, data=dict(item["data"], emergency=False)))) == 1
    page = valid("capture-urgent-item")["page-voice.json"]
    assert c.errors("capture-urgent-item", dict(page, rev=7)) == []
    held = c.subschema("capture-urgent-item", "#/$defs/held")
    frame = {k: v for k, v in valid("capture-socket-event")["urgent-alarm.json"].items() if k not in ("v", "t", "urgent_epoch")}
    assert held.is_valid(frame) and held.is_valid(dict(frame, n=None, durable=False))
    assert not held.is_valid(dict(frame, n=None)) and not held.is_valid(dict(frame, durable=False))


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
    urgent item and no frame but the root reload helper's `credentials` request. A wish names no URL either: the
    schema refuses a key named like a secret at any depth and in any case, and the hub's URL under `hub`."""
    named = re.compile(r"token|secret|password|access_(id|secret)|authorization|bearer|credential", re.I)

    def secrets(node) -> bool:
        """A key named like a secret, or a value shaped like a station's or a worker's token."""
        if isinstance(node, dict):                             # (`token: "own"` in a hello's answer is a kind, a word)
            return any((named.fullmatch(k) and v not in ("own", "shared")) or secrets(v) for k, v in node.items())
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
    wish = valid("capture-wish")["service.json"]

    def at(path, key, value="x"):
        out = copy.deepcopy(wish)
        node = out
        for part in path:
            node = node[part]
        node[key] = value
        return out
    for path, key in ((("hub",), "url"), (("hub",), "URL"), (("hub",), "Hub_Url"), (("hub",), "access_id"),
                      (("hub",), "token"), (("hub",), "TOKEN"), (("hub",), "access_secret"), ((), "station_token"),
                      (("paths",), "tokens"), (("dongles", "jobs", "sdr-a"), "apiToken"), (("channels", 0), "Password"),
                      (("p25",), "client_secret"), (("spool",), "SECRET")):
        assert len(c.errors("capture-wish", at(path, key))) == 1, (path, key)
    assert c.errors("capture-wish", at(("p25",), "deep", {"a": [{"b": {"Token": 1}}]})) != []       # at any depth
    for path, key in ((("hub",), "urls"), ((), "url"), (("paths",), "took"), (("hub",), "access")):
        assert c.errors("capture-wish", at(path, key)) == [], (path, key)     # (only those names, and a URL only in `hub`)
    assert "path" not in json.dumps(c.load("capture-socket-request")["$defs"]["args-pick"])
    assert "(?i)" not in json.dumps(c.load("capture-wish"))                    # no inline flag: not every regex engine has it


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
        if side.get("audio_dropped"):                          # never at a bound for an item with an emergency id:
            assert side["audio"] is None, file                 # only a write that failed outright leaves it without
            assert cc.is_emergency(side) == (side.get("audio_lost") == "write"), file
        if side.get("published_boot") is not None and "closed_boot" in side:
            assert side["published_boot"] >= side["closed_boot"]
    assert cc.segment_id("CH A", 1900000000.123) == "CHA-1900000000123"
    assert len(cc.wav_s16(101600)) == 203244                                  # the design's own figures
    cursor = valid("capture-spool-cursor")["channels-in-parallel.json"]
    assert c.errors("capture-spool-cursor", dict(cursor, done=list(range(12346, 12346 + 256)))) == []
    assert len(c.errors("capture-spool-cursor", dict(cursor, done=list(range(12346, 12346 + 257))))) == 1
    matched = valid("capture-spool-sidecar")["call-matched-to-its-live-alarm.json"]
    assert matched["alarm_key"] == valid("capture-urgent-item")["alarm-p25-live.json"]["key"]


def test_an_id_is_the_labels_slug_and_two_labels_of_one_slug_are_refused():
    """docs/native/capture-split.md 4.1 "Ids": spaces removed, any other character outside [A-Za-z0-9._-] replaced by
    `_`. For every label of those characters and spaces it is exactly today's clip id (sizeup/app.py)."""
    from sizeup import app as appmod
    for label in ("CH A", "Fireground 1", "PAGE-1", "Tac.2", "a_b"):
        today = f"{label.replace(' ', '')}-{int(1900000000.123 * 1000)}"
        assert cc.segment_id(label, 1900000000.123) == today and appmod.CLIP_ID.fullmatch(today)
    assert cc.slug("Fire/EMS 15") == "Fire_EMS15" and cc.slug("Tour d'été") == "Tourd__t_"
    odd = cc.segment_id("Fire/EMS 15", 1900000000.123)
    assert odd == "Fire_EMS15-1900000000123" and appmod.CLIP_ID.fullmatch(odd)
    assert not appmod.CLIP_ID.fullmatch("Fire/EMS15-1900000000123")            # (today's id for it cannot be served)
    pattern = re.compile(c.load("capture-spool-sidecar")["properties"]["id"]["pattern"])
    assert pattern.pattern == f"^{appmod.CLIP_ID.pattern}$" and pattern.fullmatch(odd)
    assert cc.slug_clashes(["CH A", "PAGE 1", "TOWER"]) == []
    assert cc.slug_clashes(["Fire/EMS 15", "Fire EMS15", "Fire_EMS 15", "CH A", "CHA"]) == [
        ["Fire/EMS 15", "Fire_EMS 15"], ["CH A", "CHA"]]                        # a channel set capture refuses


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


EMERGENCY_IDS = [{"id": "7001", "system": "mdc", "emergency": True, "from": "live"}]


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
    # a call: trunk-recorder's own JSON beside it as `.tr`, which no reader takes for a sidecar
    call = {k: v for k, v in valid("capture-spool-sidecar")["call.json"].items() if k not in ("seq", "spool_epoch", "tr")}
    side = spool.publish(dict(call, audio={k: v for k, v in call["audio"].items() if k not in ("file", "bytes")}),
                         cc.wav_s16(96000, 8000), tr=b'{"emergency": false}')
    assert side["tr"] == "000000000006-4101-1900000300_851012500.0-call_17.tr"
    assert cc.check_spool(spool.root) == []
    assert len(list((spool.root / "items").glob("*.json"))) == 6


@pytest.mark.parametrize("stop,live,settled", [("tmp", [], ["tmp"]), ("audio", [], ["debris", "seq-behind"]),
                                                ("sidecar", [], ["seq-behind"])])
def test_a_writer_killed_at_any_point_leaves_no_half_item_a_reader_would_take(spool, stop, live, settled):
    """Atomicity (tmp + rename, audio first, the sidecar last): wherever the writer dies, a reader that lists `*.json`
    sees only complete items. What a crash leaves (a write under tmp/, audio with no sidecar) is debris that capture
    removes at its next start; the sequence number it used is never given out again. (`SEQ` not yet rewritten is
    behind a file name: the check says so, and capture's start takes the next number above it.)"""
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
    assert cc.recover_seq(spool.root) == 4 and rules(cc.check_spool(spool.root)) == ["seq-behind"]
    (spool.root / "SEQ").write_text("41\n")                                    # items pruned behind the cursor
    assert cc.recover_seq(spool.root) == 42
    # a SEQ that is missing or not a number counts as 0, is reported, and the reader's cursor is read too
    for break_it in (lambda: (spool.root / "SEQ").unlink(), lambda: (spool.root / "SEQ").write_text("not a number")):
        break_it()
        assert cc.recover_seq(spool.root) == 4 and rules(cc.check_spool(spool.root)) == ["seq"]
        cursor = {"v": 1, "spool_epoch": spool.epoch, "seq": 57, "done": [60]}
        (spool.root / "cursor.app.json").write_text(json.dumps(cursor))        # the items it names are long deleted
        assert cc.recover_seq(spool.root) == 61
        (spool.root / "cursor.app.json").write_text(json.dumps(dict(cursor, spool_epoch="0000000000000000")))
        assert cc.recover_seq(spool.root) == 4                                 # (another spool's cursor says nothing)
        (spool.root / "cursor.app.json").write_text(json.dumps(cursor))
        again = cc.Spool(spool.root)                                           # capture starts: SEQ is written anew
        assert again.next == 61 and (spool.root / "SEQ").read_text() == "60\n" and cc.check_spool(spool.root) == []
        (spool.root / "cursor.app.json").unlink()
        (spool.root / "SEQ").write_text("3\n")


def test_seq_rolled_back_by_a_power_cut_is_never_taken_at_or_under_the_cursor(tmp_path):
    """The second review's sequence: SEQ is not fsynced at each publish, so a power cut rolls it back to 4 after items
    up to 10 were handled and pruned (items/ empty, the cursor at 10). The next number is ALWAYS the largest of SEQ,
    the file names (items/ and quarantine/) and the cursor, plus 1: 11, above the cursor, so the next item is
    handled. `SEQ` behind the cursor is reported. Before: 5, at or under the cursor, counted as done and never
    handled."""
    spool = cc.Spool(tmp_path / "spool")
    (spool.root / "SEQ").write_text("4\n")
    (spool.root / "cursor.app.json").write_text(json.dumps({"v": 1, "spool_epoch": spool.epoch, "seq": 10, "done": []}))
    assert cc.recover_seq(spool.root) == 11
    assert rules(cc.check_spool(spool.root)) == ["seq-behind"]
    again = cc.Spool(spool.root)
    side = again.publish(dict(seg("CH A", 1900000040.0), ids=[{"id": "7001", "system": "mdc", "emergency": True,
                                                               "from": "live"}]), cc.wav_s16(800))
    assert side["seq"] == 11 and cc.check_spool(spool.root) == []
    # quarantine/ counts too: a poison item set aside under a higher number
    (spool.root / "SEQ").write_text("2\n")
    (spool.root / "quarantine" / "000000000019-CHA-1900000050000.json").write_text("{}")
    assert cc.recover_seq(spool.root) == 20


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
    elif how == "a seq used twice":
        side = json.loads(two.read_text())
        (items / "000000000002-PAGE1-1900000001999.wav").write_bytes(cc.wav_s16(800))
        (items / "000000000002-PAGE1-1900000001999.json").write_text(json.dumps(dict(
            side, id="PAGE1-1900000001999", start=1900000001.999, end=1900000002.05,
            audio=dict(side["audio"], file="000000000002-PAGE1-1900000001999.wav"))))
    elif how == "an item of another spool":
        edit(two, spool_epoch="0000000000000000")
    elif how == "the bursts file gone":
        (items / "000000000001-CHA-1900000000123.mdc.wav").unlink()
    elif how == "a sidecar missing what a segment needs":
        doc = json.loads(one.read_text())
        del doc["alarm_key"]
        one.write_text(json.dumps(doc))
    elif how == "an emergency item's audio dropped at a bound":
        edit(three, ids=EMERGENCY_IDS, audio=None, audio_dropped=True)
        (items / "000000000003-CHA-1900000004200.wav").unlink()
    elif how == "audio that is a dangling symbolic link":
        (items / "000000000002-PAGE1-1900000001000.wav").unlink()
        (items / "000000000002-PAGE1-1900000001000.wav").symlink_to(root / "nowhere.wav")
    elif how == "audio that is a symbolic link":
        target = root / "elsewhere.wav"
        (items / "000000000002-PAGE1-1900000001000.wav").rename(target)
        (items / "000000000002-PAGE1-1900000001000.wav").symlink_to(target)
    elif how == "a sidecar that is a symbolic link":
        target = root / "elsewhere.json"
        two.rename(target)
        two.symlink_to(target)
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
    ("the name and the sidecar disagree", "name"), ("an id twice", "id"), ("a seq used twice", "seq"),
    ("an item of another spool", "epoch"), ("the bursts file gone", "audio"),
    ("a sidecar missing what a segment needs", "sidecar"), ("an emergency item's audio dropped at a bound", "sidecar"),
    ("audio that is a dangling symbolic link", "link"), ("audio that is a symbolic link", "link"),
    ("a sidecar that is a symbolic link", "link"),
    ("a file that is no item", "name"), ("a second file under a complete item's seq", "debris"), ("no epoch", "epoch"),
    ("no items folder", "layout"), ("a cursor that counts a done item", "cursor"), ("a cursor that is not one", "cursor")])
def test_the_checker_names_the_rule_a_spool_breaks(spool, how, rule):
    """Each fixture breaks one rule of contracts/capture/spool.md, and the reference check names that rule and no
    other. It never follows a link and never raises."""
    break_spool(spool.root, how)
    for settled in (True, False):
        found = cc.check_spool(spool.root, settled=settled)
        if not (how == "audio with no sidecar" and not settled):               # (a removal in progress looks like that)
            assert rules(found) == [rule] or (how == "an id twice" and rules(found) == ["id", "name"]), found


def test_a_call_whose_tr_is_a_link_or_is_gone_breaks_the_rules(spool):
    """`tr` is a hard link to trunk-recorder's own JSON: a file. A symbolic link, or one that points nowhere, is not."""
    call = {k: v for k, v in valid("capture-spool-sidecar")["call.json"].items() if k not in ("seq", "spool_epoch", "tr")}
    side = spool.publish(dict(call, audio={k: v for k, v in call["audio"].items() if k not in ("file", "bytes")}),
                         cc.wav_s16(96000, 8000), tr=b"{}")
    tr = spool.root / "items" / side["tr"]
    assert cc.check_spool(spool.root) == []
    tr.unlink()
    assert rules(cc.check_spool(spool.root)) == ["audio"]
    tr.symlink_to(spool.root / "gone.json")
    assert rules(cc.check_spool(spool.root)) == ["link"]
    (spool.root / "gone.json").write_text("{}")
    assert rules(cc.check_spool(spool.root)) == ["link"]


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
    assert spool.prune({"v": 1, "spool_epoch": "0000000000000000", "seq": 3}) == []      # another spool's cursor: nothing
    cursor = {"v": 1, "spool_epoch": spool.epoch, "seq": 1, "done": [3], "tries": {"2": 1}, "at": 1900000010.0}
    (spool.root / "cursor.app.json").write_text(json.dumps(cursor))
    assert cc.check_spool(spool.root) == []
    assert spool.prune(cursor) == [1, 3]
    assert sorted(p.name for p in (spool.root / "items").iterdir()) == ["000000000002-PAGE1-1900000001000.json"]
    assert cc.check_spool(spool.root) == [] and cc.recover_seq(spool.root) == 4          # SEQ outlives the items


def test_a_removal_caught_half_way_is_no_fault_while_capture_runs_and_is_cleaned_at_start(spool):
    """The sidecar always goes first. So a drop or a deletion that stops between its two steps leaves audio no sidecar
    names: while capture runs that is the one removal in progress, not a broken spool; after a start it must be gone,
    and the start removes it: the audio whose sidecar says `audio: null`, and the files whose sidecar is deleted."""
    with pytest.raises(cc.Crash):
        spool.drop_audio(2, stop="sidecar")                    # the sidecar rewritten, the audio still there
    assert (spool.root / "items" / "000000000002-PAGE1-1900000001000.wav").exists()
    assert cc.check_spool(spool.root, settled=False) == [] and rules(cc.check_spool(spool.root)) == ["debris"]
    cc.Spool(spool.root)                                       # capture starts again
    assert not (spool.root / "items" / "000000000002-PAGE1-1900000001000.wav").exists()
    assert cc.check_spool(spool.root) == []
    with pytest.raises(cc.Crash):
        spool.prune({"v": 1, "spool_epoch": spool.epoch, "seq": 1}, stop="sidecar")      # item 1: the sidecar deleted
    left = sorted(p.name for p in (spool.root / "items").iterdir() if p.name.startswith("000000000001"))
    assert left == ["000000000001-CHA-1900000000123.mdc.wav", "000000000001-CHA-1900000000123.wav"]
    assert cc.check_spool(spool.root, settled=False) == [] and rules(cc.check_spool(spool.root)) == ["debris"]
    with pytest.raises(cc.Crash):                              # ... and a publish in flight beside it: still no fault
        spool.publish(seg("CH A", 1900000020.0), cc.wav_s16(800), stop="audio")
    assert cc.check_spool(spool.root, settled=False) == []
    (spool.root / "items" / "000000000003-CHA-1900000004200.json").unlink()              # one more, half way:
    assert rules(cc.check_spool(spool.root, settled=False)) == ["debris"]                # that is not one in progress
    again = cc.Spool(spool.root)
    assert cc.check_spool(spool.root) == [] and again.next == 5                          # the publish's number is not reused
    assert sorted(p.name for p in (spool.root / "items").iterdir()) == ["000000000002-PAGE1-1900000001000.json"]


def test_an_item_with_an_emergency_id_keeps_its_audio_whatever_the_bounds(spool):
    """Never dropped at a bound, and written inside the free-space reserve too. Only a write that failed outright
    leaves it without audio, and its sidecar then says so: `audio_lost: "write"`."""
    side = spool.publish(dict(seg("CH A", 1900000030.0), ids=EMERGENCY_IDS, alarm_key="CHA-live-1900000030000"),
                         cc.wav_s16(800))
    with pytest.raises(ValueError):
        spool.drop_audio(side["seq"])
    assert cc.check_spool(spool.root) == []
    dropped = dict(side, audio=None, audio_dropped=True)
    assert c.errors("capture-spool-sidecar", dropped) == ["$: 'audio_lost' is a required property"]
    assert c.errors("capture-spool-sidecar", dict(dropped, audio_lost="write")) == []
    lost = spool.publish(dict(seg("CH A", 1900000040.0), ids=EMERGENCY_IDS, audio=None, audio_dropped=True,
                              audio_lost="write"))             # the write failed: published without, and saying why
    assert lost["audio"] is None and cc.check_spool(spool.root) == []
    plain = dict(seg("CH A", 1900000050.0), audio=None, audio_dropped=True)                # no emergency id: a bound may
    assert c.errors("capture-spool-sidecar", dict(plain, seq=9, spool_epoch=spool.epoch)) == []


def test_late_is_the_larger_of_two_ages_and_never_gates_by_one_clock():
    """docs/native/capture-split.md 4.1 "Late" and 6 step 5: under 60 s is live, exactly 60 s and older is late; the
    larger of the boot-clock and the wall-clock age decides, so a clock that under-counts makes a row late, never
    stale traffic live."""
    side = {"end": 1900000006.0, "closed_boot": 5120.0, "boot": BOOT}

    def late(wall_age, boot_age, boot=BOOT):
        return cc.is_late(side, wall=side["end"] + wall_age, boottime=side["closed_boot"] + boot_age, boot_now=boot)
    assert not late(59, 59) and late(61, 61)
    assert late(60, 60) and late(60, 0) and late(0, 60) and not late(59.999, 59.999)      # exactly the limit is outside
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
    assert "end" not in live["data"]                           # an alarm raised at the key-up has no end yet
    assert close["key"] == cc.segment_id(label, start) \
        == valid("capture-spool-sidecar")["segment-with-an-emergency-id.json"]["id"]
    p25 = items["alarm-p25-live.json"]
    d = p25["data"]
    assert (p25["key"], d["key_time"]) == cc.p25_key(d["system"], d["talkgroup"], d["radio_id"], event_at=p25["event_at"],
                                                     at=p25["at"]) == (f"p25-examplesys-4101-7001-{int(p25['event_at'] * 1000)}", "grant")
    other = items["alarm-p25-live-keyed-on-captures-time.json"]           # the grant carried no time: capture's `at`
    assert (other["key"], other["data"]["key_time"]) == cc.p25_key("examplesys", 4101, "7001", event_at=None, at=other["at"])
    page = items["page-voice.json"]
    assert page["key"] == f"page:{page['data']['channel']}:{page['data']['started']:.2f}" and page["rev"] == 2
    assert items["page-first-pair.json"]["rev"] == 0 and items["page-first-pair.json"]["key"] == page["key"]
    for file, item in items.items():
        if item["kind"] == "alarm":
            assert item["rev"] == 0 and item["data"]["emergency"] is True, file
        assert item["at"] >= item["event_at"] and len(item["key"]) <= 200, file


def test_a_request_is_at_most_32_items_and_64_kib():
    req = valid("capture-urgent-request")["an-alarm.json"]
    alarm = req["items"][0]
    many = [dict(alarm, key=f"CHA-live-{1900000000123 + i}") for i in range(33)]
    assert c.errors("capture-urgent-request", dict(req, items=many[:32])) == []
    assert len(c.errors("capture-urgent-request", dict(req, items=many))) == 1
    assert len(json.dumps(dict(req, items=many[:32])).encode()) < cc.REQUEST_MAX_BYTES     # 32 alarms fit in the body cap
    for file, body in valid("capture-urgent-request").items():
        assert len(json.dumps(body).encode()) <= cc.REQUEST_MAX_BYTES, file
    hub = cc.Hub()
    assert hub.urgent(dict(req, items=many))[0] == 422 and hub.alarms == []
    fat = dict(req, items=[dict(alarm, data=dict(alarm["data"], unit="U" * 70000))])
    assert hub.urgent(fat) == (413, {"error": "body over 65536 bytes", "reason": "too_large"}) and hub.alarms == []
    for code, body in (hub.urgent(dict(req, items=many)), hub.urgent(fat)):
        assert c.errors("capture-urgent-response", body) == [] and code in (413, 422)


SCENARIOS = cc.data("urgent-scenarios")


T0 = SCENARIOS["params"]["t0"]              # the hub's clock at the start of every scenario, and the vectors' epoch


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
    table's, every 200 a valid answer, and what holds on the way and at the end the table's."""
    p = SCENARIOS["params"]
    hub = cc.Hub(**{"max_age_s": p["max_age_s"], "repeat_s": p["repeat_s"], "stuck_within_s": p["stuck_within_s"],
                    "p25_slack_s": p["p25_slack_s"], **sc.get("hub", {}), **params})
    assert cc.PAGE_LIVE_MAX_S == p["page_live_max_s"]
    gen, last = hub.gen, -1.0
    for step in sc["steps"]:
        assert step["at"] > last, "steps are in the hub's time order"
        last, hub.now = step["at"], T0 + step["at"]
        station = step.get("station", "st-north")
        if "urgent" in step:
            body = {"v": 1, "station": station, "urgent_epoch": "9c2d7e10a4b35f68",
                    "sent_at": step.get("sent_at", hub.now), "items": [scenario_item(s) for s in step["urgent"]]}
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
            assert hub.event(station, step["event"]) == 200
        elif "hello" in step:
            code, answer = hub.hello()
            assert code == 200 and c.errors("capture-hello-answer", answer) == [], answer
            assert (answer["gen"] != gen) == step.get("gen_changed", False), (sc["name"], step)
            gen = answer["gen"]
        elif "ack" in step:
            hub.ack(step["ack"])
        elif "all_clear" in step:
            hub.all_clear(step["all_clear"])
        elif "state_paging" in step:
            hub.state(station, step["state_paging"])
        elif "holds" in step:
            holds(hub, step["holds"])
        elif step.get("hub") in ("handoff", "lead"):
            getattr(hub, step["hub"])()
        else:
            hub.restore(step["hub"]["restore_s"])
    return hub


def holds(hub: cc.Hub, then: dict) -> None:
    got = {"alarms": len(hub.alarms), "pushes": len(hub.pushes), "notices": len(hub.notices), "pages": len(hub.pages),
           "traffic_ids": [a["traffic_id"] for a in hub.alarms],
           "push_age_s": hub.pushes[0]["age_s"] if hub.pushes else None,
           "still_set": sum(a["still_set"] for a in hub.alarms), "sources": hub.alarms[0]["sources"] if hub.alarms else [],
           "labels": [a["label"] for a in hub.alarms]}
    for key, want in then.items():
        if key == "page":
            page = hub.pages[("st-north", want["key"])]
            flat = {"key": want["key"], "rev": page["rev"], "phase": page["data"].get("phase"), "ended": page["ended"],
                    "live": hub.page_live("st-north", want["key"]), "expires_in_s": page["expires"] - hub.now}
            assert {k: flat[k] for k in want} == want, flat
        elif key == "states":
            assert {k: hub.store[("st-north", k)]["state"] for k in want} == want
        elif key == "stale_src":
            assert {k: hub.store[("st-north", k)]["stale_src"] for k in want} == want
        else:
            assert got[key] == want, (key, got)


@pytest.mark.parametrize("sc", SCENARIOS["scenarios"], ids=lambda sc: sc["name"][:60])
def test_the_hubs_idempotency_table(sc):
    holds(run(sc), sc["then"])


def test_the_table_covers_the_designs_rows_and_says_what_is_not_decided():
    names = " | ".join(sc["design"] for sc in SCENARIOS["scenarios"])
    for row in range(1, 19):                                   # the 18 rows of "Idempotency, on the hub"
        assert re.search(rf"rows? (\d+, )*{row}\b", names), f"no scenario for row {row} of the idempotency table"
    for word in ("H1a", "H1d", "H1e", "H1j", "H1k", "handoff", "the three states", "6 end-to-end"):
        assert word in names, word
    assert SCENARIOS["params"]["max_age_s"] == 1800 and SCENARIOS["params"]["max_age_s_decided"] is False
    assert SCENARIOS["params"]["age_margin"] == cc.AGE_MARGIN == 1.1
    assert "NOT DECIDED" in SCENARIOS["description"] and "A7" in README
    kinds = {"urgent", "event", "hello", "ack", "all_clear", "state_paging", "hub", "holds"}
    for sc in SCENARIOS["scenarios"]:
        assert set(sc) <= {"name", "design", "steps", "then", "hub", "depends_on"}, sc["name"]
        for step in sc["steps"]:
            assert len(set(step) & kinds) == 1, step
    assert len({sc["name"] for sc in SCENARIOS["scenarios"]}) == len(SCENARIOS["scenarios"]) >= 35
    for kind in ("alarm", "p25", "page", "future"):
        assert c.errors("capture-urgent-item", SCENARIOS["items"][kind]) == []
    # the sequences that lost or doubled an alarm before the design's rules: each is here, by name
    for needle in ("stepped back 40 min", "carries the alarm_key and no emergency", "60 s ahead", "300 s later",
                   "two stations hear one press", "ends on the hub by itself", "says so in its hello",
                   "exactly the age limit", "exactly 600 s", "exactly 120 s apart", "exactly 10 s before",
                   # ... and the second review's (docs/native/capture-split.md 4.3 "What can go wrong", 6)
                   "ending 35, 28, 21, 14, 7, 1 min ago", "notices are off", "15 min behind", "1 h fast",
                   "before its upgrade to raised", "acknowledged at 4 s", "an ended page stays ended",
                   "emergency still set after All clear"):
        assert any(needle in sc["name"] for sc in SCENARIOS["scenarios"]), needle


def test_the_age_limit_is_a_parameter_until_the_admin_decides():
    """A7 is open: what a late emergency becomes on the hub. 30 min is the recommended rule, not a decision, so the
    limit is a parameter of the model and of the table. The scenarios that depend on it say so, and no other
    scenario changes with it."""
    depends = [sc for sc in SCENARIOS["scenarios"] if sc.get("depends_on") == "max_age_s"]
    assert len(depends) == 8
    late = next(sc for sc in depends if sc["name"].startswith("late, under"))
    older = next(sc for sc in depends if sc["name"].startswith("older than"))
    holds(run(late), late["then"])
    with pytest.raises(AssertionError):
        run(late, max_age_s=900.0)                             # with a 15 min limit, 29 min is stale
    with pytest.raises(AssertionError):
        run(older, max_age_s=3600.0)                           # with a 60 min limit, 31 min is still an alarm
    hub = cc.Hub(max_age_s=3600.0)
    hub.now = T0 + 1860.0
    assert hub.urgent({"v": 1, "station": "st-north", "items": [scenario_item({"item": "alarm", "age_s": 1860.0})]})[1][
        "results"][0]["status"] == "applied" and hub.pushes[0]["age_s"] == 1860.0
    for sc in SCENARIOS["scenarios"]:
        if "depends_on" not in sc:
            for limit in (900.0, 3600.0):
                holds(run(sc, max_age_s=limit), sc["then"])


def test_an_alarm_that_rides_only_the_apps_lane_is_dropped_when_its_wall_time_looks_old():
    """Stated, not hidden (docs/native/capture-split.md 4.3 "What can go wrong", last row): in `urgent-mode write`
    there is no sender, and with the station's wall clock stepped back more than the age limit the app's event looks
    too old: dropped, as it would be today. `send` mode closes it (the item carries a boot-clock age), and so would
    a boot-clock age on the event (11 H1m)."""
    hub = cc.Hub()
    hub.now = T0 + 1.5
    old = {"type": "alarm", "id": KEY, "radio_id": "7001", "source": "CH A", "start": T0 - 2399.0, "end": T0 - 2398.5}
    assert hub.event("st-north", old) == 200 and hub.alarms == [] and hub.pushes == []
    assert hub.store[("st-north", KEY)]["state"] == "seen" and hub.log
    hub.now = T0 + 2.0                                         # ... and with `send`: the item, on its own age
    assert statuses(hub.urgent({"v": 1, "station": "st-north", "items": [scenario_item({"item": "alarm", "age_s": 1.2})]})) \
        == ["applied"] and len(hub.pushes) == 1


# What can go wrong in the lane (docs/native/capture-split.md 4.3), row by row, on the two reference models together.

ALARM_DATA = SCENARIOS["items"]["alarm"]["data"]
KEY = SCENARIOS["items"]["alarm"]["key"]


W0, B0 = 1900000000.0, 5114.0               # the station's wall clock and boot clock when a test begins (t = 0)


def send(lane: cc.Lane, hub: cc.Hub, kind: str = "alarm", *, t: float, lost: bool = False, step: float = 0.0):
    """One request of capture's sender at t seconds (the boot clock at B0 + t; the wall clock there too, plus any
    step), and the hub's answer taken (lost: the answer never arrives). The hub's own clock is at W0 + t."""
    body = lane.request(kind, wall=W0 + t + step, boottime=B0 + t)
    if body is None:
        return None
    assert c.errors("capture-urgent-request", body) == [], c.errors("capture-urgent-request", body)
    assert len({i["kind"] for i in body["items"]}) == 1        # the sender never puts an alarm and a page in one request
    hub.now = W0 + t
    code, answer = hub.urgent(body)
    lane.answer(0 if lost else code, None if lost else answer, wall=W0 + t + step, boottime=B0 + t, kind=kind)
    return code, answer


def press(lane: cc.Lane, key: str = "", *, t: float, kind: str = "alarm", data=None, **more) -> dict:
    return lane.press(key or KEY, kind, ALARM_DATA if data is None else data, wall=W0 + t, boottime=B0 + t, **more)


def hello(lane: cc.Lane, hub: cc.Hub, *, t: float, lost: bool = False) -> None:
    """The 50 s hello on the alarm connection, and its answer taken."""
    hub.now = W0 + t
    code, answer = hub.hello()
    assert c.errors("capture-hello-answer", answer) == []
    lane.hello(0 if lost else code, None if lost else answer, wall=W0 + t, boottime=B0 + t)


def statuses(got) -> list[str]:
    return [r["status"] for r in got[1]["results"]]


def test_lane_capture_killed_between_the_decode_and_the_commit():
    """Row 1. If the request had not gone out either, the alarm is lost: it existed only in memory. Stated, not hidden."""
    lane, hub = cc.Lane(), cc.Hub()
    lane.disk = "stalled"                                      # the commit has not returned
    press(lane, t=0.39)
    assert lane.unsent() == [KEY] and lane.rows == {}
    lane.crash()
    lane.disk = "ok"
    lane.start(wall=W0 + 5.4, boottime=B0 + 5.4)
    assert send(lane, hub, t=5.4) is None and hub.alarms == []


def test_lane_capture_killed_after_the_request_went_out_before_the_commit():
    """Row 2. The hub has it; capture does not know. Nothing is sent twice and the alarm stands alone."""
    lane, hub = cc.Lane(), cc.Hub()
    lane.disk = "stalled"
    press(lane, t=0.39)
    assert statuses(send(lane, hub, t=0.44, lost=True)) == ["applied"]
    lane.crash()
    lane.disk = "ok"
    lane.start(wall=W0 + 5.4, boottime=B0 + 5.4)
    assert send(lane, hub, t=5.4) is None
    assert len(hub.alarms) == len(hub.pushes) == 1 and hub.alarms[0]["traffic_id"] == KEY


def test_lane_the_disk_is_full_and_the_item_is_sent_from_memory():
    """Row 3. A failed insert never stops the send: the item is `durable` false, in the frame to the app and in the
    request alike. It is lost only if capture also dies before the hub answers. The app takes it over once, and when
    its row appears the second frame only moves the cursor."""
    lane, hub = cc.Lane(), cc.Hub()
    lane.disk = "full"
    item = press(lane, t=0.39)
    frame = lane.frames()[0]
    assert (frame["n"], frame["durable"], frame["at"]) == (None, False, item["at"])       # `at`: set when it was made
    head = {"v": 1, "t": "urgent", "urgent_epoch": lane.epoch, **frame}
    assert c.errors("capture-socket-event", head) == []
    assert statuses(send(lane, hub, t=0.44)) == ["applied"]
    assert lane.unsent() == [] and lane.rows == {}             # answered for, though never written
    taken = {(frame["key"], frame["rev"])}                     # the app's set of (key, rev) it has taken
    lane.disk = "ok"
    lane.tick()                                                # the writer tries again: the row appears, already sent
    row = lane.rows[(KEY, 0)]
    assert (row["state"], row["hub"], row["n"], row["at"]) == ("sent", "applied", 1, item["at"])
    second = lane.frames()[0]
    assert (second["n"], second["durable"]) == (1, True) and (second["key"], second["rev"]) in taken
    assert c.errors("capture-socket-event", {"v": 1, "t": "urgent", "urgent_epoch": lane.epoch, **second}) == []
    # ... and the other way: full, never sent, then killed before any row: gone
    lane2, hub2 = cc.Lane(), cc.Hub()
    lane2.disk = "full"
    press(lane2, t=0.39)
    lane2.crash()
    lane2.start(wall=W0 + 5.4, boottime=B0 + 5.4)
    assert send(lane2, hub2, t=5.4) is None and hub2.alarms == []


def test_lane_an_fsync_stalls_and_the_row_follows_the_answer():
    """Row 4. The send goes without the row; a commit that lands after the hub's answer is written as sent, never
    left `new`: no second send, no false "not delivered"."""
    lane, hub = cc.Lane(), cc.Hub()
    lane.disk = "stalled"
    press(lane, t=0.39)
    assert statuses(send(lane, hub, t=0.44)) == ["applied"]
    lane.disk = "ok"
    lane.tick()
    assert lane.rows[(KEY, 0)]["state"] == "sent" and lane.unsent() == [] and lane.not_delivered() == []
    assert send(lane, hub, t=1.0) is None
    lane.crash()
    lane.start(wall=W0 + 5.4, boottime=B0 + 5.4)               # a restart loads `new` rows to send: this one is not
    assert send(lane, hub, t=5.4) is None and len(hub.pushes) == 1


def test_lane_the_app_is_down_and_takes_over_later():
    """Row 5. Nothing changes for the send. The take-over waits, and makes no second alarm when it comes."""
    lane, hub = cc.Lane(), cc.Hub()
    press(lane, t=0.39)
    assert statuses(send(lane, hub, t=0.44)) == ["applied"]
    hub.now = W0 + 40.0                                        # the app is back: its take-over event, then the transmission
    assert hub.event("st-north", {"type": "alarm", "id": KEY, "radio_id": "7001", "source": "CH A", "start": W0 + 0.12,
                                  "end": W0 + 0.39}) == 200
    assert hub.event("st-north", {"type": "traffic", "id": "CHA-1900000000123", "alarm_key": KEY, "emergency": True,
                                  "radio_id": "7001", "source": "CH A", "start": W0 + 0.12, "end": W0 + 6.47}) == 200
    assert len(hub.alarms) == len(hub.pushes) == 1 and hub.alarms[0]["traffic_id"] == "CHA-1900000000123"


def test_lane_the_hub_answers_applied_then_comes_back_from_an_older_replica():
    """Row 6, the resend rule. Every answer carries the hub's generation. When it is another one, capture sends again
    every alarm answered under a different generation at or after its LAST SIGHTING of the old one minus 120 s, and
    no older one."""
    lane, hub = cc.Lane(), cc.Hub()
    press(lane, "CHA-live-1899999000000", t=-1000.0, data=dict(ALARM_DATA, radio_id="7009"))
    assert statuses(send(lane, hub, t=-999.9)) == ["applied"]
    press(lane, t=0.39)
    assert statuses(send(lane, hub, t=0.44)) == ["applied"]    # (the last sighting of generation 1)
    hub.now = W0 + 10.0
    hub.restore(30.0)                                          # the alarm of 10 s ago is gone from the hub
    assert [a["traffic_id"] for a in hub.alarms] == ["CHA-live-1899999000000"]
    assert send(lane, hub, t=10.0) is None                     # capture has nothing to send ...
    page = SCENARIOS["items"]["page"]
    press(lane, "page:PAGE 1:1900000010.00", t=10.0, kind="page", data=page["data"])
    assert statuses(send(lane, hub, "page", t=10.1)) == ["applied"]            # ... until an answer says the generation
    assert lane.unsent() == [KEY]                              # changed: what was answered since 120 s before 0.44
    got = send(lane, hub, t=10.5)
    assert statuses(got) == ["applied"] and got[1]["results"][0]["key"] == KEY
    assert sorted(a["traffic_id"] for a in hub.alarms) == ["CHA-live-1899999000000", KEY]
    assert send(lane, hub, t=11.0) is None
    assert lane.rows[(KEY, 0)]["hub_gen"] == "2" and lane.rows[("CHA-live-1899999000000", 0)]["hub_gen"] == "1"


def test_lane_capture_restarts_after_an_answer_and_then_the_hub_comes_back_from_an_older_replica():
    """What the resend rule needs is in the FILE: the generation an alarm was answered under and when (`hub_gen`,
    `sent_boot`, `sent_boot_id`), and the last sighting of the hub's generation (`meta`). So capture's own restart
    between the answer and the hub's restore loses nothing: the next answer, the hello's is enough, shows the new
    generation and the alarm goes again."""
    lane, hub = cc.Lane(), cc.Hub()
    hello(lane, hub, t=0.1)
    press(lane, t=40.39)
    assert statuses(send(lane, hub, t=40.44)) == ["applied"]
    row = lane.rows[(KEY, 0)]
    assert (row["state"], row["hub_gen"], row["sent_boot"], row["sent_boot_id"]) == ("sent", "1", B0 + 40.44, "boot-1")
    assert lane.meta == {"gen": "1", "gen_seen_at": W0 + 40.44, "gen_seen_boot": B0 + 40.44, "gen_seen_boot_id": "boot-1"}
    lane.crash()                                               # capture restarts: its memory is gone
    lane.start(wall=W0 + 45.0, boottime=B0 + 45.0)
    assert lane._since_sighting(lane.rows[(KEY, 0)], W0 + 45.0)                  # in the resend window, from the file
    assert lane.memory == {} and send(lane, hub, t=45.0) is None
    hub.now = W0 + 60.0
    hub.restore(30.0)                                          # the hub is back from a replica 30 s behind
    assert hub.alarms == []
    hello(lane, hub, t=100.0)                                  # the next answer: a new generation
    assert lane.unsent() == [KEY]
    assert statuses(send(lane, hub, t=100.1)) == ["applied"] and [a["traffic_id"] for a in hub.alarms] == [KEY]
    assert lane.rows[(KEY, 0)]["hub_gen"] == "2" and send(lane, hub, t=101.0) is None
    # ... and a restart between seeing the new generation and sending again does not lose it either
    lane2, hub2 = cc.Lane(), cc.Hub()
    hello(lane2, hub2, t=0.1)
    press(lane2, t=40.39)
    send(lane2, hub2, t=40.44)
    hub2.now = W0 + 60.0
    hub2.restore(30.0)
    hello(lane2, hub2, t=100.0)
    lane2.crash()
    lane2.start(wall=W0 + 101.0, boottime=B0 + 101.0)
    assert statuses(send(lane2, hub2, t=101.1)) == ["applied"]


def test_lane_the_hub_restores_while_the_senders_are_idle():
    """The window is anchored at the last sighting of the old generation, not at now: the hub may be away for any
    time. Here it is gone for 200 s and no alarm is pressed: the 50 s hello shows the new generation, and the alarm
    it answered for just before it went down is sent again."""
    lane, hub = cc.Lane(), cc.Hub()
    press(lane, "CHA-live-1899999000000", t=-1000.0, data=dict(ALARM_DATA, radio_id="7009"))
    send(lane, hub, t=-999.9)
    press(lane, t=40.39)
    assert statuses(send(lane, hub, t=40.44)) == ["applied"]
    hello(lane, hub, t=50.0)                                   # the last sighting of generation 1
    hub.now = W0 + 60.0
    hub.restore(30.0)
    for t in (100.0, 150.0, 200.0, 250.0):                     # the hub is away: no answer
        hello(lane, hub, t=t, lost=True)
        assert lane.unsent() == []
    hello(lane, hub, t=300.0)                                  # back, 260 s after the alarm was answered
    assert lane.unsent() == [KEY]                              # answered at 40.44, the anchor is 50 - 120
    assert statuses(send(lane, hub, t=300.1)) == ["applied"]
    assert sorted(a["traffic_id"] for a in hub.alarms) == ["CHA-live-1899999000000", KEY] and lane.unsent() == []
    assert hub.pushes[-1]["age_s"] == pytest.approx(259.71)    # raised again with its real age


def test_lane_generations_are_compared_by_value_and_the_resend_is_bounded():
    """5 and "5" are one generation. Pages are never sent again. At most the 32 newest alarms go again. And a row
    whose answer time cannot be trusted (another boot, a wall clock that stepped back since) is sent: a duplicate is
    harmless, a lost alarm is not."""
    ok = {"ok": True, "v": 1, "station": "st-north"}
    lane = cc.Lane()
    page = SCENARIOS["items"]["page"]
    for i in range(40):
        press(lane, f"CHA-live-{1900000000000 + i}", t=float(i))
    press(lane, page["key"], t=40.0, kind="page", data=page["data"])
    for t, kind in ((41.0, "alarm"), (42.0, "alarm"), (43.0, "page")):
        body = lane.request(kind, wall=W0 + t, boottime=B0 + t)
        lane.answer(200, dict(ok, gen=5, results=[{"key": i["key"], "rev": i["rev"], "status": "applied"}
                                                  for i in body["items"]]), wall=W0 + t, boottime=B0 + t, kind=kind)
    assert lane.unsent() == [] and lane.meta["gen"] == "5"
    lane.hello(200, {"ok": True, "station": "st-north", "token": "own", "gen": "5"}, wall=W0 + 50.0, boottime=B0 + 50.0)
    assert lane.unsent() == [] and lane.meta["gen_seen_boot"] == B0 + 50.0     # the same generation: the sighting moves
    lane.hello(200, {"ok": True, "station": "st-north", "token": "own"}, wall=W0 + 60.0, boottime=B0 + 60.0)
    assert lane.meta["gen_seen_boot"] == B0 + 50.0             # an answer without one says nothing
    lane.hello(200, {"ok": True, "station": "st-north", "token": "own", "gen": "6"}, wall=W0 + 100.0, boottime=B0 + 100.0)
    again = lane.unsent()
    assert len(again) == cc.RESEND_MAX == 32                   # all 40 were answered at 41 and 42: the newest 32 of them
    assert lane.request("page", wall=W0 + 100.0, boottime=B0 + 100.0) is None  # the page: never again
    assert lane.rows[(page["key"], 0)]["state"] == "sent"
    # another boot: the boot clock says nothing, and the wall clock has stepped back since the answer: in doubt, sent
    lane = cc.Lane()
    press(lane, t=0.39)
    body = lane.request("alarm", wall=W0 + 0.5, boottime=B0 + 0.5)
    lane.answer(200, dict(ok, gen=5, results=[{"key": KEY, "rev": 0, "status": "applied"}]), wall=W0 + 0.5, boottime=B0 + 0.5)
    lane.crash()
    lane.start(wall=W0 - 7000.0, boottime=30.0, boot="boot-2")                 # a reboot, the clock two hours back
    assert lane._since_sighting(lane.rows[(KEY, 0)], W0 - 7000.0)                # in doubt: in the window
    lane.rows[(KEY, 0)]["sent_boot_id"] = "boot-0"             # (a row from yet another boot than the sighting)
    lane.hello(200, {"ok": True, "station": "st-north", "token": "own", "gen": 6}, wall=W0 - 6990.0, boottime=40.0)
    assert lane.unsent() == [KEY] and body["items"][0]["key"] == KEY


@pytest.mark.parametrize("seconds,status,alarms,pushes,notices", [
    (29 * 60, "applied", 1, 1, 0), (1979, "applied", 1, 1, 0), (1980, "stale", 0, 0, 1), (34 * 60, "stale", 0, 0, 1)])
def test_lane_the_uplink_is_down_for_hours(seconds, status, alarms, pushes, notices):
    """Row 7. The alarm is not given up: it stays `new`, is retried, and shows as not delivered. When the hub
    answers, A7's recommended rule (a parameter, not decided) with its 10 % margin: under 30 min x 1.1 (1980 s) an
    alarm and a push with its age, at it and older a late notice and no push. Either answer makes it sent."""
    lane, hub = cc.Lane(), cc.Hub(max_age_s=SCENARIOS["params"]["max_age_s"])
    press(lane, t=0.0)
    for t in range(15, seconds, 15):                           # every 15 s: no answer
        body = lane.request("alarm", wall=W0 + t, boottime=B0 + t)
        assert body["items"][0]["key"] == KEY and body["items"][0]["age_s"] == float(t)
        lane.answer(0, None, wall=W0 + t, boottime=B0 + t)
        assert lane.unsent() == lane.not_delivered() == [KEY] and lane.rows[(KEY, 0)]["state"] == "new"
    got = send(lane, hub, t=float(seconds))
    assert statuses(got) == [status] and lane.unsent() == [] and lane.rows[(KEY, 0)]["state"] == "sent"
    assert (len(hub.alarms), len(hub.pushes), len(hub.notices)) == (alarms, pushes, notices)
    if pushes:
        assert hub.pushes[0]["age_s"] == float(seconds) and hub.alarms[0]["at"] == W0


@pytest.mark.parametrize("step", [3600.0, -3600.0])
def test_lane_the_stations_wall_clock_steps(step):
    """Row 8. Nothing: an alarm's age is on the boot clock, and the hub places it at its own now minus that age."""
    lane, hub = cc.Lane(), cc.Hub()
    press(lane, t=0.0)
    got = send(lane, hub, t=0.5, step=step)
    assert statuses(got) == ["applied"] and hub.alarms[0]["at"] == W0
    assert hub.pushes[0] == {"alarm": 1, "at": W0, "age_s": 0.5, "approximate": False, "label": ""}


def test_lane_an_alarm_from_before_a_reboot_is_sent_with_its_wall_clock_age():
    """4.3 "Clocks", an item sent after a reboot: sent all the same, `age_src` `wall`, never held back. The hub cannot
    check a wall-sourced age: it takes it as given and labels the alarm's age as approximate."""
    lane, hub = cc.Lane(boot=BOOT), cc.Hub()
    press(lane, t=0.39)
    lane.crash()
    lane.start(wall=W0 + 400.0, boottime=12.0, boot=OTHER_BOOT)
    body = lane.request("alarm", wall=W0 + 413.0, boottime=25.0)
    assert (body["items"][0]["age_s"], body["items"][0]["age_src"]) == (pytest.approx(412.61), "wall")
    hub.now = W0 + 413.0
    assert statuses(hub.urgent(body)) == ["applied"] and hub.pushes[0]["age_s"] == pytest.approx(412.61)
    assert hub.pushes[0]["approximate"] is True
    lane.crash()                                               # and with the wall clock behind the event: 0, not negative
    lane.start(wall=W0 - 1000.0, boottime=40.0, boot="a-third-boot")
    assert lane.request("alarm", wall=W0 - 1000.0, boottime=40.0)["items"][0]["age_s"] == 0.0


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


PAGE_ITEM = SCENARIOS["items"]["page"]


def page_rev(lane: cc.Lane, rev: int, *, t: float, **data_) -> dict:
    """A revision of the one page, made at t; the page's first tone was at W0 (`event_at`), whatever the revision."""
    return lane.press(PAGE_ITEM["key"], "page", dict(PAGE_ITEM["data"], **data_), rev=rev, wall=W0 + t, boottime=B0 + t,
                      event_at=W0, event_boot=B0)


def test_a_pages_clock_runs_per_revision_and_only_its_highest_rev_is_sent_while_an_alarm_never_expires():
    lane, hub = cc.Lane(), cc.Hub()
    for rev in (0, 1, 2):
        page_rev(lane, rev, t=1.0 + rev, phase="voice" if rev == 2 else "tones")
    body = lane.request("page", wall=W0 + 4.0, boottime=B0 + 4.0)
    assert [(i["key"], i["rev"]) for i in body["items"]] == [(PAGE_ITEM["key"], 2)]
    assert [lane.rows[(PAGE_ITEM["key"], r)]["state"] for r in (0, 1, 2)] == ["expired", "expired", "new"]
    assert body["items"][0]["age_s"] == 4.0                    # the item's age is the page's (its first tone's)
    lane.answer(0, None, wall=W0 + 4.0, boottime=B0 + 4.0, kind="page")
    # the revision was made at 3.0: it is sendable until 120 s after THAT, and exactly 120 s is expired
    assert lane.request("page", wall=W0 + 122.9, boottime=B0 + 122.9) is not None
    assert lane.request("page", wall=W0 + 123.0, boottime=B0 + 123.0) is None
    assert lane.rows[(PAGE_ITEM["key"], 2)]["state"] == "expired"
    # a change late in a long page is as sendable as the first: made at 125 s, 125 s after the first tone
    page_rev(lane, 3, t=125.0, phase="voice")
    assert statuses(send(lane, hub, "page", t=125.1)) == ["applied"]
    # the larger of two ages: a wall clock stepped forward expires a revision early (the safe side is "old") ...
    page_rev(lane, 4, t=200.0)
    assert lane.request("page", wall=W0 + 200.5 + 3600.0, boottime=B0 + 200.5) is None
    page_rev(lane, 5, t=300.0)                                 # ... stepped back it changes nothing ...
    assert lane.request("page", wall=W0 + 300.5 - 3600.0, boottime=B0 + 300.5) is not None
    # ... and neither ever touches an alarm, which no clock of capture's expires
    press(lane, t=0.0)
    body = lane.request("alarm", wall=W0 + 90000.0 + 3600.0, boottime=B0 + 90000.0)
    assert body["items"][0]["age_s"] == 90000.0 and c.errors("capture-urgent-request", body) == []
    hub.now = W0 + 90000.0
    assert statuses(hub.urgent(body)) == ["stale"]             # clamped to 24 h on the hub, and past any limit


def test_a_page_of_130_s_with_the_app_down_ends_on_the_hub():
    """Every revision reaches the hub by capture alone, the `ended` one too, though it is made after the first tone's
    120 s: each revision has its own 120 s, and `ended` has 10 min (exactly 10 min is expired). And if the `ended`
    one never comes, the hub ends the entry itself, 300 s after the last revision it took."""
    lane, hub = cc.Lane(), cc.Hub()
    page_rev(lane, 0, t=1.0)
    assert statuses(send(lane, hub, "page", t=2.0)) == ["applied"]
    page_rev(lane, 1, t=5.0, phase="voice")
    assert statuses(send(lane, hub, "page", t=5.0)) == ["applied"]
    assert hub.page_live("st-north", PAGE_ITEM["key"])
    page_rev(lane, 2, t=130.0, phase="voice", ended=True, until=W0 + 140.0)
    got = send(lane, hub, "page", t=130.5)
    assert statuses(got) == ["applied"] and got[1]["results"][0]["rev"] == 2
    page = hub.pages[("st-north", PAGE_ITEM["key"])]
    assert page["ended"] and not hub.page_live("st-north", PAGE_ITEM["key"])
    assert page["expires"] == hub.now + 9.5                    # `until` only by its distance from `sent_at`
    # the uplink down: `ended` stays sendable for 10 min after it was made, and not a moment longer
    lane2, hub2 = cc.Lane(), cc.Hub()
    page_rev(lane2, 0, t=1.0)
    send(lane2, hub2, "page", t=5.0)                           # the hub took this revision at 5.0
    page_rev(lane2, 1, t=130.0, ended=True, until=W0 + 140.0)
    assert lane2.request("page", wall=W0 + 729.9, boottime=B0 + 729.9)["items"][0]["rev"] == 1
    lane2.answer(0, None, wall=W0 + 729.9, boottime=B0 + 729.9, kind="page")
    assert lane2.request("page", wall=W0 + 730.0, boottime=B0 + 730.0) is None
    assert lane2.rows[(PAGE_ITEM["key"], 1)]["state"] == "expired"
    hub2.now = W0 + 304.5                                      # the `ended` one never came: 300 s after the last one
    assert hub2.page_live("st-north", PAGE_ITEM["key"])
    hub2.now = W0 + 305.0
    assert not hub2.page_live("st-north", PAGE_ITEM["key"]) and not hub2.pages[("st-north", PAGE_ITEM["key"])]["ended"]
    # a station clock far off does not keep a page alive: the distance is kept within 0 to 300 s
    hub3 = cc.Hub()
    hub3.now = W0
    item = dict(PAGE_ITEM, data=dict(PAGE_ITEM["data"], until=W0 + 99999.0))
    hub3.urgent({"v": 1, "station": "st-north", "sent_at": W0 + 1.0, "items": [item]})
    assert hub3.pages[("st-north", PAGE_ITEM["key"])]["expires"] == W0 + 300.0


def lane_with(n: int = 2) -> cc.Lane:
    lane = cc.Lane()
    for i in range(n):
        press(lane, f"CHA-live-{1900000000123 + i}", t=float(i))
    return lane


OK = {"ok": True, "v": 1, "station": "st-north", "gen": 5}
A, B_ = "CHA-live-1900000000123", "CHA-live-1900000000124"


def named(*pairs) -> dict:
    return dict(OK, results=[{"key": k, "rev": 0, "status": s} for k, s in pairs])


def test_an_answer_makes_an_item_sent_only_when_it_names_it():
    """4.3 "The sender": sent is a 200 whose results name the item applied, duplicate or stale. Nothing else counts."""
    for code, body, sent in (
            (200, named((A, "applied")), 1), (200, dict(OK, results=[]), 0), (200, dict(OK), 0), (204, None, 0),
            (0, None, 0), (503, {"error": "updating", "updating": True}, 0), (401, {"error": "no", "reason": "token_revoked"}, 0),
            (404, {"error": "not found"}, 0), (200, named((A, "from_the_future")), 0),
            (200, dict(OK, results=[{"key": A, "rev": 1, "status": "applied"}]), 0),
            (200, named((A, "duplicate"), (B_, "stale")), 2)):
        lane = lane_with()
        assert len(lane.request("alarm", wall=W0 + 2.0, boottime=B0 + 2.0)["items"]) == 2
        lane.answer(code, body, wall=W0 + 2.1, boottime=B0 + 2.1)
        assert 2 - len(lane.unsent()) == sent, (code, body)
        assert lane.not_delivered() == lane.unsent()
    lane = lane_with(1)
    lane.request("alarm", wall=W0 + 2.0, boottime=B0 + 2.0)
    lane.answer(200, dict(OK, results=[{"key": A, "rev": 0, "status": "rejected", "reason": "kind"}]), wall=W0 + 2.1,
                boottime=B0 + 2.1)
    assert lane.rows[(A, 0)]["state"] == "rejected" and lane.unsent() == []
    assert lane.request("alarm", wall=W0 + 3.0, boottime=B0 + 3.0) is None     # never sent again on this lane
    lane = lane_with(40)                                       # at most 32 items a request, the oldest first
    body = lane.request("alarm", wall=W0 + 50.0, boottime=B0 + 50.0)
    assert len(body["items"]) == 32 and body["items"][0]["key"] == A


def test_an_answer_capture_cannot_read_gets_five_tries_after_the_healthy_items_and_then_the_apps_lane():
    """A status word capture does not know, for an item in a 200 in the hub's shape: the item is kept and tried
    again, at most 5 times, after the items that are healthy and in a request of its own, so it never stands in front
    of another alarm. Only this, of all answers, counts toward the 5. Then it is `rejected` on this lane, and never
    only that: a critical Health item names its key, the station's page shows it as not delivered, and the app's lane
    carries it."""
    lane = lane_with(3)
    C_ = "CHA-live-1900000000125"

    def round_(t, expect):
        body = lane.request("alarm", wall=W0 + t, boottime=B0 + t)
        assert [i["key"] for i in body["items"]] == expect, t
        lane.answer(200, named(*[(k, "applied") for k in expect if k != A], *([(A, "from_the_future")] if A in expect else [])),
                    wall=W0 + t, boottime=B0 + t)
    round_(3.0, [A, B_, C_])                                   # try 1, with the others: they are answered for
    assert lane.unsent() == [A] and lane.rows[(A, 0)]["tries"] == 1
    press(lane, "CHA-live-1900000000200", t=4.0)               # another alarm is pressed
    round_(4.1, ["CHA-live-1900000000200"])                    # it goes first: the doubtful one is not in front of it
    for n, t in enumerate((5.0, 6.0, 7.0), start=2):
        round_(t, [A])                                         # tries 2, 3, 4: alone
        assert lane.unsent() == [A] and lane.rows[(A, 0)]["tries"] == n and lane.health() == []
    lane.crash()
    lane.start(wall=W0 + 8.0, boottime=B0 + 8.0)               # (the count is in the file: a restart does not reset it)
    round_(9.0, [A])                                           # try 5
    assert lane.rows[(A, 0)]["state"] == "rejected" and lane.unsent() == []
    assert lane.request("alarm", wall=W0 + 10.0, boottime=B0 + 10.0) is None
    assert lane.not_delivered() == [A] and len(lane.health()) == 1 and A in lane.health()[0]
    assert lane.health()[0].startswith("critical")
    lane.app_delivered(A)                                      # the hub has it by the other lane
    assert lane.not_delivered() == [] and lane.health() == []


def test_what_is_not_the_hubs_answer_is_no_answer_and_no_try():
    """A 200, 400, 413 or 422 counts only with a body in the hub's shape. `200 {}` five times with three alarms
    pending, a 200 that fails the response schema, a bare 422 from an edge, a 422 with another `reason`: each is no
    answer. All stay `new`, nothing is `sent`, no try is counted, nothing is ever rejected for it. And an item missing
    from a good 200 is sent again, no try counted."""
    lane = lane_with(3)
    for t in range(5):
        body = lane.request("alarm", wall=W0 + 3.0 + t, boottime=B0 + 3.0 + t)
        assert len(body["items"]) == 3                         # never split, never in doubt
        lane.answer(200, {}, wall=W0 + 3.0 + t, boottime=B0 + 3.0 + t)
    assert len(lane.unsent()) == 3 and not lane.tries and not lane.alone
    for bad in (dict(named((A, "applied")), gen=-1), dict(named((A, "applied")), results=[{"key": A}]),
                dict(named((A, "applied")), ok=False)):
        assert c.errors("capture-urgent-response", bad) != [] or bad["ok"] is not True
        lane.request("alarm", wall=W0 + 9.0, boottime=B0 + 9.0)
        lane.answer(200, bad, wall=W0 + 9.0, boottime=B0 + 9.0)
        assert len(lane.unsent()) == 3, bad                    # a 200 that fails the schema: nothing marked sent
    for t in range(6):                                         # a lone pending alarm, an edge's 422s
        one = lane_with(1)
        for k in range(t + 1):
            one.request("alarm", wall=W0 + 10.0 + k, boottime=B0 + 10.0 + k)
            one.answer(422, [None, {}, {"error": "x"}, {"error": "x", "reason": "nope"}, "Unprocessable", None][t],
                       wall=W0 + 10.0 + k, boottime=B0 + 10.0 + k)
        assert one.unsent() == [A] and not one.alone and not one.tries and one.health() == []
    lane = lane_with(2)
    lane.request("alarm", wall=W0 + 3.0, boottime=B0 + 3.0)
    lane.answer(200, named((B_, "applied")), wall=W0 + 3.0, boottime=B0 + 3.0)
    assert lane.unsent() == [A] and not lane.tries                                  # missing from the results: no try
    assert [i["key"] for i in lane.request("alarm", wall=W0 + 4.0, boottime=B0 + 4.0)["items"]] == [A]


def test_a_hub_refusal_splits_the_request_and_only_a_second_refusal_alone_is_final():
    """The hub's own 400, 413 or 422 (its shape, `reason` bad_json, too_large or bad_batch) refused the request whole
    and applied nothing. The first refusal of any request, of one item or many, marks each of its items to go
    alone; only the refusal of a request of one item that was already alone makes it `rejected`. So a lone pending
    alarm needs two such refusals of itself to be final; then it rides the app's lane with a critical Health item."""
    bad = {"error": "not a version 1 request of 1 to 32 whole items", "reason": "bad_batch"}
    lane = lane_with(1)
    lane.request("alarm", wall=W0 + 1.0, boottime=B0 + 1.0)
    lane.answer(422, bad, wall=W0 + 1.1, boottime=B0 + 1.1)
    assert lane.rows[(A, 0)]["state"] == "new" and lane.alone == {(A, 0)} and lane.health() == []
    assert [i["key"] for i in lane.request("alarm", wall=W0 + 2.0, boottime=B0 + 2.0)["items"]] == [A]
    lane.answer(422, bad, wall=W0 + 2.1, boottime=B0 + 2.1)
    assert lane.rows[(A, 0)]["state"] == "rejected" and lane.request("alarm", wall=W0 + 3.0, boottime=B0 + 3.0) is None
    assert lane.not_delivered() == [A] and len(lane.health()) == 1
    for code, reason in ((400, "bad_json"), (413, "too_large"), (422, "bad_batch")):
        lane, hub = lane_with(3), cc.Hub()
        assert len(lane.request("alarm", wall=W0 + 3.0, boottime=B0 + 3.0)["items"]) == 3
        lane.answer(code, dict(bad, reason=reason), wall=W0 + 3.1, boottime=B0 + 3.1)
        assert len(lane.unsent()) == 3 and lane.health() == []                 # nothing was applied, nothing given up
        sizes = []
        while (got := send(lane, hub, t=4.0 + len(sizes))) is not None:
            sizes.append(len(got[1]["results"]))
        assert sizes == [1, 1, 1] and lane.unsent() == [] and len(hub.alarms) == 1 and len(hub.pushes) == 1
        # (three presses of one radio within REPEAT_S: the alarm code joins them; each item is `applied`)


def test_the_memory_bound_never_drops_an_alarm_that_has_no_row(monkeypatch):
    """At most 256 items in the senders' memory: page items, and the copies of items that have a row (those are read
    back from the file). An alarm WITHOUT a row is never dropped to make room: a page item goes, the oldest first.
    Such alarms are bounded only at 10,000, past which the oldest is written out as a CRITICAL line and let go."""
    lane = cc.Lane()
    lane.disk = "full"                                         # nothing can be written: no item has a row
    for i in range(100):
        lane.press(f"page:PAGE 1:{1900000000 + i}.00", "page", PAGE_ITEM["data"], wall=W0 + i, boottime=B0 + i)
    for i in range(200):
        press(lane, f"CHA-live-{1900000100000 + i}", t=100.0 + i)
    kinds = [it["kind"] for it in lane.memory.values()]
    assert len(lane.memory) == 256 and kinds.count("alarm") == 200 and kinds.count("page") == 56      # 300 were waiting
    assert min(k for k, _ in lane.memory if k.startswith("page")) == "page:PAGE 1:1900000044.00"      # the oldest went
    for i in range(200, 300):
        press(lane, f"CHA-live-{1900000100000 + i}", t=100.0 + i)
    kinds = [it["kind"] for it in lane.memory.values()]
    assert kinds.count("alarm") == 300 and kinds.count("page") == 0 and lane.log == []                # over 256: alarms stay
    assert len(lane.unsent()) == 300 and len(lane.request("alarm", wall=W0 + 500.0, boottime=B0 + 500.0)["items"]) == 32
    # with rows, the copies in memory may go: the file has them, and every one is still sent
    lane, hub = cc.Lane(), cc.Hub()
    for i in range(300):
        press(lane, f"CHA-live-{1900000100000 + i}", t=float(i), data=dict(ALARM_DATA, radio_id=str(8000 + i)))
    assert len(lane.memory) == 256 and len(lane.rows) == 300 and len(lane.unsent()) == 300
    sent = 0
    while (got := send(lane, hub, t=400.0 + sent)) is not None:
        sent += len(got[1]["results"])
    assert sent == 300 == len(hub.alarms) and lane.unsent() == []
    # the one hard stop
    monkeypatch.setattr(cc, "ALARMS_WITHOUT_A_ROW", 50)
    lane = cc.Lane()
    lane.disk = "full"
    for i in range(60):
        press(lane, f"CHA-live-{1900000100000 + i}", t=float(i))
    assert len(lane.memory) == 50 and len(lane.log) == 10 and lane.log[0].startswith("CRITICAL")
    assert "CHA-live-1900000100000" in lane.log[0] and "CH A" in lane.log[0] and "7001" in lane.log[0]


def test_new_rows_are_loaded_at_start_oldest_alarm_first_and_a_page_only_if_still_sendable():
    lane = cc.Lane()
    lane.press(PAGE_ITEM["key"], "page", PAGE_ITEM["data"], wall=W0, boottime=B0)
    press(lane, "CHA-live-1900000001000", t=1.0)
    lane.press("page:PAGE 1:1900000090.00", "page", PAGE_ITEM["data"], wall=W0 + 90.0, boottime=B0 + 90.0)
    press(lane, "CHA-live-1900000002000", t=2.0)
    lane.press("page:PAGE 1:1900000005.00", "page", dict(PAGE_ITEM["data"], ended=True), wall=W0 + 5.0, boottime=B0 + 5.0)
    lane.crash()
    lane.start(wall=W0 + 130.0, boottime=B0 + 130.0)
    assert list(lane.memory) == [("CHA-live-1900000001000", 0), ("CHA-live-1900000002000", 0),
                                 ("page:PAGE 1:1900000090.00", 0), ("page:PAGE 1:1900000005.00", 0)]
    assert lane.rows[(PAGE_ITEM["key"], 0)]["state"] == "expired"              # 130 s old: live state, not sent
    assert [f["durable"] for f in lane.frames()] == [True] * 4


def test_a_p25_emergency_bit_alarms_on_a_transition_only_on_the_boot_clock():
    """#775 (Q-B), capture's side: one alarm per press; a stuck bit never re-alarms; the 10 min are on the BOOT clock,
    so a wall clock stepped back cannot hide a real new press and one stepped forward cannot re-alarm a stuck bit; a
    restart of capture does not re-alarm, and across a reboot the next set bit alarms, once."""
    def seen(bits, t, radio="7001", system="examplesys", step=0.0, boot="boot-1"):
        return bits.seen(system, radio, wall=W0 + t + step, boottime=B0 + t, boot=boot)
    bits = cc.P25Bits()
    assert seen(bits, 0.0) is True
    assert [seen(bits, t) for t in (2, 30, 300, 890, 1480)] == [False] * 5     # still set: the last-set time moves on
    assert seen(bits, 10.0, radio="7002") is True and seen(bits, 20.0, system="othersys") is True
    assert seen(bits, 2079.0) is False and seen(bits, 2679.0) is True          # 599 s: still set; exactly 10 min: a new press
    restarted = cc.P25Bits()
    restarted.rows = dict(bits.rows)                           # the p25_bit table outlives capture
    assert seen(restarted, 2700.0) is False
    assert bits.per_hour(boottime=B0 + 2700.0, boot="boot-1") == 4 and bits.per_hour(boottime=B0 + 3600.0, boot="boot-1") == 3
    # the wall clock stepped back an hour, then a real new press 30 min later: the boot clock shows 30 min
    bits = cc.P25Bits()
    assert seen(bits, 0.0) is True and seen(bits, 1800.0, step=-3600.0) is True
    # the wall clock stepped forward an hour, a stuck bit 2 s later: the wall clock outran the boot clock by an hour,
    # which is what a frozen boot clock looks like: counted as not set, ONE repeated alarm; the next sighting nothing
    bits = cc.P25Bits()
    assert seen(bits, 0.0) is True and seen(bits, 2.0, step=3600.0) is True and seen(bits, 4.0, step=3600.0) is False
    # the host slept 20 min with the boot clock frozen (same boot), then a real new press: the wall clock shows 20 min
    bits = cc.P25Bits()
    assert seen(bits, 0.0) is True
    assert bits.seen("examplesys", "7001", wall=W0 + 1205.0, boottime=B0 + 5.0, boot="boot-1") is True
    # the edge: the wall clock ahead of the boot clock by exactly 60 s more is not over 60 s
    bits = cc.P25Bits()
    assert seen(bits, 0.0) is True and seen(bits, 30.0, step=60.0) is False and seen(bits, 60.0, step=120.5) is True
    # across a reboot the boot clock says nothing: the row counts as not set lately. One repeated alarm, never a miss
    assert bits.seen("examplesys", "7001", wall=W0 + 60.0, boottime=12.0, boot="boot-2") is True
    assert bits.seen("examplesys", "7001", wall=W0 + 62.0, boottime=14.0, boot="boot-2") is False
    assert set(bits.rows[("examplesys", "7001")]) == {"last_set", "last_set_boot", "boot"}


def test_capture_matches_a_finished_call_to_its_live_alarm_on_the_station():
    """#775 (Q-I): the match of a live P25 alarm and its call is made by capture's ingest, on the station, where both
    times are on one clock: system + talkgroup + unit, the grant inside the call's span, 10 s either side, the edges
    included. The call's sidecar then carries the alarm's key, and the hub compares no clocks. When that fails and
    capture saw a wall step (over 5 s, `WallSteps`) since the grant, the boot clock decides."""
    p25 = SCENARIOS["items"]["p25"]
    lane = cc.Lane()
    lane.press(p25["key"], "alarm", p25["data"], wall=W0 + 300.02, boottime=B0 + 300.02, event_at=W0 + 300.0,
               event_boot=B0 + 300.0)
    press(lane, t=1.0)                                         # (an analog alarm in the file: never a match)
    rows = list(lane.rows.values())

    def match(start, end, taken=314.0, step=0.0, boot="boot-1", **other):
        """The wall clock stepped by `step` just after the grant; capture's tick saw it if it is over 5 s."""
        steps = cc.WallSteps()
        steps.sample(wall=W0 + 300.5, boottime=B0 + 300.5)
        steps.sample(wall=W0 + 301.5 + step, boottime=B0 + 301.5)
        call = {"system": "examplesys", "talkgroup": 4101, "unit": "7001", "start": W0 + start + step,
                "end": W0 + end + step, **other}
        return cc.match_call(rows, call, wall=W0 + taken + step, boottime=B0 + taken, boot=boot, steps=steps)
    assert match(300.0, 312.0) == p25["key"]
    assert match(310.0, 322.0, taken=324.0) == p25["key"] and match(310.5, 322.5, taken=324.5) == ""      # +-10 s,
    assert match(278.0, 290.0) == p25["key"] and match(277.5, 289.5) == ""                               # inclusive
    assert match(300.0, 312.0, talkgroup=4102) == "" and match(300.0, 312.0, unit="7002") == ""
    assert match(300.0, 312.0, system="othersys") == ""
    # the wall clock stepped back an hour between the grant and the call's end: the wall test fails, the boot clock decides
    assert match(300.0, 312.0, step=-3600.0) == p25["key"]
    assert match(300.0, 312.0, step=3600.0) == p25["key"]
    assert match(600.0, 612.0, taken=614.0, step=-3600.0) == ""                # another call, 5 min later: not this alarm's
    assert match(300.0, 312.0, taken=300.0 + 12.0 + 69.9, step=-3600.0) == p25["key"]     # the call's length plus 70 s:
    assert match(300.0, 312.0, taken=300.0 + 12.0 + 70.0, step=-3600.0) == ""             # exactly that is outside
    # a step of 6 s and a slow ingest: the wall test still holds, and it is tried first (the boot rule would miss it)
    assert match(310.0, 322.0, taken=391.0, step=-6.0) == p25["key"]
    # 3 s is no step: only the wall test, so a grant 13 s before the call (by the wall) is not this call's
    assert match(313.0, 325.0, taken=327.0) == "" and match(316.0, 328.0, taken=330.0, step=-3.0) == ""
    # ... and what it gives is what the sidecar and the hub's take-over need
    side = valid("capture-spool-sidecar")["call-matched-to-its-live-alarm.json"]
    assert side["alarm_key"] == p25["key"] == valid("capture-urgent-item")["alarm-p25-live.json"]["key"]
    hub = cc.Hub()
    hub.now = W0 + 240.1                                       # the station is 60 s ahead of the hub
    hub.urgent(lane.request("alarm", wall=W0 + 300.1, boottime=B0 + 300.1))
    hub.now = W0 + 254.0
    hub.event("st-north", {"type": "alarm", "id": side["id"], "alarm_key": match(300.0, 312.0), "radio_id": "7001",
                           "source": "Fire Dispatch 1", "system": "examplesys", "talkgroup": 4101, "start": W0 + 300.0,
                           "end": W0 + 312.0})
    assert [a["traffic_id"] for a in hub.alarms if a["radio_id"] == "7001" and a["system"]] == [side["id"]]
    assert len([p for p in hub.pushes if p["alarm"] == hub.alarms[0]["id"]]) == 1


def test_the_urgent_file_is_the_designs_ddl_and_keeps_its_promises(tmp_path):
    """contracts/capture/urgent.sql is the DDL of docs/native/capture-split.md 4.3, byte for byte (compared with the
    document once it is in the tree). It runs; (key, rev) is the row's identity; a row's `data` is the item's; and it
    holds everything the resend rule and the P25 transition rule need to outlive capture."""
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
        assert cols == ["n", "key", "rev", "kind", "at", "at_boot", "event_at", "event_boot", "boot", "data", "state",
                        "sent_at", "sent_boot", "sent_boot_id", "hub", "hub_gen", "tries", "last_error"]
        held = valid("capture-socket-event")["urgent-alarm.json"]
        row = (held["key"], held["rev"], held["kind"], held["at"], 5114.36, held["event_at"], held["event_boot"],
               held["boot"], json.dumps(held["data"]))
        insert = ("INSERT OR IGNORE INTO urgent (key, rev, kind, at, at_boot, event_at, event_boot, boot, data) "
                  "VALUES (?,?,?,?,?,?,?,?,?)")
        assert db.execute(insert, row).rowcount == 1 and db.execute(insert, row).rowcount == 0     # (key, rev) once
        assert db.execute(insert, (row[0], 1, *row[2:])).rowcount == 1                 # another rev: another row
        n, state, data_ = db.execute("SELECT n, state, data FROM urgent WHERE key = ? AND rev = 0",
                                     (held["key"],)).fetchone()
        assert (n, state) == (1, "new") and json.loads(data_) == held["data"]
        back = dict(zip(("n", "key", "rev", "kind", "at", "event_at", "event_boot", "boot"), db.execute(
            "SELECT n, key, rev, kind, at, event_at, event_boot, boot FROM urgent WHERE n = 1").fetchone()),
            data=json.loads(data_), durable=True)
        assert c.subschema("capture-urgent-item", "#/$defs/held").is_valid(back)
        db.execute("UPDATE urgent SET state = 'sent', hub = 'applied', hub_gen = ?, sent_at = ?, sent_boot = ?, "
                   "sent_boot_id = ? WHERE n = 1", ("9007199254740993", 1900000000.44, 5114.41, held["boot"]))
        assert db.execute("SELECT hub_gen FROM urgent WHERE n = 1").fetchone()[0] == "9007199254740993"    # text: exact
        db.execute("INSERT INTO meta VALUES ('v', '1'), ('urgent_epoch', '9c2d7e10a4b35f68'), ('gen', '412'), "
                   "('gen_seen_at', '1900000000.44'), ('gen_seen_boot', '5114.41'), ('gen_seen_boot_id', ?)", (held["boot"],))
        assert [r[1] for r in db.execute("PRAGMA table_info(p25_bit)")] == ["system", "radio", "last_set", "last_set_boot",
                                                                           "boot"]
        db.execute("INSERT INTO p25_bit VALUES ('examplesys', '7001', 1900000300.0, 5414.0, ?)", (held["boot"],))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO p25_bit VALUES ('examplesys', '7001', 1900000301.0, 5415.0, ?)", (held["boot"],))
    finally:
        db.close()
    held_keys = set(c.load("capture-urgent-item")["$defs"]["held"]["properties"])
    assert held_keys - {"durable"} <= set(cols)                                        # the held item is the row's columns
    # the reference models keep exactly what the file has columns for
    lane = cc.Lane()
    press(lane, t=0.39)
    assert set(lane.rows[(KEY, 0)]) <= set(cols) and {"at_boot", "sent_boot", "sent_boot_id", "hub_gen"} <= set(lane.rows[(KEY, 0)])
    lane.hello(200, {"ok": True, "station": "st-north", "token": "own", "gen": 412}, wall=W0, boottime=B0)
    for key in lane.meta:
        assert f"'{key}'" in ddl, key
    bits = cc.P25Bits()
    bits.seen("examplesys", "7001", wall=W0, boottime=B0, boot=BOOT)
    assert set(bits.rows[("examplesys", "7001")]) | {"system", "radio"} == {
        "system", "radio", "last_set", "last_set_boot", "boot"}


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
    assert hello["urgent_epoch"] == valid("capture-urgent-request")["an-alarm.json"]["urgent_epoch"]
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
        {k: item[k] for k in ("key", "rev", "kind", "at", "event_at", "data")}         # `at`: the item's, everywhere
    reply = valid("capture-socket-reply")["urgent-since-page.json"]
    since = c.subschema("capture-socket-reply", "#/$defs/urgent-since-result")
    assert list(since.iter_errors(reply["result"])) == []
    result = reply["result"]
    assert all(r["durable"] and r["n"] > 56 for r in result["rows"]) and result["more"] is False
    assert all(m["n"] is None and not m["durable"] for m in result["memory"]) and result["memory"]
    for bad in ({k: v for k, v in result.items() if k != "memory"}, dict(result, rows=[{"n": 57}]),
                dict(result, more="no"), dict(result, rows=[dict(result["rows"][0], n=None)]),
                dict(result, memory=[dict(result["memory"][0], n=3)])):
        assert list(since.iter_errors(bad)) != []
    ask = valid("capture-socket-request")["urgent-since.json"]
    assert ask["id"] == reply["id"] and ask["args"]["n"] == 56 and ask["args"]["urgent_epoch"] == result["urgent_epoch"]
    limit = c.load("capture-socket-request")["$defs"]["args-urgent_since"]["properties"]["limit"]
    assert (limit["default"], limit["maximum"]) == (200, 500)
    taken = valid("capture-socket-request")["urgent-taken.json"]
    assert (taken["urgent_epoch"], taken["n"]) == (result["urgent_epoch"], 57)         # both: capture ignores another epoch's
    assert c.errors("capture-socket-request", {k: v for k, v in taken.items() if k != "urgent_epoch"}) != []
    snap = c.subschema("capture-socket-event", "#/$defs/snapshot")                     # the `snapshot` request's result
    assert snap.is_valid({k: v for k, v in ev["snapshot.json"].items() if k not in ("v", "t", "at")})
    assert all("dispatcher" in e for e in ev["talking.json"]["entries"])               # `LiveTalk`'s own: it stays


def apply_pick(op, args):
    """A stand-in for capture's ops: (ok, reason, error, confirm, result), or None for an op it does not know."""
    if op == "pick":
        if args.get("job") == "PAGE 1" and not args.get("confirm"):
            return False, "confirm", "PAGE 1 is a fire job: move it off sdr-a anyway?", True, None
        return (False, "parked", "refused: parked", False, None) if args.get("sdr") == "sdr-b" else (True, "", "", False, None)
    if op == "squelch_apply":
        running = {"open_db": 11.0, "close_db": 7.0}           # (someone else applied a value in between)
        return (False, "stale", "the squelch changed since this suggestion was made", False, None) \
            if args.get("expect", running) != running else (True, "", "", False, None)
    if op == "probe":
        return True, "", "", False, "audio"
    if op == "credentials":
        return True, "", "", False, None
    return None


def test_a_request_is_applied_once_by_its_id_and_never_after_its_deadline():
    """docs/native/capture-split.md 4.2 "Rule": a repeated id gets the first reply again and changes nothing; a request
    that reaches capture at or after `expires_boot` is answered `expired` and not applied, whatever the wall clock
    did; an unknown op is `unsupported`; a viewer is `forbidden`; a refusal that needs the person's yes is asked again
    under a new id. A refusal has a `reason` word for the client and an `error` text for the person."""
    ask = valid("capture-socket-request")
    rq = cc.Requests(apply_pick)

    def go(req, boottime=5329.0, wall=1900000200.0, role="reader", uid=1000):
        reply = rq.handle(req, role=role, uid=uid, boottime=boottime, wall=wall)
        assert c.errors("capture-socket-reply", reply) == [], reply
        assert c.errors("capture-socket-event", reply) == [] and reply["id"] == req["id"]
        assert reply["ok"] or reply["reason"] in cc.REASONS
        return reply
    pick = ask["pick.json"]
    first = go(pick)
    assert (first["ok"], first["reason"], first["error"], first["confirm"]) == (False, "parked", "refused: parked", False)
    assert first == dict(valid("capture-socket-reply")["refused-parked.json"], at=first["at"])
    ok = dict(pick, id="r-1", args={"sdr": "sdr-c", "job": "CH A"})
    assert go(ok)["ok"] is True and rq.applied == [("r-1", "pick")]
    assert go(ok, boottime=5329.5)["ok"] is True and rq.applied == [("r-1", "pick")]   # sent again: answered, not applied
    assert go(ok, boottime=5400.0)["ok"] is True and len(rq.applied) == 1              # ... even after its deadline
    assert go(dict(ok, id="r-2a"), boottime=5329.999)["ok"] is True                    # under the deadline: applied
    assert go(dict(ok, id="r-2"), boottime=5330.0)["reason"] == "expired"              # exactly at it: outside
    assert go(dict(ok, id="r-2b"), boottime=5330.5)["reason"] == "expired" and len(rq.applied) == 2
    assert go(dict(ok, id="r-3"), boottime=5329.9, wall=1900000200.0 + 7200.0)["ok"] is True       # a stepped wall clock
    assert go(dict(ok, id="r-4"), boottime=5329.9, wall=1900000200.0 - 7200.0)["ok"] is True       # expires nothing
    fire = dict(pick, id="r-5", args={"sdr": "sdr-a", "job": "PAGE 1", "confirm": False})
    asked = go(fire)
    assert (asked["ok"], asked["reason"], asked["confirm"]) == (False, "confirm", True) and asked["error"].endswith("?")
    same_id = go(dict(fire, args=dict(fire["args"], confirm=True)))
    assert same_id["ok"] is False                              # the same id again: the first reply again, whatever it says
    assert go(dict(fire, id="r-6", args=dict(fire["args"], confirm=True)))["ok"] is True   # the yes goes under a new id
    assert go(ask["an-op-from-the-future.json"])["reason"] == "unsupported"
    assert go(dict(ok, id="r-7"), role="viewer")["reason"] == "forbidden" and len(rq.applied) == 5
    stale = go(ask["squelch-apply-with-expect.json"])
    assert (stale["ok"], stale["reason"]) == (False, "stale")  # what runs is not `expect`: refused, as #685 and #700
    assert go(dict(ask["squelch-apply-plain-set.json"], id="r-8"))["ok"] is True       # without `expect`: a plain set
    assert go(ask["probe.json"])["result"] == "audio"
    for i in range(cc.REQUEST_IDS):                            # the id memory is bounded: 256 ids, 10 min
        go(dict(ok, id=f"fill-{i}"), boottime=5329.0)
    assert len(rq.seen) <= cc.REQUEST_IDS + 1
    for gap, applied in ((cc.REQUEST_IDS_S - 0.5, 1), (cc.REQUEST_IDS_S, 2)):          # remembered under 10 min; at
        rq2 = cc.Requests(apply_pick)                                                 # exactly 10 min, forgotten
        rq2.handle(ok, role="reader", uid=1000, boottime=5000.0, wall=1900000000.0)
        rq2.handle(dict(ok, expires_boot=6000.0), role="reader", uid=1000, boottime=5000.0 + gap, wall=1900000700.0)
        assert len(rq2.applied) == applied, gap
    known = {valid("capture-socket-reply")[f]["reason"] for f in valid("capture-socket-reply") if "reason" in valid("capture-socket-reply")[f]}
    assert known == {*cc.REASONS, "from_the_future"}           # a vector for every word, and one for a word of tomorrow


def test_the_credentials_request_is_roots_alone():
    """docs/native/capture-split.md 4.3 "Rotation without a restart", 7.5: capture accepts `credentials` only from a
    peer whose uid is 0 (SO_PEERCRED), whatever role it declared; anyone else is `forbidden`. It has a deadline like
    every request, it is never kept in the memory of answered requests, and it is the one frame that carries a
    secret."""
    creds = valid("capture-socket-request")["credentials-from-the-root-helper.json"]
    rq = cc.Requests(apply_pick)
    for role in ("reader", "viewer"):
        refused = rq.handle(creds, role=role, uid=1000, boottime=5329.0, wall=1900000200.0)
        assert (refused["ok"], refused["reason"]) == (False, "forbidden") and c.errors("capture-socket-reply", refused) == []
        assert rq.handle(creds, role=role, uid=0, boottime=5329.0, wall=1900000200.0)["ok"] is True
    assert rq.seen == {} and len(rq.applied) == 2              # never kept in the id memory: no reply replays a secret
    late = rq.handle(creds, role="reader", uid=0, boottime=creds["expires_boot"], wall=1900000200.0)
    assert (late["ok"], late["reason"]) == (False, "expired") and len(rq.applied) == 2     # root's too has its deadline
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
    assert "required" not in wish["paths"]                     # each optional, with the station's default
    assert "rtl_fm" not in json.dumps(c.load("capture-wish")["properties"]["paths"])   # nothing capture executes
    spool = wish["spool"]["properties"]
    assert list(spool) == ["max_age_h", "max_bytes", "max_items", "min_free_bytes", "sync_s"]
    for key, default in (("max_age_h", "12"), ("max_bytes", str(2 ** 31)), ("max_items", "50000"),
                         ("min_free_bytes", str(2 ** 28)), ("sync_s", "5")):
        assert f"Default {default}." in spool[key]["description"], key
    assert valid("capture-wish")["service.json"]["spool"] == {"max_age_h": 12, "max_bytes": 2 ** 31, "max_items": 50000,
                                                             "min_free_bytes": 2 ** 28, "sync_s": 5}
    assert wish["mode"]["x-known"] == ["service", "shadow"]
    running, service = valid("capture-status")["running.json"], valid("capture-wish")["service.json"]
    assert running["wish_seq"] == service["seq"] and running["urgent"]["urgent_epoch"] == "9c2d7e10a4b35f68"
    assert [ch["label"] for ch in running["channels"]] == [ch["label"] for ch in service["channels"]]
    assert cc.slug_clashes([ch["label"] for ch in service["channels"]]) == []
    assert running["channels"][0]["squelch"] == service["squelch"]["CH A"]             # the wish's values, as running
    assert running["sdrs"][1]["set_by"] == "auto" and "sdr-b" not in service["dongles"]["jobs"]    # capture's own choice


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
    """`urgent.state` is one word, the first that holds of off, no_credentials, no_route, refused, retrying, ok. A
    reset of the urgent file is not a state (it can hold beside any of them): `resets` and `reset_at`."""
    urgent = c.load("capture-status")["properties"]["urgent"]
    assert urgent["properties"]["state"]["x-known"] == list(cc.URGENT_STATES) == [
        "off", "no_credentials", "no_route", "refused", "retrying", "ok"]
    assert urgent["properties"]["mode"]["x-known"] == ["write", "send"]               # never `off`: service treats it as write
    assert list(urgent["properties"])[-2:] == ["resets", "reset_at"]
    send = {"mode": "send", "forward": True, "caps": ["urgent"], "credentials": True, "last": "ok"}
    assert cc.urgent_state(**send) == "ok"
    for change, state in (({"mode": "write"}, "off"), ({"forward": False}, "off"), ({"caps": ["jobs"]}, "off"),
                          ({"credentials": False}, "no_credentials"), ({"last": "no_route"}, "no_route"),
                          ({"last": "refused"}, "refused"), ({"last": "retrying"}, "retrying"),
                          ({"mode": "write", "credentials": False, "last": "refused"}, "off"),          # the first that holds
                          ({"credentials": False, "last": "no_route"}, "no_credentials"),
                          ({"caps": [], "last": "retrying"}, "off")):
        assert cc.urgent_state(**{**send, **change}) == state, change
    states = {inst["urgent"]["state"] for inst in valid("capture-status").values()}
    assert states == set(cc.URGENT_STATES)                     # a vector for each, and `reset` is no state any more
    reset = valid("capture-status")["the-urgent-file-was-reset.json"]["urgent"]
    assert (reset["resets"], reset["state"]) == (1, "ok") and reset["reset_at"] and reset["hub_gen"] is None
    assert valid("capture-status")["running.json"]["urgent"]["reset_at"] is None
    none = valid("capture-status")["no-credentials.json"]["urgent"]
    assert none["creds_at_boot"] is None and none["mode"] == "send"                    # it does everything but send
    unsent = valid("capture-status")["an-alarm-not-delivered.json"]["urgent"]
    assert unsent["unsent"][0]["age_s"] >= 10 and unsent["pending_alarms"] == len(unsent["unsent"]) == 1
    assert valid("capture-status")["an-alarm-in-memory-only.json"]["urgent"]["undurable"] == 1
    rejected = valid("capture-status")["a-rejected-alarm-not-delivered.json"]["urgent"]
    assert rejected["rejected"] == 1 and rejected["pending_alarms"] == 0 and len(rejected["unsent"]) == 1
    refused = valid("capture-status")["a-part-of-the-wish-refused.json"]
    assert {r["what"] for r in refused["refusals"]} <= set(
        c.load("capture-status")["properties"]["refusals"]["items"]["properties"]["what"]["x-known"])
    assert all(r["wish_seq"] == refused["wish_seq"] for r in refused["refusals"])


def test_a_status_file_is_stale_after_15_s_on_the_readers_boot_clock():
    status = dict(valid("capture-status")["running.json"], at_boot=5114.0)
    assert not cc.capture_down(status, boottime=5114.0 + 14.9, boot_now=status["boot"])
    assert cc.capture_down(status, boottime=5114.0 + 15.0, boot_now=status["boot"])    # exactly the limit is outside
    assert cc.capture_down(status, boottime=3.0, boot_now=OTHER_BOOT)                  # a file from before a reboot
    assert status["at"] > c.TIME_LIMIT / 100 > status["at_boot"]                       # `at` is for people, `at_boot` decides


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
    tests/test_contracts_routes.py both fail until the row is promoted. Until then a client string for it under
    sizeup/ is accepted by the wider net there, so capture's sender can merge with or after the hub's route."""
    table = c.routes()
    rows = table["designed"]
    assert [(r["method"], r["path"]) for r in rows] == [("POST", "/api/station/urgent")]
    row = rows[0]
    assert set(row) - {"note", "designed"} == {"method", "path", "credential", "role", "request", "response"}
    assert row["credential"] == ["sz1"] and row["role"] == {"sz1": ["radio-receive"], "sw1": "never"}
    assert "shared" not in row["credential"] and "shared station token" in row["note"]        # it does not open the route
    assert set(row["role"]["sz1"]) <= set(c.load("roles")["properties"])
    assert (row["request"], row["response"]) == ("capture-urgent-request", "capture-urgent-response")
    assert row["designed"] == {"design": "docs/native/capture-split.md 4.3, 7.5, 10, 11 (#773)", "body_max_bytes": 65536,
                               "items_max": 32, "seen": False, "idempotent_by": ["station", "key", "rev"],
                               "not_leader": 503}
    assert (row["designed"]["body_max_bytes"], row["designed"]["items_max"]) == (cc.REQUEST_MAX_BYTES, cc.REQUEST_MAX_ITEMS)
    events = next(r for r in table["routes"] if r["path"] == "/api/station/events")
    assert set(row["role"]["sz1"]) <= set(events["role"]["sz1"])               # "as /api/station/events is"
    listed = {c.example(r["path"]) for r in table["routes"]}
    for r in rows:                                             # a designed row the hub starts serving must be promoted
        assert r["path"] not in listed, f"{r['path']} is in `routes` now: take it out of `designed`"
        assert c.example(r["path"]) not in c.served_paths(), \
            f"the hub serves {r['path']} now: move its row from `designed` to `routes` in contracts/routes.json"
    assert not [p for r in table["reserved"] for p in [row["path"]] if p.startswith(r["prefix"])]
    routes_test = (ROOT / "tests" / "test_contracts_routes.py").read_text(encoding="utf-8")
    assert 'ROWS + TABLE.get("designed", [])' in routes_test   # the wider net takes a designed route's client string
    live = c.Live(tmp_path)
    try:
        body = valid("capture-urgent-request")["an-alarm.json"]
        code, answer = live.call("POST", row["path"], body, token="sz1")
        assert (code, answer) == (404, {"error": "not found"})                 # today's hub: `no_route` for the sender
        assert live.call("POST", row["path"], body, token=None)[0] == 401
    finally:
        live.close()


def test_the_readme_maps_every_schema_to_its_design_section_and_says_what_is_still_open():
    section = README.split("## Capture split (designed)", 1)[1].split("\n## ", 1)[0]
    for name in cc.NAMES:
        assert re.search(rf"^\| `{name}` \| (designed) \| 1 \|", README, re.M), name
        assert re.search(rf"^\| `{name}`.*\| 4\.\d", section, re.M), f"{name}: its design section"
    files = ["spool.md", "socket.md", "urgent.md", "urgent.sql", "urgent-scenarios.json", "socket-frames.json",
             "modes.json"]
    for file in files:
        assert (cc.CAPTURE / file).is_file() and f"`{file}`" in section, file
    assert sorted(p.name for p in cc.CAPTURE.iterdir()) == sorted(files)
    top = re.findall(r"^\| (C\d+(?: \+ C\d+)?) \|", section, re.M)             # the decisions with the most weight, first
    assert top == ["C29", "C18", "C20", "C19", "C15", "C11 + C8"]
    still = re.findall(r"^\| (S\d+) \|", section, re.M)                        # what the design does not answer
    assert still == [f"S{i}" for i in range(1, len(still) + 1)] and 5 <= len(still) <= 15
    found = re.findall(r"^\| (Y\d+) \|", section, re.M)                        # where the design is still ambiguous
    assert found == [f"Y{i}" for i in range(1, len(found) + 1)] and len(found) >= 5
    assert "Still to confirm" in section and "radio maintainers" in section and "225b06ff" in section
    assert "A7" in section and "not decided" in section
    for word in ("specification aid", "not production code"):
        assert word in section or word in (ROOT / "tests" / "contracts_capture.py").read_text(encoding="utf-8").lower()
    for doc in ("spool.md", "socket.md", "urgent.md"):         # no page points at a choice that is no longer listed
        assert not re.search(r"\(C\*?\d|C\*n\*", (cc.CAPTURE / doc).read_text(encoding="utf-8")), doc


# -- the second adversarial review (docs/native/capture-split.md at 225b06ff) ---------------------------------------------

def test_the_hub_refuses_a_request_that_is_not_well_formed_and_applies_nothing():
    """A request of another version, of none, or with an item without its age is refused whole (422 `bad_batch`):
    the hub never applies it with a made-up age."""
    for file in ("version-2.json", "no-version.json", "an-item-without-its-age.json", "items-empty.json",
                 "items-as-an-object.json", "no-items.json"):
        body = dict(c.vectors("capture-urgent-request", "invalid"))[file]
        hub = cc.Hub()
        code, answer = hub.urgent(body)
        assert (code, answer["reason"]) == (422, "bad_batch") and hub.alarms == [] and hub.store == {}, file
        assert cc.hub_shaped(code, answer)                     # the sender reads it as the hub's refusal


def test_the_press_window_is_on_the_hubs_receive_time_and_a_negative_gap_is_a_new_press():
    """Two station wall times are never compared: the window is timed on when the hub received each flag. A flag
    whose gap is negative (the hub's own clock stepped back) is not "within": it is judged on its own."""
    p25 = SCENARIOS["items"]["p25"]
    ev = {"type": "alarm", "radio_id": "7001", "source": "Fire Dispatch 1", "system": "examplesys", "talkgroup": 4101}
    hub = cc.Hub()
    hub.now = T0 + 300.1
    assert statuses(hub.urgent({"v": 1, "station": "st-north", "items": [p25]})) == ["applied"]
    assert hub.windows[("examplesys", "7001")] == {"alarm": 1, "last": T0 + 300.1, "cleared": False}
    hub.now = T0 + 400.0                                       # the station's times say an hour later: they do not count
    hub.event("st-north", dict(ev, id="call_2", start=T0 + 3900.0, end=T0 + 3912.0))
    assert len(hub.alarms) == 1 and hub.alarms[0]["still_set"] == 1
    hub.now = T0 + 350.0                                       # the hub's clock stepped back 50 s: a negative gap
    hub.event("st-north", dict(ev, id="call_3", start=T0 + 340.0, end=T0 + 349.0))
    assert len(hub.alarms) == 1 and len(hub.pushes) == 1       # judged on its own: the same-radio rule joins it
    assert hub.alarms[0]["still_set"] == 1
    hub = cc.Hub(repeat_s=0.0)                                 # (without the same-radio join it is a new alarm)
    hub.now = T0 + 400.0
    hub.event("st-north", dict(ev, id="call_2", start=T0 + 388.0, end=T0 + 399.0))
    hub.now = T0 + 350.0
    hub.event("st-north", dict(ev, id="call_3", start=T0 + 340.0, end=T0 + 349.0))
    assert len(hub.alarms) == 2 and hub.alarms[1]["label"] == ""


def test_an_alarm_over_the_byte_limit_alone_is_sent_trimmed_and_never_dropped():
    """The 65,536 bytes are enforced before sending: items are added while the body fits; one over the limit on its
    own goes with its `data` cut to the essentials and `trimmed: true` (no `ids`, each string at most 200
    characters; a page: as many pairs as fit). The row keeps the full `data`."""
    lane, hub = cc.Lane(), cc.Hub()
    garbled = [{"id": str(10000 + i), "system": "mdc", "emergency": True, "from": "live"} for i in range(1500)]
    press(lane, t=0.0, data=dict(ALARM_DATA, ids=garbled, unit="U" * 900))
    press(lane, "CHA-live-1900000000500", t=0.5)
    body = lane.request("alarm", wall=W0 + 1.0, boottime=B0 + 1.0)
    assert [i["key"] for i in body["items"]] == [KEY, "CHA-live-1900000000500"] and cc.encoded(body) <= cc.REQUEST_MAX_BYTES
    assert "trimmed" not in body["items"][1]["data"]          # (the next item still fits beside the trimmed one)
    d = body["items"][0]["data"]
    assert d["trimmed"] is True and "ids" not in d and len(d["unit"]) == 200 and d["radio_id"] == "7001"
    assert c.errors("capture-urgent-request", body) == []
    assert len(lane.rows[(KEY, 0)]["data"]["ids"]) == 1500     # the row keeps it whole
    hub.now = W0 + 1.0
    lane.answer(*hub.urgent(body), wall=W0 + 1.0, boottime=B0 + 1.0)
    assert lane.unsent() == [] and len(hub.alarms) == len(hub.pushes) == 1     # (one radio: the second joins)
    # items are added while the body fits: 32 alarms of 3 KB each go as two requests, never one over the limit
    lane = cc.Lane()
    for i in range(32):
        press(lane, f"CHA-live-{1900000001000 + i}", t=float(i), data=dict(ALARM_DATA, unit="U" * 3000))
    first = lane.request("alarm", wall=W0 + 40.0, boottime=B0 + 40.0)
    assert 1 < len(first["items"]) < 32 and cc.encoded(first) <= cc.REQUEST_MAX_BYTES
    assert not any(i["data"].get("trimmed") for i in first["items"])
    # a page over the limit keeps as many pairs as fit
    lane = cc.Lane()
    pairs = [dict(PAGE_ITEM["data"]["pairs"][0], names=["Example Co " + "x" * 300]) for _ in range(400)]
    lane.press(PAGE_ITEM["key"], "page", dict(PAGE_ITEM["data"], pairs=pairs), wall=W0, boottime=B0)
    body = lane.request("page", wall=W0 + 1.0, boottime=B0 + 1.0)
    got = body["items"][0]["data"]
    assert got["trimmed"] is True and 0 < len(got["pairs"]) < 400 and cc.encoded(body) <= cc.REQUEST_MAX_BYTES
    assert c.errors("capture-urgent-request", body) == []


def test_the_resend_cap_keeps_the_newest_32_by_answer_time_then_n_and_names_the_rest():
    """More than 32 alarms in the resend window: the newest 32 by when the hub answered for them (the boot clock
    within one boot), and between equal times the higher `n` is the newer. The rest are named in a critical Health
    item."""
    lane = cc.Lane()
    for i in range(40):
        press(lane, f"CHA-live-{1900000000000 + i}", t=float(i) / 100)
    for t in (41.0, 41.0):                                     # two requests answered at the very same moment
        body = lane.request("alarm", wall=W0 + t, boottime=B0 + t)
        lane.answer(200, dict(OK, results=[{"key": i["key"], "rev": 0, "status": "applied"} for i in body["items"]]),
                    wall=W0 + t, boottime=B0 + t)
    assert lane.unsent() == [] and len({r["sent_boot"] for r in lane.rows.values()}) == 1      # ties, all of them
    lane.hello(200, {"ok": True, "station": "st-north", "token": "own", "gen": 6}, wall=W0 + 60.0, boottime=B0 + 60.0)
    again = lane.unsent()
    assert len(again) == 32 and set(again) == {f"CHA-live-{1900000000000 + i}" for i in range(8, 40)}  # the higher n
    assert sorted(lane.beyond) == [f"CHA-live-{1900000000000 + i}" for i in range(8)]
    health = lane.health()
    assert len(health) == 1 and health[0].startswith("critical") and "CHA-live-1900000000000" in health[0]


def test_an_emergency_item_has_four_signs_and_never_loses_its_audio_to_a_bound(tmp_path):
    """An `ids` entry with `emergency: true`; a turn with `emergency: true`; the call-level `emergency: true`; a
    non-empty `alarm_key`. Any one makes the item an emergency: its audio is never dropped at a bound, its sidecar
    never pruned past `max_items`, and the schema refuses `audio_dropped` without `audio_lost` for it."""
    base = valid("capture-spool-sidecar")["call.json"]
    signs = {"call-level flag": dict(base, emergency=True), "alarm_key": dict(base, alarm_key="p25-x-1-2-3"),
             "turn": dict(base, turns=[dict(base["turns"][0], emergency=True), *base["turns"][1:]]),
             "ids": dict(base, ids=[{"id": "7001", "system": "p25", "emergency": True, "from": "close"}])}
    assert not cc.is_emergency(base) and not cc.is_emergency(dict(base, alarm_key=""))
    for name, side in signs.items():
        assert cc.is_emergency(side), name
        dropped = dict(side, audio=None, audio_dropped=True)
        assert c.errors("capture-spool-sidecar", dropped) != [], name
        assert c.errors("capture-spool-sidecar", dict(dropped, audio_lost="write")) == [], name
    spool = cc.Spool(tmp_path / "spool")
    plain = spool.publish(seg("CH A", 1900000000.0), cc.wav_s16(800))
    flagged = spool.publish(dict(seg("CH A", 1900000001.0), alarm_key="CHA-live-1900000001000", ids=[]), cc.wav_s16(800))
    with pytest.raises(ValueError):
        spool.drop_audio(flagged["seq"])
    spool.drop_audio(plain["seq"])
    assert spool.bound(0) == [plain["seq"]]                    # past max_items: never the emergency item's sidecar
    assert (spool.root / "items" / flagged["audio"]["file"]).exists() and cc.check_spool(spool.root) == []


def test_a_wall_step_is_over_5_s_between_samples_at_most_5_s_apart():
    """capture samples wall - boot clock on its tick. The station's routine backward corrections (about 2 s every
    30 s) are not steps; 5.1 s within 5 s is one step, logged once with its size; drift is never one."""
    steps = cc.WallSteps()
    wall = W0
    for t in range(0, 600):                                    # a 1 s tick for 10 min
        if t and t % 30 == 0:
            wall -= 2.0                                        # a routine correction
        assert steps.sample(wall=wall + t * 1.05, boottime=B0 + t) is None    # (and the wall clock 5 % fast between)
    assert steps.at == [] and steps.log == []
    assert steps.sample(wall=wall + 600 * 1.05 + 5.1, boottime=B0 + 600) == pytest.approx(5.15, abs=1e-5)
    assert steps.sample(wall=wall + 601 * 1.05 + 5.1, boottime=B0 + 601) is None
    assert len(steps.log) == 1 and "stepped +5." in steps.log[0]
    gap = cc.WallSteps()                                       # samples more than 5 s apart: not a step, whatever moved
    gap.sample(wall=W0, boottime=B0)
    assert gap.sample(wall=W0 + 106.0, boottime=B0 + 6.0) is None and gap.at == []
    edge = cc.WallSteps()                                      # exactly 5 s is not over 5 s
    edge.sample(wall=W0, boottime=B0)
    assert edge.sample(wall=W0 + 6.0, boottime=B0 + 1.0) is None and edge.sample(wall=W0 + 12.5, boottime=B0 + 2.0) == 5.5


@pytest.mark.parametrize("rate", [0.05, -0.05])
def test_a_boot_clock_5_percent_fast_or_slow_with_routine_corrections_loses_and_doubles_nothing(rate):
    """#788: the production station's boot clock runs fast by 2 to 5 % and its wall clock is kept near true time by
    small backward corrections (about 1.5 s every 30 s). Every station-side window is designed to be safe with a boot
    clock off by +-10 %. Here: 5 % fast and 5 % slow, a correction every 30 s. No step is ever detected, no alarm is
    lost or doubled, and each window ends within its stated real-time effect (its nominal length / (1 +- 10 %))."""
    def clocks(r: float) -> tuple[float, float]:               # (wall, boot) at real second r
        return W0 + r + 0.05 * (r % 30.0), B0 + r * (1 + rate)

    def window(test, limit: float) -> float:
        """The first real second at which `test(r)` is true, for a window of nominal length `limit`."""
        r = 0.0
        while not test(r):
            r += 0.25
        assert limit / (1 + cc.RATE_BOUND) <= r <= limit / (1 - cc.RATE_BOUND) + 0.25, (limit, r)
        return r
    steps = cc.WallSteps()
    for r in range(0, 3600):
        w, b = clocks(float(r))
        assert steps.sample(wall=w, boottime=b) is None, r
    # `late`: 60 s, the larger of the two ages
    w0, b0 = clocks(0.0)
    side = {"end": w0, "closed_boot": b0, "boot": "boot-1"}
    window(lambda r: cc.is_late(side, wall=clocks(r)[0], boottime=clocks(r)[1], boot_now="boot-1"), 60.0)
    # the P25 bit's 10 min: a stuck bit every 30 s for an hour alarms once; after a quiet window, a press alarms
    bits = cc.P25Bits()
    seen = [bits.seen("examplesys", "7001", wall=clocks(r)[0], boottime=clocks(r)[1], boot="boot-1")
            for r in range(0, 3600, 30)]
    assert seen == [True] + [False] * (len(seen) - 1)
    last = 3570.0

    def press_after(r):
        trial = cc.P25Bits()
        trial.rows = copy.deepcopy(bits.rows)
        return trial.seen("examplesys", "7001", wall=clocks(last + r)[0], boottime=clocks(last + r)[1], boot="boot-1")
    window(press_after, 600.0)
    # a page revision's 120 s
    lane = cc.Lane()
    w, b = clocks(0.0)
    lane.press(PAGE_ITEM["key"], "page", PAGE_ITEM["data"], wall=w, boottime=b)
    window(lambda r: cc.outside(cc.larger_age(w, b, "boot-1", wall=clocks(r)[0], boottime=clocks(r)[1],
                                              boot_now="boot-1"), cc.PAGE_ITEM_MAX_AGE_S), 120.0)
    # the P25 match: no step is seen, the wall test holds through the corrections
    rows = [{"kind": "alarm", "key": "k", "boot": "boot-1", "event_at": clocks(300.0)[0], "event_boot": clocks(300.0)[1],
             "data": {"emergency_via": "P25", "system": "examplesys", "talkgroup": 4101, "radio_id": "7001"}}]
    call = {"system": "examplesys", "talkgroup": 4101, "unit": "7001", "start": clocks(300.0)[0] - 0.2,
            "end": clocks(312.0)[0]}
    w, b = clocks(380.0)
    assert cc.match_call(rows, call, wall=w, boottime=b, boot="boot-1", steps=steps) == "k"
    # an alarm through the lane: sent once, raised once, its age within 5 % of the truth, under the cutoff
    lane, hub = cc.Lane(), cc.Hub()
    w, b = clocks(0.0)
    lane.press(KEY, "alarm", ALARM_DATA, wall=w, boottime=b)
    w, b = clocks(1885.0)                                      # the uplink was down 31.4 real min
    body = lane.request("alarm", wall=w, boottime=b)
    assert body["items"][0]["age_s"] == pytest.approx(1885.0 * (1 + rate))
    hub.now = T0 + 1885.0
    lane.answer(*hub.urgent(body), wall=w, boottime=b)
    assert len(hub.alarms) == len(hub.pushes) == 1 and lane.unsent() == [] and lane.request("alarm", wall=w, boottime=b) is None
