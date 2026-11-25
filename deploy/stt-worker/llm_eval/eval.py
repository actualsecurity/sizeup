#!/usr/bin/env python3
"""Evaluate a local llama-server on the pd_classify task. Standard library only.

    python eval.py --url http://127.0.0.1:18431 --cases cases.json --runs 3 --model-label qwen3-8b-q4km

Sends every case with the system prompt and the pd_classify v1 schema (response_format json_schema,
temperature 0), validates the reply, scores it against the case's expectations, and writes
results-<label>.json next to this file. Use --extra '{"chat_template_kwargs":{"enable_thinking":false}}'
to pass extra request fields (Qwen3 non-thinking mode).
"""
import argparse
import json
import re
import statistics
import sys
import time
import urllib.request
from pathlib import Path

CATEGORIES = ["none", "weapons", "assault", "dispute", "edp", "barricade", "fire_report", "fire_call",
              "mva_injury", "aided", "hazmat", "water_rescue", "pursuit", "alarm", "theft", "suspicious",
              "other_incident"]
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["category", "confidence", "in_progress", "weapon", "deescalation", "address", "reason"],
    "properties": {
        "category": {"enum": CATEGORIES},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "in_progress": {"type": "boolean"},
        "weapon": {"enum": ["none", "knife", "gun", "other", "unknown"]},
        "deescalation": {"type": "boolean"},
        "address": {"type": ["string", "null"], "maxLength": 80},
        "reason": {"type": "string", "maxLength": 120},
    },
}
SYSTEM_SHORT = ("You classify one police radio transmission for a fire department's situational-awareness feed. "
                "Use only what is said. Return JSON matching the schema. category none when routine or unclear.")
# Padded to roughly the real prompt's ~350 tokens: one line per category plus field rules.
SYSTEM_PADDED = SYSTEM_SHORT + """

Categories:
- none: routine traffic (10-4, meals, fuel, status, paperwork, plate checks, traffic stops) or unclear.
- weapons: a knife, gun or other weapon is involved, shown or threatened.
- assault: someone was attacked or hurt by another person, no weapon mentioned.
- dispute: an argument or domestic or neighbor dispute without injury or weapon.
- edp: emotionally disturbed, suicidal or mental health crisis.
- barricade: a person barricaded or refusing to come out, or holding a hostage.
- fire_report: police see or are told about smoke or fire.
- fire_call: police assisting at a fire department call already under way.
- mva_injury: motor vehicle crash with injury or entrapment.
- aided: medical emergency (fall, unconscious, overdose, breathing problem, welfare check).
- hazmat: gas odor or leak, fuel or chemical spill, carbon monoxide.
- water_rescue: a person in the water or through the ice, boat in distress.
- pursuit: vehicle or foot pursuit.
- alarm: burglar, fire or carbon monoxide alarm activation.
- theft: theft, burglary, robbery after the fact, stolen vehicle.
- suspicious: suspicious person, vehicle or package.
- other_incident: any other incident (tree down, road hazard, lost person, animal).

Fields:
- in_progress: true if it is happening now or the threat is still present; false for routine traffic or past events.
- weapon: the weapon mentioned, none if no weapon.
- deescalation: true if the transmission says things calmed down, a weapon was secured or units can slow down.
- address: the street address or intersection said in the Trigger, with apartment if given; null if only a street
  name or landmark. Fix obvious speech-to-text number errors ("for tea" = forty).
- confidence: 0 to 1. reason: a few words."""
SYSTEMS = {"short": SYSTEM_SHORT, "padded": SYSTEM_PADDED}


def validate(schema, value, path="$"):
    """Small JSON-schema validator for the keywords pd_classify uses."""
    errs = []
    types = schema.get("type")
    if types is not None:
        types = types if isinstance(types, list) else [types]
        checks = {"string": lambda v: isinstance(v, str), "null": lambda v: v is None,
                  "boolean": lambda v: isinstance(v, bool), "object": lambda v: isinstance(v, dict),
                  "array": lambda v: isinstance(v, list),
                  "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
                  "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)}
        if not any(checks[t](value) for t in types):
            return [f"{path}: type {type(value).__name__} not in {types}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in enum")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: {value} > maximum {schema['maximum']}")
    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        errs.append(f"{path}: length {len(value)} > maxLength {schema['maxLength']}")
    if isinstance(value, dict):
        errs += [f"{path}: missing {k}" for k in schema.get("required", []) if k not in value]
        props = schema.get("properties", {})
        for k, v in value.items():
            if k in props:
                errs += validate(props[k], v, f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}: extra property {k}")
    return errs


ABBREV = {"road": "rd", "avenue": "ave", "street": "st", "lane": "ln", "drive": "dr", "court": "ct",
          "apartment": "apt", "&": "and", "at": "and"}


def norm_addr(a):
    words = re.sub(r"[^a-z0-9& ]+", " ", a.lower()).split()
    return " ".join(ABBREV.get(w, w) for w in words)


NULLISH = {"", "none", "null", "n/a", "na", "unknown"}


def nullish_str(v):
    return isinstance(v, str) and v.strip().lower() in NULLISH


def addr_scores(got, want):
    """(null-agreement, exact, loose). Strings like "none" count as null (the worker should map them).
    Loose compares the part before the first comma, either order of an intersection."""
    if nullish_str(got):
        got = None
    if want is None or not isinstance(got, str) or not got.strip():
        same = (want is None) == (got is None or (isinstance(got, str) and not got.strip()))
        return same, same, same
    exact = norm_addr(got) == norm_addr(want)
    g, w = norm_addr(got.split(",")[0]), norm_addr(want.split(",")[0])
    loose = g == w or g.startswith(w + " ") or " and ".join(reversed(g.split(" and "))) == w
    return True, exact, exact or loose


def post(url, body, timeout=120):
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    return resp, time.perf_counter() - t0


def run_case(args, system, case, extra):
    body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": case["user"]}],
            "temperature": 0, "max_tokens": 256,
            "response_format": {"type": "json_schema", "json_schema": {"name": "pd_classify", "schema": SCHEMA}},
            **extra}
    resp, wall = post(args.url, body)
    msg = resp["choices"][0]["message"]
    content = msg.get("content") or ""
    out = {"id": case["id"], "wall_s": round(wall, 3), "content": content,
           "finish_reason": resp["choices"][0].get("finish_reason"),
           "reasoning": bool(msg.get("reasoning_content")), "think_leak": "<think" in content,
           "usage": resp.get("usage", {}), "timings": resp.get("timings", {})}
    try:
        obj = json.loads(content)
        out["parsed"], out["errors"] = obj, validate(SCHEMA, obj)
    except ValueError as e:
        out["parsed"], out["errors"] = None, [f"json: {e}"]
    return out


def score(case, out):
    exp, got = case["expect"], out["parsed"]
    if not isinstance(got, dict):
        return {k: False for k in ("category", "category_lenient", "in_progress", "weapon", "deescalation",
                                   "addr_null", "addr_exact", "addr_loose")}
    s = {k: got.get(k) == exp[k] for k in ("category", "in_progress", "weapon", "deescalation")}
    s["category_lenient"] = s["category"] or got.get("category") in case.get("accept_category", [])
    s["addr_null"], s["addr_exact"], s["addr_loose"] = addr_scores(got.get("address"), exp["address"])
    return s


def pct(xs):
    xs = list(xs)
    return round(100.0 * sum(map(bool, xs)) / len(xs), 1) if xs else 0.0


def quantile(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))] if xs else None


def field(out, key):
    return out["parsed"].get(key) if isinstance(out["parsed"], dict) else None


def evaluate(args, variant, cases, extra):
    system = SYSTEMS[variant]
    run_case(args, system, cases[0], extra)  # warm-up (also primes the system-prompt cache)
    outs = []
    for run in range(args.runs):
        for c in cases:
            o = run_case(args, system, c, extra)
            o["run"], o["score"] = run, score(c, o)
            outs.append(o)
    first = [o for o in outs if o["run"] == 0]
    tim = [o["timings"] for o in outs if o["timings"]]
    summary = {
        "n_calls": len(outs),
        "json_parse_pct": pct(o["parsed"] is not None for o in outs),
        "schema_valid_pct": pct(o["parsed"] is not None and not o["errors"] for o in outs),
        "think_leak_pct": pct(o["think_leak"] for o in outs),
        "addr_nullish_string_pct": pct(nullish_str(field(o, "address")) for o in outs),
        "confidence_out_of_range_pct": pct(isinstance(field(o, "confidence"), (int, float))
                                           and not 0 <= field(o, "confidence") <= 1 for o in outs),
        "reasoning_content_pct": pct(o["reasoning"] for o in outs),
        **{f"{k}_acc": pct(o["score"][k] for o in first) for k in
           ("category", "category_lenient", "in_progress", "weapon", "deescalation", "addr_null", "addr_exact",
            "addr_loose")},
        "runs_identical_pct": pct(len({o["content"] for o in outs if o["id"] == c["id"]}) == 1 for c in cases),
        "latency_p50_s": round(quantile([o["wall_s"] for o in outs], 0.5), 3),
        "latency_p95_s": round(quantile([o["wall_s"] for o in outs], 0.95), 3),
        "prompt_tokens_mean": round(statistics.mean(o["usage"].get("prompt_tokens", 0) for o in outs), 1),
        "completion_tokens_mean": round(statistics.mean(o["usage"].get("completion_tokens", 0) for o in outs), 1),
        "prompt_tok_s_mean": round(statistics.mean(t.get("prompt_per_second", 0) for t in tim), 1) if tim else None,
        "gen_tok_s_mean": round(statistics.mean(t.get("predicted_per_second", 0) for t in tim), 1) if tim else None,
        "prompt_n_mean": round(statistics.mean(t.get("prompt_n", 0) for t in tim), 1) if tim else None,
    }
    failures = []
    for o, c in zip(first, cases):
        bad = [k for k in ("category_lenient", "in_progress", "weapon", "deescalation", "addr_loose")
               if not o["score"][k]]
        if bad or o["errors"]:
            failures.append({"id": o["id"], "fields": bad, "errors": o["errors"], "got": o["parsed"] or o["content"],
                             "expect": c["expect"]})
    return {"summary": summary, "failures": failures, "outputs": outs}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", required=True)
    ap.add_argument("--cases", default=str(Path(__file__).with_name("cases.json")))
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--model-label", required=True)
    ap.add_argument("--variants", default="short,padded", help="comma list of: short, padded")
    ap.add_argument("--extra", default="{}", help="JSON object merged into every request body")
    args = ap.parse_args()
    cases = json.loads(Path(args.cases).read_text())
    extra = json.loads(args.extra)
    results = {"model_label": args.model_label, "extra": extra, "runs": args.runs, "variants": {}}
    for v in args.variants.split(","):
        r = evaluate(args, v, cases, extra)
        results["variants"][v] = r
        print(f"\n== {args.model_label} [{v}]")
        for k, val in r["summary"].items():
            print(f"  {k:24s} {val}")
        for f in r["failures"]:
            got = f["got"]
            got = {k: got.get(k) for k in ("category", "in_progress", "weapon", "deescalation", "address")} \
                if isinstance(got, dict) else got
            print(f"  FAIL {f['id']}: {','.join(f['fields'] + f['errors'])} got={json.dumps(got)}")
    out = Path(__file__).with_name(f"results-{re.sub(r'[^A-Za-z0-9._-]', '_', args.model_label)}.json")
    out.write_text(json.dumps(results, indent=1))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    sys.exit(main())
