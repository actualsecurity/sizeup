"""The deploy stops before ECS when a secret the hub's task definition names is not in SSM (.github/workflows/deploy.yml,
tools/taskdef_params.py): a task whose secret is missing never starts, and the service would roll to it."""
import json
import re
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
    assert names == want and all(n.startswith("/") for n in names)
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "taskdef_params.py"), str(TASKDEF)], capture_output=True,
                         text=True, check=True, timeout=30).stdout.split()
    assert out == names


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
