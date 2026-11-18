"""contracts/ (ADR 0004): the harness. Every schema in contracts/schemas/ is JSON Schema 2020-12 with an $id, a title,
a status, a version and a description for every field; its $refs resolve without a network; it takes unknown keys;
its vectors in contracts/vectors/<name>/ validate (valid/) or are refused (invalid/); and contracts/README.md lists it
with its status. The harness is /api/v1's (jsonschema from the dev extras; tests/openapi_spec.py is the OpenAPI
side)."""
from __future__ import annotations

import re

import pytest
from jsonschema import Draft202012Validator

from tests import contracts as c

NAMES = c.names()
ID_BASE = "https://raw.githubusercontent.com/actualsecurity/sizeup/main/contracts/schemas/"
FUTURE = re.compile(r"from[_-]the[_-]future")           # the vectors' stand-in for a key this build does not know


def walk(node, path="$"):
    """Every (path, object) in a schema document."""
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from walk(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}[{i}]")


def test_there_are_schemas_and_each_has_vectors_both_ways():
    assert len(NAMES) >= 10
    for name in NAMES:
        assert c.vectors(name, "valid") and c.vectors(name, "invalid"), f"{name}: valid/ and invalid/ vectors"
    assert {p.name for p in c.VECTORS.iterdir()} == set(NAMES), "a vectors folder without its schema (or the reverse)"
    extra = [str(p.relative_to(c.VECTORS)) for p in c.VECTORS.rglob("*") if p.is_file()
             and p.parent.name not in ("valid", "invalid") and p.relative_to(c.VECTORS).as_posix() != "lease/order.json"]
    assert extra == [], "files in contracts/vectors/ no test reads"


@pytest.mark.parametrize("name", NAMES)
def test_the_schema_is_2020_12_and_says_what_it_is(name):
    doc = c.load(name)
    Draft202012Validator.check_schema(doc)
    assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert doc["$id"] == f"{ID_BASE}{name}.schema.json"
    assert doc["title"] and len(doc["description"]) > 80
    assert doc["x-status"] in c.STATUSES and isinstance(doc["x-version"], int) and doc["x-version"] >= 1
    assert doc["description"].startswith("Designed (docs/nodes.md") == (doc["x-status"] == "designed")


@pytest.mark.parametrize("name", NAMES)
def test_every_field_has_a_description_and_every_ref_resolves_here(name):
    doc = c.load(name)
    resolver = c.registry().resolver(doc["$id"])
    for path, node in walk(doc):
        if isinstance(node.get("$ref"), str):
            resolver.lookup(node["$ref"])                              # raises when it resolves nowhere in contracts/
    for path, prop in c.properties(doc):
        target = resolver.lookup(prop["$ref"]).contents if "$ref" in prop else {}
        assert (prop.get("description") or target.get("description") or "").strip(), f"{name} {path}: no description"


@pytest.mark.parametrize("name", NAMES)
def test_objects_take_unknown_keys(name):
    """The compatibility rule in the schemas themselves: no object refuses a key it does not name (the code under these
    schemas refuses none: tests/test_contracts_station_state.py sends them). A closed object would have to be listed
    under Known deviations in contracts/README.md first."""
    for path, node in walk(c.load(name)):
        assert node.get("additionalProperties") is not False, f"{name} {path}"
        assert node.get("unevaluatedProperties") is not False, f"{name} {path}"
        if node.get("x-complete") is True:
            assert "properties" in node, f"{name} {path}: x-complete with no properties"


def cases(kind):
    return [pytest.param(name, file, inst, id=f"{name}/{file}") for name in NAMES for file, inst in c.vectors(name, kind)]


@pytest.mark.parametrize("name,file,inst", cases("valid"))
def test_a_valid_vector_validates(name, file, inst):
    assert inst is not c.NOT_JSON, "a valid vector is JSON a strict reader takes"
    assert c.errors(name, inst) == []
    # ... names no key the schema does not, but for the stand-ins for a newer build's
    assert [p for p in c.undeclared(name, inst) if not FUTURE.search(p)] == []


@pytest.mark.parametrize("name,file,inst", cases("invalid"))
def test_an_invalid_vector_is_refused(name, file, inst):
    assert inst is c.NOT_JSON or c.errors(name, inst) != []


def test_unknown_keys_are_among_the_valid_vectors_of_every_schema():
    """Each schema proves the rule on itself: at least one valid vector carries a key from a newer build."""
    for name in NAMES:
        if c.load(name).get("allOf"):
            continue                                   # (the designed extensions: the schema they extend has one)
        assert any(FUTURE.search(repr(inst)) for _, inst in c.vectors(name, "valid")), name


def test_no_valid_vector_has_an_integer_a_float_would_change():
    """Transport (docs/nodes.md 5): an integer that may pass 2^53 goes as a string. A plain one above it is refused by
    lease.schema.json's counter, and no valid vector of any schema has one."""
    def ints(node):
        if isinstance(node, dict):
            for v in node.values():
                yield from ints(v)
        elif isinstance(node, list):
            for v in node:
                yield from ints(v)
        elif isinstance(node, int) and not isinstance(node, bool):
            yield node
    for name in NAMES:
        for file, inst in c.vectors(name, "valid"):
            assert all(c.float_exact(n) for n in ints(inst)), (name, file)
    assert c.float_exact(2 ** 53 - 1) and not c.float_exact(2 ** 53 + 1) and float(2 ** 53 + 1) == float(2 ** 53)


def test_the_strict_reader_refuses_what_is_not_json():
    assert c.loads_strict('{"a": 9007199254740993}') == {"a": 9007199254740993}       # exact, never a float
    for bad in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}', '{"a": 1, "a": 2}'):
        with pytest.raises(c.NotStrictJson):
            c.loads_strict(bad)


def test_undeclared_finds_a_key_the_schema_does_not_name():
    """The check that keeps an as-built schema complete, on itself: a key added where the schema lists every key is
    found at any depth; one added where it does not, or under a map of free keys, is not."""
    post = dict(c.vectors("station-state", "valid"))["with-status.json"]
    assert c.undeclared("station-state", post) == []
    post["status"]["outbox"]["new_counter"] = 1
    post["talking"] = [dict(dict(c.vectors("station-state", "valid"))["talking-only.json"]["talking"][0], new_flag=True)]
    post["status"]["health"]["new_gauge"] = 1                          # (health is not listed completely)
    post["status"]["stt_input"]["dropped"]["new_reason"] = 0           # (a map of counts by reason)
    post["new_top"] = 1
    assert sorted(c.undeclared("station-state", post)) == ["$.new_top", "$.status.outbox.new_counter",
                                                           "$.talking[0].new_flag"]
    assert c.errors("station-state", post) == []                       # ... while it still validates: keys are open


def test_the_readme_lists_every_schema_with_its_status_and_version():
    text = (c.ROOT / "README.md").read_text(encoding="utf-8")
    rows = dict((m[1], (m[2], int(m[3]))) for m in
                re.finditer(r"^\| `([a-z-]+)` \| (as built|designed) \| (\d+) \|", text, re.M))
    want = {n: (c.load(n)["x-status"].replace("-", " "), c.load(n)["x-version"]) for n in NAMES}
    assert rows == want


def test_each_known_deviation_is_pinned_by_a_test_or_says_it_was_read():
    text = (c.ROOT / "README.md").read_text(encoding="utf-8")
    tests = (c.ROOT.parent / "tests" / "test_contracts_station_state.py").read_text(encoding="utf-8")
    rows = {m[1]: m[0] for m in re.finditer(r"^\| (D\d+) \|.*$", text, re.M)}
    assert len(rows) >= 4
    pinned = set(re.findall(r'def test_known_deviation_\w+\(.*\):\n    \"\"\"(D\d+)\. ', tests))
    for d, row in rows.items():
        assert (d in pinned) != ("Found by reading" in row), f"{d}: pinned by a test, or found by reading"
        assert re.search(r"`[a-z/_.]+\.(py|md):\d+", row), f"{d}: file:line"
    assert pinned <= set(rows), "a test pins a deviation the README does not list"
