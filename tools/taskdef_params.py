#!/usr/bin/env python3
"""The SSM parameters the hub's task definition names (deploy/aws/taskdef.json, every container's secrets), one name a
line, for the deploy's check that each exists before ECS is touched (.github/workflows/deploy.yml): a task whose secret
is missing never starts, so a deploy that would roll to it must stop first. Stdlib only (the runner's python3).

    python3 tools/taskdef_params.py deploy/aws/taskdef.json     -> /sizeup-hub/station-token ...

Exit 2 (with the offending entry named) when a secret's valueFrom is not a full SSM parameter ARN
(arn:aws:ssm:<region>:<account>:parameter/<name>): a bare name would be looked up in whatever region the task runs."""

from __future__ import annotations

import json
import re
import sys

ARN = re.compile(r"arn:aws:ssm:[a-z0-9-]+:\d{12}:parameter(/[A-Za-z0-9_.\-/]+)")


def params(taskdef: dict) -> list[str]:
    """Each parameter's name ("/sizeup-hub/x"), in order, once; ValueError naming a secret that isn't a full ARN."""
    out: list[str] = []
    for c in taskdef.get("containerDefinitions", []):
        for s in c.get("secrets", []) or []:
            m = ARN.fullmatch(str(s.get("valueFrom", "")))
            if not m:
                raise ValueError(f"{c.get('name')}: {s.get('name')}: valueFrom is not a full SSM parameter ARN")
            if m.group(1) not in out:
                out.append(m.group(1))
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: taskdef_params.py deploy/aws/taskdef.json", file=sys.stderr)
        return 2
    try:
        with open(argv[1]) as f:
            names = params(json.load(f))
    except (OSError, ValueError) as e:
        print(f"taskdef_params: {e}", file=sys.stderr)
        return 2
    print("\n".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
