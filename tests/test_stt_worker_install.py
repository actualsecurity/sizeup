"""The GPU STT worker's one-shot Windows installer (deploy/stt-worker/install.ps1): its pins, the README's and
pins.json must agree, and what it writes must be a config the worker takes."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.stt_worker_module import W

HERE = Path(W.__file__).parent
PS1 = HERE / "install.ps1"
PINS = json.loads((HERE / "pins.json").read_text())
ART = PINS["artifacts"]


def _readme() -> str:
    return (HERE / "README.md").read_text()


def _ps1() -> str:
    return PS1.read_text()


def _here_string(name: str) -> str:
    """A PowerShell single-quoted here-string assigned to $name in install.ps1."""
    m = re.search(rf"^\${name} = @'\n(.*?)\n'@$", _ps1(), re.S | re.M)
    assert m, f"install.ps1 has no ${name} = @'...'@"
    return m.group(1)


def _ps_string(name: str) -> str:
    m = re.search(rf"^\${name} = '([^']*)'$", _ps1(), re.M)
    assert m, f"install.ps1 has no ${name} = '...'"
    return m.group(1)


def test_every_pin_is_a_fixed_https_download():
    allowed = (r"https://github\.com/ggml-org/(whisper|llama)\.cpp/releases/download/b\d+/[\w.-]+\.zip",
               r"https://huggingface\.co/[\w.-]+/[\w.-]+/resolve/[0-9a-f]{40}/[\w.-]+",
               r"https://raw\.githubusercontent\.com/actualsecurity/sizeup/[0-9a-f]{40}/deploy/stt-worker/worker\.py")
    assert set(ART) >= {"whisper_zip", "whisper_model", "worker_py", "llama_zip", "llama_cudart_zip", "qwen3_8b"}
    for key, pin in ART.items():
        assert any(re.fullmatch(a, pin["url"]) for a in allowed), key
        assert re.fullmatch(r"[0-9A-F]{64}", pin["sha256"]), key
        assert isinstance(pin["bytes"], int) and pin["bytes"] > 0, key


def test_pins_json_and_the_readme_agree():
    """Every pin is in the README (URL, size, SHA-256) and every hash the README checks is a pin."""
    readme = _readme()
    for key, pin in ART.items():
        url = pin["url"]
        base, name = url.rsplit("/", 1)
        assert url in readme or (f"'{base}'" in readme and f"/{name}" in readme), f"README: {key}'s URL {url}"
        assert f"{pin['bytes']:,}" in readme, f"README: {key}'s size {pin['bytes']:,}"
        assert pin["sha256"] in readme, f"README: {key}'s SHA256"
    pinned = {p["sha256"] for p in ART.values()} | {hashlib.sha256(PS1.read_bytes()).hexdigest().upper()}
    for sha in re.findall(r"\.Hash -eq '([0-9A-Fa-f]{64})'", readme):
        assert sha.upper() in pinned, f"README checks {sha}, which isn't in pins.json"
    assert f"({PINS['nvidia_driver_min']} or newer" in readme
    # the README's example worker.toml: the default LLM's hash, lower case
    assert f'llm_model_sha256 = "{ART["qwen3_8b"]["sha256"].lower()}"' in readme


def test_install_ps1_embeds_pins_json():
    assert json.loads(_here_string("PinsJson")) == PINS, \
        "install.ps1's $PinsJson must be deploy/stt-worker/pins.json (copy it in, then re-pin the README's Quick install)"


def test_the_readme_quick_install_pins_this_install_ps1():
    """The Quick install's hash is install.ps1's; when git has the pinned commit, the file there is this one. A change
    to install.ps1 needs a second, README-only commit that pins the commit with the change and its hash."""
    readme = _readme()
    m = re.search(r"raw\.githubusercontent\.com/actualsecurity/sizeup/([0-9a-f]{40})/deploy/stt-worker/install\.ps1",
                  readme)
    assert m, "README: the Quick install downloads install.ps1 from a pinned commit"
    digest = hashlib.sha256(PS1.read_bytes()).hexdigest().upper()
    assert f"sizeup-stt-install.ps1\").Hash -eq '{digest}'" in readme, \
        f"README Quick install: install.ps1's SHA256 is now {digest} (pin the commit that has it)"
    git = shutil.which("git")
    if git:
        r = subprocess.run([git, "show", f"{m.group(1)}:deploy/stt-worker/install.ps1"], cwd=HERE,
                           capture_output=True)
        if r.returncode == 0:              # a shallow checkout (CI) may not have the commit
            assert hashlib.sha256(r.stdout).hexdigest().upper() == digest, "the pinned commit has another install.ps1"


def test_install_ps1_is_ascii_and_kept_byte_for_byte():
    # Windows PowerShell 5.1 reads a file without a BOM as the ANSI code page: ASCII reads the same everywhere
    PS1.read_bytes().decode("ascii")
    attrs = (HERE / ".gitattributes").read_text()          # (deploy/station's convention: its own .gitattributes)
    assert re.search(r"^install\.ps1 -text$", attrs, re.M)
    git = shutil.which("git")
    if git:
        r = subprocess.run([git, "check-attr", "text", "--", "install.ps1"], cwd=HERE, capture_output=True, text=True)
        assert r.returncode != 0 or r.stdout.strip().endswith("text: unset"), r.stdout


def test_no_drive_qualified_variable_in_a_string():
    """"$name: x" is a parse error in PowerShell (a drive-qualified variable): it must be "${name}: x"."""
    bad = [(i, line) for i, line in enumerate(_ps1().splitlines(), 1)
           if re.search(r"\$(?!(env|script|global|using):)[A-Za-z_]\w*:", line)]
    assert not bad, bad


def test_the_token_shape_is_the_workers():
    shape = _ps_string("TokenShape")
    assert shape == "^" + W.TOKEN.pattern.replace("(", "").replace(")", "") + "$"   # the worker's, without its groups
    rx = re.compile(shape)
    assert rx.match("sw1.gpu-1.AbC_d-9")
    for bad in ("", "sw1.GPU.x", "sw2.gpu.x", "sw1.gpu.", "sw1.gpu.a b", " sw1.gpu.x", "sw1..x", "x sw1.gpu.y"):
        assert not rx.match(bad), bad


def test_the_hub_url_shape():
    rx = re.compile(_ps_string("HubShape"))
    for good in ("https://sizeup.actualsecurity.org", "https://hub.example.org:8443", "https://h.example/x/y"):
        assert rx.match(good), good
    for bad in ("http://hub.example.org", "https://u:p@hub.example.org", 'https://hub"x', "https://hub/?q=1",
                "https://hub .org", "https://hub\\x", ""):
        assert not rx.match(bad), bad


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib")
@pytest.mark.parametrize("with_llm", [False, True])
def test_the_worker_toml_it_writes_loads(tmp_path, with_llm):
    text = _here_string("TomlStt").replace("{{HUB_URL}}", "https://hub.example.org") + "\n"
    if with_llm:
        text += "\n" + _here_string("TomlLlm").replace("{{LLM_SHA256}}", ART["qwen3_8b"]["sha256"].lower()) + "\n"
    assert "{{" not in text
    p = tmp_path / "worker.toml"
    p.write_text(text)
    cfg = W.load_config(p, env={"SIZEUP_STT_WORKER_TOKEN": "sw1.gpu-test.secret"})
    W.validate(cfg)
    assert cfg.hub_url == "https://hub.example.org"
    assert cfg.whisper_server[0] == r"whisper\Release\whisper-server.exe"
    assert cfg.whisper_server[cfg.whisper_server.index("--host") + 1] == "127.0.0.1"
    if with_llm:
        assert cfg.llm_model_sha256 == ART["qwen3_8b"]["sha256"].lower()
        assert cfg.llm_server[cfg.llm_server.index("--host") + 1] == "127.0.0.1"
        assert cfg.llm_server[-2:] == ["--reasoning", "off"]
    else:
        assert not cfg.llm_url and not cfg.llm_server
    # the README's example: the same programs and arguments
    (example,) = re.findall(r"```toml\n(.*?)```", _readme(), re.S)
    ex = W.load_config(_write(tmp_path / "ex.toml", example), env={})
    assert cfg.whisper_server == ex.whisper_server
    if with_llm:
        assert (cfg.llm_url, cfg.llm_model, cfg.llm_server) == (ex.llm_url, ex.llm_model, ex.llm_server)


def _write(p: Path, text: str) -> Path:
    p.write_text(text)
    return p


def test_the_check_servers_listen_on_loopback_as_in_the_readme():
    readme, ps1 = _readme(), _ps1()
    assert "0.0.0.0" not in ps1
    whisper = _ps_string("WhisperArgs")
    assert "--host 127.0.0.1 --port 8179" in whisper and "--host 127.0.0.1 --port 8179 -t 4" in readme
    llama = _ps_string("LlamaArgs")
    assert llama.startswith("-m models\\Qwen3-8B-Q4_K_M.gguf --host 127.0.0.1 --port 8180 ")
    assert llama.split(" ", 2)[2] in readme      # the README's step 4b flags, from --host on


def test_the_task_is_the_readmes_step_8():
    readme, ps1 = _readme(), _ps1()
    step8 = readme[readme.index("### 8."):readme.index("### Updating")]
    for line in ('-Argument "`"$d\\worker.py`" run -c `"$d\\worker.toml`"" -WorkingDirectory $d',
                 "New-ScheduledTaskTrigger -AtLogOn -User $me",
                 "-RepetitionInterval (New-TimeSpan -Minutes 5)",
                 "-RepetitionDuration (New-TimeSpan -Days 3650)",
                 "-RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)",
                 "-ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
                 "-MultipleInstances IgnoreNew",
                 "New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited"):
        assert line in step8 and line in ps1, line
    assert "pythonw.exe" in ps1


def test_the_secrets_are_never_shown_or_written_elsewhere():
    ps1 = _ps1()
    assert "Start-Transcript" not in ps1 and "Set-PSDebug" not in ps1
    assert "Read-Host \"$name ($hint; hidden)\" -AsSecureString" in ps1
    for name in ("SIZEUP_STT_WORKER_TOKEN", "SIZEUP_STT_CF_CLIENT_ID", "SIZEUP_STT_CF_CLIENT_SECRET"):
        assert name in ps1
    # the only places the plain value goes: the user variable (README step 6) and this process's env for `check`
    for i, line in enumerate(ps1.splitlines(), 1):
        if "$plain" in line or "$have" in line or "$val" in line:
            assert not re.search(r"Write-|Say |Warn |Ok |Out-|Add-Content|Set-Content|WriteAll|Fail ", line), (i, line)


# --- the script itself, under PowerShell (GitHub's ubuntu runners have pwsh; skipped without it) --------------------

PWSH = shutil.which("pwsh")
needs_pwsh = pytest.mark.skipif(not PWSH, reason="no pwsh")


@needs_pwsh
def test_install_ps1_parses():
    assert "'" not in str(PS1)
    cmd = (f"$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile('{PS1}', [ref]$null, [ref]$e); "
           "$e | ForEach-Object { \"$($_.Extent.StartLineNumber): $($_.Message)\" }; exit $e.Count")
    r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


@needs_pwsh
@pytest.mark.parametrize("args", [["-Llm", "yes"], ["-Llm", "no"], ["-Uninstall"]])
def test_install_ps1_dry_run(tmp_path, args):
    home, local = tmp_path / "home", tmp_path / "local"          # pwsh keeps its own cache under HOME
    home.mkdir()
    local.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LOCALAPPDATA": str(local)}
    r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(PS1), "-DryRun", *args],
                       capture_output=True, text=True, timeout=180, env=env, stdin=subprocess.DEVNULL)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "STOPPED" not in out
    assert not any(local.rglob("*")), "a dry run changes nothing"
    if "-Uninstall" in args:
        assert "would delete" in out and "Unregister-ScheduledTask" in out
        return
    llm = args[1] == "yes"
    for key, pin in ART.items():
        if key == "gemma3_12b":
            assert pin["url"] not in out
            continue
        wanted = llm or not key.startswith(("llama", "qwen"))
        assert (pin["url"] in out) == wanted, key
        if wanted:
            assert pin["sha256"] in out, key
    assert "register the task sizeup-stt-worker" in out
    assert ("llm_model_sha256" in out) == llm
