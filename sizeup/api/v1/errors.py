"""The one error shape of /api/v1: {"error": {"code": "machine_code", "message": "human text", "retry_after_s"?}}."""
from __future__ import annotations

import json

JSON = "application/json"


def envelope(code: str, message: str, retry_after_s: int | None = None, extra: dict | None = None) -> dict:
    """The error object. extra: fields a client needs beside the code (epoch_changed: epoch; cursor_too_old: oldest)."""
    err: dict = {"code": code, "message": message, **(extra or {})}
    if retry_after_s is not None:
        err["retry_after_s"] = retry_after_s
    return {"error": err}


def body(code: str, message: str, retry_after_s: int | None = None, extra: dict | None = None) -> bytes:
    return json.dumps(envelope(code, message, retry_after_s, extra), separators=(",", ":")).encode()


def error(status: int, code: str, message: str, retry_after_s: int | None = None,
          headers: dict | None = None, extra: dict | None = None) -> tuple[int, bytes, str, dict]:
    """(status, body, content type, headers). Never cached; Retry-After is sent with retry_after_s."""
    h = {"Cache-Control": "no-store", **(headers or {})}
    if retry_after_s is not None:
        h["Retry-After"] = str(retry_after_s)
    return status, body(code, message, retry_after_s, extra), JSON, h
