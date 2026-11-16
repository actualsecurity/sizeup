"""Loads the /api/v1 OpenAPI spec (api/openapi/v1/) for the contract tests: the files' relative $refs bundled into one
document, so it can be validated and every response checked against its schema. Dev-only (jsonschema, PyYAML)."""
from __future__ import annotations

from pathlib import Path

import yaml

SPEC_DIR = Path(__file__).resolve().parent.parent / "api" / "openapi" / "v1"
METHODS = {"get", "put", "post", "delete", "patch", "head", "options"}


def _load(path: Path):
    return yaml.safe_load(path.read_text())


def bundle(node, base: Path, seen: tuple = ()):
    """node with every {"$ref": "<file>"} (a relative file) replaced by that file's content, itself bundled."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            target = (base.parent / ref).resolve()
            if target in seen:
                raise ValueError(f"$ref cycle at {target}")
            return bundle(_load(target), target, seen + (target,))
        return {k: bundle(v, base, seen) for k, v in node.items()}
    if isinstance(node, list):
        return [bundle(v, base, seen) for v in node]
    return node


def spec() -> dict:
    root = SPEC_DIR / "openapi.yaml"
    return bundle(_load(root), root, (root,))


def operations(doc: dict) -> set[tuple[str, str]]:
    """{(METHOD, path)} the spec promises."""
    return {(m.upper(), p) for p, item in doc["paths"].items() for m in item if m in METHODS}


def response_schema(doc: dict, method: str, path: str, status: int):
    """The JSON schema of one operation's response (its `default` when the status is not listed), else None."""
    rs = doc["paths"][path][method.lower()]["responses"]
    r = rs.get(str(status)) or rs.get("default")
    return ((r or {}).get("content") or {}).get("application/json", {}).get("schema")
