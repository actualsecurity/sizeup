"""The one error shape of /api/v1: {"error": {"code": "machine_code", "message": "human text", "retry_after_s"?}}."""
from __future__ import annotations

import json

JSON = "application/json"


def body(code: str, message: str, retry_after_s: int | None = None) -> bytes:
    err: dict = {"code": code, "message": message}
    if retry_after_s is not None:
        err["retry_after_s"] = retry_after_s
    return json.dumps({"error": err}, separators=(",", ":")).encode()


def error(status: int, code: str, message: str, retry_after_s: int | None = None,
          headers: dict | None = None) -> tuple[int, bytes, str, dict]:
    """(status, body, content type, headers). Never cached; Retry-After is sent with retry_after_s."""
    h = {"Cache-Control": "no-store", **(headers or {})}
    if retry_after_s is not None:
        h["Retry-After"] = str(retry_after_s)
    return status, body(code, message, retry_after_s), JSON, h
