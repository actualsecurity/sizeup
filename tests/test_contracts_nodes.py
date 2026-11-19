"""contracts/ (ADR 0004): the node schemas of docs/nodes.md (node, roles, capabilities, lease, and the node fields on
the state post and its answer). Designed, not implemented: these tests hold the schemas to the design document's own
tables and to the constants the design points at in today's code, and check the dangerous cases the design calls out
(a lease without a relative time, lease ids compared as a pair, integers above 2^53). What today's hub and station do
with these keys (ignore them) is in tests/test_contracts_station_state.py."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sizeup import sttpool
from sizeup.cloud import stations, sttworkers
from tests import contracts as c

DOC = (Path(__file__).resolve().parent.parent / "docs" / "nodes.md").read_text(encoding="utf-8")
DESIGNED = ("node", "roles", "capabilities", "lease", "node-state", "node-state-answer", "node-worker-hello")


def table_names(heading: str) -> list[str]:
    """The names in the first column of the table under a heading of docs/nodes.md (each in backticks)."""
    body = DOC.split(heading, 1)[1]
    rows = []
    for line in body.splitlines()[1:]:
        if line.startswith("|"):
            rows.append(line)
        elif rows:
            break
    return [n for row in rows[2:] for n in re.findall(r"`([^`]+)`", row.split("|")[1])]


def test_the_designed_schemas_say_so():
    assert {n for n in c.names() if c.load(n)["description"].startswith("Designed (docs/nodes.md")} == set(DESIGNED)


def test_the_roles_are_the_design_documents():
    assert table_names("### Roles") == ["radio-receive", "feed-adapter", "stt-light", "stt-heavy", "infer", "adsb", "ais",
                                        "training"]
    assert list(c.load("roles")["properties"]) == table_names("### Roles")


def test_the_capabilities_are_the_design_documents():
    names = table_names("### Capabilities (reported, never trusted for security)")
    assert names == ["cpu", "mem", "disk", "gpu", "sdrs", "groups", "channels", "whisper", "llm", "adapter", "state",
                     "net", "build"]
    assert list(c.load("capabilities")["properties"]) == names


def test_the_wire_names_are_the_design_documents():
    wire = DOC.split("### Wire contract", 1)[1].split("## 6.", 1)[0]
    post, answer = c.load("node-state")["properties"], c.load("node-state-answer")["properties"]
    for name in ("capabilities", "capabilities_rev", "roles_applied"):
        assert f"`{name}`" in wire and name in post
    for name in ("capabilities_wanted", "roles_rev"):
        assert f"`{name}`" in wire and name in answer
    assert "`{role, holder, gen, ttl_s}`" in wire and c.load("lease")["required"] == ["role", "holder", "gen", "ttl_s"]
    assert "caps" not in post and "capabilities" not in answer        # `caps` is the hub's list, `capabilities` the node's


def test_the_node_id_and_name_are_todays_station_and_worker_ids():
    node = c.load("node")["properties"]
    assert node["id"]["pattern"] == f"^{stations.ID.pattern}$" == f"^{sttworkers.ID.pattern}$"
    assert node["name"]["maxLength"] == stations.NAME_MAX == sttworkers.NAME_MAX == 60
    assert c.load("lease")["properties"]["holder"]["pattern"] == node["id"]["pattern"]
    for wid in (c.STATION, c.WORKER, "0", "a_b-c"):
        assert stations.ID.fullmatch(wid) and c.errors("node", {"id": wid}) == []
    for bad in ("St-North", "-north", "st.north", "", "a" * 41):
        assert not stations.ID.fullmatch(bad) and c.errors("node", {"id": bad})
    assert node["kind"]["x-known"] == ["station", "worker", "station+worker"] and node["kind"]["readOnly"] is True


def test_the_pool_classes_and_worker_states_are_todays():
    roles, classes = c.load("roles")["properties"], [sttpool.class_key(x) for x in sttpool.CLASSES]
    assert sorted(roles["stt-heavy"]["properties"]["classes"]["items"]["x-known"]) == sorted(classes)
    fast = {sttpool.class_key(x) for x in sttpool.FAST_CLASSES}        # the pre-alert classes: never a station's
    light = roles["stt-light"]["properties"]["classes"]["items"]
    assert set(light["x-known"]) == set(classes) - fast and set(light["not"]["enum"]) == fast
    for cls in classes:
        assert (c.errors("roles", {"stt-light": {"classes": [cls]}}) == []) == (cls not in fast), cls
    assert roles["stt-light"]["properties"]["pool"]["default"] is False
    assert roles["infer"]["properties"]["llm_jobs"]["default"] is True
    assert c.load("capabilities")["properties"]["state"]["x-known"] == list(sttpool.STATES)


def test_a_word_from_a_newer_build_is_valid_and_a_wrong_type_is_not():
    """docs/nodes.md's closed lists (a dongle's state, a channel's running state, coverage, the pool classes, a
    worker's state, a node's kind) are open strings here: a reader must take a word it does not know."""
    caps, roles = c.load("capabilities")["properties"], c.load("roles")["properties"]
    assert caps["sdrs"]["items"]["properties"]["state"]["x-known"] == ["ok", "out", "held", "off"]
    assert caps["channels"]["items"]["properties"]["state"]["x-known"] == ["running", "waiting", "out", "held", "shed",
                                                                           "not_in_set", "local"]
    cover = roles["radio-receive"]["properties"]["channels"]["items"]["properties"]["coverage"]
    assert cover["x-known"] == ["exclusive", "overlap", "failover"]
    for name, make in (("capabilities", lambda v: {"sdrs": [{"serial": "00000101", "state": v}]}),
                       ("capabilities", lambda v: {"channels": [{"job": "Paging 1", "state": v}]}),
                       ("capabilities", lambda v: {"state": v}),
                       ("roles", lambda v: {"radio-receive": {"channels": [{"job": "Paging 1", "coverage": v}]}}),
                       ("roles", lambda v: {"stt-heavy": {"classes": [v]}}),
                       ("roles", lambda v: {"stt-light": {"classes": [v]}}),
                       ("node", lambda v: {"id": c.STATION, "kind": v})):
        assert c.errors(name, make("from_the_future")) == [], name
        for bad in (1, None, True, ["ok"], {"x": 1}):
            assert c.errors(name, make(bad)) != [], (name, bad)


def test_a_worker_sends_its_capabilities_in_its_hello():
    """docs/nodes.md 1 and 5 step 1: the worker's capabilities go in its hello. Every hello valid today stays valid."""
    doc = c.load("node-worker-hello")
    assert doc["allOf"] == [{"$ref": "stt-worker-hello.schema.json"}] and "required" not in doc
    assert set(doc["properties"]) == {"capabilities", "capabilities_rev"}
    for file, inst in c.vectors("stt-worker-hello", "valid"):
        assert c.errors("node-worker-hello", inst) == [], file
    for file, inst in c.vectors("stt-worker-hello", "invalid"):
        assert c.errors("node-worker-hello", inst), file


def test_every_post_and_answer_of_today_is_valid_with_the_node_fields_absent():
    """The new keys are all optional: what is valid today stays valid (the recorded bodies of a real station and hub
    are checked the same way in tests/test_contracts_station_state.py)."""
    for old, new in (("station-state", "node-state"), ("station-state-answer", "node-state-answer")):
        assert c.load(new)["allOf"] == [{"$ref": f"{old}.schema.json"}] and "required" not in c.load(new)
        for file, inst in c.vectors(old, "valid"):
            assert c.errors(new, inst) == [], (new, file)
        for file, inst in c.vectors(old, "invalid"):
            assert inst is c.NOT_JSON or c.errors(new, inst), (new, file)       # ... and what is invalid stays invalid


# -- the lease ---------------------------------------------------------------------------------------------------------

LEASE = {"role": "feed-adapter", "holder": c.STATION, "gen": [7, 14], "ttl_s": 90}


def test_a_lease_needs_its_relative_time_and_an_absolute_one_is_advisory():
    assert c.errors("lease", LEASE) == []
    assert c.errors("lease", dict(LEASE, expires_at=1_794_990_090.0)) == []
    only = {k: v for k, v in LEASE.items() if k != "ttl_s"} | {"expires_at": 1_794_990_090.0}
    assert c.errors("lease", only) == ["$: 'ttl_s' is a required property"]
    assert dict(c.vectors("lease", "invalid"))["only-expires-at.json"] == only | {"holder": "st-north"}
    for ttl in (0, -1, "90", None, 90_000):                           # 90000: milliseconds by mistake
        assert c.errors("lease", dict(LEASE, ttl_s=ttl)), ttl
    answer = dict(c.vectors("node-state-answer", "valid"))["todays-answer.json"]
    assert c.errors("node-state-answer", dict(answer, leases=[only])) != []
    assert c.errors("node-state-answer", dict(answer, leases=[LEASE])) == []


def test_lease_ids_compare_as_a_pair_by_value():
    order = json.loads((c.VECTORS / "lease" / "order.json").read_text(encoding="utf-8"))["cases"]
    assert len(order) >= 10
    for case in order:
        a, b = case["a"], case["b"]
        assert c.errors("lease", dict(LEASE, gen=a)) == [] and c.errors("lease", dict(LEASE, gen=b)) == [], case
        assert c.lease_cmp(a, b) == case["expect"] == -c.lease_cmp(b, a), case
    # the three ways to get it wrong, each caught by a case above
    def by(key):
        return lambda a, b: ([key(x) for x in a] > [key(x) for x in b]) - ([key(x) for x in a] < [key(x) for x in b])
    wrong = {"one number, not a pair": lambda a, b: (int(a[1]) > int(b[1])) - (int(a[1]) < int(b[1])),
             "as strings": by(str), "as floats": by(float)}
    for how, cmp in wrong.items():
        assert any(cmp(x["a"], x["b"]) != x["expect"] for x in order), f"no case catches a comparison {how}"


def test_a_counter_above_2_53_is_a_string_or_refused():
    counter = c.subschema("lease", "#/$defs/counter")
    big = 2 ** 53 + 1
    assert counter.is_valid(2 ** 53 - 1) and not counter.is_valid(2 ** 53) and not counter.is_valid(big)
    assert counter.is_valid(str(big)) and c.counter(str(big)) == big
    assert counter.is_valid("18446744073709551615")                   # an unsigned 64-bit counter's top
    for bad in (-1, 1.5, True, None, "", "07", "+7", "-7", "7.0", "1e3", " 7", "0x10", "9" * 21, [7]):
        assert not counter.is_valid(bad), bad
        with pytest.raises(ValueError):
            c.counter(bad)
    for good in (0, 7, "0", "7", 2 ** 53 - 1):
        assert counter.is_valid(good) and c.counter(good) == int(good)
    with pytest.raises(ValueError):
        c.counter(big)                                                # the reference refuses what the schema refuses
    # the reference comparison and the schema agree on every lease vector
    for file, inst in c.vectors("lease", "valid"):
        assert c.lease_id(inst["gen"]) == tuple(int(x) for x in inst["gen"]), file
    for file, inst in c.vectors("lease", "invalid"):
        if file.startswith("generation-"):
            with pytest.raises(ValueError):
                c.lease_id(inst["gen"])
