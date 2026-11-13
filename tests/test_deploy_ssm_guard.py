"""The deploy stops before ECS when a secret the hub's task definition names is not in SSM (.github/workflows/deploy.yml,
tools/taskdef_params.py): a task whose secret is missing never starts, and the service would roll to it."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TASKDEF = ROOT / "deploy" / "aws" / "taskdef.json"
sys.path.insert(0, str(ROOT / "tools"))
import taskdef_params  # noqa: E402

ARN = re.compile(r"arn:aws:ssm:[a-z0-9-]+:\d{12}:parameter/[A-Za-z0-9_.\-/]+")


def test_every_taskdef_secret_is_a_full_ssm_arn():
    td = json.loads(TASKDEF.read_text())
    secrets = [(c["name"], s) for c in td["containerDefinitions"] for s in c.get("secrets", [])]
    assert secrets
    for container, s in secrets:
        assert ARN.fullmatch(s["valueFrom"]), f"{container}: {s['name']}: {s['valueFrom']!r} is not a full SSM parameter ARN"


def test_the_tool_lists_every_parameter_once_by_name():
    td = json.loads(TASKDEF.read_text())
    names = taskdef_params.params(td)
    want = []
    for c in td["containerDefinitions"]:
        for s in c.get("secrets", []):
            n = s["valueFrom"].split(":parameter", 1)[1]
            if n not in want:
                want.append(n)
    assert [n for n, _, _ in names] == want and all(n.startswith("/") for n, _, _ in names)
    region, account = names[0][1], names[0][2]
    assert all((r, a) == (region, account) for _, r, a in names)
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "taskdef_params.py"), str(TASKDEF)], capture_output=True,
                         text=True, check=True, timeout=30).stdout.split()
    assert out == want


def test_the_tool_refuses_a_bare_name(tmp_path):
    bad = {"containerDefinitions": [{"name": "hub", "secrets": [{"name": "X", "valueFrom": "/sizeup-hub/x"}]}]}
    with pytest.raises(ValueError, match="hub: X: valueFrom is not a full SSM parameter ARN"):
        taskdef_params.params(bad)
    p = tmp_path / "td.json"
    p.write_text(json.dumps(bad))
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "taskdef_params.py"), str(p)], capture_output=True, text=True,
                       timeout=30)
    assert r.returncode == 2 and "not a full SSM parameter ARN" in r.stderr and r.stdout == ""


def test_the_workflow_checks_before_it_builds_renders_or_touches_ecs():
    wf = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    check = wf.index("python3 tools/taskdef_params.py deploy/aws/taskdef.json")
    assert wf.index("aws-actions/configure-aws-credentials") < check
    for later in ("amazon-ecr-login", "docker/build-push-action", "amazon-ecs-render-task-definition",
                  "aws ecs describe-services", "amazon-ecs-deploy-task-definition", "aws ecs register-task-definition"):
        assert check < wf.index(later), later
    step = wf[check:wf.index("amazon-ecr-login")]
    assert "aws ssm describe-parameters" in step and "--with-decryption" not in step and "get-parameter" not in step
    assert "exit 1" in step and "::error" in step


def one(value):
    return {"containerDefinitions": [{"name": "hub", "secrets": [{"name": "X", "valueFrom": value}]}]}


def test_a_non_hierarchical_arn_is_refused_clearly_and_a_hierarchical_one_named_right():
    for v in ("arn:aws:ssm:us-east-1:123456789012:parameter/foo", "arn:aws:ssm:us-east-1:123456789012:parameter/"):
        with pytest.raises(ValueError, match="hub: X: (not a hierarchical parameter|valueFrom is not)"):
            taskdef_params.params(one(v))
    assert taskdef_params.params(one("arn:aws:ssm:us-west-2:123456789012:parameter/a/b.c")) == [("/a/b.c", "us-west-2", "123456789012")]


def run_tool(tmp_path, td, *args):
    p = tmp_path / "td.json"
    p.write_text(json.dumps(td))
    return subprocess.run([sys.executable, str(ROOT / "tools" / "taskdef_params.py"), str(p), *args], capture_output=True,
                          text=True, timeout=30)


def test_the_region_and_account_must_be_the_deploys(tmp_path):
    td = one("arn:aws:ssm:us-east-1:123456789012:parameter/sizeup-hub/x")
    ok = run_tool(tmp_path, td, "--region", "us-east-1", "--account", "123456789012")
    assert ok.returncode == 0 and ok.stdout.split() == ["/sizeup-hub/x"]
    for args in (("--region", "us-west-2", "--account", "123456789012"), ("--region", "us-east-1", "--account", "999999999999")):
        r = run_tool(tmp_path, td, *args)
        assert r.returncode == 2 and r.stdout == "" and "/sizeup-hub/x is in us-east-1 / 123456789012" in r.stderr


# -- the step's shell, with a stub aws on PATH ---------------------------------------------------------------------------------

def step_script() -> str:
    """The deploy.yml step's run: block, dedented (no YAML parser in the test environment: by its indentation)."""
    lines = (ROOT / ".github" / "workflows" / "deploy.yml").read_text().splitlines()
    i = lines.index("      - name: Every secret the task definition names is in SSM")
    assert lines[i + 1].strip() == "shell: bash" and lines[i + 2].strip() == "run: |"
    body = []
    for ln in lines[i + 3:]:
        if ln.strip() and not ln.startswith(" " * 10):
            break
        body.append(ln[10:])
    return "\n".join(body) + "\n"


STUB = r"""#!/usr/bin/env bash
case "$1 $2" in
  "sts get-caller-identity")
    [ "$STUB_STS" = fail ] && { echo "Unable to locate credentials. You can configure credentials by running \"aws configure\"." >&2; exit 253; }
    echo "$STUB_ACCOUNT";;
  "ssm describe-parameters")
    echo "$@" >> "$STUB_LOG"
    [ "$STUB_SSM" = fail ] && { echo "An error occurred (AccessDeniedException) when calling the DescribeParameters operation: not authorized" >&2; exit 254; }
    printf '%s\n' "$STUB_FOUND";;
  *) echo "unexpected aws $*" >&2; exit 99;;
esac
"""


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash")
@pytest.mark.parametrize("case", ["all", "one_missing", "ssm_fails", "sts_fails", "other_account"])
def test_the_step_with_a_stub_aws(tmp_path, case):
    names = taskdef_params.params(json.loads(TASKDEF.read_text()))
    region, account = names[0][1], names[0][2]
    every = [n for n, _, _ in names]
    found = every if case != "one_missing" else every[1:]
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "aws").write_text(STUB)
    (bin_ / "aws").chmod(0o755)
    script = tmp_path / "step.sh"
    script.write_text(step_script())
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "AWS_REGION": region,
           "STUB_ACCOUNT": "999999999999" if case == "other_account" else account, "STUB_FOUND": "\t".join(found),
           "STUB_SSM": "fail" if case == "ssm_fails" else "", "STUB_STS": "fail" if case == "sts_fails" else "",
           "STUB_LOG": str(tmp_path / "aws.log")}
    r = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    if case == "all":
        assert r.returncode == 0 and f"All {len(every)} SSM parameters" in out and "::error" not in out
        logged = (tmp_path / "aws.log").read_text()
        assert "Key=Name,Option=Equals,Values=" + ",".join(every) in logged
    elif case == "one_missing":
        assert r.returncode == 1
        assert out.count("::error title=Missing SSM parameter::") == 1 and f"::{every[0]} is named" in out
    elif case == "ssm_fails":
        assert r.returncode == 1 and "Missing SSM parameter" not in out
        assert "could not check SSM: AccessDeniedException (ssm describe-parameters)" in out
    elif case == "sts_fails":
        assert r.returncode == 1 and "Missing SSM parameter" not in out
        assert "could not check SSM: Unable to locate credentials" in out and "(sts get-caller-identity)" in out
    else:
        assert r.returncode == 1 and "Missing SSM parameter" not in out and "not us-east-1 / 999999999999" in out
        assert not (tmp_path / "aws.log").exists()                           # never asked SSM about another account
