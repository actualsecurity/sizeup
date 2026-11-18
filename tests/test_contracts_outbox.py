"""contracts/outbox/ (ADR 0004): the station's outbox file, state/outbox.sqlite, as built. It is the one file a second
implementation of the station must open across an update AND a switch back: whatever one build left in it, the other
goes on from. Nothing here is a JSON Schema: the contract is the file.

    ddl.sql, columns.json   a new file as the real Outbox makes it (recorded: `python -m tests.contracts record`)
    sequences/*.json        operations and what the file holds after each: run here against the real Outbox
    history/<n>-<what>.sql  the file as each older build made it, and every statement that build ran

Every id, name and byte in them is made up; the "audio" is a run of one byte, never a recording."""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import types
from pathlib import Path

import pytest

from sizeup.cloud import outbox as outbox_mod
from sizeup.cloud import station as stationmod
from sizeup.cloud.client import HubClient
from sizeup.cloud.outbox import Outbox
from tests import contracts as c
from tests import contracts_lane as lane
from tests.test_app import wait

HERE = c.ROOT / "outbox"
DDL, COLUMNS, SEQUENCES, HISTORY = HERE / "ddl.sql", HERE / "columns.json", HERE / "sequences", HERE / "history"
RECORDING = os.environ.get(c.RECORD_ENV, "") not in ("", "0")
T0 = c.T0
HEX16 = re.compile(r"[0-9a-f]{16}")
TABLES = ("outbox", "meta", "stt_inputs", "quarantine", "source_latest")

# -- known deviations (contracts/README.md "Known deviations"), pinned as the code is today ------------------------------
# Each test below states today's behaviour and the fixed one. When a fix lands, flip its line here from True to False
# (nothing else) and mark the row in contracts/README.md fixed.
O1_A_FILE_SET_BACK_HANDS_OUT_THE_SAME_SEQ_AGAIN = False   # fixed: never the same seq twice under one epoch
O2_NAN_IS_STORED_AS_A_TOKEN_THAT_IS_NOT_JSON = True      # fixed: stored as null, like #779 on the wire
O3_A_ROW_IT_CANNOT_READ_BLOCKS_THE_FORWARDER = False        # fixed: that row is set aside, the ones behind it go up
O4_A_FILE_SQLITE_CANNOT_OPEN_STOPS_THE_STATION = False    # fixed: the file is moved aside, a new outbox, the station starts
O5_A_RESENT_SNAPSHOT_PUTS_BACK_WHAT_THE_FEED_CHANGED = False   # fixed: the hub never applies a re-sent copy (qid) older
#                                                                      than its snapshot of that event


# -- the file as it is: tables, columns, pragmas -----------------------------------------------------------------------

def connect(path, ro: bool = True) -> sqlite3.Connection:
    """A connection of its own to an outbox file, as another program would open it."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True) if ro else sqlite3.connect(str(path), isolation_level=None)


def columns(path) -> dict:
    """What the file is, column by column, however it was made: each table's columns (PRAGMA table_info), which
    tables count with AUTOINCREMENT, the indexes the code made, and the pragmas kept in the file's header."""
    db = connect(path)
    try:
        made = dict(db.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"))
        out: dict = {"tables": {}, "autoincrement": sorted(t for t, sql in made.items() if "AUTOINCREMENT" in sql.upper()),
                     "indexes": []}
        for t in sorted(made):
            out["tables"][t] = [{"name": name, "type": typ, "notnull": bool(notnull), "default": dflt, "pk": bool(pk)}
                                for _, name, typ, notnull, dflt, pk in db.execute(f"PRAGMA table_info({t})")]
            for _, name, unique, origin, _partial in db.execute(f"PRAGMA index_list({t})").fetchall():
                if origin == "c":                          # (made by CREATE INDEX: a primary key's own is not listed)
                    out["indexes"].append({"name": name, "table": t, "unique": bool(unique),
                                           "columns": [r[2] for r in db.execute(f"PRAGMA index_info({name})")]})
        out["indexes"].sort(key=lambda i: i["name"])
        out["file"] = {p: db.execute(f"PRAGMA {p}").fetchone()[0]
                       for p in ("journal_mode", "encoding", "auto_vacuum", "application_id", "user_version")}
        return out
    finally:
        db.close()


DDL_HEAD = """\
-- The station's outbox file (state/outbox.sqlite) as sizeup/cloud/outbox.py makes a new one. RECORDED from a real
-- Outbox by `python -m tests.contracts record` (tests/test_contracts_outbox.py), never edited by hand. It is a script:
-- run on an empty SQLite file it makes a file the station opens as its own. columns.json beside it says the same
-- column by column: a file made any other way (by an older build and then altered, by another program) is held to that.

-- In the file's header: every connection finds these.
PRAGMA encoding = '{encoding}';
PRAGMA auto_vacuum = {auto_vacuum};             -- none: the file does not shrink when rows go (its pages are used again)
PRAGMA journal_mode = {journal_mode};          -- a write-ahead log: outbox.sqlite-wal and -shm beside the file while open
PRAGMA application_id = {application_id};          -- never set
PRAGMA user_version = {user_version};            -- never set, never read: the file carries no version

-- Per connection: the station sets it every time it opens the file; another connection has its SQLite's default.
PRAGMA synchronous = {synchronous};             -- NORMAL: in WAL mode a commit is not synced to the disk (README, O1)

-- sqlite_master, in the order the objects were made.
"""
DDL_OWN = {"sqlite_sequence": "-- SQLite's own, made with the first AUTOINCREMENT table. The next seq and the next n are "
                              "kept in it: never MAX + 1.\n--   {sql};\n",
           "sqlite_autoindex_meta_1": "-- SQLite's own index for meta's primary key: {name}\n",
           "sqlite_autoindex_source_latest_1": "-- SQLite's own index for source_latest's primary key: {name}\n"}
DDL_TAIL = """
-- A new file's one row of meta, made when it is first opened: the epoch, 16 hex characters of its own
-- (secrets.token_hex(8)). Never this value, and never copied from another file:
--   INSERT INTO meta (key, value) VALUES ('epoch', '0123456789abcdef');
"""


def ddl_text(ob: Outbox) -> str:
    """ddl.sql for the file this Outbox has open: the pragmas as another connection finds them in the file (and
    synchronous as the Outbox's own connection has it), then sqlite_master as it is."""
    file = columns(ob.path)["file"]
    out = DDL_HEAD.format(**file, synchronous=ob.db.execute("PRAGMA synchronous").fetchone()[0])
    db = connect(ob.path)
    try:
        for name, sql in db.execute("SELECT name, sql FROM sqlite_master ORDER BY rowid"):
            out += DDL_OWN[name].format(name=name, sql=sql) if name in DDL_OWN else sql + ";\n"
    finally:
        db.close()
    return out + DDL_TAIL


def test_the_recorded_ddl_and_columns_are_what_a_new_outbox_file_is(tmp_path):
    """contracts/outbox/ddl.sql and columns.json are recordings of a new file made by the real Outbox, written anew
    by `python -m tests.contracts record`: a column, a table, an index or a pragma that changes in the code fails
    here until they are recorded again, and the diff is the change."""
    ob = Outbox(tmp_path / "st" / "outbox.sqlite")
    try:
        ddl, cols = ddl_text(ob), columns(ob.path)
        assert ob.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        ob.close()
    if RECORDING:
        DDL.write_text(ddl, encoding="utf-8")
        COLUMNS.write_text(c.dump(cols) + "\n", encoding="utf-8")
    assert DDL.read_text(encoding="utf-8") == ddl
    assert c.loads_strict(COLUMNS.read_text(encoding="utf-8")) == cols
    # ... and what the recording says, in words: five tables, three of them counting with AUTOINCREMENT, two indexes,
    # WAL, no version of any kind
    assert set(cols["tables"]) == set(TABLES) and cols["autoincrement"] == ["outbox", "quarantine", "stt_inputs"]
    assert [x["name"] for x in cols["tables"]["outbox"]] == ["seq", "at", "kind", "data", "audio", "audio_dropped",
                                                             "context", "qid"]
    assert [x["name"] for x in cols["tables"]["stt_inputs"]] == ["n", "at", "data", "flac", "meta", "prio"]
    assert [x["name"] for x in cols["tables"]["quarantine"]] == [
        "id", "seq", "at", "kind", "data", "audio", "audio_dropped", "context", "reason", "error", "first_at", "last_at",
        "tries", "critical", "next_at", "state"]
    assert [x["name"] for x in cols["tables"]["source_latest"]] == ["key", "seq", "at"]
    assert cols["indexes"] == [{"name": "outbox_qid", "table": "outbox", "unique": False, "columns": ["qid"]},
                               {"name": "stt_inputs_order", "table": "stt_inputs", "unique": False,
                                "columns": ["prio", "n", "at"]}]
    assert "CREATE INDEX outbox_qid ON outbox (qid) WHERE qid IS NOT NULL;" in ddl      # (only the requeued copies)
    assert cols["file"] == {"journal_mode": "wal", "encoding": "UTF-8", "auto_vacuum": 0, "application_id": 0,
                            "user_version": 0}
    assert "PRAGMA synchronous = 1;" in ddl
    nullable = {"outbox": ["audio", "qid"], "quarantine": ["audio", "next_at"]}
    for t, rows in cols["tables"].items():               # what may be NULL; every column added to a table has a default
        assert [x["name"] for x in rows if not x["notnull"] and not x["pk"]] == nullable.get(t, [])
    assert {(t, x["name"]): x["default"] for t, rows in cols["tables"].items() for x in rows if x["default"] is not None} \
        == {("outbox", "audio_dropped"): "0", ("outbox", "context"): "0",
            ("stt_inputs", "prio"): str(outbox_mod.STT_NORMAL), ("quarantine", "audio_dropped"): "0",
            ("quarantine", "context"): "0", ("quarantine", "tries"): "1", ("quarantine", "critical"): "0",
            ("quarantine", "state"): "'held'"}


def test_a_file_made_by_running_ddl_sql_is_one_the_station_opens_as_its_own(tmp_path):
    """ddl.sql is a script, not a picture: another program that runs it on an empty file has made an outbox. The real
    Outbox opens that file, changes nothing in its shape, gives it its epoch and counts from 1."""
    path = tmp_path / "other" / "outbox.sqlite"
    path.parent.mkdir()
    db = connect(path, ro=False)
    db.executescript(DDL.read_text(encoding="utf-8"))
    db.close()
    made = columns(path)
    assert made == c.loads_strict(COLUMNS.read_text(encoding="utf-8"))
    ob = Outbox(path)
    try:
        assert HEX16.fullmatch(ob.epoch) and ob.add("heard", {"system": "p25", "radio_id": "990001"}, at=T0) == 1
        assert ob.add_stt({"id": "a1b2c3d4e5"}, b"\xc3" * 12, {"v": 1}, at=T0) == 1
        assert ddl_text(ob) == DDL.read_text(encoding="utf-8") and columns(path) == made
    finally:
        ob.close()


def test_while_it_is_open_the_outbox_is_three_files_and_a_row_is_in_the_log_not_in_the_main_file(tmp_path):
    """WAL, as built: a row is committed the moment add() returns (another connection, another process, sees it at
    once), but it is in outbox.sqlite-wal. The main file alone, copied from a running or a killed station, has none
    of it: here not even the tables. The file and its -wal travel together, and whoever opens them must let SQLite
    read the log back. Closed in order, the log is folded in and one file is left."""
    path = tmp_path / "st" / "outbox.sqlite"
    ob = Outbox(path)
    try:
        assert ob.add("heard", {"system": "p25", "radio_id": "990001"}, at=T0) == 1
        assert sorted(p.name for p in path.parent.iterdir()) == ["outbox.sqlite", "outbox.sqlite-shm", "outbox.sqlite-wal"]
        other = connect(path)
        assert other.execute("SELECT seq, kind FROM outbox").fetchall() == [(1, "heard")]
        other.close()
        (tmp_path / "copy").mkdir()
        shutil.copy(path, tmp_path / "copy" / "outbox.sqlite")
        alone = connect(tmp_path / "copy" / "outbox.sqlite")
        assert alone.execute("SELECT name FROM sqlite_master").fetchall() == []
        alone.close()
    finally:
        ob.close()
    assert [p.name for p in path.parent.iterdir()] == ["outbox.sqlite"]
    assert connect(path).execute("SELECT seq, kind FROM outbox").fetchall() == [(1, "heard")]


# -- behaviour: the sequences ------------------------------------------------------------------------------------------

def blob(spec) -> bytes | None:
    """A sequence's bytes: null, {"hex": "..."} or {"bytes": n, "fill": "a1"} (n times that one byte)."""
    if spec is None:
        return None
    assert set(spec) in ({"hex"}, {"bytes", "fill"}), spec
    return bytes.fromhex(spec["hex"]) if "hex" in spec else bytes.fromhex(spec["fill"]) * spec["bytes"]


def unblob(b):
    if b is None:
        return None
    b = bytes(b)
    return {"bytes": len(b), "fill": b[:1].hex()} if len(b) > 8 and len(set(b)) == 1 else {"hex": b.hex()}


def dump(o, indent: int = 0, width: int = 118, lead: int = 0) -> str:
    """JSON as the sequences are written: whatever fits on a line is on one; an object or a list that does not is
    filled line by line, so a step is a few lines and a row of a table one or two. (lead: what stands before it on
    its first line, a key.)"""
    inline = json.dumps(o, ensure_ascii=False)
    if not isinstance(o, (dict, list)) or not o or indent + lead + len(inline) <= width:
        return inline
    pad = " " * (indent + 2)
    keys = [json.dumps(k, ensure_ascii=False) + ": " for k in o] if isinstance(o, dict) else [""] * len(o)
    items = [k + dump(v, indent + 2, width, len(k)) for k, v in zip(keys, o.values() if isinstance(o, dict) else o)]
    lines: list[str] = []
    for it in items:
        if lines and "\n" not in it and "\n" not in lines[-1] and len(pad) + len(lines[-1]) + 2 + len(it) <= width \
                and not (isinstance(o, list) and it[0] in "[{"):
            lines[-1] += ", " + it
        else:
            lines.append(it)
    return "[{"[isinstance(o, dict)] + "\n" + ",\n".join(pad + ln for ln in lines) + "\n" + " " * indent \
        + "]}"[isinstance(o, dict)]


class Run:
    """One sequence against the real Outbox: files by name under a folder of the test's own, one open at a time."""

    def __init__(self, tmp: Path):
        self.tmp, self.ob, self.file = tmp, None, ""
        self.epochs: list[str] = []
        self.sent: list[dict] = []        # the last batch's rows, as the forwarder holds them while the hub answers

    def path(self, name: str) -> Path:
        assert re.fullmatch(r"[a-z]", name), name
        return self.tmp / name / "outbox.sqlite"

    def step(self, s: dict):
        """Do one step; what it returned, as the sequence writes it."""
        op, ob = s["op"], self.ob
        a = {k: v for k, v in s.items() if k not in ("op", "returns", "after")}
        if op == "open":                                  # (arm: false, as the quarantine command opens it, #791)
            assert ob is None and {"file", "max_audio_bytes"} <= set(a) <= {"file", "max_audio_bytes", "arm"}, s
            self.file = a["file"]
            self.ob = Outbox(self.path(self.file), a["max_audio_bytes"], arm=a.get("arm", True))
            assert HEX16.fullmatch(self.ob.epoch), self.ob.epoch
            if self.ob.epoch not in self.epochs:
                self.epochs.append(self.ob.epoch)
            return {"epoch": f"epoch-{self.epochs.index(self.ob.epoch) + 1}"}
        if op == "remove":                                # the file is gone (deleted by hand, a new disk)
            assert ob is None and set(a) == {"file"}, s
            shutil.rmtree(self.path(a["file"]).parent)
            return None
        assert ob is not None, s
        if op == "close":
            assert not a, s
            ob.close()
            self.ob = None
            return None
        if op == "add":
            assert set(a) <= {"kind", "data", "audio", "at", "context"} and "at" in a, s
            return ob.add(a["kind"], a["data"], blob(a.get("audio")), at=a["at"], context=a.get("context", False))
        if op == "batch":
            assert set(a) <= {"max_bytes", "max_events", "meta"}, s
            self.sent = ob.batch(**a)
            return [dict(r, audio=unblob(r["audio"])) for r in self.sent]
        if op == "quarantine":                            # the hub refused a row of the last batch (station.py _quarantined)
            assert set(a) == {"seq", "reason", "error", "now"}, s
            return ob.quarantine(next(r for r in self.sent if r["seq"] == a["seq"]), a["reason"], a["error"], now=a["now"])
        if op == "requeue_due":
            assert set(a) == {"now"}, s
            return ob.requeue_due(now=a["now"])
        if op == "released":
            return ob.released(a["qids"])
        if op == "late":
            assert set(a) == {"qid", "why", "now"}, s
            return ob.late(a["qid"], a["why"], now=a["now"])
        if op == "drop":                                  # qid null: all of them
            return ob.drop(a["qid"])
        if op == "dismiss":
            return ob.dismiss(a["kind"])
        if op == "strip_audio":
            return ob.strip_audio(a["seq"])
        if op in ("release", "resume"):                  # the station stops / starts again over the same file (#791)
            assert not a, s
            return getattr(ob, op)()
        if op == "summary":
            assert set(a) <= {"limit"}, s
            return ob.summary(**a)
        if op == "ack":
            return ob.ack(a["upto"])
        if op == "add_stt":
            assert set(a) == {"data", "flac", "meta", "at", "prio"}, s
            return ob.add_stt(a["data"], blob(a["flac"]), a["meta"], at=a["at"], prio=a["prio"])
        if op == "stt_batch":
            assert set(a) <= {"now", "max_bytes", "max_items"} and "now" in a, s
            return [dict(r, flac=unblob(r["flac"])) for r in ob.stt_batch(**a)]
        if op == "stt_ack":
            return ob.stt_ack(a["ns"])
        if op == "sql":                                   # read only, on a connection of its own
            db = connect(ob.path)
            try:
                return [list(r) for r in db.execute(a["query"])]
            finally:
                db.close()
        raise AssertionError(f"a step this runner does not know: {op!r}")

    def after(self) -> dict:
        """What can be seen after a step: the Outbox's own totals (while one is open), and what is in the file, read
        by another connection: the outbox and stt_inputs tables whole, and SQLite's counters."""
        out: dict = {}
        ob = self.ob
        if ob is not None:
            n, oldest = ob.pending()
            stt_n, stt_bytes = ob.stt_pending()
            out["totals"] = {"pending": n, "oldest": oldest, "audio_bytes": ob.audio_bytes,
                             "context_bytes": ob.context_bytes, "stt_pending": stt_n, "stt_bytes": stt_bytes,
                             "dropped_audio": ob.dropped_audio,
                             "dropped_stt": ob.dropped_stt, "dropped_stt_pre": ob.dropped_stt_pre,
                             "dropped_stt_fire": ob.dropped_stt_fire, "quarantine_evicted": ob.quarantine_evicted,
                             "too_large_audio": ob.too_large_audio}
        path = self.path(self.file)
        if not path.is_file():
            return dict(out, **dict.fromkeys(FILE_KEYS))
        db = connect(path)
        try:
            out["outbox"] = [[seq, at, kind, data, unblob(audio), dropped, context, qid] for seq, at, kind, data, audio,
                             dropped, context, qid in db.execute("SELECT seq, at, kind, data, audio, audio_dropped, context, "
                                                                 "qid FROM outbox ORDER BY seq")]
            out["stt_inputs"] = [[n, at, data, unblob(flac), meta, prio] for n, at, data, flac, meta, prio in
                                 db.execute("SELECT n, at, data, flac, meta, prio FROM stt_inputs ORDER BY n")]
            out["quarantine"] = [[r[0], *r[1:5], unblob(r[5]), *r[6:]] for r in db.execute(
                "SELECT id, seq, at, kind, data, audio, audio_dropped, context, reason, error, first_at, last_at, tries, "
                "critical, next_at, state FROM quarantine ORDER BY id")]
            out["source_latest"] = [list(r) for r in db.execute("SELECT key, seq, at FROM source_latest ORDER BY key")]
            out["sqlite_sequence"] = dict(db.execute("SELECT name, seq FROM sqlite_sequence ORDER BY name"))
        finally:
            db.close()
        return out

    def close(self) -> None:
        if self.ob is not None:
            self.ob.close()


# what can be seen of the file; `after` names one only when the step changed it
FILE_KEYS = ("outbox", "stt_inputs", "quarantine", "source_latest", "sqlite_sequence")


def run_sequence(doc: dict, tmp: Path, record: bool = False) -> list[dict]:
    """Every step of a sequence against the real Outbox. What each step returned and what could be seen after it must
    be what the sequence says (record: is written into it instead). The full view after each step."""
    run, seen, last = Run(tmp), [], {}
    try:
        for i, s in enumerate(doc["steps"]):
            got = run.step(s)
            now = run.after()
            if record:
                s.pop("returns", None)
                s.pop("after", None)
                if got is not None:
                    s["returns"] = got
                s["after"] = {k: v for k, v in now.items() if k not in FILE_KEYS or v != last.get(k, "")}
            where = f"step {i}: {s['op']}"
            assert s.get("returns") == got, where
            want = s["after"]
            assert set(want) <= {"totals", *FILE_KEYS} and ("totals" in want) == ("totals" in now), where
            for k in FILE_KEYS:                            # (a table the step says nothing of: as it was)
                assert now[k] == (want[k] if k in want else last.get(k, "")), f"{where}: {k}"
            assert want.get("totals") == now.get("totals"), where
            seen.append(now)
            last = now
    finally:
        run.close()
    return seen


def sequence(name: str) -> dict:
    return c.loads_strict((SEQUENCES / f"{name}.json").read_text(encoding="utf-8"))


SEQUENCE_NAMES = sorted(p.stem for p in SEQUENCES.glob("*.json"))


@pytest.mark.parametrize("name", SEQUENCE_NAMES)
def test_the_recorded_sequence_is_what_the_outbox_does(tmp_path, name):
    """Each file of contracts/outbox/sequences/ is a list of operations with what each returns and what the file holds
    afterwards. The real Outbox must do exactly that, step by step. `python -m tests.contracts record` writes the
    `returns` and `after` anew from the real Outbox (the operations are written by hand); the tests below then hold
    each recording to what it is there to show, in words."""
    doc = sequence(name)
    assert set(doc) == {"description", "steps"} and len(doc["description"]) > 80
    if RECORDING:
        run_sequence(doc, tmp_path / "rec", record=True)
        (SEQUENCES / f"{name}.json").write_text(dump(doc) + "\n", encoding="utf-8")
    seen = run_sequence(sequence(name), tmp_path / "run")
    assert len(seen) == len(doc["steps"]) >= 15
    text = (SEQUENCES / f"{name}.json").read_text(encoding="utf-8")
    # ... and names no machine, no run and no real epoch
    assert not re.search(r"/Users/|/home/|/private/|/var/folders|/tmp/|pytest|\"[0-9a-f]{16}\"", text)


def test_the_sequences_cover_every_operation_and_the_checks_on_them_fail_when_the_outbox_differs(tmp_path):
    """The check on the check. Every public method of the Outbox that reads or writes the file is run by a sequence;
    and a sequence run against an Outbox that behaves otherwise (here: one that would reuse a seq, one that would
    drop the oldest audio before the context audio) fails."""
    ops = {s["op"] for n in SEQUENCE_NAMES for s in sequence(n)["steps"]}
    quarantine = {"quarantine", "requeue_due", "released", "late", "drop", "dismiss", "strip_audio", "summary",
                  "release", "resume"}
    assert ops == {"open", "close", "remove", "add", "batch", "ack", "add_stt", "stt_batch", "stt_ack", "sql"} | quarantine
    public = {n for n, f in vars(Outbox).items() if callable(f) and not n.startswith("_")}
    assert public == {"add", "batch", "ack", "pending", "add_stt", "stt_batch", "stt_ack", "stt_pending", "close"} \
        | quarantine
    doc = sequence("add-batch-ack")
    doc["steps"][-1]["returns"] = 1                       # "after everything was acked, seq starts again"
    with pytest.raises(AssertionError, match="step 21: add"):
        run_sequence(doc, tmp_path / "a")
    doc = sequence("drop-order")
    step = next(s for s in doc["steps"] if s["op"] == "add" and s.get("context") and "outbox" in s["after"]
                and any(r[5] for r in s["after"]["outbox"]))
    for row in step["after"]["outbox"]:                   # "the oldest audio went, not the context audio"
        row[5] = 0
    with pytest.raises(AssertionError, match="outbox"):
        run_sequence(doc, tmp_path / "b")


def events(name: str) -> list[tuple[dict, dict]]:
    """A recorded sequence as (step, the whole view after it): the tables a step left alone filled in from before."""
    out, last = [], {}
    for s in sequence(name)["steps"]:
        last = {**last, **s["after"]}
        if "totals" not in s["after"]:
            last.pop("totals", None)
        out.append((s, dict(last)))
    return out


def test_seq_is_never_handed_out_twice_and_ack_deletes_up_to_it():
    """add-batch-ack.json, in words."""
    ev = events("add-batch-ack")
    assert [s["returns"] for s, _ in ev if s["op"] == "add"] == [1, 2, 3, 4, 5, 6]
    # ... 5 after everything up to 4 was acked and gone, 6 after the file was closed empty and opened again
    before_5 = next(v for s, v in ev if s["op"] == "ack" and s["upto"] == 1000)
    assert before_5["outbox"] == [] and before_5["sqlite_sequence"] == {"outbox": 4}
    reopened = [v for s, v in ev if s["op"] == "open"][1]
    assert reopened["outbox"] == [] and reopened["sqlite_sequence"] == {"outbox": 5} and reopened["totals"]["pending"] == 0
    assert [s["returns"] for s, _ in ev if s["op"] == "ack"] == [2, 0, 0, 2, 1]      # (twice, zero, beyond the last row)
    batches = [[r["seq"] for r in s["returns"]] for s, _ in ev if s["op"] == "batch"]
    assert batches == [[1, 2, 3], [1, 2], [1], [1, 2], [1], [3]]                     # oldest first; at least one
    whole = next(s for s, _ in ev if s["op"] == "batch")["returns"]
    assert [set(r) for r in whole] == [{"seq", "at", "type", "data", "audio", "audio_dropped"}] * 3
    assert [r["type"] for r in whole] == ["page", "page_text", "traffic"] and whole[1]["audio"] is None
    # as stored: at a REAL, data compact JSON text in ASCII, audio a BLOB or NULL
    types_ = next(s for s, _ in ev if s["op"] == "sql")["returns"]
    assert [r[1:5] for r in types_] == [["real", "text", "text", "blob"], ["real", "text", "text", "null"],
                                        ["real", "text", "text", "blob"]]
    rows = next(v for s, v in ev if s["op"] == "sql")["outbox"]
    for (seq, at, kind, data, audio, dropped, context, qid), r, t in zip(rows, whole, types_):
        assert data == json.dumps(r["data"], separators=(",", ":")) and data.isascii() and len(data) == t[5]
        assert (seq, at, kind, audio, dropped, context, qid) == (r["seq"], r["at"], r["type"], r["audio"], 0, 0, None)
    assert "caf\\u00e9" in rows[2][3] and whole[2]["data"]["text"].endswith("café side")
    # max_bytes: the stored data's length plus the audio's bytes; a batch that fits exactly is taken, one byte less is not
    two = len(rows[0][3]) + 40 + len(rows[1][3])
    sized = [s for s, _ in ev if s["op"] == "batch" and "max_bytes" in s]
    assert [(s["max_bytes"], len(s["returns"])) for s in sized] == [(10, 1), (two, 2), (two - 1, 1)]


def test_the_epoch_is_the_files_and_the_totals_are_counted_again():
    """epoch-and-reopen.json, in words."""
    ev = events("epoch-and-reopen")
    opens = [(s["file"], s["returns"]["epoch"]) for s, _ in ev if s["op"] == "open"]
    assert opens == [("a", "epoch-1"), ("a", "epoch-1"), ("b", "epoch-2"), ("a", "epoch-1"), ("a", "epoch-1"),
                     ("a", "epoch-3")]
    assert [s["returns"] for s, _ in ev if s["op"] == "add"] == [1, 2, 1, 3, 4, 5, 1]   # per file; from 1 in a new one
    # the second clip put the file over its limit: the first one's audio went, its row stayed, marked
    over = [v for s, v in ev if s["op"] == "add"][1]
    assert [(r[0], r[4] is None, r[5]) for r in over["outbox"]] == [(1, True, 1), (2, False, 0)]
    assert (over["totals"]["audio_bytes"], over["totals"]["dropped_audio"], over["totals"]["pending"]) == (60, 1, 2)
    again = [v for s, v in ev if s["op"] == "open"][1]
    assert again["outbox"] == over["outbox"]                                 # the rows and the mark: kept
    assert (again["totals"]["audio_bytes"], again["totals"]["dropped_audio"]) == (60, 0)     # counted again; this run's
    first, second = [s["returns"] for s, _ in ev if s["op"] == "batch"]
    assert first == second and (first[0]["audio"], first[0]["audio_dropped"]) == (None, True)
    # opened with a lower limit: over it, and nothing goes until the next add (then the oldest audio, as ever)
    lower = [(s, v) for s, v in ev if s["op"] == "open"][4][1]
    assert lower["totals"]["audio_bytes"] == 180 > 100 and lower["totals"]["dropped_audio"] == 0
    after_add = next(v for s, v in ev if s["op"] == "add" and s["kind"] == "page_text")
    assert after_add["totals"]["audio_bytes"] == 60 and after_add["totals"]["dropped_audio"] == 2
    assert [r[0] for r in after_add["outbox"] if r[4] is not None] == [4]


def gone(ev: list[tuple[dict, dict]]) -> list[list[str]]:
    """What each step of a recorded sequence dropped, in the order of the steps: "stt <n>" a speech-to-text input's
    row, "audio <seq>" an event's audio (its row is still there)."""
    out, stt, audio = [], set(), {}
    for s, v in ev:
        now_stt = {r[0] for r in v["stt_inputs"]}
        now_audio = {r[0]: r[4] is not None for r in v["outbox"]}
        added = s["returns"] if s["op"] == "add_stt" else None
        step = [f"stt {n}" for n in sorted(stt | ({added} if added else set())) if n not in now_stt]
        step += [f"audio {seq}" for seq, has in sorted(now_audio.items())
                 if not has and (audio.get(seq) or (seq not in audio and s.get("audio")))]
        out.append(step)
        stt, audio = now_stt, now_audio
    return out


def test_what_is_dropped_over_the_limit_and_in_what_order():
    """drop-order.json, in words: speech-to-text inputs first (the lowest priority first), then context audio, then the
    oldest audio; never a row of the outbox table, never its data."""
    ev = events("drop-order")
    assert next(s for s, _ in ev if s["op"] == "open")["max_audio_bytes"] == 1000
    assert (outbox_mod.STT_SHARE, outbox_mod.CONTEXT_SHARE) == (0.3, 0.2)
    dropped = dict(zip(range(len(ev)), gone(ev)))
    assert {i: d for i, d in dropped.items() if d} == {
        4: ["stt 1"],                  # the inputs over their own 300: the lowest priority (0) goes, not the oldest of all
        7: ["audio 2"],                # context audio over its own 200: its oldest
        8: ["stt 3"],                  # the whole over 1000: an input first (priority 1), and that was enough
        9: ["stt 2", "stt 4"],         # ... then fire traffic's (2) and the pre-alert's (3): no clip was touched for it
        10: ["audio 3"],               # no input left: context audio, though inside its own budget
        11: ["audio 1"],               # no context audio left: the oldest audio
        12: ["stt 5"],                 # an input that does not fit is gone as it is added (its n was still handed out)
        13: ["audio 4", "audio 5", "audio 6", "audio 7"],     # as many of the oldest as it takes
        14: ["audio 8", "audio 9"],    # audio larger than the whole limit: everything before it, then its own
    }
    t = ev[9][1]["totals"]
    assert (t["dropped_stt"], t["dropped_stt_fire"], t["dropped_stt_pre"], t["dropped_audio"]) == (4, 1, 1, 1)
    last = ev[-1][1]
    assert [r[0] for r in last["outbox"]] == list(range(1, 10)) and last["totals"]["pending"] == 9
    assert all(r[4] is None and r[5] == 1 for r in last["outbox"]) and last["stt_inputs"] == []
    assert last["totals"]["audio_bytes"] == 0 and last["totals"]["dropped_audio"] == 9
    sent = ev[-1][0]["returns"]                           # what would go to the hub: every event, its data whole
    adds = [s for s, _ in ev if s["op"] == "add"]
    assert [(r["seq"], r["data"], r["audio"], r["audio_dropped"]) for r in sent] \
        == [(i + 1, s["data"], None, True) for i, s in enumerate(adds)]
    assert [r[6] for r in last["outbox"]] == [0, 1, 1, 0, 0, 0, 0, 0, 0]     # (context: kept as a mark on the row)


def test_the_speech_to_text_lane_goes_by_priority_and_fresh_first():
    """stt-lane.json, in words."""
    ev = events("stt-lane")
    assert outbox_mod.FRESH_S == 300.0 and (outbox_mod.STT_PRE, outbox_mod.STT_FIRE, outbox_mod.STT_NORMAL) == (3, 2, 1)
    batches = [([r["n"] for r in s["returns"]], s) for s, _ in ev if s["op"] == "stt_batch"]
    order = [6, 5, 4, 3, 1, 7, 2]       # pre-alert: fresh, older; fire: fresh, older; other in order; context last
    assert [b for b, _ in batches] == [order, order[:3], [6], [6, 5], [5, 6, 3, 4, 1, 7, 2], [4, 3, 1, 7, 2],
                                       [4, 3, 1, 7, 2]]
    assert batches[4][1]["now"] - T0 == 5000              # (nothing is fresh any more: each priority in order)
    assert set(batches[0][1]["returns"][0]) == {"n", "at", "data", "flac", "meta", "prio"}
    assert [s["returns"] for s, _ in ev if s["op"] == "stt_ack"] == [2, 0, 0, 6]
    assert [s["returns"] for s, _ in ev if s["op"] == "add_stt"] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    emptied = next(v for s, v in ev if s["op"] == "stt_ack" and len(s["ns"]) == 6)
    assert emptied["stt_inputs"] == [] and emptied["sqlite_sequence"] == {"outbox": 1, "stt_inputs": 8}
    reopened = [v for s, v in ev if s["op"] == "open"][1]
    assert [(r[0], r[5]) for r in reopened["stt_inputs"]] == [(1, 1), (2, 0), (3, 2), (4, 2), (7, 1)]
    assert reopened["totals"]["stt_bytes"] == 150 and reopened["totals"]["stt_pending"] == 5
    # the lane and the events share the file and nothing else: an event's seq and an input's n count apart
    assert next(s["returns"] for s, _ in ev if s["op"] == "add") == 1


def test_a_released_outbox_hands_out_nothing_and_an_unarmed_one_arms_before_it_does():
    """release-resume.json, in words (#791): a marker per opener while it may hand out seqs; released, nothing goes out
    (add still queues) until resume(); opened with arm false, no marker until its first batch; a stop in order keeps
    the epoch."""
    ev = events("release-resume")
    marks = [s["returns"][0][0] for s, _ in ev if s["op"] == "sql"]
    assert marks == [1, 0, 1, 0, 1, 1]
    assert [[r["seq"] for r in s["returns"]] for s, _ in ev if s["op"] == "batch"] == [[], [1, 2], [3]]
    assert [s["returns"] for s, _ in ev if s["op"] == "add"] == [1, 2, 3]
    assert {s["returns"]["epoch"] for s, _ in ev if s["op"] == "open"} == {"epoch-1"}
    assert [s.get("arm", True) for s, _ in ev if s["op"] == "open"] == [True, False, True]


def test_the_quarantine_keeps_what_the_hub_refused_and_sends_the_critical_ones_again():
    """quarantine.json, in words (#762)."""
    ev = events("quarantine")
    assert (outbox_mod.CRITICAL_KINDS, outbox_mod.RETRY_S, outbox_mod.CRITICAL_TRIES, outbox_mod.CRITICAL_FOR_S) \
        == (("page", "page_text", "alarm", "source_event"), (600.0, 1800.0, 3600.0), 8, 86400.0)
    q = lambda s: {k: s["returns"][k] for k in ("kind", "critical", "state")}      # noqa: E731
    put = [s for s, _ in ev if s["op"] == "quarantine"]
    # moved out of the outbox: critical only for those four kinds, and never a malformed one
    assert [q(s) for s in put[:4]] == [{"kind": "traffic", "critical": False, "state": "held"},
                                       {"kind": "page", "critical": True, "state": "held"},
                                       {"kind": "page_text", "critical": False, "state": "held"},
                                       {"kind": "source_event", "critical": True, "state": "held"}]
    moved = [v for s, v in ev if s["op"] == "quarantine"][3]
    assert moved["outbox"] == [] and [r[1] for r in moved["quarantine"]] == [3, 1, 2, 4]   # (each row's seq kept)
    assert next(s for s, _ in ev if s["op"] == "ack")["returns"] == 0                 # (the ack after: nothing left)
    # sent again: a new event at the end of the outbox carrying qid, after 600 s, then 1800 s; never while a copy waits
    requeued = [(s["now"] - T0, s["returns"]) for s, _ in ev if s["op"] == "requeue_due"]
    assert [(t, [(x["kind"], x["superseded"]) for x in r]) for t, r in requeued[:6]] == [
        (300, []), (700, [("page", False), ("source_event", True)]), (800, []), (2600, []), (2700, [("page", False)]),
        (640, [("alarm", False)])]
    copies = [v["outbox"] for s, v in ev if s["op"] == "requeue_due" and s["returns"]
              and not all(x["superseded"] for x in s["returns"])]
    assert [r[0] for r in copies[0]] == [6] and copies[0][0][7] == 2 and copies[0][0][1] == T0 + 700
    assert [s["returns"]["tries"] for s in put if s["seq"] == 6] == [2]
    # a source_event the feed has a newer snapshot of is dropped, never sent again over it
    latest = next(v for s, v in ev if s["op"] == "add" and s["kind"] == "source_event" and s["at"] == T0 + 20)
    assert latest["source_latest"] == [["east-cad\u001fE-1001", 5, T0 + 20]]
    # the hub took the copy: gone; an alarm taken too late: kept as late until dismissed
    assert next(s for s, _ in ev if s["op"] == "released")["returns"] == 1
    late = next(v for s, v in ev if s["op"] == "late")["quarantine"][-1]
    assert (late[3], late[8], late[14], late[15]) == ("alarm", "late", None, "late")
    summary = next(s for s, _ in ev if s["op"] == "summary")["returns"]
    assert (summary["n"], summary["critical"], summary["kinds"]["alarm"]["late"]) == (3, 1, 1)
    assert all(set(r) == {"id", "kind", "reason", "error", "first_at", "last_at", "tries", "critical", "next_at", "state"}
               for r in summary["rows"])                                         # (never an event's data)
    assert next(s for s, _ in ev if s["op"] == "dismiss")["returns"] == 1
    # the rows survive a restart, the counters are per run
    reopened = [v for s, v in ev if s["op"] == "open"][1]
    assert len(reopened["quarantine"]) == 3 and reopened["totals"]["too_large_audio"] == 0
    # a clip too large for the hub: left off, the event stays
    stripped = [(s["returns"], v) for s, v in ev if s["op"] == "strip_audio"]
    assert [r for r, _ in stripped] == [True, False]
    assert stripped[0][1]["outbox"][0][4:6] == [None, 1] and stripped[0][1]["totals"]["too_large_audio"] == 1
    # refused for longer than a day: stuck, never sent again
    assert put[-3]["returns"]["state"] == "stuck" and requeued[-2][1] == []
    # a person drops one while its copy is on the way: the copy goes too, the hub's refusal of it is nothing
    assert put[-1]["returns"]["state"] == "dropped" and [v for s, v in ev if s["op"] == "drop"][1]["outbox"] == []
    assert [s["returns"] for s, _ in ev if s["op"] == "drop"] == [1, 1, 2] and ev[-1][1]["quarantine"] == []


# -- as built and worth knowing ------------------------------------------------------------------------------------------

def test_a_column_a_table_or_a_row_it_does_not_know_is_left_alone(tmp_path):
    """The compatibility rule, for the file: what a newer build added (a column that is nullable or has a default, a
    table, an index, a row in meta, a user_version) is ignored by today's code and still there afterwards. A column
    that is NOT NULL with no default is the one thing a newer build may never add: every add() of an older build
    then fails (and App._emit drops that event with a line in the log)."""
    path = tmp_path / "st" / "outbox.sqlite"
    ob = Outbox(path)
    ob.add("heard", {"radio_id": "990001"}, b"\xa1" * 12, at=T0)
    ob.add_stt({"id": "a1b2c3d4e5"}, b"\xc3" * 12, {"v": 1}, at=T0, prio=3)
    epoch = ob.epoch
    ob.close()
    db = connect(path, ro=False)
    db.executescript("""
        ALTER TABLE outbox ADD COLUMN from_the_future TEXT;
        ALTER TABLE outbox ADD COLUMN from_the_future_n INTEGER NOT NULL DEFAULT 7;
        ALTER TABLE stt_inputs ADD COLUMN from_the_future BLOB;
        CREATE TABLE from_the_future (k TEXT PRIMARY KEY, v);
        CREATE INDEX from_the_future_i ON outbox (kind);
        INSERT INTO meta (key, value) VALUES ('from_the_future', '2');
        UPDATE outbox SET from_the_future = 'kept';
        PRAGMA user_version = 9;
    """)
    db.close()
    ob = Outbox(path)
    try:
        assert ob.epoch == epoch and ob.pending()[0] == 1 and ob.stt_pending() == (1, 12)
        assert ob.add("heard", {"radio_id": "990002"}, at=T0 + 1) == 2
        assert ob.add_stt({"id": "f6a7b8c9d0"}, b"\xc3" * 12, {"v": 1}, at=T0 + 1) == 2
        assert [r["seq"] for r in ob.batch()] == [1, 2] and [r["n"] for r in ob.stt_batch(now=T0 + 2)] == [1, 2]
        assert ob.db.execute("SELECT from_the_future, from_the_future_n FROM outbox ORDER BY seq").fetchall() \
            == [("kept", 7), (None, 7)]
        assert ob.db.execute("PRAGMA user_version").fetchone()[0] == 9              # never read, never reset
        assert ob.db.execute("SELECT value FROM meta WHERE key = 'from_the_future'").fetchone() == ("2",)
        assert ob.ack(2) == 2 and ob.stt_ack([1, 2]) == 2
    finally:
        ob.close()
    strict = tmp_path / "newer" / "outbox.sqlite"         # (ALTER cannot add such a column: a table made anew can)
    strict.parent.mkdir()
    db = connect(strict, ro=False)
    db.executescript(DDL.read_text(encoding="utf-8").replace("    audio BLOB,\n",
                                                             "    audio BLOB,\n    must TEXT NOT NULL,\n"))
    db.close()
    ob = Outbox(strict)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="outbox.must"):
            ob.add("heard", {"radio_id": "990001"}, at=T0)
        assert ob.pending()[0] == 0
    finally:
        ob.close()


def test_nothing_gives_the_file_one_owner(tmp_path):
    """As built: nothing stops a second program from opening the outbox while the station has it (no lock file, no
    exclusive mode). SQLite keeps the rows and the counters right: no seq is handed out twice between them. But each
    keeps its own totals, so the limit is each one's own (here 100: 120 bytes stay queued), and each would send the
    same rows. A second implementation takes the file over when the first has stopped, never beside it."""
    path = tmp_path / "st" / "outbox.sqlite"
    one, two = Outbox(path, 100), Outbox(path, 100)
    try:
        assert one.epoch == two.epoch
        assert [one.add("traffic", {"id": "Fireground1-1794990000000"}, b"\xa1" * 60, at=T0),
                two.add("traffic", {"id": "Fireground1-1794990001000"}, b"\xa2" * 60, at=T0 + 1),
                one.add("heard", {"radio_id": "990001"}, at=T0 + 2)] == [1, 2, 3]
        assert (one.audio_bytes, two.audio_bytes, one.dropped_audio, two.dropped_audio) == (60, 60, 0, 0)
        assert [r["seq"] for r in one.batch()] == [r["seq"] for r in two.batch()] == [1, 2, 3]
        assert sum(len(r["audio"] or b"") for r in two.batch()) == 120 > 100
    finally:
        one.close()
        two.close()


def test_what_is_not_json_is_stored_as_its_text_and_a_number_of_numpy_as_a_number(tmp_path):
    """As built (outbox.py _plain): a numpy scalar is stored as the plain number; anything else json cannot write is
    stored as str() of it, with no error: bytes as "b'..'", a set as "{1, 2}". The event goes up like that."""
    import numpy as np
    ob = Outbox(tmp_path / "st" / "outbox.sqlite")
    try:
        ob.add("traffic", {"confidence": np.float32(0.5), "turn": np.int64(3), "low_energy": np.bool_(True),
                           "raw": b"ab", "names": {"x"}}, at=T0)
        assert ob.db.execute("SELECT data FROM outbox").fetchone()[0] \
            == '{"confidence":0.5,"turn":3,"low_energy":true,"raw":"b\'ab\'","names":"{\'x\'}"}'
        with pytest.raises(ValueError):                   # (an array of several has no one value: nothing is stored)
            ob.add("traffic", {"samples": np.zeros(2)}, at=T0)
        assert ob.pending()[0] == 1
    finally:
        ob.close()


# -- an update: an older build's file under today's code -----------------------------------------------------------------

class Shape:
    """One file of contracts/outbox/history/: the SQL an older build ran, by part ("open", "new file", "statements"),
    each statement with its parameters and the column it was conditional on."""

    def __init__(self, path: Path):
        self.name = path.stem
        self.text = path.read_text(encoding="utf-8")
        self.commit = re.search(r"sizeup/cloud/outbox\.py at ([0-9a-f]{8}) \(", self.text)[1]
        self.parts: dict[str, list[tuple[str, list, str]]] = {}
        part, params, missing, sql = "", [], "", []
        for line in self.text.splitlines():
            if line.startswith("-- == "):
                part = line[6:].strip()
                self.parts[part] = []
            elif line.startswith("-- params: "):
                params = [bytes.fromhex(p["hex"]) if isinstance(p, dict) else p for p in json.loads(line[11:])]
            elif line.startswith("-- if-missing: "):
                missing = line[15:].strip()
            elif part and line.strip() and not line.startswith("--"):
                sql.append(line)
                if line.rstrip().endswith(";"):
                    self.parts[part].append(("\n".join(sql).rstrip().rstrip(";"), params, missing))
                    params, missing, sql = [], "", []
        assert set(self.parts) == {"open", "new file", "statements"} and not sql, self.name

    def run(self, db, part: str) -> list:
        """Run one part as that build did; each statement's rows."""
        out = []
        for sql, params, missing in self.parts[part]:
            if missing:
                table, col = missing.split(".")
                if col in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
                    continue
            assert sql.count("?") == len(params), sql
            out.append((sql, db.execute(sql, params).fetchall()))
        return out

    def one(self, start: str) -> tuple[str, list]:
        got = [(sql, params) for sql, params, _ in self.parts["statements"] if sql.startswith(start)]
        assert len(got) == 1, (self.name, start)
        return got[0]


SHAPES = [Shape(p) for p in sorted(HISTORY.glob("*.sql"))]
BY_NAME = {s.name: s for s in SHAPES}


def old_file(path: Path, shape: Shape, events_n: int = 3) -> None:
    """A file as that older build left it: opened by it for the first time, events_n events added (and as many
    speech-to-text inputs, if it had them), the first of them sent and acked."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = connect(path, ro=False)
    shape.run(db, "open")
    shape.run(db, "new file")
    for start in ("INSERT INTO outbox", "INSERT INTO stt_inputs"):
        if any(sql.startswith(start) for sql, _, _ in shape.parts["statements"]):
            sql, params = shape.one(start)
            for _ in range(events_n):
                db.execute(sql, params)
    db.execute(*shape.one("DELETE FROM outbox WHERE seq <="))
    db.close()


def test_the_history_is_the_four_shapes_before_todays():
    assert [s.name for s in SHAPES] == ["1-events", "2-context", "3-stt-inputs", "4-stt-priority"]
    assert [s.commit for s in SHAPES] == ["1c49db24", "7707058c", "606b2225", "93c7658c"]
    made = {}
    for s in SHAPES:
        db = sqlite3.connect(":memory:")
        s.run(db, "open")
        made[s.name] = {t: [r[1] for r in db.execute(f"PRAGMA table_info({t})")] for t in TABLES}
        db.close()
    assert made["1-events"] == {"outbox": ["seq", "at", "kind", "data", "audio", "audio_dropped"], "meta": ["key", "value"],
                                "stt_inputs": [], "quarantine": [], "source_latest": []}
    assert made["2-context"] == dict(made["1-events"], outbox=made["1-events"]["outbox"] + ["context"])
    assert made["3-stt-inputs"] == dict(made["2-context"], stt_inputs=["n", "at", "data", "flac", "meta"])
    assert made["4-stt-priority"] == dict(made["3-stt-inputs"], stt_inputs=made["3-stt-inputs"]["stt_inputs"] + ["prio"])
    today = c.loads_strict(COLUMNS.read_text(encoding="utf-8"))["tables"]      # (#762: the quarantine)
    assert {t: [x["name"] for x in rows] for t, rows in today.items()} \
        == dict(made["4-stt-priority"], outbox=made["4-stt-priority"]["outbox"] + ["qid"],
                quarantine=[x["name"] for x in today["quarantine"]], source_latest=["key", "seq", "at"])


@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: s.name)
def test_an_older_builds_file_opens_under_todays_code_and_keeps_everything(tmp_path, shape):
    """The update. A file an older build made and wrote to is opened by today's Outbox: afterwards it has today's
    columns and index (column by column what a new file has), the rows it had, its epoch, and its counters: the next
    seq is the one after the last the older build handed out, also for the row already acked and gone."""
    path = tmp_path / "st" / "outbox.sqlite"
    old_file(path, shape)
    before = connect(path)
    rows = before.execute("SELECT seq, at, kind, data, audio, audio_dropped FROM outbox ORDER BY seq").fetchall()
    lane = shape.name not in ("1-events", "2-context")                       # (a build with the speech-to-text lane)
    stt = before.execute("SELECT n, at, data, flac, meta FROM stt_inputs ORDER BY n").fetchall() if lane else []
    before.close()
    assert [r[0] for r in rows] == [2, 3] and len(stt) == (3 if lane else 0)
    ob = Outbox(path)
    try:
        assert columns(path) == c.loads_strict(COLUMNS.read_text(encoding="utf-8"))
        assert ob.epoch == "0123456789abcdef"                                 # (the history files' made-up one)
        assert ob.db.execute("SELECT seq, at, kind, data, audio, audio_dropped FROM outbox ORDER BY seq").fetchall() == rows
        assert ob.db.execute("SELECT n, at, data, flac, meta FROM stt_inputs ORDER BY n").fetchall() == stt
        got = ob.batch()
        assert [(r["seq"], r["type"], bool(r["audio"])) for r in got] == [(2, rows[0][2], True), (3, rows[1][2], True)]
        assert got[0]["data"] == json.loads(rows[0][3]) and ob.audio_bytes == 2 * len(rows[0][4])
        # what the older build could not say gets the column's default: its context clips keep their mark (from #419
        # on), its speech-to-text inputs are "other traffic" (from #701 on: their own priority); no event of it is a
        # quarantined one sent again, and nothing is in the quarantine
        marks = [r[0] for r in ob.db.execute("SELECT context FROM outbox ORDER BY seq")]
        assert marks == ([0, 0] if shape.name == "1-events" else [1, 1])
        assert ob.context_bytes == sum(marks) * len(rows[0][4])
        prio = outbox_mod.STT_PRE if shape.name == "4-stt-priority" else outbox_mod.STT_NORMAL
        assert [r["prio"] for r in ob.stt_batch(now=T0)] == [prio] * len(stt)
        assert [r["qid"] for r in ob.batch(meta=True)] == [None, None] and ob.summary()["n"] == 0
        assert ob.requeue_due(now=T0 + 86400) == []
        assert ob.add("heard", {"radio_id": "990001"}, at=T0) == 4
        assert ob.add_stt({"id": "a1b2c3d4e5"}, b"\xc3" * 12, {"v": 1}, at=T0, prio=3) == len(stt) + 1
    finally:
        ob.close()
    ob = Outbox(path)                                     # ... and a second open changes nothing more
    try:
        assert columns(path) == c.loads_strict(COLUMNS.read_text(encoding="utf-8")) and ob.pending()[0] == 3
    finally:
        ob.close()


def test_a_file_with_the_columns_of_a_commit_that_was_never_on_main_opens_too(tmp_path):
    """Inside #671 (2e55493a, never on main by itself) the speech-to-text input sat on the event's own row: three
    columns added to outbox. A development station's file may still have them: today's code leaves them alone."""
    path = tmp_path / "st" / "outbox.sqlite"
    old_file(path, BY_NAME["2-context"])
    db = connect(path, ro=False)
    db.executescript("ALTER TABLE outbox ADD COLUMN stt BLOB; ALTER TABLE outbox ADD COLUMN stt_meta TEXT;"
                     "ALTER TABLE outbox ADD COLUMN stt_dropped INTEGER NOT NULL DEFAULT 0;"
                     "UPDATE outbox SET stt = x'c3c3c3c3', stt_meta = '{\"v\":1}' WHERE seq = 2;")
    db.close()
    ob = Outbox(path)
    try:
        assert [r["seq"] for r in ob.batch()] == [2, 3] and ob.add("heard", {"radio_id": "990001"}, at=T0) == 4
        assert ob.stt_pending() == (0, 0)                 # (those inputs are not in the lane: never sent, never counted)
        assert ob.db.execute("SELECT LENGTH(stt), stt_dropped FROM outbox ORDER BY seq").fetchall() \
            == [(4, 0), (None, 0), (None, 0)]
    finally:
        ob.close()


# -- a switch back: today's file under an older build ----------------------------------------------------------------------

def todays_file(path: Path) -> dict:
    """A file today's code made and wrote everything it can into: events with and without audio, a context clip, one
    whose audio was dropped, speech-to-text inputs of every priority, and the quarantine (#762): a page the hub refused
    whose copy waits to go again (carrying qid), a transmission kept aside, an incident source's event the feed knows
    (source_latest). What it then holds."""
    ob = Outbox(path, 100)
    try:
        ob.add("traffic", {"id": "Fireground1-1794990000000", "source": "Fireground 1"}, b"\xa1" * 30, at=T0)
        ob.add("traffic", {"id": "east-7001-1794990001", "source": "East Dispatch", "system": "east"}, b"\xb2" * 15,
               at=T0 + 1, context=True)
        ob.add("page_text", {"id": "a1b2c3d4e5", "raw": "Northfield Fire"}, at=T0 + 2)
        ob.add("traffic", {"id": "Fireground1-1794990003000", "source": "Fireground 1"}, b"\xa4" * 30, at=T0 + 3)
        ob.add("traffic", {"id": "Fireground1-1794990004000", "source": "Fireground 1"}, b"\xa5" * 40, at=T0 + 4)
        ob.ack(1)
        for i, prio in enumerate((0, 3, 1, 2)):
            ob.add_stt({"id": f"clip-{i}", "kind": "traffic"}, bytes([0xc0 + prio]) * 5, {"v": 1, "format": "flac"},
                       at=T0 + i, prio=prio)
        ob.add("page", {"id": "f6a7b8c9d0", "label": "Paging 1"}, at=T0 + 5)
        ob.add("traffic", {"id": "Fireground1-1794990006000", "source": "Fireground 1"}, at=T0 + 6)
        for row in ob.batch(meta=True)[-2:]:
            ob.quarantine(row, "apply_failed", "RuntimeError: a bug on the hub", now=T0 + 10)
        assert [x["kind"] for x in ob.requeue_due(now=T0 + 700)] == ["page"]
        ob.add("source_event", {"source": "east-cad", "event": {"event_id": "E-1001", "kind": "Fire alarm"}}, at=T0 + 8)
        side = connect(path)
        try:
            kept = {t: side.execute(f"SELECT * FROM {t}").fetchall() for t in ("quarantine", "source_latest")}
        finally:
            side.close()
        return {"epoch": ob.epoch, "seqs": [r["seq"] for r in ob.batch()], "next": 10, "stt": ob.stt_pending(),
                "audio_bytes": ob.audio_bytes, "qids": {r["seq"]: r["qid"] for r in ob.batch(meta=True) if r["qid"]},
                "kept": kept}
    finally:
        ob.close()


@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: s.name)
def test_every_statement_an_older_build_ran_works_on_todays_file(tmp_path, shape):
    """The switch back, statement by statement. The updater puts the previous release back in seconds; that build
    then opens the file today's code wrote. Every statement it ran (contracts/outbox/history/) is run here against
    such a file, in its own order: none fails, its opening changes nothing, it reads the epoch and every waiting
    event, and what it adds and acks is there for today's code afterwards."""
    path = tmp_path / "st" / "outbox.sqlite"
    was = todays_file(path)
    assert was["seqs"] == [2, 3, 4, 5, 8, 9] and was["stt"][0] >= 1 and was["qids"] == {8: 1}
    assert [len(rows) for rows in was["kept"].values()] == [2, 1]
    shape_before = columns(path)
    db = connect(path, ro=False)
    try:
        opened = dict(shape.run(db, "open"))
        assert columns(path) == shape_before                                 # (its CREATE IF NOT EXISTS and ALTER: nothing)
        assert opened["SELECT value FROM meta WHERE key = 'epoch'"] == [(was["epoch"],)]
        assert opened["SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox"] == [(was["audio_bytes"],)]
        sql, params = shape.one("SELECT seq, at, kind, data, audio, audio_dropped FROM outbox")
        assert [r[0] for r in db.execute(sql, params)] == was["seqs"]       # every event today's code left, in order
        done = shape.run(db, "statements")
        assert len(done) == len(shape.parts["statements"]) >= 7
        added = db.execute("SELECT seq, context FROM outbox ORDER BY seq DESC LIMIT 1").fetchone()
        assert added == (was["next"], 0 if shape.name == "1-events" else 1)  # its own add: the next seq, never an old one
        if shape.name in ("3-stt-inputs", "4-stt-priority"):                # its input: after today's, "other traffic"
            assert db.execute("SELECT n, prio FROM stt_inputs ORDER BY n DESC LIMIT 1").fetchone() \
                == (5, outbox_mod.STT_PRE if shape.name == "4-stt-priority" else outbox_mod.STT_NORMAL)  # (or its own)
        else:                                                                # a build without the lane: the rows stay
            assert db.execute("SELECT COUNT(*) FROM stt_inputs").fetchone()[0] == was["stt"][0]
        # a build before the quarantine leaves it alone: its tables as they were, the copy's qid on its row
        assert {t: db.execute(f"SELECT * FROM {t}").fetchall() for t in was["kept"]} == was["kept"]
        assert db.execute("SELECT seq, qid FROM outbox WHERE qid IS NOT NULL").fetchall() == [(8, 1)]
    finally:
        db.close()
    ob = Outbox(path, 100)                                # ... and forward again
    try:
        assert ob.epoch == was["epoch"] and columns(path) == shape_before
        assert [r["seq"] for r in ob.batch()] == [s for s in was["seqs"] if s > 1] + [was["next"]]
        assert ob.add("heard", {"radio_id": "990001"}, at=T0 + 9) == was["next"] + 1
        assert {r["seq"]: r["qid"] for r in ob.batch(meta=True) if r["qid"]} == was["qids"] and ob.summary()["n"] == 2
    finally:
        ob.close()


def old_module(commit: str) -> tuple[types.ModuleType, str] | None:
    """sizeup/cloud/outbox.py as it was at that commit, from this checkout's git history, and its source; None where
    the history is not there (a shallow clone, a source archive)."""
    try:
        src = subprocess.run(["git", "-C", str(c.ROOT.parent), "show", f"{commit}:sizeup/cloud/outbox.py"],
                             capture_output=True, timeout=30, check=True).stdout.decode()
    except (OSError, subprocess.SubprocessError):
        return None
    mod = types.ModuleType(f"outbox_at_{commit}")
    exec(compile(src, f"outbox.py@{commit}", "exec"), mod.__dict__)   # noqa: S102 - this repository's own older source
    return mod, src


def statements_of(mod_src: str) -> set[str]:
    """Every SQL statement a version of outbox.py runs, read from its source (execute / executescript)."""
    import ast
    tree = ast.parse(mod_src)
    schema = next(n.value.value for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "SCHEMA")
    out = {s.strip() for s in schema.split(";") if s.strip()}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "execute":
            a = n.args[0]
            out.add(a.value if isinstance(a, ast.Constant) else
                    "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in a.values))
    return out


def norm(sql: str) -> str:
    """A statement as its words: whitespace as one space, a list of marks as one."""
    return re.sub(r"\((\?,?)+\)|\(\{\}\)", "(?)", re.sub(r"\s+", " ", sql)).strip()


def same_statements(ours: set[str], theirs: set[str]) -> list[str]:
    """What differs between the history file's statements and the source's, both as norm() gives them. A value the
    source puts into its text ({}: an f-string's {STT_NORMAL}, or a condition it adds or leaves out) stands for any
    text there, so the history file has each form that build ran."""
    pattern = {s: re.compile(re.escape(s).replace(re.escape(" {} "), " (?:.+ )?").replace(re.escape("{}"), ".+") + "$")
               for s in theirs if "{}" in s}
    left = {s for s in ours if s not in theirs and not any(p.match(s) for p in pattern.values())}
    unused = {s for s in theirs if s not in ours and not (s in pattern and any(pattern[s].match(o) for o in ours))}
    return sorted(left | unused)


@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: s.name)
def test_the_older_build_itself_runs_on_todays_file(tmp_path, shape):
    """The switch back with the older build's own code, where this checkout has the history (else skipped: the test
    above is the one that always runs). outbox.py as it was at that commit opens the file today's code wrote, reads
    every waiting event, adds, sends and acks; today's code then goes on from there. And the history file is that
    commit's SQL, statement for statement."""
    got = old_module(shape.commit)
    if got is None:
        pytest.skip(f"no git history here for {shape.commit}")
    old, src = got
    theirs = {norm(x) for x in statements_of(src)}
    ours = {norm(sql) for part in shape.parts.values() for sql, _, _ in part}
    assert same_statements(ours, theirs) == []
    path = tmp_path / "st" / "outbox.sqlite"
    was = todays_file(path)
    shape_before = columns(path)
    ob = old.Outbox(path, 100)
    try:
        assert ob.epoch == was["epoch"] and columns(path) == shape_before
        assert [r["seq"] for r in ob.batch()] == was["seqs"]
        assert ob.add("traffic", {"id": "Fireground1-1794990009000"}, b"\xa9" * 10, at=T0 + 9) == was["next"]
        assert ob.ack(3) == 2 and ob.pending()[0] == len(was["seqs"]) - 1
        if shape.name == "3-stt-inputs":
            assert ob.add_stt({"id": "clip-9"}, b"\xc9" * 5, {"v": 1}, at=T0 + 9) == 5
            # before #701: oldest first whatever the priority, and the oldest dropped first (here the context one)
            assert [r["n"] for r in ob.stt_batch()] == sorted(r["n"] for r in ob.stt_batch())
        elif shape.name == "4-stt-priority":
            assert ob.add_stt({"id": "clip-9"}, b"\xc9" * 5, {"v": 1}, at=T0 + 9) == 5
            prios = [r["prio"] for r in ob.stt_batch(now=T0 + 9)]                # (by priority, as today)
            assert prios == sorted(prios, reverse=True) and outbox_mod.STT_NORMAL in prios
        else:
            assert not hasattr(ob, "add_stt")
        assert not hasattr(ob, "quarantine")              # (every one of them is from before #762)
    finally:
        ob.close()
    ob = Outbox(path, 100)
    try:
        assert ob.epoch == was["epoch"] and [r["seq"] for r in ob.batch()] == [s for s in was["seqs"] if s > 3] + [
            was["next"]]
        assert ob.add("heard", {"radio_id": "990001"}, at=T0 + 10) == was["next"] + 1
        if shape.name in ("3-stt-inputs", "4-stt-priority"):
            assert ob.db.execute("SELECT prio FROM stt_inputs WHERE n = 5").fetchone() == (outbox_mod.STT_NORMAL,)
        assert ob.summary()["n"] == 2 and {r["seq"]: r["qid"] for r in ob.batch(meta=True) if r["qid"]} == was["qids"]
    finally:
        ob.close()


def test_what_a_build_from_before_the_lane_leaves_behind_after_a_switch_back(tmp_path):
    """As built, and the cost of a switch back that far: a build from before #671 does not know the stt_inputs table.
    The inputs today's code queued stay in the file, unsent, and count toward nobody's limit until a build that knows
    them is back (it then sends them: the hub keeps each by its clip id, so late is harmless). A build from before
    #419 treats context audio as any audio: no budget of its own, and the oldest audio goes first whoever's it is
    (today: the context clip before the fire one)."""
    path = tmp_path / "st" / "outbox.sqlite"
    ob = Outbox(path, 1000)
    ob.add("traffic", {"id": "Fireground1-1794990000000", "source": "Fireground 1"}, b"\xa1" * 30, at=T0)
    ob.add("traffic", {"id": "east-7001-1794990001", "source": "East Dispatch", "system": "east"}, b"\xb2" * 15,
           at=T0 + 1, context=True)
    ob.add_stt({"id": "a1b2c3d4e5", "kind": "page"}, b"\xc3" * 12, {"v": 1}, at=T0, prio=3)
    ob.close()
    first = BY_NAME["1-events"]
    assert not [sql for part in first.parts.values() for sql, _, _ in part if "stt_inputs" in sql or "context" in sql]
    db = connect(path, ro=False)
    first.run(db, "open")
    its_next = db.execute(first.one("SELECT seq, LENGTH(audio) FROM outbox WHERE audio IS NOT NULL")[0]).fetchone()
    todays_next = db.execute("SELECT seq FROM outbox WHERE audio IS NOT NULL AND context = 1 ORDER BY seq "
                             "LIMIT 1").fetchone()
    assert (its_next[0], todays_next[0]) == (1, 2)        # the audio that goes next: the fire clip's / the context one's
    first.run(db, "statements")                           # (it adds one event, acks seq 1 and drops no input)
    assert db.execute("SELECT n, LENGTH(flac), prio FROM stt_inputs").fetchall() == [(1, 12, 3)]
    db.close()
    ob = Outbox(path, 1000)
    try:
        assert ob.stt_pending() == (1, 12) and [r["n"] for r in ob.stt_batch(now=T0)] == [1]
        assert [(r["seq"], bool(r["audio"])) for r in ob.batch()] == [(2, True), (3, True)]
    finally:
        ob.close()


def test_what_a_build_from_before_the_quarantine_does_with_it_after_a_switch_back(tmp_path):
    """As built, and the cost of a switch back to a build from before #762 (4-stt-priority.sql): it does not know the
    quarantine. It sends a refused page's copy that waits in the outbox as any event and deletes it on the ack, but
    the quarantine row stays: once today's code is back it sends the page again (twice in all: the hub keys a page by
    its id). It notes no newer snapshot of an incident source's event in source_latest, so today's code then sends a
    quarantined older snapshot again after the newer one, and the hub applies it (O5, pinned below). What was held
    stays held; nothing is lost either way."""
    path = tmp_path / "st" / "outbox.sqlite"
    feed = {"source": "east-cad", "event": {"event_id": "E-1001", "kind": "Fire alarm"}}
    ob = Outbox(path)
    try:
        ob.add("source_event", feed, at=T0)
        ob.quarantine(ob.batch(meta=True)[0], "apply_failed", "RuntimeError", now=T0 + 200)      # (again at +800)
        ob.add("page", {"id": "f6a7b8c9d0", "label": "Paging 1"}, at=T0 + 1)
        ob.quarantine(ob.batch(meta=True)[0], "apply_failed", "RuntimeError", now=T0 + 10)       # (again at +610)
        assert [(x["kind"], x["superseded"]) for x in ob.requeue_due(now=T0 + 700)] == [("page", False)]
        assert [(r["seq"], r["qid"]) for r in ob.batch(meta=True)] == [(3, 2)]
    finally:
        ob.close()
    older = BY_NAME["4-stt-priority"]
    db = connect(path, ro=False)
    try:
        older.run(db, "open")
        newer = json.dumps(dict(feed, event=dict(feed["event"], closed=True)), separators=(",", ":"))
        db.execute(older.one("INSERT INTO outbox")[0], (T0 + 300, "source_event", newer, None, 0))
        assert [r[0] for r in db.execute(*older.one("SELECT seq, at, kind, data, audio, audio_dropped FROM outbox"))] \
            == [3, 4]                                     # the copy goes up as any event, the newer snapshot after it
        db.execute(older.one("DELETE FROM outbox WHERE seq <=")[0], (4,))         # the hub took both
    finally:
        db.close()
    ob = Outbox(path)
    try:
        assert ob.summary()["n"] == 2 and ob.pending()[0] == 0
        assert ob.db.execute("SELECT seq FROM source_latest").fetchall() == [(1,)]   # (the newer one is not noted)
        assert [(x["kind"], x["superseded"]) for x in ob.requeue_due(now=T0 + 900)] \
            == [("source_event", False), ("page", False)]                     # both again: the page twice in all
        assert [(r["seq"], r["type"], r["qid"]) for r in ob.batch(meta=True)] == [(5, "source_event", 1), (6, "page", 2)]
    finally:
        ob.close()


# -- known deviations: pinned (the switches are at the top of this file) -------------------------------------------------

def test_known_deviation_a_file_set_back_hands_out_the_same_seq_again(tmp_path):
    """O1. The hub takes each event once by (epoch, seq): a seq at or under its cursor is skipped and acked. The file
    keeps the counter (sqlite_sequence) and the epoch, and nothing else says how far the station got. So a file set
    back to an earlier state, with the same epoch, hands out seqs the hub has already had: the next events are
    skipped by the hub without a word, acked, and deleted here. A file is set back by a power cut: the station opens
    it with synchronous = NORMAL in WAL mode, where SQLite does not sync a commit to the disk, so the last commits
    (the adds AND the acks' deletes) can be gone after the restart; and by putting back a copy. Here the cut is made
    by keeping the log only as long as it was a moment before. Fixed: a seq once handed out is never handed out
    again under the same epoch (the commit synced before the row is sent, or a new epoch after a stop that was not
    in order), or the station says so."""
    path = tmp_path / "st" / "outbox.sqlite"
    wal = path.with_name("outbox.sqlite-wal")
    ob = Outbox(path)
    try:
        assert [ob.add("page", {"id": "a1b2c3d4e5"}, at=T0), ob.add("page_text", {"id": "a1b2c3d4e5"}, at=T0 + 1)] == [1, 2]
        ob.ack(2)                                         # sent, and the hub has them: its cursor is (epoch, 2)
        synced = wal.stat().st_size                       # ... the disk has the log up to here
        assert [ob.add("page", {"id": "f6a7b8c9d0"}, at=T0 + 2),
                ob.add("page_text", {"id": "f6a7b8c9d0"}, at=T0 + 3)] == [3, 4]
        assert [r["seq"] for r in ob.batch()] == [3, 4]
        ob.ack(4)                                         # sent too: the hub's cursor is (epoch, 4)
        assert wal.stat().st_size > synced
        after = tmp_path / "after-the-cut" / "outbox.sqlite"
        after.parent.mkdir()
        shutil.copy(path, after)                          # the power goes: the main file as it is, the log's start
        after.with_name("outbox.sqlite-wal").write_bytes(wal.read_bytes()[:synced])
        epoch, sync = ob.epoch, ob.db.execute("PRAGMA synchronous").fetchone()[0]
    finally:
        ob.close()
    back = Outbox(after)
    try:
        assert back.pending()[0] == 0                     # (a valid file: as it was after the first ack)
        seq = back.add("page", {"id": "0a1b2c3d4e"}, at=T0 + 60)       # the first page after the power is back
        if O1_A_FILE_SET_BACK_HANDS_OUT_THE_SAME_SEQ_AGAIN:
            assert (back.epoch, seq, sync) == (epoch, 3, 1)             # under the hub's cursor: skipped there, and acked
        else:
            assert sync >= 2 or back.epoch != epoch or seq > 4
    finally:
        back.close()


def test_known_deviation_nan_is_stored_as_a_token_that_is_not_json(tmp_path):
    """O2. D1 again, in the file: add() writes a float NaN or infinity in an event's data as the bare token NaN /
    Infinity / -Infinity (json.dumps' default). That is not JSON: a second implementation that reads the row with a
    strict reader fails on it. Python's own reader takes it back, and the forwarder then sends it to the hub as it
    is (D1). Fixed: stored as null."""
    ob = Outbox(tmp_path / "st" / "outbox.sqlite")
    try:
        ob.add("traffic", {"id": "Tower-1794990000000", "snr_db": float("nan"), "floor_dbfs": float("-inf")}, at=T0)
        text = ob.db.execute("SELECT data FROM outbox").fetchone()[0]
        if O2_NAN_IS_STORED_AS_A_TOKEN_THAT_IS_NOT_JSON:
            assert text == '{"id":"Tower-1794990000000","snr_db":NaN,"floor_dbfs":-Infinity}'
            with pytest.raises(c.NotStrictJson):
                c.loads_strict(text)
            assert ob.batch()[0]["data"]["snr_db"] != ob.batch()[0]["data"]["snr_db"]      # (a NaN again)
        else:
            assert c.loads_strict(text) == {"id": "Tower-1794990000000", "snr_db": None, "floor_dbfs": None}
    finally:
        ob.close()


O3_ROWS = {    # the row; what it raises; where: outside the forwarder's error handling (the loop is started again) or
               # inside it (since #762: taken as a failed post, the same batch again after the backoff)
    "data that is not JSON": ("INSERT INTO outbox (at, kind, data) VALUES (1794990000.0, 'heard', 'not json')",
                              "JSONDecodeError", "restarted"),
    "audio stored as text": ("INSERT INTO outbox (at, kind, data, audio) VALUES (1794990000.0, 'heard', '{}', 'RIFF')",
                             "TypeError", "retried"),
}


@pytest.mark.parametrize("case", list(O3_ROWS))
def test_known_deviation_a_row_the_station_cannot_read_blocks_the_forwarder(tmp_path, monkeypatch, case):
    """O3. The station as the reader of its own file does not handle a row of the wrong type. `data` that is not JSON
    raises in Outbox.batch(), outside the forwarder's own error handling: since #782 the thread no longer ends, the
    link starts its loop again and counts it (the Hub row, Health), and the loop raises again on the same row, which is
    still the oldest. `audio` stored as text (a writer that bound a string) comes back as a str and raises TypeError in
    the base64 of the post; since #762 that is inside the forwarder's error handling (StationLink._post_events), which
    takes it as a failed post: the hub link says "a bytes-like object is required" as its error and the same batch
    goes again after the backoff (no restart, no quarantine: the quarantine is for what the hub refuses). Either way
    no event goes to the hub, across restarts of the station too. Only a second writer of the file (or a damaged row)
    can make one: today's add() cannot. Fixed: the row is set aside (said once in the log), and the events behind it
    go up."""
    sql, error, how = O3_ROWS[case]
    died: list[tuple[str, str]] = []
    monkeypatch.setattr(threading, "excepthook", lambda a: died.append((a.thread.name, type(a.exc_value).__name__)))
    monkeypatch.setattr(stationmod, "RESTART_S", (0.05, 0.2))                            # (the loop's restart delays)
    lv = c.Live(tmp_path)
    try:
        st = lv.station()
        link = st.link
        forward = next(t for t in link._threads if t.name == "hub-forward")
        hub = lv.hub.app.hub
        failed: list[str] = []
        fail = link._fail
        link._fail = lambda e: (failed.append(type(e).__name__), fail(e))[1]          # the link's error handling
        link.emit("heard", {"system": "p25", "radio_id": "990001", "alias": "", "at": None, "channel": ""})
        assert wait(lambda: link.outbox.pending()[0] == 0 and hub.seq == 1, 30)          # the lane works
        link.outbox.db.execute(sql)                                                      # another writer's row: seq 2
        link.emit("heard", {"system": "p25", "radio_id": "990002", "alias": "", "at": None, "channel": ""})
        if O3_A_ROW_IT_CANNOT_READ_BLOCKS_THE_FORWARDER and how == "restarted":
            assert wait(lambda: link.restarts.get("hub-forward", {}).get("n", 0) >= 2, 30)   # started again, raised again
            assert link.restarts["hub-forward"]["why"] == error and forward.is_alive() and not died
            assert link.outbox.pending()[0] == 2 and hub.seq == 1                        # nothing more goes up
            with pytest.raises(ValueError):
                link.outbox.batch()                                                      # ... nor would it next time
        elif O3_A_ROW_IT_CANNOT_READ_BLOCKS_THE_FORWARDER:
            assert wait(lambda: failed.count(error) >= 2, 30)                            # a failed post, and again
            assert "hub-forward" not in link.restarts and forward.is_alive() and not died
            assert link.outbox.pending()[0] == 2 and hub.seq == 1 and link.outbox.summary()["n"] == 0
        else:
            assert wait(lambda: hub.seq == 3, 30) and forward.is_alive() and not died
            assert link.outbox.pending()[0] == 0
    finally:
        lv.close()


def test_known_deviation_a_file_sqlite_cannot_open_stops_the_station(tmp_path):
    """O4. The outbox is opened while the station starts, outside any error handling: a file that is not an SQLite
    database (damaged, or written by a program with a newer SQLite's features) raises there, and the station does
    not start at all: no radio, no console, no speech-to-text, over a file that only queues what goes to the hub.
    Fixed: the file is moved aside with a line in the log, a new outbox is made (a new epoch) and the station
    starts."""
    lv = c.Live(tmp_path)
    try:
        (tmp_path / "st").mkdir()
        (tmp_path / "st" / "outbox.sqlite").write_bytes(b"not an SQLite file " * 300)
        if O4_A_FILE_SQLITE_CANNOT_OPEN_STOPS_THE_STATION:
            with pytest.raises(sqlite3.DatabaseError, match="not a database"):
                lv.station()
        else:
            st = lv.station()
            assert HEX16.fullmatch(st.link.outbox.epoch) and st.link.outbox.add("heard", {}, at=T0) == 1
    finally:
        lv.close()


def test_known_deviation_a_resent_snapshot_puts_back_what_the_feed_changed_after_a_switch_back(tmp_path):
    """O5. An incident source's snapshot S1 the hub refused is quarantined on the station (critical: sent again later).
    The station is switched back to the build from before #762 (contracts/outbox/history/4-stt-priority.sql) and
    queues the feed's newer snapshot S2 of the same event, a minute later, without noting it in source_latest (that
    build has no such table); the hub applies S2. Back on today's code, requeue_due() finds no newer snapshot noted and
    sends S1 again, and the hub applies it: sizeup/cad/sources.py on_event skips an older snapshot only when it is more
    than SNAPSHOT_SKEW_S (120 s) older than the one applied. So the incident's type, location and units go back to S1's
    and stay so until the feed changes the event again; nobody is told. Fixed (the hub's maintainers' choice, #791):
    the station says a copy is a re-send (`qid` on the event in the post), the hub marks that source_event `resent`,
    and on_event drops a re-sent copy whose seen_at is older than the applied report's, however small the gap, said
    once in the log; a first send keeps the 120 s tolerance (checked here too). Not chosen: the station reconciling
    source_latest from the outbox when it opens a file."""
    now = time.time()
    lv = lane.Lane(tmp_path / "hub")
    client = HubClient(lv.url, lv.own, timeout=30.0)
    sources = lv.hub.app.cad.sources
    said: list[str] = []
    real_log = sources.log
    sources.log = lambda m: (said.append(m), real_log(m))[1]

    def snapshot(seen: float, kind: str, where: str, units: list[str]) -> dict:
        return {"source": "demo", "label": "Demo feed", "rank": 1, "seen_at": seen, "first_seen": now - 600,
                "event": {"source": "demo", "event_id": "S1", "type": kind, "location": where, "units": units,
                          "times": {"dispatched": now - 600}, "unit_times": {}, "comments": [], "closed": False,
                          "extra": {}, "aliases": []}}

    def on_the_hub() -> tuple:
        d = json.loads(sources._report("demo", "S1")["data"])
        return d["type"], d["location"], d["units"]
    s1 = snapshot(now - 300, "ALARM", "12 Example Ave", ["E5"])
    s2 = snapshot(now - 240, "STRUCTURE FIRE", "14 Example Ave", ["E5", "L2"])
    path = tmp_path / "st" / "outbox.sqlite"
    ob = Outbox(path)
    try:
        ob.add("source_event", s1, at=now - 300)
        ob.quarantine(ob.batch(meta=True)[0], "apply_failed", "RuntimeError", now=now - 290)    # (the hub's trouble)
        epoch = ob.epoch
    finally:
        ob.close()
    older = BY_NAME["4-stt-priority"]                     # the switch back: that build's own statements
    db = connect(path, ro=False)
    try:
        older.run(db, "open")
        db.execute(older.one("INSERT INTO outbox")[0], (now - 240, "source_event", json.dumps(s2), None, 0))
        rows = db.execute(*older.one("SELECT seq, at, kind, data, audio, audio_dropped FROM outbox")).fetchall()
        sent = client.request("POST", "/api/station/events", {"epoch": epoch, "station": c.STATION, "events": [
            {"seq": seq, "at": at, "type": kind, "data": json.loads(data), "audio": None, "audio_dropped": False}
            for seq, at, kind, data, _, _ in rows]}, timeout=30.0)
        assert sent["acked"] == 2 and "rejected" not in sent
        db.execute(older.one("DELETE FROM outbox WHERE seq <=")[0], (2,))
    finally:
        db.close()
    assert on_the_hub() == ("STRUCTURE FIRE", "14 Example Ave", ["E5", "L2"])          # S2 applied
    ob = Outbox(path)                                     # ... and today's code again
    link = types.SimpleNamespace(outbox=ob, station_id=c.STATION)
    try:
        assert [(x["kind"], x["superseded"]) for x in ob.requeue_due(now=now + 400)] == [("source_event", False)]
        copy = ob.batch(meta=True)
        assert [(r["seq"], r["qid"], r["data"]["seen_at"]) for r in copy] == [(3, 1, now - 300)]
        posted: list[dict] = []
        real = client.request
        client.request = lambda *a, **k: (posted.append(a[2]), real(*a, **k))[1]   # the body as the station sends it
        assert stationmod.StationLink._post_events(link, client, copy)["acked"] == 3
        client.request = real
        if O5_A_RESENT_SNAPSHOT_PUTS_BACK_WHAT_THE_FEED_CHANGED:
            assert "qid" not in posted[0]["events"][0]                                 # (nothing says it is a re-send)
            assert on_the_hub() == ("ALARM", "12 Example Ave", ["E5"])                # S1 over S2: the change is gone
            assert not any("re-sent older report" in m for m in said)
        else:
            assert posted[0]["events"][0]["qid"] == 1
            assert on_the_hub() == ("STRUCTURE FIRE", "14 Example Ave", ["E5", "L2"])  # S2 stays
            assert sum("re-sent older report" in m for m in said) == 1                 # said once
        # a first send (no qid) a minute older than the hub's snapshot: applied, as today (a clock stepping back)
        s3 = snapshot(now - 300, "ALARM", "16 Example Ave", ["E7"])
        ob.add("source_event", s3, at=now - 300)
        assert stationmod.StationLink._post_events(link, client, ob.batch(meta=True)[-1:])["acked"] == 4
        assert on_the_hub() == ("ALARM", "16 Example Ave", ["E7"])
    finally:
        ob.close()
        client.close()
        lv.close()


def test_each_outbox_deviation_has_its_switch_and_its_test():
    """O1.. are pinned here: each switch at the top of this file is read by exactly one test whose docstring starts
    with its number."""
    src = Path(__file__).read_text(encoding="utf-8")
    switches = re.findall(r"^(O\d+)_[A-Z0-9_]+ = (?:True|False)\b", src, re.M)
    pinned = re.findall(r'def test_known_deviation_\w+\(.*\):\n    \"\"\"(O\d+)\. ', src)
    assert switches == pinned == [f"O{i}" for i in range(1, len(switches) + 1)] and len(switches) == 5
