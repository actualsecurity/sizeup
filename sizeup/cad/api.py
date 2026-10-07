"""CAD JSON API under /api/cad/ (served by sizeup/web/server.py). Every POST takes a JSON body;
a bad one is a 400 {"error": reason}.

GET  /api/cad/state                       units, incidents (pending, active, recently closed), run cards
GET  /api/cad/stream                      SSE: the state, pushed every second (server.py)
GET  /api/cad/geocode?q=209 westchester   gazetteer matches [{label, lat, lon, kind, confidence, ...}]
GET  /api/cad/runcards                    run cards
GET  /api/cad/dispatch/<id>.wav           a logged dispatch's audio
GET  /api/cad/audio/<key>.wav             a preview's audio
POST /api/cad/incident                    {type, address?, cross_street?, narrative?, lat?, lon?, geo_label?, priority?, operator?}
POST /api/cad/incident/<id>               edit: any of the fields above
POST /api/cad/incident/<id>/accept        {operator?}            pending -> active (numbered)
POST /api/cad/incident/<id>/dismiss       {operator?, reason?}   pending -> dismissed
POST /api/cad/incident/<id>/close         {operator?}            active -> closed, units cleared
POST /api/cad/incident/<id>/note          {text, operator?}
POST /api/cad/incident/<id>/recommend     {type?}                -> {units, unfilled, already, card}
POST /api/cad/incident/<id>/preview       {unit_ids}             -> {text, audio_url, audio_note}
POST /api/cad/incident/<id>/dispatch      {unit_ids, operator, channel?} -> {dispatch_id, text, audio_url, transmitted, ...}
POST /api/cad/unit/<id>/status            {status, operator?, note?}
POST /api/cad/runcards                    {type, slots, spoken?, rename_from?}   add / edit one
POST /api/cad/runcards/delete             {type}
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs

from sizeup.cad.service import Cad
from sizeup.cad.store import CadError

JSON = "application/json"
MAX_BODY = 65536
INCIDENT = re.compile(r"/api/cad/incident/(\d{1,9})(?:/(accept|dismiss|close|note|recommend|preview|dispatch))?")
UNIT = re.compile(r"/api/cad/unit/(\d{1,9})/status")
DISPATCH_WAV = re.compile(r"/api/cad/dispatch/(\d{1,9})\.wav")
PREVIEW_WAV = re.compile(r"/api/cad/audio/([0-9a-f]{20})\.wav")


def _json(code: int, obj) -> tuple[int, bytes, str]:
    return code, json.dumps(obj, separators=(",", ":")).encode(), JSON


class CadApi:
    def __init__(self, cad: Cad):
        self.cad = cad

    def get(self, path: str, query: str) -> tuple[int, bytes, str]:
        try:
            if path == "/api/cad/state":
                return _json(200, self.cad.state())
            if path == "/api/cad/runcards":
                return _json(200, {"run_cards": self.cad.run_cards()})
            if path == "/api/cad/geocode":
                return _json(200, self.cad.geocode((parse_qs(query).get("q") or [""])[0]))
            if m := DISPATCH_WAV.fullmatch(path):
                wav = self.cad.dispatch_audio(int(m.group(1)))
                return (200, wav, "audio/wav") if wav else (404, b"no audio for that dispatch\n", "text/plain")
            if m := PREVIEW_WAV.fullmatch(path):
                wav = self.cad.preview_audio(m.group(1))
                return (200, wav, "audio/wav") if wav else (404, b"no such preview\n", "text/plain")
        except CadError as e:
            return _json(400, {"error": str(e)})
        return 404, b"not found\n", "text/plain"

    def post(self, path: str, raw: bytes) -> tuple[int, bytes, str]:
        try:
            body = json.loads(raw or b"{}")
        except ValueError as e:
            return _json(400, {"error": f"bad JSON: {e}"})
        if not isinstance(body, dict):
            return _json(400, {"error": "the body must be a JSON object"})
        c = self.cad
        try:
            if path == "/api/cad/incident":
                return _json(200, c.create(body))
            if m := INCIDENT.fullmatch(path):
                iid, action = int(m.group(1)), m.group(2)
                fn = {None: c.update, "accept": c.accept, "dismiss": c.dismiss, "close": c.close, "note": c.note,
                      "recommend": c.recommend, "preview": c.preview, "dispatch": c.dispatch}[action]
                return _json(200, fn(iid, body))
            if m := UNIT.fullmatch(path):
                return _json(200, c.unit_status(int(m.group(1)), body))
            if path == "/api/cad/runcards":
                return _json(200, c.save_run_card(body))
            if path == "/api/cad/runcards/delete":
                return _json(200, c.delete_run_card(body))
        except CadError as e:
            return _json(400, {"error": str(e)})
        return 404, b"not found\n", "text/plain"
