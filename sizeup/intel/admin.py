"""Intel in the CAD: Settings > System > Intel (admins change it, anyone signed in reads it), the events API (the map
layer's, PR 5), the console card's state, Dismiss and feedback.

Settings sections (audited as "system", exported like any setting):

    intel         {enabled, push (the kill switch), push_pii (no PII scrub on intel pushes), per_hour, per_day,
                   per_category_hour, repush_min, max_repush, quiet_from, quiet_to, digest_min}
                   (sizeup/intel/policy.py DEFAULTS)
    intel_packs   pack -> {mode, threshold, window_s, categories: {category: {mode, priority, ttl_min}}}
    intel_rules   rule id -> {mode, w, enabled}; "local.<name>": an admin's own rule {pack, category, any, not?, w, mode}

POST /api/cad/settings/intel {action, ...}:
    set       {key: a policy key, value}                     the intel section
    pack      {pack, mode?, threshold?, window_s?, area?: [town, ...] ([]: the pack file's own)}
    category  {pack, category, mode?, priority?, ttl_min?}
    rule      {rule, mode?, w?, enabled?}
    local     {rule: "local.x", pack, category, any: [...], not?: [...], w?, mode?}   (new rules start in shadow)
    reset     {what: set | pack | category | rule, key?, pack?, category?, rule?}
Every change compiles the packs with it first (a bad phrase, an unknown category: 400 with the reason).

Who sees intel: everyone signed in (user, 2026-11-12), never anyone else: sourcekeys.may_see_context() (the
verified sign-in gate, the same as context traffic), on the API and in the console's state.
"""

from __future__ import annotations

import copy
import re

from sizeup import sourcekeys
from sizeup.cad.store import CadError
from sizeup.intel import policy as policy_mod
from sizeup.intel import rules as rules_mod
from sizeup.intel.events import VERDICTS
from sizeup.intel.model import MODES
from sizeup.intel.push import category_label

SECTIONS = ("intel", "intel_packs", "intel_rules")
LIST_S = 24 * 3600
EVENTS_MAX = 500


def _f(v, lo: float, hi: float, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= float(v) <= hi:
        raise CadError(f"{what}: a number from {lo:g} to {hi:g}")
    return float(v)


def _mode(v, what: str) -> str:
    if v not in MODES:
        raise CadError(f"{what}: {' | '.join(MODES)}")
    return v


def clean_value(key: str, v, section: dict):
    """One key of the "intel" section as Settings > System > Intel takes it (a policy key, policy.py DEFAULTS, or the
    model's, infer.py DEFAULTS): the value to store, or CadError "<key>: why". section: the rest of the section (the
    model's price is checked against its infer_usd_in / _out). The settings import checks a file's section with this
    too (sizeup/cad/settings.py _check_import): policy.settings() would otherwise read a bad value as the default
    without a word (an imported "push": "false" left the kill switch on)."""
    if key in policy_mod.DEFAULTS:
        if key in policy_mod.BOOLS:
            if not isinstance(v, bool):
                raise CadError(f"{key}: true or false")
        elif key in ("quiet_from", "quiet_to"):
            if not isinstance(v, str) or (v and policy_mod._hm(v) is None):
                raise CadError(f"{key}: HH:MM, or empty")
        else:
            lo, hi = policy_mod.LIMITS[key]
            v = int(_f(v, lo, hi, key))
        return v
    from sizeup.intel import infer as infer_mod
    if key not in infer_mod.DEFAULTS:
        raise CadError(f"{key}: not an intel setting")
    if key in infer_mod.BOOLS:
        if not isinstance(v, bool):
            raise CadError(f"{key}: true or false")
    elif key == "infer_provider":
        if v not in infer_mod.PROVIDERS:
            raise CadError(f"infer_provider: {' | '.join(infer_mod.PROVIDERS)}")
    elif key in ("infer_mode", "infer_alone_mode", "infer_pd_mode"):
        v = _mode(v, key)
    elif key == "infer_pd_pack":
        if not isinstance(v, str) or not re.fullmatch(r"[a-z0-9_]{1,40}", v):
            raise CadError(f"{key}: a pack's name")
    elif key in ("infer_model", "infer_region"):
        if not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{2,120}", v):
            raise CadError(f"{key}: letters, digits and . _ : / - (a Bedrock model id or region)")
        cur = {**infer_mod.DEFAULTS, **(section or {})}
        if key == "infer_model" and infer_mod.price({**cur, "infer_model": v}) is None:
            raise CadError(f"infer_model: no known price for {v}: set infer_usd_in and infer_usd_out first "
                           f"(known: {', '.join(sorted(infer_mod.PRICES))})")
    else:
        lo, hi = infer_mod.LIMITS[key]
        v = _f(v, lo, hi, key)
    return v


def _model_status(eng) -> dict:
    """The local model behind the police rules (sizeup/intel/backing.py): its status, never a prompt or an answer."""
    b = getattr(eng, "backing", None)
    if b is None:
        return {"on": False, "ready": False, "backend": "none", "pc": "not here"}
    try:
        return b.status()
    except Exception as e:  # noqa: BLE001 - never keeps the card from showing
        return {"on": False, "ready": False, "backend": "none", "pc": type(e).__name__}


class IntelAdmin:
    def __init__(self, cad, engine):
        self.cad, self.engine = cad, engine
        self._state = None

    # -- reading ----------------------------------------------------------------------------------------------------

    def _ov(self) -> dict:
        s = self.cad.settings
        return {k: s.section(k) for k in SECTIONS}

    def page(self, gate: bool = True) -> dict:
        """GET /api/cad/intel: Settings > System > Intel (gate: the sign-in check; a settings write is an admin's)."""
        if gate and not sourcekeys.may_see_context(self.cad.comms.context_broadcast):
            raise CadError("intel needs a sign-in")
        ov = self._ov()
        eng = self.engine
        now = self.cad.clock()
        stats = eng.events.stats(now - 7 * 86400) if hasattr(eng.events, "stats") else {}
        po, ro = ov["intel_packs"], ov["intel_rules"]
        packs = []
        for p in eng.packs:
            mine = po.get(p.name) or {}
            cats = [{"name": c, "label": d["label"], "mode": d["mode"], "priority": d["priority"], "ttl_min": d["ttl_min"],
                     "edited": c in (mine.get("categories") or {}), "safety": d["safety"]} for c, d in p.categories.items()]
            rules = [{"id": r.id, "v": r.v, "category": r.category, "w": r.w, "mode": r.mode, "enabled": r.enabled,
                      "any": [x.src for x in r.any], "not": [x.src for x in r.nots], "edited": r.id in ro,
                      "local": r.id.startswith("local."), **stats.get(r.id, {"hits": 0, "events": 0})} for r in p.rules]
            packs.append({"name": p.name, "label": p.label, "mode": p.mode, "threshold": p.threshold, "window_s": p.window_s,
                          "accept": p.accept, "scope": p.scope, "area": list(p.area), "categories": cats, "rules": rules,
                          "edited": {k: k in mine for k in ("mode", "threshold", "window_s", "area")}})
        counts = eng.events.counts(now - LIST_S) if hasattr(eng.events, "counts") else {}
        try:        # what the engine did with what it was handed, a line per pack (sizeup/intel/counters.py)
            counters = eng.counts(now)
        except Exception:  # noqa: BLE001 - the counters never keep the card from showing
            counters = None
        return {"settings": eng.policy.cfg, "defaults": policy_mod.DEFAULTS, "edited": sorted(ov["intel"]),
                "packs": packs, "modes": list(MODES), "ruleset_hash": eng.ruleset_hash(), "counts_24h": counts,
                "counters": counters,
                "verdicts": list(VERDICTS), "queue": {"dropped": eng.dropped, "errors": eng.errors},
                "listen": eng.listen.status() if eng.listen is not None else {"configured": False, "why": "not here"},
                "infer": eng.infer.status() if eng.infer is not None else {"on": False, "ready": False, "why": "not here"},
                "model": _model_status(eng),
                "push_topic": self._push_topic(), "may_edit": True}

    def _push_topic(self) -> dict | None:
        """Whether intel pushes have a topic in effect (sizeup/notify.py: their own only), never its name: {set, source}.
        None: no pushes here (no Pushes, or Settings unreadable)."""
        p = getattr(self.cad, "pushes", None)
        if p is None:
            return None
        try:
            from sizeup.notify import INTEL
            e = p.effective(INTEL)
        except Exception:  # noqa: BLE001 - never keeps the card from showing
            return None
        return {"set": bool(e["topic"]), "source": e["source"]}

    def events(self, q: dict, admin: bool = False) -> dict:
        """GET /api/cad/intel/events?since=&bbox=w,s,e,n&mode=map,push&status=open|all&limit=: the map layer's. The
        shadow and log events (rules being tuned) only for an admin."""
        if not sourcekeys.may_see_context(self.cad.comms.context_broadcast):
            raise CadError("intel needs a sign-in")
        now = self.cad.clock()

        def one(k: str, d: str = "") -> str:
            return str((q.get(k) or [d])[0])
        try:
            since = float(one("since", "0") or 0)
        except ValueError:
            raise CadError("since: unix seconds") from None
        allowed = MODES if admin else ("map", "push")
        modes = [m for m in one("mode", "map,push").split(",") if m in allowed] or ["map", "push"]
        status = one("status", "open")
        try:
            limit = max(1, min(EVENTS_MAX, int(one("limit", "200"))))
        except ValueError:
            limit = 200
        where, args = ["last_at >= ?", f"mode IN ({','.join('?' * len(modes))})"], [max(since, now - 7 * 86400), *modes]
        if status == "open":
            where.append("status = 'open'")
        bbox = one("bbox")
        if bbox:
            try:
                w, s, e, n = (float(x) for x in bbox.split(","))
            except ValueError:
                raise CadError("bbox: w,s,e,n") from None
            where.append("lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?")
            args += [s, n, w, e]
        rows = self.engine.events.rows(" AND ".join(where), args, limit) if hasattr(self.engine.events, "rows") else []
        return {"events": [self.shown(r) for r in rows], "now": now, "ruleset_hash": self.engine.ruleset_hash()}

    def shown(self, e: dict) -> dict:
        """An event as the console and the map read it (no transcript text: the rows play from their ids)."""
        p = self.engine.pack(e["pack"])
        cat = p.category(e["category"]) if p else {}
        label = category_label(cat, p.label if p else "", e["category"])     # beside its pack's label: never twice
        return {"id": e["id"], "domain": e["domain"], "pack": e["pack"], "pack_label": p.label if p else e["pack"],
                "category": e["category"], "label": label, "tag": cat.get("tag") or "",
                "safety": bool(cat.get("safety")), "subject": e["subject"], "source_key": e["source_key"],
                "first_at": e["first_at"], "last_at": e["last_at"], "until": e["until"], "status": e["status"],
                "what": e["what_score"], "level": e["level"], "where": e["where_score"], "precision": e["precision"],
                "confirmed": bool(e["confirmed"]), "lat": e["lat"], "lon": e["lon"], "radius_m": e["radius_m"],
                "place": e["label_full"], "town": e["town"], "alts": e["alts"], "basis": e["basis"], "mode": e["mode"],
                "priority": e["priority"], "pushed_at": e["pushed_at"], "suppressed": e["suppressed"],
                "delivery": e.get("delivery"),        # the last push at ntfy: {ok, at, status, id | error} or None
                "traffic_ids": e["traffic_ids"][-12:], "rules": e["rules"], "accept": bool(p.accept) if p else False,
                "dismissed_by": e.get("dismissed_by", ""), "note": e.get("note", ""),
                "near_incident": e.get("near_incident"),
                "fd": e.get("fd") or "", "fd_shown": self._fd_shown(e),
                # the model's category when it is the model's alone (not pushed): "model: MVA with injuries?" on the card
                "suggested": label if any(str(r[0]).startswith("infer.") for r in e["rules"] or [])
                and e["mode"] != "push" else ""}

    def _fd_shown(self, e: dict) -> str:
        """"FD on scene · #26-000123" for an event the FD was already at (engine.py _fire, fd_shown: the number as the
        lookup last gave it, no read here); "" for none."""
        return self.engine.fd_shown(e.get("fd") or "", e.get("near_incident")) if e.get("fd") else ""

    def state(self, now: float | None = None) -> dict | None:
        """The console's card: open events at map or push, newest first; None for a viewer who may not see them."""
        if not sourcekeys.may_see_context(self.cad.comms.context_broadcast) or not hasattr(self.engine.events, "rows"):
            return None
        now = self.cad.clock() if now is None else now
        # read again only when intel wrote something (its own rev, never store.rev) or a minute passed (until)
        key = (getattr(self.engine.events, "rev", 0), int(now // 60))
        if self._state is None or self._state[0] != key:
            rows = self.engine.events.rows("status = 'open' AND mode IN ('map', 'push') AND until > ?", (now,), 20)
            self._state = (key, {"open": [self.shown(r) for r in rows]})
        return self._state[1]

    # -- a person's verdict -----------------------------------------------------------------------------------------

    def dismiss(self, eid: int, body: dict) -> dict:
        op = str(body.get("operator") or "")[:120]
        if not self.engine.events.dismiss(eid, op, self.cad.clock()):
            raise CadError(f"no open pre-alert {eid}")
        ev = self.engine.events.get(eid)
        if ev is not None:
            self.engine.dismissed(ev)             # its window spent; mute_min keeps it from coming straight back
        self.engine.events.feedback(eid, None, str(body.get("verdict") or "noise") if body.get("verdict") in VERDICTS
                                    else "noise", op, self.cad.clock())
        return {"ok": True}

    def feedback(self, eid: int, body: dict) -> dict:
        v = body.get("verdict")
        if v not in VERDICTS:
            raise CadError(f"verdict: {' | '.join(VERDICTS)}")
        if self.engine.events.get(eid) is None:
            raise CadError(f"no pre-alert {eid}")
        cc = str(body.get("category") or "")[:40]
        fid = self.engine.events.feedback(eid, None, v, str(body.get("operator") or ""), self.cad.clock(),
                                          corrected_category=cc)
        return {"ok": True, "id": fid}

    # -- Settings > System > Intel ----------------------------------------------------------------------------------

    def settings_op(self, action: str, body: dict, op: str) -> dict:
        from sizeup.cad.settings import REMOVE
        ov = self._ov()
        trial = copy.deepcopy(ov)
        audit_key, old, new = "", None, None
        if action == "set" and str(body.get("key") or "").startswith("infer"):
            from sizeup.intel import infer as infer_mod
            key = str(body.get("key"))
            if key not in infer_mod.DEFAULTS:
                raise CadError(f"key: {' | '.join(infer_mod.DEFAULTS)}")
            v = clean_value(key, body.get("value"), ov["intel"])
            changes = [("intel", key, REMOVE if v == infer_mod.DEFAULTS[key] else v)]
            audit_key, old, new = f"intel/{key}", ov["intel"].get(key, infer_mod.DEFAULTS[key]), v
        elif action == "set":
            key = body.get("key")
            if key not in policy_mod.DEFAULTS:
                raise CadError(f"key: {' | '.join(policy_mod.DEFAULTS)}")
            v = clean_value(key, body.get("value"), ov["intel"])
            changes = [("intel", key, REMOVE if v == policy_mod.DEFAULTS[key] else v)]
            audit_key, old, new = f"intel/{key}", ov["intel"].get(key, policy_mod.DEFAULTS[key]), v
        elif action in ("pack", "category"):
            pname = str(body.get("pack") or "")
            if self.engine.pack(pname) is None:
                raise CadError(f"no pack {pname!r}")
            cur = copy.deepcopy(ov["intel_packs"].get(pname) or {})
            if action == "pack":
                for k in ("mode", "threshold", "window_s"):
                    if k in body:
                        cur[k] = (_mode(body[k], "mode") if k == "mode" else
                                  _f(body[k], 0, 1, "threshold") if k == "threshold" else _f(body[k], 10, 1800, "window_s"))
                if "area" in body:
                    try:
                        towns = list(rules_mod.area(body["area"], "area"))
                    except ValueError as e:
                        raise CadError(str(e)) from None
                    if towns:
                        cur["area"] = towns
                    else:
                        cur.pop("area", None)           # empty: the pack file's own area
                audit_key = f"intel_packs/{pname}"
            else:
                cat = str(body.get("category") or "")
                if cat not in self.engine.pack(pname).categories:
                    raise CadError(f"no category {cat!r} in {pname}")
                cc = cur.setdefault("categories", {}).setdefault(cat, {})
                if "mode" in body:
                    cc["mode"] = _mode(body["mode"], "mode")
                if "priority" in body:
                    cc["priority"] = int(_f(body["priority"], 1, 5, "priority"))
                if "ttl_min" in body:
                    cc["ttl_min"] = _f(body["ttl_min"], 1, 1440, "ttl_min")
                audit_key = f"intel_packs/{pname}/{cat}"
            changes = [("intel_packs", pname, cur)]
            old, new = ov["intel_packs"].get(pname), cur
        elif action in ("rule", "local"):
            rid = str(body.get("rule") or "")
            cur = copy.deepcopy(ov["intel_rules"].get(rid) or {})
            if action == "local":
                if not rid.startswith("local.") or not rid[6:].replace("_", "").isalnum() or len(rid) > 40:
                    raise CadError("rule: local.<letters, digits, _> (at most 40)")
                if not isinstance(body.get("any"), list) or not body["any"]:
                    raise CadError("any: a list of phrases")
                cur = {"pack": str(body.get("pack") or ""), "category": str(body.get("category") or ""),
                       "any": [str(x)[:rules_mod.PHRASE_MAX] for x in body["any"]][:20],
                       "not": [str(x)[:rules_mod.PHRASE_MAX] for x in body.get("not") or []][:20],
                       "w": _f(body.get("w", 0.5), -1, 1, "w"), "mode": _mode(body.get("mode", "shadow"), "mode"),
                       "v": int(cur.get("v", 0)) + 1}
                if self.engine.pack(cur["pack"]) is None or cur["category"] not in self.engine.pack(cur["pack"]).categories:
                    raise CadError("pack and category: one of the packs' categories")
            else:
                if not any(r.id == rid for p in self.engine.packs for r in p.rules):
                    raise CadError(f"no rule {rid!r}")
                if "mode" in body:
                    cur["mode"] = _mode(body["mode"], "mode")
                if "w" in body:
                    cur["w"] = _f(body["w"], -1, 1, "w")
                if "enabled" in body:
                    if not isinstance(body["enabled"], bool):
                        raise CadError("enabled: true or false")
                    cur["enabled"] = body["enabled"]
            changes = [("intel_rules", rid, cur)]
            audit_key, old, new = f"intel_rules/{rid}", ov["intel_rules"].get(rid), cur
        elif action == "reset":
            what = body.get("what")
            if what == "set":
                key = str(body.get("key") or "")
                changes, audit_key = [("intel", key, REMOVE)], f"intel/{key}"
                old = ov["intel"].get(key)
            elif what == "pack":
                key = str(body.get("pack") or "")
                changes, audit_key, old = [("intel_packs", key, REMOVE)], f"intel_packs/{key}", ov["intel_packs"].get(key)
            elif what == "category":
                pname, cat = str(body.get("pack") or ""), str(body.get("category") or "")
                cur = copy.deepcopy(ov["intel_packs"].get(pname) or {})
                (cur.get("categories") or {}).pop(cat, None)
                changes = [("intel_packs", pname, cur if cur.get("categories") or set(cur) - {"categories"} else REMOVE)]
                audit_key, old = f"intel_packs/{pname}/{cat}", ov["intel_packs"].get(pname)
            elif what == "rule":
                key = str(body.get("rule") or "")
                changes, audit_key, old = [("intel_rules", key, REMOVE)], f"intel_rules/{key}", ov["intel_rules"].get(key)
            else:
                raise CadError("what: set | pack | category | rule")
            new = "default"
        else:
            raise CadError("action: set | pack | category | rule | local | reset")
        for sec, key, val in changes:                     # compiles with the change, or says why not
            if val is REMOVE:
                trial[sec].pop(key, None)
            else:
                trial[sec][key] = val
        try:
            rules_mod.load_packs(None, trial)
        except ValueError as e:
            raise CadError(str(e)) from None
        out = self.cad.settings._commit(changes, op, [("system", audit_key, old, new)])
        self.engine.reload()
        return {**out, "intel": self.page(gate=False)}
