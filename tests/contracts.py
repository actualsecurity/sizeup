"""The contract tests' helper (contracts/, ADR 0004): the JSON Schemas of contracts/schemas/ loaded with their
cross-file $refs, the vectors of contracts/vectors/, a strict JSON reader, the reference comparison of lease ids, and
the /api/station/ routes read from the hub's own code. Dev-only (jsonschema, as /api/v1's contract tests:
tests/openapi_spec.py). Nothing here is imported by sizeup/."""
from __future__ import annotations

import ast
import copy
import inspect
import json
import os
import re
import textwrap
import time
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parent.parent / "contracts"
SCHEMAS, VECTORS = ROOT / "schemas", ROOT / "vectors"
SUFFIX = ".schema.json"
STATUSES = ("as-built", "designed")
MAX_SAFE = 2 ** 53 - 1          # the largest integer every JSON reader keeps exact


# -- schemas -----------------------------------------------------------------------------------------------------------

def names() -> list[str]:
    """Every schema's name: its file name without .schema.json."""
    return sorted(p.name[:-len(SUFFIX)] for p in SCHEMAS.glob("*" + SUFFIX))


def load(name: str) -> dict:
    return json.loads((SCHEMAS / (name + SUFFIX)).read_text(encoding="utf-8"))


def registry() -> Registry:
    """Every schema by its $id, so a $ref from one file to another resolves here and never over the network."""
    docs = [load(n) for n in names()]
    return Registry().with_resources((d["$id"], Resource.from_contents(d)) for d in docs)


def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(load(name), registry=registry())


def errors(name: str, instance) -> list[str]:
    """Why instance is not valid against the schema: [] when it is."""
    return [f"{'/'.join(map(str, e.absolute_path)) or '$'}: {e.message}" for e in validator(name).iter_errors(instance)]


def subschema(name: str, pointer: str) -> Draft202012Validator:
    """A validator for one part of a schema, e.g. ("lease", "#/$defs/counter")."""
    doc = load(name)
    return Draft202012Validator({"$ref": doc["$id"] + pointer}, registry=registry())


NOT_JSON = type("NotJson", (), {"__repr__": lambda self: "NOT_JSON"})()     # a vector file a strict reader refuses


PATCH = ".patch.json"            # a vector written as a base vector and the changes to it (RFC 6902 JSON Patch)


class Unfit(str):
    """What vector() gives for a patched vector whose patch no longer fits its base: the file and the operation, as
    text. A value rather than an exception, so the stale vector is one named test failure and not a collection error
    for its whole file."""


def _pointer(doc, path: str):
    """(the container, the key or index in it) an RFC 6901 JSON Pointer names in doc."""
    assert path == "" or path.startswith("/"), f"not a JSON Pointer: {path!r}"
    parts = [x.replace("~1", "/").replace("~0", "~") for x in path.split("/")[1:]]
    assert parts, "the whole document is replaced by another vector, not by a patch"
    for part in parts[:-1]:
        doc = doc[int(part)] if isinstance(doc, list) else doc[part]
    last = parts[-1]
    return doc, (len(doc) if last == "-" else int(last)) if isinstance(doc, list) else last


def patched(doc, ops: list[dict]):
    """A copy of doc with an RFC 6902 JSON Patch applied: `add`, `remove` and `replace`, which is all the vectors
    use. A `remove` or `replace` of what is not there fails, as the RFC says: a patch that no longer fits its base
    says so instead of passing."""
    out = copy.deepcopy(doc)
    for op in ops:
        assert set(op) <= {"op", "path", "value"} and op.get("op") in ("add", "remove", "replace"), op
        try:
            box, key = _pointer(out, op["path"])
            there = key < len(box) if isinstance(box, list) else key in box
        except (LookupError, TypeError, ValueError):       # a step of the pointer that the base no longer has
            raise AssertionError(f"{op['op']} {op['path']}: not in the base") from None
        if op["op"] == "add":
            if isinstance(box, list):
                box.insert(key, copy.deepcopy(op["value"]))
            else:
                box[key] = copy.deepcopy(op["value"])
        else:
            assert there, f"{op['op']} {op['path']}: not in the base"
            if op["op"] == "remove":
                del box[key]
            else:
                box[key] = copy.deepcopy(op["value"])
    return out


def vector(path: Path):
    """One vector file as the body it stands for. A plain .json file is the body (NOT_JSON when a strict reader refuses
    it: an invalid vector may be that, `NaN`). A .patch.json file is {"base": a vector's path under contracts/vectors/,
    "patch": [RFC 6902 operations]}: the base with the patch applied, or an Unfit naming the file and the operation
    when the patch no longer fits."""
    text = path.read_text(encoding="utf-8")
    if not path.name.endswith(PATCH):
        try:
            return loads_strict(text)
        except ValueError:
            return NOT_JSON
    doc = loads_strict(text)
    assert set(doc) == {"base", "patch"} and isinstance(doc["patch"], list) and doc["patch"], f"{path.name}: base, patch"
    base = VECTORS / doc["base"]
    assert base.is_file() and not base.name.endswith(PATCH), f"{path.name}: its base is a plain vector, {doc['base']}"
    try:
        return patched(vector(base), doc["patch"])
    except AssertionError as e:
        where = path.relative_to(VECTORS) if path.is_relative_to(VECTORS) else path.name
        return Unfit(f"{where}: the patch no longer fits {doc['base']}: {e}")


def vectors(name: str, kind: str) -> list[tuple[str, object]]:
    """[(name.json, the body)] of contracts/vectors/<name>/<kind>/ (kind: valid | invalid), plain and patched alike:
    `x.patch.json` is listed as `x.json`."""
    out = [(p.name.removesuffix(PATCH) + ".json" if p.name.endswith(PATCH) else p.name, vector(p))
           for p in (VECTORS / name / kind).glob("*.json")]
    assert len({n for n, _ in out}) == len(out), f"{name}/{kind}: a vector both plain and patched"
    return sorted(out, key=lambda x: x[0])


def dump(o, indent: int = 0, root: bool = True) -> str:
    """JSON as the recorded vectors are written: one line per object or list that fits in 118 columns, so a recorded
    status is a few hundred lines and a key added to it is a one-line diff."""
    inline = json.dumps(o, ensure_ascii=False)
    pad = " " * (indent + 2)
    if not isinstance(o, (dict, list)) or not o or (not root and len(inline) + indent <= 118):
        return inline
    if isinstance(o, list):
        return "[\n" + ",\n".join(pad + dump(v, indent + 2, False) for v in o) + "\n" + " " * indent + "]"
    return "{\n" + ",\n".join(f"{pad}{json.dumps(k, ensure_ascii=False)}: {dump(v, indent + 2, False)}"
                              for k, v in o.items()) + "\n" + " " * indent + "}"


# -- recording the base vectors from the real code -----------------------------------------------------------------------
# `python -m tests.contracts record` runs the tests that record (their names have "recorded" in them) with
# SIZEUP_CONTRACTS_RECORD=1: each writes what the real code sent, scrubbed, over its base vector. Without it the same
# tests compare the committed base with what the code sends now.

RECORD_ENV = "SIZEUP_CONTRACTS_RECORD"
T0 = 1_794_990_000.0             # every recorded post is moved to this moment
TIME_LIMIT = 100000000000        # a schema's mark of a point in time: Unix seconds below this (never milliseconds)
MACHINE = {"host": "st-north", "cpu_pct": 38.0, "load1": 1.2, "cpus": 4, "mem_pct": 44.0, "mem_total": 17179869184,
           "temp_c": 51.0, "disk_path": "/var/lib/sizeup", "disk_pct": 12.5, "disk_free": 236223201280,
           "disk_total": 269490393088}


def _is_time(branches) -> bool:
    return any(b.get("exclusiveMaximum") == TIME_LIMIT for b, _ in branches)


def _retime(node: dict, resolver, inst, move):
    """inst with every point in time (by its schema) put through move()."""
    branches = _branches(node, resolver, inst)
    if isinstance(inst, dict):
        props: dict[str, tuple[dict, object]] = {}
        extra = None
        for b, r in branches:
            props.update({k: (v, r) for k, v in (b.get("properties") or {}).items()})
            if isinstance(b.get("additionalProperties"), dict):
                extra = (b["additionalProperties"], r)
        return {k: _retime(*(props.get(k) or extra), v, move) if (k in props or extra) else v for k, v in inst.items()}
    if isinstance(inst, list):
        items = next(((b["items"], r) for b, r in branches if isinstance(b.get("items"), dict)), None)
        return [_retime(*items, v, move) if items else v for v in inst]
    if isinstance(inst, (int, float)) and not isinstance(inst, bool) and _is_time(branches):
        return move(inst)
    return inst


def scrub(name: str, body: dict, tmp: Path | str = "") -> dict:
    """A recorded body with everything of this machine, this run and this moment replaced by fixed made-up values, so
    that a vector says nothing about where it was recorded and a new recording differs only where the code does:

    - time: every point in time (a field whose schema is one) is moved so that the post's `at` is T0, to the whole
      second; `health.started` is T0 - 3600 and `health.boot` T0 - 86400; the start time inside a talker's and a
      page's `key` follows;
    - the machine: MACHINE's keys in `status.health`; `load.cpu_pct` of a station's whisper;
    - this run: any `detail` naming the test's temporary folder; the Voice row (it depends on the OS); the counters
      of the metrics link, the speech-to-text queue's `ours_ago_s` and the pool worker's first word; the worker's
      `model` (the test config's);
    - the code's own names: the county system's (sizeup.sourcekeys.COUNTY) becomes `countynet`, its dongle job
      (sizeup.app.P25_MODE) `County P25`."""
    import sizeup.app as appmod
    from sizeup import sourcekeys
    doc = load(name)
    at = body.get("at") if isinstance(body.get("at"), (int, float)) else None
    if at is not None:
        body = _retime(doc, registry().resolver(doc["$id"]), body, lambda t: T0 + round(t - at))

    def words(o):
        if isinstance(o, dict):
            return {words(k): words(v) for k, v in o.items()}
        if isinstance(o, list):
            return [words(v) for v in o]
        if isinstance(o, str):
            o = o.replace(sourcekeys.COUNTY, "countynet").replace(appmod.P25_MODE, "County P25")
            o = re.sub(r"^((?:fm|page):.*?:)\d{9,}\.\d\d(?=:|$)", lambda m: f"{m[1]}{T0:.2f}", o)
            if tmp and (str(tmp) in o or "pytest" in o):
                o = "cannot start rtl_fm: not installed" if "rtl_fm" in o else "trunk-recorder not installed" \
                    if "trunk-recorder" in o else "/var/lib/sizeup"
        return o
    body = words(body)
    st = body.get("status")
    if isinstance(st, dict):
        hl = st["health"]
        hl.update({k: v for k, v in MACHINE.items() if k in hl}, started=T0 - 3600.0, boot=T0 - 86400.0)
        if isinstance(hl.get("stt_queue"), dict):
            hl["stt_queue"]["ours_ago_s"] = 0.0
        st["metrics"].update(pending=0, sent=0, last_ok=None)
        if st["pool_worker"]["state"] == "off":            # (its first word comes a moment after the first post)
            st["pool_worker"]["why"] = "this station's whisper isn't in the pool"
        for row in st["radios"]:
            if row["label"] == "Voice":
                row.update(state="off", detail="no voice on this machine")
    if isinstance(body.get("load"), dict) and "cpu_pct" in body["load"]:
        body["load"]["cpu_pct"] = 27.5
    if name.startswith("stt-worker-") and "model" in body:
        body["model"] = "large-v3-turbo-q5_0" if "version" in body else "small.en-q5_1"
    text = json.dumps(body)
    mine = [str(tmp), "pytest", "/private/", "/Users/", "/home/"]
    if os.environ.get(RECORD_ENV, "") not in ("", "0"):      # (written to the repo: this machine's and this user's name
        import getpass                                      # too; not in every run, where a user may be called "east")
        import socket
        mine += [socket.gethostname(), getpass.getuser()]
    for word in mine:
        assert len(word) < 3 or word not in text, f"the recording still names this machine or run: {word!r}"
    return body


def recorded(rel: str, body) -> object:
    """The base vector contracts/vectors/<rel>. With SIZEUP_CONTRACTS_RECORD=1 it is first written anew from body
    (what the real code just sent, scrubbed)."""
    path = VECTORS / rel
    if os.environ.get(RECORD_ENV, "") not in ("", "0"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump(body) + "\n", encoding="utf-8")
    return vector(path)


def same_shape(name: str, a, b) -> list[str]:
    """Where two bodies of one schema differ in shape: wherever the schema lists an object's keys completely
    (x-complete) both must have the same keys, at every depth; and a list on one side is a list on the other. The
    paths that differ ([] when none), and how many objects were compared, as the last entry's count."""
    doc = load(name)
    out: list[str] = []
    seen = [0]

    def walk(node, resolver, x, y, path):
        if isinstance(x, list) != isinstance(y, list) or isinstance(x, dict) != isinstance(y, dict):
            out.append(f"{path}: {type(x).__name__} / {type(y).__name__}")
            return
        bx = _branches(node, resolver, x)
        if isinstance(x, dict):
            props: dict[str, tuple[dict, object]] = {}
            extra, complete = None, False
            for n, r in bx + _branches(node, resolver, y):
                props.update({k: (v, r) for k, v in (n.get("properties") or {}).items()})
                complete = complete or n.get("x-complete") is True
                if isinstance(n.get("additionalProperties"), dict):
                    extra = (n["additionalProperties"], r)
            if complete and extra is None:
                seen[0] += 1
                if set(x) != set(y):
                    out.append(f"{path}: {sorted(set(x) ^ set(y))}")
            for k in set(x) & set(y):
                sub = props.get(k) or extra
                if sub:
                    walk(*sub, x[k], y[k], f"{path}.{k}")
        elif isinstance(x, list):
            items = next(((n["items"], r) for n, r in bx if isinstance(n.get("items"), dict)), None)
            for i, (u, v) in enumerate(zip(x, y)):
                if items:
                    walk(*items, u, v, f"{path}[{i}]")
    walk(doc, registry().resolver(doc["$id"]), a, b, "$")
    same_shape.compared = seen[0]
    return out


# -- what a schema declares ----------------------------------------------------------------------------------------------

def _branches(node: dict, resolver, inst) -> list[tuple[dict, object]]:
    """node with its $ref followed and its allOf / anyOf / oneOf opened: every (schema, resolver) that applies to
    inst. anyOf / oneOf: only the branches inst is valid against."""
    out, todo = [], [(node, resolver)]
    while todo:
        n, r = todo.pop()
        if not isinstance(n, dict):
            continue
        out.append((n, r))
        if "$ref" in n:
            got = r.lookup(n["$ref"])
            todo.append((got.contents, got.resolver))
        todo += [(b, r) for b in n.get("allOf", [])]
        for key in ("anyOf", "oneOf"):
            for b in n.get(key, []):
                full = b if "$ref" not in b else r.lookup(b["$ref"]).contents
                if Draft202012Validator(full, registry=registry()).is_valid(inst):
                    todo.append((b, r))
    return out


def undeclared(name: str, instance) -> list[str]:
    """The keys of instance the schema does not name, wherever the schema says it lists them all ("x-complete": true):
    their paths. A schema that takes unknown keys (additionalProperties: true) validates whatever is added, so this is
    what proves an as-built schema still describes everything the code sends."""
    doc = load(name)
    return _undeclared(doc, registry().resolver(doc["$id"]), instance, "$")


def _undeclared(node: dict, resolver, inst, path: str) -> list[str]:
    out: list[str] = []
    branches = _branches(node, resolver, inst)
    if isinstance(inst, dict):
        props: dict[str, tuple[dict, object]] = {}
        extra, complete = None, False
        for n, r in branches:
            props.update({k: (v, r) for k, v in (n.get("properties") or {}).items()})
            complete = complete or n.get("x-complete") is True
            if isinstance(n.get("additionalProperties"), dict):
                extra = (n["additionalProperties"], r)
        for k, v in inst.items():
            if k in props:
                out += _undeclared(*props[k], v, f"{path}.{k}")
            elif extra is not None:
                out += _undeclared(*extra, v, f"{path}.{k}")
            elif complete:
                out.append(f"{path}.{k}")
    elif isinstance(inst, list):
        for n, r in branches:
            for i, v in enumerate(inst):
                if i < len(n.get("prefixItems", [])):
                    out += _undeclared(n["prefixItems"][i], r, v, f"{path}[{i}]")
                elif isinstance(n.get("items"), dict):
                    out += _undeclared(n["items"], r, v, f"{path}[{i}]")
    return out


def with_unknown_keys(name: str, instance, key: str = "from_the_future") -> tuple[object, int]:
    """(a copy of instance with a key no schema names added to every object the schema describes field by field, at
    every depth; how many were added). A map of free keys (additionalProperties with a schema: per channel, per job,
    per reason) gets none, since a key there is data, and neither does an object the schema leaves open."""
    doc = load(name)
    out = copy.deepcopy(instance)
    return out, _inject(doc, registry().resolver(doc["$id"]), out, key)


def _inject(node: dict, resolver, inst, key: str) -> int:
    n = 0
    branches = _branches(node, resolver, inst)
    if isinstance(inst, dict):
        props: dict[str, tuple[dict, object]] = {}
        extra = None
        for b, r in branches:
            props.update({k: (v, r) for k, v in (b.get("properties") or {}).items()})
            if isinstance(b.get("additionalProperties"), dict):
                extra = (b["additionalProperties"], r)
        for k, v in list(inst.items()):
            if k in props:
                n += _inject(*props[k], v, key)
            elif extra is not None:
                n += _inject(*extra, v, key)
        if props and extra is None:
            inst[key] = {"x": [1, {"y": None}]}
            n += 1
    elif isinstance(inst, list):
        for b, r in branches:
            for i, v in enumerate(inst):
                if i < len(b.get("prefixItems", [])):
                    n += _inject(b["prefixItems"][i], r, v, key)
                elif isinstance(b.get("items"), dict):
                    n += _inject(b["items"], r, v, key)
    return n


def properties(node, path: str = "$"):
    """Every (path, property schema) a schema document declares, at any depth."""
    if isinstance(node, dict):
        for k, v in (node.get("properties") or {}).items():
            yield f"{path}.{k}", v
        for k, v in node.items():
            if k == "properties":
                for name, sub in v.items():
                    yield from properties(sub, f"{path}.{name}")
            elif isinstance(v, (dict, list)) and k not in ("enum", "const", "default", "examples", "required"):
                yield from properties(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from properties(v, f"{path}[{i}]")


# -- JSON as a strict reader takes it ------------------------------------------------------------------------------------

class NotStrictJson(ValueError):
    pass


def loads_strict(text: str | bytes):
    """JSON as a reader that is not Python takes it: NaN, Infinity and -Infinity are refused (Python's json writes and
    reads them, RFC 8259 has no such tokens), and so is a key given twice in one object. Integers stay exact."""
    def no(token):
        raise NotStrictJson(f"{token} is not JSON")

    def pairs(items):
        out = dict(items)
        if len(out) != len(items):
            raise NotStrictJson("a key given twice in one object")
        return out
    return json.loads(text, parse_constant=no, object_pairs_hook=pairs)


def float_exact(value) -> bool:
    """Would a reader that keeps every JSON number as a 64-bit float still have this integer exactly."""
    return not isinstance(value, int) or isinstance(value, bool) or abs(value) <= MAX_SAFE


# -- the lease id: reference comparison (contracts/schemas/lease.schema.json "gen") --------------------------------------

def counter(value) -> int:
    """A counter off the wire as a number: a JSON integer up to 2^53 - 1, or a string of decimal digits (no sign, no
    leading zero, at most 20). Anything else is refused: never a float, never a bool."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"not a counter: {value!r}")
    if isinstance(value, int):
        if not 0 <= value <= MAX_SAFE:
            raise ValueError(f"a counter outside 0..2^53-1 must be a string: {value!r}")
        return value
    if isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,19}", value):
        return int(value)
    raise ValueError(f"not a counter: {value!r}")


def lease_id(gen) -> tuple[int, int]:
    """A lease's `gen` as the pair it is: (hub leader-lock generation, lease generation)."""
    if not isinstance(gen, list) or len(gen) != 2:
        raise ValueError(f"a lease id is a pair: {gen!r}")
    return counter(gen[0]), counter(gen[1])


def lease_cmp(a, b) -> int:
    """-1, 0 or 1: lease id a is older than, the same as, or newer than b. The pair is compared as a pair, the leader
    generation first, by value (never as strings, never as floats)."""
    x, y = lease_id(a), lease_id(b)
    return (x > y) - (x < y)


# -- the hub's /api/station/ routes, read from its code ------------------------------------------------------------------

HEX16 = "0123456789abcdef"
FORMS = ("path == X, path != X, path in X, path not in X, X.fullmatch(path), X.match(path), path.startswith(X), "
         "X.handle(..., path, ...) behind a path.startswith(), or path inside an f-string (a message)")


def path_uses(src: str, name: str, where: str = "") -> list[tuple[str, ast.expr | None]]:
    """Every use of `path` in the handler `name` of this source: ("eq", X) for path == / != / in / not in X, ("re", X)
    for X.fullmatch(path) / X.match(path), ("prefix", X) for path.startswith(X), ("hand", None) for path handed whole to
    another dispatcher's handle(). ANY other use fails (path sliced, split, copied to another name, a lookup keyed by a
    part of it: a way to route that this cannot read, so a route it would never see): the enumeration fails closed."""
    where = where or name
    tree = ast.parse(textwrap.dedent(src))
    fns = [f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == name]
    assert len(fns) == 1, f"{where}: one handler expected, found {len(fns)}"
    fn = fns[0]
    assert "path" in [a.arg for a in fn.args.args], \
        f"{where} no longer takes `path`: the route enumeration reads nothing from it"
    parent = {child: par for par in ast.walk(fn) for child in ast.iter_child_nodes(par)}
    line = textwrap.dedent(src).splitlines()
    out: list[tuple[str, ast.expr | None]] = []
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Name) and node.id == "path"):
            continue
        par = parent[node]
        up = parent.get(par)
        if isinstance(node.ctx, ast.Load) and isinstance(par, ast.Compare) and par.left is node \
                and all(isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)) for op in par.ops):
            out += [("eq", x) for x in par.comparators]
        elif isinstance(node.ctx, ast.Load) and isinstance(par, ast.Call) and isinstance(par.func, ast.Attribute) \
                and par.func.attr in ("fullmatch", "match") and par.args == [node]:
            out.append(("re", par.func.value))
        elif isinstance(node.ctx, ast.Load) and isinstance(par, ast.Call) and isinstance(par.func, ast.Attribute) \
                and par.func.attr == "handle" and node in par.args:
            out.append(("hand", None))
        elif isinstance(node.ctx, ast.Load) and isinstance(par, ast.Attribute) and par.attr == "startswith" \
                and isinstance(up, ast.Call) and up.func is par and len(up.args) == 1:
            out.append(("prefix", up.args[0]))
        elif isinstance(node.ctx, ast.Load) and isinstance(par, ast.FormattedValue):
            continue
        else:
            raise AssertionError(
                f"{where}, line {node.lineno}: `{line[node.lineno - 1].strip()}` uses `path` in a way the route "
                f"enumeration cannot read, so a route there would never be seen. Add the route to contracts/routes.json "
                f"and teach tests/contracts.py path_uses this form. Recognised: {FORMS}")
    return out


def _value(expr: ast.expr, namespace: dict, where: str):
    """The expression's value among the handler's module's constants (API + "hello", JOB_PATH, ...)."""
    try:
        return eval(compile(ast.Expression(expr), where, "eval"), dict(namespace))   # noqa: S307 - our own source
    except NameError as e:
        raise AssertionError(f"{where} tests `path` against {ast.unparse(expr)}, which is no module constant ({e}): "
                             f"the route enumeration can't read it. Add the route to contracts/routes.json and teach "
                             f"tests/contracts.py this form") from None


def _expand(pattern: str) -> list[str]:
    """A route regex as the concrete paths it takes: a 16-hex group becomes HEX16, a group of literal alternatives
    each of them. Anything else in the pattern fails (a new kind of route: teach this, then list the route)."""
    paths = [""]
    for part in re.split(r"(\([^()]*\))", pattern):
        if part == "([0-9a-f]{16})":
            paths = [p + HEX16 for p in paths]
        elif part.startswith("("):
            alts = part[1:-1].split("|")
            assert all(re.fullmatch(r"[a-z_-]+", a) for a in alts), f"route pattern not understood: {pattern!r}"
            paths = [p + a for p in paths for a in alts]
        else:
            assert re.fullmatch(r"[A-Za-z0-9/_-]*", part), f"route pattern not understood: {pattern!r}"
            paths = [p + part for p in paths]
    return paths


def source_paths(src: str, name: str, namespace: dict, where: str = "") -> tuple[set[str], set[str]]:
    """(the concrete paths a handler tests for, the path prefixes it hands on to another dispatcher), from its source."""
    where = where or name
    paths: set[str] = set()
    prefixes: set[str] = set()
    hands = 0
    for kind, expr in path_uses(src, name, where):
        if kind == "hand":
            hands += 1
            continue
        v = _value(expr, namespace, where)
        if kind == "prefix":
            assert isinstance(v, str), (where, v)
            prefixes.add(v)
        elif kind == "re":
            assert isinstance(v, re.Pattern), (where, v)
            paths.update(_expand(v.pattern))
        else:
            vals = [v] if isinstance(v, str) else list(v)
            assert vals and all(isinstance(x, str) for x in vals), \
                f"{where} tests `path` against {v!r}: not a path or a collection of paths the enumeration can read"
            paths.update(vals)
    assert hands == len(prefixes), f"{where}: {hands} hand-over(s) to another dispatcher for {sorted(prefixes)}"
    return paths, prefixes


def handler_paths(fn) -> tuple[set[str], set[str]]:
    return source_paths(inspect.getsource(fn), fn.__name__, vars(inspect.getmodule(fn)), fn.__qualname__)


def dispatchers() -> dict:
    """The hub's three dispatchers and the prefix each hands on: Hub.handle (sizeup/cloud/hub.py) hands
    /api/station/stt/ to WorkerApi.handle (sizeup/cloud/sttworkers.py), which hands /api/station/stt/infer/ to the LLM
    lane's handle (sizeup/cloud/infer.py)."""
    from sizeup.cloud import infer, sttworkers
    from sizeup.cloud.hub import Hub
    return {Hub.handle: {"/api/station/stt/"}, sttworkers.WorkerApi.handle: {"/api/station/stt/infer/"},
            infer.Lane.handle: set()}


def served_paths() -> set[str]:
    """Every concrete path under /api/station/ the hub's code routes, read from its dispatchers. A prefix handed to
    anything else fails here: a fourth dispatcher has to be added to dispatchers() before its routes can be listed."""
    out: set[str] = set()
    for fn, hands_on in dispatchers().items():
        paths, prefixes = handler_paths(fn)
        assert prefixes == hands_on, (f"{fn.__qualname__} hands on {sorted(prefixes)}, the contract tests know "
                                      f"{sorted(hands_on)}: add the new dispatcher to tests/contracts.py dispatchers")
        out |= paths
    assert all(p.startswith("/api/station/") for p in out), sorted(out)
    return out


def routes() -> dict:
    return json.loads((ROOT / "routes.json").read_text(encoding="utf-8"))


def example(path: str) -> str:
    """A route's path template as a concrete path: every {parameter} becomes 16 hex characters."""
    return re.sub(r"\{[a-z_]+\}", HEX16, path)


# -- a real hub and a real station, with made-up names -------------------------------------------------------------------

STATION, WORKER = "st-north", "gpu-1"
SHARED = "contracts-shared-station-token-0123456789"
DONGLES = [(0, "RTL2838UHIDIR", "00000101"), (1, "Blog V4", "00000102"), (2, "RTL2838UHIDIR", "00000103"),
           (3, "RTL2838UHIDIR", "00000104"), (4, "RTL2838UHIDIR", "00000105")]
TALKGROUPS = {4101: "Fire Dispatch 1", 4102: "Fire Ops 2"}       # the "county" system's, made up
PEOPLE_ROUTES = ("/api/state", "/api/cad/state", "/api/cad/health", "/healthz")    # what people's consoles read


def _cfg_names() -> dict:
    from sizeup.config import ChannelCfg
    from sizeup.radio.tones import ToneCode
    return {"station_id": STATION, "station_name": "North firehouse",
            "channels": (ChannelCfg("Paging 1", 460.1, "paging"), ChannelCfg("Fireground 1", 453.1, "traffic")),
            "tones": (ToneCode("Northfield FD", 716.1, 600.9, "NFD"),), "ours": frozenset({"Northfield FD"})}


class Live:
    """A hub on an ephemeral port (tests/test_cloud.py HubRun) that takes one station, STATION, by its own token (sz1)
    and by the shared token, and one GPU worker, WORKER (sw1). Every state post the hub handles is kept as it came
    and as it was answered: `seen` = [(request bytes, status, answer bytes)]; every request to the workers' API
    likewise: `worker_seen` = [(path, request bytes, status, answer bytes)]. answer_with: keys added to every state
    answer (what a newer hub would send); answer_without: keys left out of it (what an older one would not);
    worker_answer_with: keys added to every JSON answer of the workers' API; state_delay_s: every state post is
    handled this much later (a slow hub: what a loaded machine does to the order of things; for every Live of a run,
    SIZEUP_CONTRACTS_STATE_DELAY_S=0.4 in the environment: how the tests' own races are looked for)."""

    def __init__(self, tmp_path, answer_with: dict | None = None, answer_without: tuple = (),
                 worker_answer_with: dict | None = None, state_delay_s: float | None = None):
        from sizeup.cloud.health import status_token
        from tests.test_cloud import HubRun, hub_cfg
        self.tmp = tmp_path
        if state_delay_s is None:
            state_delay_s = float(os.environ.get("SIZEUP_CONTRACTS_STATE_DELAY_S") or 0.0)
        self.hub = HubRun(hub_cfg(tmp_path, cloud_token=SHARED, cloud_legacy_station_id=STATION, **_cfg_names()))
        h = self.hub.app.hub
        self.own = h.stations.add(STATION, "North firehouse")
        self.worker = h.workers.add(WORKER, "GPU box")
        self.tokens = {"sz1": self.own, "shared": SHARED, "status": status_token(self.own), "sw1": self.worker}
        self.answer_with = answer_with          # (may be set later: the hub's answers from then on)
        self.seen: list[tuple[bytes, int, bytes]] = []
        self.worker_seen: list[tuple[str, bytes, int, bytes]] = []
        self.stations: list = []
        self._apps: list = []
        real = h._state

        def state(raw, p=None):
            if state_delay_s:
                time.sleep(state_delay_s)
            code, body, ctype = real(raw, p)
            if code == 200 and (self.answer_with or answer_without):
                d = {k: v for k, v in json.loads(body).items() if k not in answer_without}
                body = json.dumps(dict(d, **(self.answer_with or {})), separators=(",", ":")).encode()
            self.seen.append((raw, code, body))
            return code, body, ctype
        h._state = state
        api = self.hub.app.stt_api
        real_api = api.handle

        def worker_api(method, path, headers, body):
            got: list[bytes] = []

            def read(limit):
                raw = body(limit)
                got.append(raw or b"")
                return raw
            code, out, ctype = real_api(method, path, headers, read if body is not None else None)
            if code == 200 and worker_answer_with and ctype == "application/json":
                out = json.dumps(dict(json.loads(out), **worker_answer_with), separators=(",", ":")).encode()
            self.worker_seen.append((path, got[0] if got else b"", code, out))
            return code, out, ctype
        api.handle = worker_api

    @property
    def url(self) -> str:
        return self.hub.url

    def call(self, method: str, path: str, body=None, token: str | None = "sz1"):
        """(status, JSON) from the hub over HTTP; token: a key of self.tokens, a literal token, or None for none."""
        from tests.test_cloud import call
        return call(self.url + path, method, body, token=self.tokens.get(token, token) if token else None)

    def people(self) -> dict[str, int]:
        """The status of each route people's consoles and the deploy check read: 200 each while the hub is well."""
        out = {p: self.call("GET", p, token=None)[0] for p in PEOPLE_ROUTES}
        out["/api/station/status"] = self.call("GET", "/api/station/status", token="status")[0]
        return out

    def _station_cfg(self, token: str, **more):
        from tests.test_cloud import base
        return base(**{"clips_dir": str(self.tmp / "st" / "clips"), "cloud_outbox": str(self.tmp / "st" / "outbox.sqlite"),
                       "cloud_role": "station", "cloud_hub_url": self.url, "cloud_token": self.tokens[token],
                       **_cfg_names(), **more})

    def station(self, token: str = "sz1"):
        """A real station (the all-in-one app with its hub link, speech-to-text faked, no receiver started), posting to
        this hub with that token. Two FM channels on a dongle each: no P25, no shared dongles."""
        from tests.test_cloud import station
        st = station(self._station_cfg(token), {})
        st.paging.on_change = st.link.live_changed
        self.stations.append(st)
        return st

    def rich_station(self, monkeypatch, token: str = "sz1"):
        """A real station with everything a status can carry, on the repo's own fakes (tests/test_antenna_groups.py,
        tests/test_p25_systems.py: the USB list is a Python list, rtl_fm and trunk-recorder don't exist, so nothing is
        opened or started): five shared dongles, three of them in an antenna group; a paging and a fireground FM
        channel and an AM airband one; the county P25 system with two talkgroups, one heard; a second P25 system,
        "east"; a reception test that ended. Every name is made up but the county system's job, the code's own."""
        import sizeup.app as appmod
        from sizeup.config import ChannelCfg
        from sizeup.radio import systems
        from sizeup.app import make_app
        from sizeup.radio.recorder import TrunkRecorder
        from tests.test_app import FakeWhisper
        from tests.test_p25_systems import make_system
        plugged = list(DONGLES)
        for name, value in (("SWITCH_PAUSE_S", 0.0), ("SDR_WATCH_S", 3600.0), ("SDR_SETTLE_S", 0.0), ("GROUP_HOLD_S", 0.0)):
            monkeypatch.setattr(appmod, name, value)
        monkeypatch.setattr(appmod.sdr_lookup, "devices", lambda *a, **k: list(plugged))
        monkeypatch.setattr(appmod.sdr_lookup, "resolve",
                            lambda serial, *a: next((i for i, _p, s in plugged if s == serial), None))
        monkeypatch.setattr(TrunkRecorder, "one_dongle", lambda self: (True, ""))
        work = self.tmp / "tr"
        make_system(work, name="east", jobs=("East 1",),
                    tgs="7001,1b59,East Dispatch,T,d,Police,X,1,alert\n7002,1b5a,East Tac,T,d,Police,X,2,record\n")
        system = systems.discover(work, set(TALKGROUPS))[0]
        names = _cfg_names()
        cfg = self._station_cfg(
            token, channels=names["channels"] + (ChannelCfg("Tower", 118.5, "airband", "", 36.4, 3.5),),
            rtl_fm=str(self.tmp / "no-rtl_fm"), sdr_auto=True, sdr_auto_assign=True, sdr_names={"00000101": "Roof 1"},
            p25_dir=str(self.tmp / "captures" / "p25"), p25_talkgroups=dict(TALKGROUPS),
            p25_tg_info={tg: {"name": name, "group": "Fire", "encrypted": False} for tg, name in TALKGROUPS.items()},
            p25_recorder=str(self.tmp / "no-trunk-recorder") + " --config=config.json",
            p25_recorder_dir=str(work), p25_systems=(system,))
        st = make_app(cfg)                 # as tests/test_cloud.py station(), but the link starts once it is set up:
        st.stt.client = FakeWhisper({})    # no post is ever built from a station half set up
        st.stt.start()
        st.live.on_change = st.link.live_changed
        st.paging.on_change = st.link.live_changed
        self.stations.append(st)
        self._apps.append(st)
        county = appmod.P25_MODE
        for job, serial in ((county, "00000101"), ("Paging 1", "00000102"), ("Fireground 1", "00000103"),
                            ("East 1", "00000104"), ("Tower", "00000105")):
            assert st.set_mode(job, serial, confirm=True) is None, job
        assert st.set_antenna_groups([{"name": "roof", "sdrs": ["00000101", "00000102", "00000103"],
                                       "jobs": [county, "Paging 1", "Fireground 1"]}]) is None
        st.tg.set(4101, main=True)
        st.tg.seen(4101)                                           # a call on a main talkgroup: health.heard
        now = time.time()
        st.reception.history.insert(0, {                           # a reception test that ended, as its file keeps it
            "id": "r-0001", "system": "east", "label": "East", "sdr": "00000105", "sdr_name": "00000105", "was": "Tower",
            "plan": ["East 1"], "minutes": 15, "started": now - 1200.0, "ended": now - 300.0, "status": "done", "why": "",
            "note": "", "verdict": {"grade": "good", "hint": ""}, "restored": "Tower restored", "restored_ok": True,
            "halves": [{"job": "East 1", "seconds": 900.0, "verdict": {"grade": "good", "hint": ""},
                        "cc": {"mhz": 769.90625, "rate": 39.5, "decoded_pct": 98.7, "windows": 300, "dropout_s": 0.0,
                               "retunes": 0}, "talkgroups": [], "voice": {"calls": 4, "err_per_s": 0.2}}]})
        st.link.start()
        return st

    def posts(self) -> list[dict]:
        return [json.loads(raw) for raw, code, _ in self.seen if code == 200]

    def close(self) -> None:
        for st in self.stations:
            st.link.stop()
        self.hub.close()
        for app in self._apps:
            app.stop()


if __name__ == "__main__":
    import subprocess
    import sys
    if sys.argv[1:] != ["record"]:
        sys.exit("usage: python -m tests.contracts record    (write the base vectors anew from what the code sends)")
    repo = ROOT.parent
    sys.exit(subprocess.call(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-k", "recorded",
         "tests/test_contracts_station_state.py", "tests/test_contracts_workers.py"],
        cwd=repo, env={**os.environ, RECORD_ENV: "1", "PYTHONPATH": os.pathsep.join(
            [str(repo), *filter(None, [os.environ.get("PYTHONPATH")])])}))
