"""The contract tests' helper (contracts/, ADR 0004): the JSON Schemas of contracts/schemas/ loaded with their
cross-file $refs, the vectors of contracts/vectors/, a strict JSON reader, the reference comparison of lease ids, and
the /api/station/ routes read from the hub's own code. Dev-only (jsonschema, as /api/v1's contract tests:
tests/openapi_spec.py). Nothing here is imported by sizeup/."""
from __future__ import annotations

import ast
import copy
import inspect
import json
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
PEOPLE_ROUTES = ("/api/state", "/api/cad/state", "/healthz")     # what people's consoles read (no token in the tests)


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
    worker_answer_with: keys added to every JSON answer of the workers' API."""

    def __init__(self, tmp_path, answer_with: dict | None = None, answer_without: tuple = (),
                 worker_answer_with: dict | None = None):
        from sizeup.cloud.health import status_token
        from tests.test_cloud import HubRun, hub_cfg
        self.tmp = tmp_path
        self.hub = HubRun(hub_cfg(tmp_path, cloud_token=SHARED, cloud_legacy_station_id=STATION, **_cfg_names()))
        h = self.hub.app.hub
        self.own = h.stations.add(STATION, "North firehouse")
        self.worker = h.workers.add(WORKER, "GPU box")
        self.tokens = {"sz1": self.own, "shared": SHARED, "status": status_token(self.own), "sw1": self.worker}
        self.seen: list[tuple[bytes, int, bytes]] = []
        self.worker_seen: list[tuple[str, bytes, int, bytes]] = []
        self.stations: list = []
        self._apps: list = []
        real = h._state

        def state(raw, p=None):
            code, body, ctype = real(raw, p)
            if code == 200 and (answer_with or answer_without):
                d = {k: v for k, v in json.loads(body).items() if k not in answer_without}
                body = json.dumps(dict(d, **(answer_with or {})), separators=(",", ":")).encode()
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
        from sizeup.radio.recorder import TrunkRecorder
        from tests.test_cloud import station
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
        st = station(cfg, {})
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
        return st

    def posts(self) -> list[dict]:
        return [json.loads(raw) for raw, code, _ in self.seen if code == 200]

    def close(self) -> None:
        for st in self.stations:
            st.link.stop()
        self.hub.close()
        for app in self._apps:
            app.stop()
