#!/usr/bin/env python3
"""The SSM parameters the hub's task definition names (deploy/aws/taskdef.json, every container's secrets), one name a
line, for the deploy's check that each exists before ECS is touched (.github/workflows/deploy.yml): a task whose secret
is missing never starts, so a deploy that would roll to it must stop first. Stdlib only (the runner's python3).

    python3 tools/taskdef_params.py deploy/aws/taskdef.json [--region us-east-1] [--account 123456789012]
        -> /sizeup-hub/station-token ...

Every secret's valueFrom must be a full SSM parameter ARN of a hierarchical name
(arn:aws:ssm:<region>:<account>:parameter/<path>/<name>, the parameter "/<path>/<name>"). A bare name would be looked up
in whatever region the task runs; a name with no "/" after "parameter/" is refused too, because its ARN can't say
whether the parameter is "foo" or "/foo". With --region / --account, every ARN must be in that region and account (the
deploy's: AWS_REGION and sts get-caller-identity), or nothing is printed. Exit 2 names the offending entry."""

from __future__ import annotations

import json
import re
import sys

ARN = re.compile(r"arn:aws:ssm:([a-z0-9-]+):(\d{12}):parameter/(.+)")
PATH = re.compile(r"[A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-]+)+")


def params(taskdef: dict) -> list[tuple[str, str, str]]:
    """(name "/sizeup-hub/x", region, account) for each parameter, in order, once; ValueError naming a secret that is
    not a full ARN of a hierarchical parameter."""
    out: list[tuple[str, str, str]] = []
    for c in taskdef.get("containerDefinitions", []):
        for s in c.get("secrets", []) or []:
            where = f"{c.get('name')}: {s.get('name')}"
            m = ARN.fullmatch(str(s.get("valueFrom", "")))
            if not m:
                raise ValueError(f"{where}: valueFrom is not a full SSM parameter ARN")
            region, account, rest = m.groups()
            if not PATH.fullmatch(rest):
                raise ValueError(f"{where}: not a hierarchical parameter (parameter/<path>/<name>): its ARN can't tell "
                                 f"\"{rest}\" from \"/{rest}\"; use a /path/name parameter")
            p = ("/" + rest, region, account)
            if p not in out:
                out.append(p)
    return out


def main(argv: list[str]) -> int:
    args, want = [], {}
    it = iter(argv[1:])
    for a in it:
        if a in ("--region", "--account"):
            want[a[2:]] = next(it, "")
        else:
            args.append(a)
    if len(args) != 1 or any(not v for v in want.values()):
        print("usage: taskdef_params.py deploy/aws/taskdef.json [--region R] [--account A]", file=sys.stderr)
        return 2
    try:
        with open(args[0]) as f:
            ps = params(json.load(f))
    except (OSError, ValueError) as e:
        print(f"taskdef_params: {e}", file=sys.stderr)
        return 2
    bad = [f"{n} is in {r} / {a}, not {want.get('region', r)} / {want.get('account', a)}" for n, r, a in ps
           if r != want.get("region", r) or a != want.get("account", a)]
    if bad:
        print("taskdef_params: " + "; ".join(bad), file=sys.stderr)
        return 2
    print("\n".join(n for n, _, _ in ps))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
