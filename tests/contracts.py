"""The contract tests' helper (contracts/, ADR 0004): the JSON Schemas of contracts/schemas/ loaded with their
cross-file $refs, the vectors of contracts/vectors/, a strict JSON reader, the reference comparison of lease ids, and
the /api/station/ routes read from the hub's own code. Dev-only (jsonschema, as /api/v1's contract tests:
tests/openapi_spec.py). Nothing here is imported by sizeup/."""
from __future__ import annotations

import ast
import inspect
import json
import re
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


def vectors(name: str, kind: str) -> list[tuple[str, object]]:
    """[(file name, parsed JSON)] of contracts/vectors/<name>/<kind>/ (kind: valid | invalid). NOT_JSON for a file
    that is not JSON as a strict reader takes it (an invalid vector may be one: `NaN`)."""
    out = []
    for p in sorted((VECTORS / name / kind).glob("*.json")):
        try:
            out.append((p.name, loads_strict(p.read_text(encoding="utf-8"))))
        except ValueError:
            out.append((p.name, NOT_JSON))
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


def _path_tests(fn) -> list[tuple[str, ast.expr]]:
    """Every expression a handler tests its `path` against: ("eq", expr) for path == / != / in / not in expr,
    ("re", expr) for expr.fullmatch(path) / expr.match(path), ("prefix", expr) for path.startswith(expr)."""
    src = inspect.getsource(fn)
    tree = ast.parse("if 1:\n" + src if src[:1] in " \t" else src)
    args = [a.arg for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == fn.__name__
            for a in f.args.args]
    assert "path" in args, f"{fn.__qualname__} no longer takes `path`: the route enumeration reads nothing from it"
    out: list[tuple[str, ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "path":
            out += [("eq", c) for c in node.comparators]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            f = node.func
            is_path = lambda x: isinstance(x, ast.Name) and x.id == "path"   # noqa: E731
            if f.attr in ("fullmatch", "match") and len(node.args) == 1 and is_path(node.args[0]):
                out.append(("re", f.value))
            elif f.attr == "startswith" and is_path(f.value) and len(node.args) == 1:
                out.append(("prefix", node.args[0]))
    return out


def _value(expr: ast.expr, fn):
    """The expression's value in the handler's own module (its constants: API + "hello", JOB_PATH, ...)."""
    try:
        return eval(compile(ast.Expression(expr), inspect.getsourcefile(fn) or "?", "eval"),   # noqa: S307 - our own source
                    dict(vars(inspect.getmodule(fn))))
    except NameError as e:
        raise AssertionError(f"{fn.__qualname__} tests `path` against {ast.unparse(expr)}, which is no module "
                             f"constant ({e}): the route enumeration can't read it") from None


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


def handler_paths(fn) -> tuple[set[str], set[str]]:
    """(the concrete /api/station/ paths a handler tests for, the path prefixes it tests for), from its source."""
    paths: set[str] = set()
    prefixes: set[str] = set()
    for kind, expr in _path_tests(fn):
        v = _value(expr, fn)
        if kind == "prefix":
            assert isinstance(v, str), (fn.__qualname__, v)
            prefixes.add(v)
        elif kind == "re":
            assert isinstance(v, re.Pattern), (fn.__qualname__, v)
            paths.update(_expand(v.pattern))
        else:
            vals = [v] if isinstance(v, str) else list(v)
            assert all(isinstance(x, str) for x in vals), (fn.__qualname__, v)
            paths.update(vals)
    return paths, prefixes


def served_paths() -> set[str]:
    """Every concrete path under /api/station/ the hub's code routes, read from its three dispatchers: Hub.handle
    (sizeup/cloud/hub.py), which hands /api/station/stt/ to WorkerApi.handle (sizeup/cloud/sttworkers.py), which hands
    /api/station/stt/infer/ to the LLM lane's handle (sizeup/cloud/infer.py). A prefix handed to anything else fails
    here: a fourth dispatcher has to be added to this list before its routes can be listed."""
    from sizeup.cloud import infer, sttworkers
    from sizeup.cloud.hub import Hub
    handlers = {Hub.handle: {"/api/station/stt/"}, sttworkers.WorkerApi.handle: {"/api/station/stt/infer/"},
                infer.Lane.handle: set()}
    out: set[str] = set()
    for fn, hands_on in handlers.items():
        paths, prefixes = handler_paths(fn)
        assert prefixes == hands_on, (f"{fn.__qualname__} hands on {sorted(prefixes)}, the contract tests know "
                                      f"{sorted(hands_on)}: add the new dispatcher to tests/contracts.py served_paths")
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


def _cfg_names() -> dict:
    from sizeup.config import ChannelCfg
    from sizeup.radio.tones import ToneCode
    return {"station_id": STATION, "station_name": "North firehouse",
            "channels": (ChannelCfg("Paging 1", 460.1, "paging"), ChannelCfg("Fireground 1", 453.1, "traffic")),
            "tones": (ToneCode("Northfield FD", 716.1, 600.9, "NFD"),), "ours": frozenset({"Northfield FD"})}


class Live:
    """A hub on an ephemeral port (tests/test_cloud.py HubRun) that takes one station, STATION, by its own token (sz1)
    and by the shared token, and one GPU worker, WORKER (sw1). Every state post the hub handles is kept as it came
    and as it was answered: `seen` = [(request bytes, status, answer bytes)]. answer_with: keys added to every state
    answer (what a newer hub would send); answer_without: keys left out of it (what an older one would not)."""

    def __init__(self, tmp_path, answer_with: dict | None = None, answer_without: tuple = ()):
        from sizeup.cloud.health import status_token
        from tests.test_cloud import HubRun, hub_cfg
        self.tmp = tmp_path
        self.hub = HubRun(hub_cfg(tmp_path, cloud_token=SHARED, cloud_legacy_station_id=STATION, **_cfg_names()))
        h = self.hub.app.hub
        self.own = h.stations.add(STATION, "North firehouse")
        self.worker = h.workers.add(WORKER, "GPU box")
        self.tokens = {"sz1": self.own, "shared": SHARED, "status": status_token(self.own), "sw1": self.worker}
        self.seen: list[tuple[bytes, int, bytes]] = []
        self.stations: list = []
        real = h._state

        def state(raw, p=None):
            code, body, ctype = real(raw, p)
            if code == 200 and (answer_with or answer_without):
                d = {k: v for k, v in json.loads(body).items() if k not in answer_without}
                body = json.dumps(dict(d, **(answer_with or {})), separators=(",", ":")).encode()
            self.seen.append((raw, code, body))
            return code, body, ctype
        h._state = state

    @property
    def url(self) -> str:
        return self.hub.url

    def call(self, method: str, path: str, body=None, token: str | None = "sz1"):
        """(status, JSON) from the hub over HTTP; token: a key of self.tokens, a literal token, or None for none."""
        from tests.test_cloud import call
        return call(self.url + path, method, body, token=self.tokens.get(token, token) if token else None)

    def station(self, token: str = "sz1"):
        """A real station (the all-in-one app with its hub link, speech-to-text faked, no receiver started), posting to
        this hub with that token."""
        from tests.test_cloud import base, station
        st = station(base(clips_dir=str(self.tmp / "st" / "clips"), cloud_outbox=str(self.tmp / "st" / "outbox.sqlite"),
                          cloud_role="station", cloud_hub_url=self.url, cloud_token=self.tokens[token], **_cfg_names()), {})
        st.paging.on_change = st.link.live_changed
        self.stations.append(st)
        return st

    def posts(self) -> list[dict]:
        return [json.loads(raw) for raw, code, _ in self.seen if code == 200]

    def close(self) -> None:
        for st in self.stations:
            st.link.stop()
        self.hub.close()
