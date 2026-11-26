# GPU STT worker

`worker.py` makes a PC with a GPU part of the hub's speech-to-text pool (#639). It takes clips from the hub, runs them
through a local whisper.cpp `whisper-server` and sends the text back. When the PC is gaming, paused or off, the hub
sends the clips the way it would without the worker (AWS Transcribe, then the station's whisper). Nothing is dropped.

Optionally it also serves a **local LLM** (llama.cpp's `llama-server`, on the same GPU) for the hub's infer lane:
short JSON classifications for the intel engine, never a cloud model. Clips always come first, and a game gets the
GPU back at once (see [The local LLM](#the-local-llm-optional)).

- **Connections:** the worker only connects out, to the hub over HTTPS through Cloudflare Access, to
  `whisper-server` (and `llama-server`) on 127.0.0.1, and, for its [self-update](#updating), to GitHub
  (`api.github.com`, `raw.githubusercontent.com`; no token), and for its pinned downloads (llama.cpp and whisper.cpp
  builds, speech models) to ggml-org's GitHub releases and Hugging Face. Nothing on the PC listens on the network.
- **Python:** standard library only, Python 3.10 or newer. `worker.toml` needs 3.11+. On 3.10, use `worker.json`
  with the same keys.
- **Logs:** they hold job ids, timings and token counts. They never hold transcripts, prompts, the LLM's answers or
  the token. `worker.log`'s lines also go to the hub, which keeps them in CloudWatch (see [Logs](#logs-cloudwatch)).

## Quick install (one script)

In an ordinary (not elevated) PowerShell window, as the user who games on the PC:

```powershell
$u = 'https://raw.githubusercontent.com/actualsecurity/sizeup/a3975a75d83a620aea27f47ae79b4a12aa73cf3b/deploy/stt-worker/install.ps1'
Invoke-WebRequest $u -UseBasicParsing -OutFile "$env:TEMP\sizeup-stt-install.ps1"
(Get-FileHash "$env:TEMP\sizeup-stt-install.ps1").Hash -eq 'AD1715EC4470E06D012D6E363B31AB06C163F5CFF005B2815A9AA04725B49DC9'   # True
powershell -ExecutionPolicy Bypass -File "$env:TEMP\sizeup-stt-install.ps1"
```

Run the last line only if the one before printed `True`.

`install.ps1` does steps 1-8 below. It asks its questions first: the local LLM (y/N), the hub URL, then the worker
token and the Cloudflare client id and secret (read hidden, stored only as user environment variables). Then it
downloads, checks each file's size and SHA-256 against [`pins.json`](pins.json) before using it, writes `worker.toml`,
runs `check` with whisper-server on 127.0.0.1, and registers and starts the task.

- **First install only:** after it, `worker.py` updates itself and keeps llama.cpp and whisper.cpp at the builds it
  pins ([Runtime updates](#runtime-updates-llamacpp-whispercpp)). Nothing to re-run for an upgrade.
- **Again:** safe to run again (a repair). It keeps what already matches its pin and asks before it replaces
  `worker.toml`, a secret or the task. A `worker.py` that has updated itself is kept.
- **`-DryRun`:** prints what it would do, with no prompts, downloads or changes.
- **`-Uninstall`:** removes the task, `%LOCALAPPDATA%\sizeup-stt` and the three variables, after you confirm. A copy
  of the script stays in that folder for this.
- **Pins:** the URLs, sizes and hashes in `pins.json`, in the script and in the steps below are the same (a test
  checks). The script's own commit and hash above change with every change to it.

## Windows setup (NVIDIA GPU)

Everything goes in `%LOCALAPPDATA%\sizeup-stt`. Only your Windows user can write there, so other local users can't
swap the programs the worker runs. Every download's URL, size and SHA-256 is also in [`pins.json`](pins.json) (what
`install.ps1` uses). Run the commands in PowerShell as that user:

```powershell
$d = "$env:LOCALAPPDATA\sizeup-stt"
New-Item -ItemType Directory "$d\models" -Force | Out-Null
$ProgressPreference = 'SilentlyContinue'      # Invoke-WebRequest is much faster without its progress bar
```

### 1. Python

```powershell
winget install -e --id Python.Python.3.12
py -3 --version
```

The python.org installer works too. It also installs the `py` launcher.

### 2. whisper.cpp with CUDA

You need an NVIDIA driver from 2024 or later (551.61 or newer, CUDA 12.4's minimum). `nvidia-smi` should print the GPU.

Use whisper.cpp pre-release **b5454**, asset **`whisper-bin-win-cuda-12.4.0-x64.zip`** (684,913,404 bytes). It
includes the CUDA runtime DLLs, so you don't need the CUDA Toolkit. The program is `Release\whisper-server.exe` in the
zip.

```powershell
Invoke-WebRequest https://github.com/ggml-org/whisper.cpp/releases/download/b5454/whisper-bin-win-cuda-12.4.0-x64.zip -OutFile "$d\whisper-cuda.zip"
(Get-FileHash "$d\whisper-cuda.zip").Hash -eq 'AFEF0B881C500958921C3F5523B50E59EE2EC9B6F5CBD25B324C51ED308A957A'   # True
Expand-Archive "$d\whisper-cuda.zip" "$d\whisper"          # -> $d\whisper\Release\whisper-server.exe
Remove-Item "$d\whisper-cuda.zip"
```

To build it yourself instead, install Visual Studio 2022 Build Tools (C++), the CUDA Toolkit and CMake, then run this
in a whisper.cpp checkout:

```powershell
cmake -B build -DGGML_CUDA=1
cmake --build build -j --config Release --target whisper-server   # -> build\bin\Release\whisper-server.exe
```

### 3. The model

`ggml-large-v3-turbo-q5_0.bin` is pinned to one revision of the ggerganov/whisper.cpp repo on Hugging Face
(574,041,195 bytes):

```powershell
Invoke-WebRequest https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-large-v3-turbo-q5_0.bin `
  -OutFile "$d\models\ggml-large-v3-turbo-q5_0.bin"
(Get-FileHash "$d\models\ggml-large-v3-turbo-q5_0.bin").Hash -eq '394221709CD5AD1F40C46E6031CA61BCE88931E6E088C188294C6D5A55FFA7E2'   # True
nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv
```

The model needs about 1 GB of free VRAM while it is loaded.

### 4. Try whisper-server

```powershell
& "$d\whisper\Release\whisper-server.exe" -m "$d\models\ggml-large-v3-turbo-q5_0.bin" --host 127.0.0.1 --port 8179 -t 4 -l en -sns -et 2.4 -lpt -1.0 -nth 0.6
```

The output should list the GPU (`ggml_cuda_init: found 1 CUDA devices`).

whisper-server answers any web page (`Access-Control-Allow-Origin: *`), so it must listen on `127.0.0.1` only, never
`0.0.0.0`. Otherwise any site open in a browser on the network could use the GPU. The worker refuses a config that
binds it anywhere else.

Leave it running for step 7, then stop it with Ctrl+C.

### 4b. The local LLM (optional): llama.cpp with CUDA and the model

Skip this step for speech-to-text only. What it is for: [The local LLM](#the-local-llm-optional).

llama.cpp release **b12728** (commit `ea722948`; Qwen3-ASR's audio needs b12643 or later), two assets for CUDA 12.4. Both unpack into
the same folder (they are flat), and together they hold `llama-server.exe` with its DLLs and the CUDA runtime, so you
don't need the CUDA Toolkit. The build covers the RTX 30 series (sm_86).

| asset | bytes | SHA256 |
|---|---|---|
| `llama-b12728-bin-win-cuda-12.4-x64.zip` | 358,626,875 | `85B6E332ED7B40FD7A240AC7F32C4F1A29E9DDF6B52853925A215FDA23EF24B3` |
| `cudart-llama-bin-win-cuda-12.4-x64.zip` | 391,443,627 | `8C79A9B226DE4B3CACFD1F83D24F962D0773BE79F1E7B75C6AF4DED7E32AE1D6` |

```powershell
$r = 'https://github.com/ggml-org/llama.cpp/releases/download/b12728'
Invoke-WebRequest "$r/llama-b12728-bin-win-cuda-12.4-x64.zip" -OutFile "$d\llama-cuda.zip"
Invoke-WebRequest "$r/cudart-llama-bin-win-cuda-12.4-x64.zip" -OutFile "$d\llama-cudart.zip"
(Get-FileHash "$d\llama-cuda.zip").Hash -eq '85B6E332ED7B40FD7A240AC7F32C4F1A29E9DDF6B52853925A215FDA23EF24B3'     # True
(Get-FileHash "$d\llama-cudart.zip").Hash -eq '8C79A9B226DE4B3CACFD1F83D24F962D0773BE79F1E7B75C6AF4DED7E32AE1D6'   # True
Expand-Archive "$d\llama-cuda.zip" "$d\llama"
Expand-Archive "$d\llama-cudart.zip" "$d\llama" -Force     # -> $d\llama\llama-server.exe and its DLLs
Remove-Item "$d\llama-cuda.zip", "$d\llama-cudart.zip"
```

**Newer builds come by themselves:** this step (or the Quick install) is for the first install only. After it the
worker keeps llama.cpp and whisper.cpp at the builds its `worker.py` pins, with nothing to do on the PC (see
[Runtime updates](#runtime-updates-llamacpp-whispercpp)). Don't re-run the installer or this step to upgrade.

The zip has llama.cpp's other tools too; the worker runs only `llama-server.exe` (which loads
`llama-server-impl.dll` and the `ggml*.dll` beside it).

**The model is a choice in `worker.toml`, or the hub's:** three are pinned, and switching is config only (download
the other one, change three keys, restart the worker). The hub can also name one of them for a worker that runs its
own llama-server: the worker then downloads and switches by itself, and `worker.toml` stays as it is (see [The hub
names the LLM](#the-hub-names-the-llm)). Qwen3-8B is the default. Why these: the PR that added the LLM compares
Qwen3-8B and Gemma with Qwen2.5-7B and Llama-3.1-8B on synthetic dispatch traffic (`llm_eval/`). Qwen3-4B is the
small one: it fits beside every speech model on a 12 GB card (see [Speech models](#speech-models), VRAM). It isn't
the default yet: intel re-evaluates it first.

| | Qwen3-8B Q4_K_M (default) | Gemma-3-12B-it Q4_K_M | Qwen3-4B-Instruct-2507 Q4_K_M |
|---|---|---|---|
| repo, revision | `Qwen/Qwen3-8B-GGUF`, `7c41481f57cb95916b40956ab2f0b139b296d974` | `bartowski/google_gemma-3-12b-it-GGUF`, `648e3a36a77c8a9f12d86e741f9dcb9089c769c4` | `bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF`, `ae44f08e1392f39c0e474af10c3ff8355c8b6688` |
| file, bytes | `Qwen3-8B-Q4_K_M.gguf`, 5,027,783,488 | `google_gemma-3-12b-it-Q4_K_M.gguf`, 7,300,575,264 | `Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf`, 2,497,280,736 |
| SHA256 | `D98CDCBD03E17CE47681435B5150E34C1417F50B5C0019DD560E4882C5745785` | `FC57F67EFA46D711C346E587CBEF7D049E95F3DF8DB2EB2271153343EF0ACC7B` | `2FDE00CE69DD4899C70D020845E2638353015BBA0FDF161B3EB965F2BCA4464E` |
| license | Apache-2.0 | Gemma Terms of Use | Apache-2.0 |
| VRAM (4k context; the worker's estimate) | about 6 GB (6,179 MB) | about 8.6 GB (8,566 MB) | about 3.5 GB (3,522 MB) |
| `llm_model`, the catalog id the hub names | `qwen3-8b-q4_k_m` | `gemma-3-12b-it-q4_k_m` | `qwen3-4b-instruct-2507-q4_k_m` |
| `llm_server` | `-m models\Qwen3-8B-Q4_K_M.gguf ... --reasoning off` | `-m models\google_gemma-3-12b-it-Q4_K_M.gguf ...` (no `--reasoning off`: Qwen3's) | `-m models\Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf ... --reasoning off` |

```powershell
# Qwen3-8B (the default)
Invoke-WebRequest https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/7c41481f57cb95916b40956ab2f0b139b296d974/Qwen3-8B-Q4_K_M.gguf `
  -OutFile "$d\models\Qwen3-8B-Q4_K_M.gguf"
(Get-FileHash "$d\models\Qwen3-8B-Q4_K_M.gguf").Hash -eq 'D98CDCBD03E17CE47681435B5150E34C1417F50B5C0019DD560E4882C5745785'   # True
# Gemma-3-12B (the other choice)
Invoke-WebRequest https://huggingface.co/bartowski/google_gemma-3-12b-it-GGUF/resolve/648e3a36a77c8a9f12d86e741f9dcb9089c769c4/google_gemma-3-12b-it-Q4_K_M.gguf `
  -OutFile "$d\models\google_gemma-3-12b-it-Q4_K_M.gguf"
(Get-FileHash "$d\models\google_gemma-3-12b-it-Q4_K_M.gguf").Hash -eq 'FC57F67EFA46D711C346E587CBEF7D049E95F3DF8DB2EB2271153343EF0ACC7B'   # True
# Qwen3-4B-Instruct-2507 (the small one)
Invoke-WebRequest https://huggingface.co/bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF/resolve/ae44f08e1392f39c0e474af10c3ff8355c8b6688/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf `
  -OutFile "$d\models\Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
(Get-FileHash "$d\models\Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf").Hash -eq '2FDE00CE69DD4899C70D020845E2638353015BBA0FDF161B3EB965F2BCA4464E'   # True
```

To switch to Qwen3-4B, these three lines in `worker.toml` (the rest of `llm_server` stays as it is):

```ini
llm_model = "qwen3-4b-instruct-2507-q4_k_m"
llm_model_sha256 = "2fde00ce69dd4899c70d020845e2638353015bba0fdf161b3eb965f2bca4464e"
llm_server = ['llama\llama-server.exe', '-m', 'models\Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf', '--host', '127.0.0.1', '--port', '8180',
              '-c', '4096', '-ngl', '99', '-np', '1', '--jinja', '--no-webui', '--no-slots', '--reasoning', 'off']
```

To switch, set `llm_model`, `llm_model_sha256` (the SHA256 above, lower or upper case) and the `-m` file in
`llm_server` (Gemma: without `--reasoning off`), then restart the worker. It checks the new file's hash before it
starts llama-server, and the hub's Health shows the model and its version (the hash's first 12 digits) per worker.
Qwen3 is the smaller and the quicker (about 1.0-1.35 s against 1.5-1.9 s on a 3080 Ti); Gemma was the more accurate
in the comparison, and the only one that mended garbled numbers ("for tea" for 40), at about 2.3 GB more VRAM (see
the budget).

Try it (Ctrl+C to stop):

```powershell
& "$d\llama\llama-server.exe" -m "$d\models\Qwen3-8B-Q4_K_M.gguf" --host 127.0.0.1 --port 8180 -c 4096 -ngl 99 -np 1 --jinja --no-webui --no-slots --reasoning off
```

The output should list the GPU (`ggml_cuda_init: found 1 CUDA devices`) and end with `server is listening on
http://127.0.0.1:8180`. With it running, `py -3 "$d\worker.py" check` also tries a constrained answer
(`llama-server: ok (...)`). Then add the `llm_*` keys to `worker.toml` (step 5) and the worker runs it from then on.

### 5. The worker

For the first install, `worker.py` is pinned to a commit (354,646 bytes). Check its hash after the download. From then on it
[updates itself](#updating). A worker installed from an older pin (before the self-update) needs this download once
more to get it.

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/actualsecurity/sizeup/df457e8e1506503fcac2106b3d9bfa997491b696/deploy/stt-worker/worker.py -OutFile "$d\worker.py"
(Get-FileHash "$d\worker.py").Hash -eq '933A4CE5E4C9BE1B405C73C016205B687BC9D3922368983886777D62654F699C'   # True
notepad "$d\worker.toml"
```

In `worker.toml`, relative paths are from the folder `worker.toml` is in. The worker also runs whisper-server from
that folder. Use single quotes, so the backslashes stay as they are:

```toml
hub_url = "https://hub.example.org"          # the hub's address (the one the station uses)
model = "large-v3-turbo-q5_0"
# the worker starts whisper-server itself, restarts it when it exits, and knows its pid (its GPU use isn't a game)
whisper_server = ['whisper\Release\whisper-server.exe', '-m', 'models\ggml-large-v3-turbo-q5_0.bin',
                  '--host', '127.0.0.1', '--port', '8179', '-t', '4',
                  '-l', 'en', '-sns', '-et', '2.4', '-lpt', '-1.0', '-nth', '0.6']
keep_awake = true                            # no idle sleep while the worker runs (see "Keeping the PC awake")
# the local LLM (optional: leave these out for none; see "The local LLM" below)
llm_url = "http://127.0.0.1:8180"
llm_model = "qwen3-8b-q4_k_m"
llm_model_sha256 = "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"
llm_server = ['llama\llama-server.exe', '-m', 'models\Qwen3-8B-Q4_K_M.gguf', '--host', '127.0.0.1', '--port', '8180',
              '-c', '4096', '-ngl', '99', '-np', '1', '--jinja', '--no-webui', '--no-slots', '--reasoning', 'off']
```

### 6. The two tokens

1. **Worker token:** the admin creates it in **Settings > Station > STT workers > Add**. It looks like
   `sw1.<worker>.<secret>` and is shown only once. Rotate keeps the old one working for 10 minutes. Revoke stops it
   at once.
2. **Cloudflare Access service token, for the worker only:** in Zero Trust, set up a separate path for the worker:
   - Add a self-hosted Access application for the hub's hostname, with the path `/api/station/stt/*` only.
   - Give it a Service Auth policy that allows only a new service token for this worker (Access > Service Auth >
     Service Tokens > Create).

   Don't reuse the station's `/api/station/*` application or token. That way the worker's token reaches nothing of
   the station's, and you can revoke it on its own.

Store the tokens as user environment variables. Nothing is echoed and nothing goes into the PowerShell history:

```powershell
function Set-Secret($name) {
  $s = Read-Host $name -AsSecureString
  [Environment]::SetEnvironmentVariable($name, [Net.NetworkCredential]::new('', $s).Password, 'User')
}
Set-Secret SIZEUP_STT_WORKER_TOKEN
Set-Secret SIZEUP_STT_CF_CLIENT_ID
Set-Secret SIZEUP_STT_CF_CLIENT_SECRET
```

Open a new PowerShell window so it sees them. Sign out and back in before the task in step 8 runs. The worker passes
none of them on to whisper-server or `nvidia-smi`.

### 7. Check

With whisper-server from step 4 running:

```powershell
py -3 "$d\worker.py" check
```

It prints `whisper-server: ok (...)`, `hub: ok (worker ...)` and the GPU. If something fails, see
[Troubleshooting](#troubleshooting).

### 8. Run it at logon (Task Scheduler)

Run this in an ordinary (not elevated) PowerShell window, signed in as the user who games on the PC. The task runs as
that user, with an interactive logon and no admin rights (`-RunLevel Limited`):

```powershell
$d   = "$env:LOCALAPPDATA\sizeup-stt"
$me  = "$env:USERDOMAIN\$env:USERNAME"
$py  = py -3 -c "import sys, pathlib; print(pathlib.Path(sys.executable).with_name('pythonw.exe'))"   # no console window
$act = New-ScheduledTaskAction -Execute $py -Argument "`"$d\worker.py`" run -c `"$d\worker.toml`"" -WorkingDirectory $d
$trg = @(
  New-ScheduledTaskTrigger -AtLogOn -User $me
  New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)                                                 # starts it again if it died
)
$set = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
         -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$who = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'sizeup-stt-worker' -Action $act -Trigger $trg -Settings $set -Principal $who
Start-ScheduledTask -TaskName 'sizeup-stt-worker'
(Get-ScheduledTask sizeup-stt-worker).Triggers[1].Repetition      # Interval PT5M, Duration P3650D
```

If Windows refuses to register it (`Access is denied`), run the same commands from an elevated window. The task
still runs as you, `Limited`.

How the task behaves:

- **Errors:** the worker itself never exits on hub or whisper-server errors, or on a bug of its own. It logs the
  error to `worker.log`, waits (1, 2, 5, 10, 30 s) and carries on. It also restarts whisper-server.
- **If the process dies anyway:** Task Scheduler's restart-on-failure doesn't always fire for a process that exits.
  The 5-minute trigger starts the worker again in that case. While it runs, `IgnoreNew` skips those starts.
- **One copy at a time:** the worker holds a lock on `state\worker.lock` and refuses to run a second copy. Windows
  lets go of the lock when the process ends, even when `schtasks /End` kills it, so the next start isn't blocked.
- **No time limit:** `ExecutionTimeLimit` zero turns off Task Scheduler's 72-hour default.
- **Self-update:** after it swaps in a new `worker.py` the worker exits with code 75 (Last Run Result `0x4B`). The
  5-minute trigger starts the new file. The worker doesn't start a copy of itself, because `schtasks /End` couldn't
  stop a process outside the task.
- **No schtasks:** `schtasks /Create` can't set any of this, so use the PowerShell commands above to create the task.

To manage it:

```powershell
schtasks /Query /TN sizeup-stt-worker /V /FO LIST
schtasks /End /TN sizeup-stt-worker
schtasks /Run /TN sizeup-stt-worker
Unregister-ScheduledTask -TaskName sizeup-stt-worker -Confirm:$false
```

**whisper-server as a task of its own (optional):** leave `whisper_server` out of `worker.toml` and register a
second task. The worker still finds whisper-server's pid by its name (`whisper_process`). This task shows a console
window.

```powershell
$wact = New-ScheduledTaskAction -Execute "$d\whisper\Release\whisper-server.exe" -WorkingDirectory $d `
          -Argument "-m `"$d\models\ggml-large-v3-turbo-q5_0.bin`" --host 127.0.0.1 --port 8179 -t 4"
Register-ScheduledTask -TaskName 'sizeup-whisper-server' -Action $wact -Trigger $trg -Settings $set -Principal $who
```

### Updating

After the first install the worker updates itself. The hub tells it which `worker.py` to run. By default that is the
file in the hub's own build, so a change merged into `worker.py` reaches the PC once the hub has deployed it.

1. **The hub names a version:** a commit and the file's SHA-256, in every hello and heartbeat answer. In
   **Settings > Station > STT workers**, each worker has **Auto** (the hub's build, the default), **Hold** (no
   updates) or **Pin** (a merge commit on main and its `worker.py` SHA-256).
2. **Only main, only that hash:** GitHub's API must say the commit is on `main` of `actualsecurity/sizeup` and is
   the merge commit of a pull request merged into `main`, as the hub's builds are. A commit from inside a merged
   branch, or from a fork, is refused. Then the worker downloads `deploy/stt-worker/worker.py` at that commit from
   `raw.githubusercontent.com` (2 MB at most), or `tools/stt_worker/worker.py` when GitHub has no file at the new
   path (a commit from before the move). It refuses the file unless its SHA-256 is exactly the hub's. The repo,
   branch, paths and hosts are written in `worker.py`, so the hub can't send the worker anywhere else, and the hub's
   `url` is never used.
3. **Self-check:** the file is saved as `worker.py.new`, and `worker.py.new selftest` must pass. The self-check
   imports the file, reads `worker.toml` and builds a worker, with no network.
4. **Only when idle:** the swap waits until the worker has no clip or LLM job, isn't gaming and hasn't gamed for 2
   minutes. Then the worker takes no new jobs, copies `worker.py` to `worker.py.prev`, renames `worker.py.new` to
   `worker.py` and exits with code 75. Task Scheduler's 5-minute trigger (step 8) starts the new file, so the worker
   can be gone for up to 5 minutes. The hub sends its clips elsewhere meanwhile.
5. **Rollback:** the new file must reach the hub within 10 minutes of running (time asleep doesn't count), and within 3
   starts. Otherwise `worker.py.prev` is copied back, the new hash is marked bad, and Health shows `rolled back`. A
   network outage looks the same as a broken file, so a hash marked bad this way is tried again after 6 hours. A hash
   you roll back by hand is never tried again until `update retry`.

The worker asks GitHub at most every 10 minutes, and less often after errors or when GitHub's rate limit says so
(GitHub allows 60 requests an hour without a token). A file with the wrong hash or over 2 MB is never tried again; any
other refusal (not on main yet, a self-check that timed out) is tried again after an hour. `worker.py update` prints
the state.

**The move to `deploy/stt-worker`:** a worker installed before the move knows only `tools/stt_worker/worker.py`. That
file stays a byte-identical copy of this one for a while (a test checks), so such a worker finds the hub's hash there,
switches, and from then on downloads from `deploy/stt-worker/worker.py`. Nothing to do on the PC. The copy goes once
Health shows every worker on a version from the move on.

The rollback runs in the new file, so it protects against a new version that fails at run time, not against one
whose own start-up or rollback code is broken. The self-check runs that start-up (logging, the lock, the update
record) in a scratch folder before the swap.

```powershell
py -3 "$d\worker.py" update               # what runs, what is staged, the last update or rollback, hashes marked bad
py -3 "$d\worker.py" update rollback      # worker.py.prev back; the version you left is marked bad
py -3 "$d\worker.py" update retry         # the hashes marked bad may be tried again
```

- **Turn it off:** set `auto_update = false` in `worker.toml` and restart the task, or choose **Hold** in Settings.
- **Roll back by hand:** run `update rollback`, then `schtasks /End` and `schtasks /Run` the task. The version you
  left is marked bad, so the worker doesn't fetch it again. **Hold** keeps it from taking a newer one.
- **Update by hand** (with `auto_update = false`): download `worker.py` from the commit this README names and check its
  hash (step 5), then `schtasks /End` and `schtasks /Run` the task.

### Runtime updates (llama.cpp, whisper.cpp)

The programs the worker's servers run (`llama-server.exe` and `whisper-server.exe`) update themselves too: no
installer run, nothing to do on the PC. `worker.py` pins one llama.cpp build and one whisper.cpp build (release tag,
asset URLs, sizes and SHA-256s: the same as [`pins.json`](pins.json), a test checks), so a new build reaches the PC as
a new `worker.py`, through the self-update's checks above.

1. **Download:** when the build that runs isn't the pinned one, the worker downloads the pinned zips from the
   ggml-org GitHub releases only (the same hosts and checks as the speech models: exact size and SHA-256). It checks
   the free disk space first, never downloads while gaming or paused, and resumes a stopped download where it left
   off.
2. **Unpack beside the running build:** into `runtimes\llama\b12728\` (or `runtimes\whisper\b5454\`) next to
   `worker.toml`, never over the folder in use. A zip with a path outside its folder, or a link, is refused whole.
3. **Switch only when idle:** no clip, no LLM job, not gaming or paused, no gaming for 2 minutes, and no other switch
   (one lock for every switch: a runtime, the LLM's model, the self-update). The worker takes no new jobs
   (`paused: updating`), stops the servers that run that program and starts them from the new folder.
   `worker.toml` keeps its paths (`llama\llama-server.exe`, `whisper\Release\whisper-server.exe`): the worker runs
   the active build's program in their place.
4. **Test:** whisper-server must decode a second of silence. For llama.cpp, `--version` must name the build, and
   llama-server must give a 1-token completion on its own 1.2 MB test model (`stories260K.gguf`, pinned in
   `worker.py`, in `runtimes\test\`), whatever the LLM and the speech models are doing. When the LLM runs, it must
   also give its 1-token constrained answer. Then every wanted speech model of that kind whose files are there must
   decode a second of silence; one that doesn't fit beside what is loaded (the LLM is never stopped for it), or
   runs out of VRAM, is left untested and covered by the probation below. Each part has 3 minutes. A whisper.cpp build is downloaded only when `worker.toml` has
   its own `whisper_server` to test it with.
5. **Rollback:** any failure makes the build before active again (its folder is kept), restarts it and checks it.
   While gaming or paused it only stops llama-server and the speech models' servers (whisper-server keeps running
   as it always does while gaming, until `gaming_stop_whisper_min`); the worker starts them again once the PC is
   free. The new
   build is tried again after 30 minutes, then 60; after 3 failures in a row it is marked bad for a day. Gaming
   during the test doesn't count as a failure. A worker stopped mid-switch goes back to the build before at its next
   start. For 30 minutes after a switch, a speech model of that kind that fails to load rolls the build back (a
   failure of the build, not of the model). Such failures add up across passing tests (30, then 60 minutes, then
   bad for a day) until the build passes a whole probation.
6. **Prune:** only the running build and the one before it stay in `runtimes\`. The installer's own `llama\` and
   `whisper\` folders are never deleted.

Health shows each worker's builds (`Runtimes`: `whisper b5454 · llama b12728`) with the update's state in the hover:
`ok`, `downloading b12728`, `b12728: waiting (gaming)`, `b12728 ready, waiting: a clip`, `switching to b12728`,
`rolled back: <why> (again in 30 min)`, `bad: <why>`. A program outside the installer's layout (your own build) is
left alone (`not managed (own path)`), and so is everything with `auto_update = false`. `worker.py update retry`
also clears the builds marked bad.

**Linux (`systemd --user`):** the same `worker.py` updates itself the same way. Run it as a user service with
`Restart=always` (for example `RestartSec=5`): systemd starts the new file right after the exit with code 75. Keep
`worker.py` and `worker.toml` in a folder the user owns, such as `~/.local/share/sizeup-stt`. The swap is a rename in
that folder and keeps the file's mode. The state stays in `~/.local/state/sizeup-stt-worker`.

## How it works with the hub

- **One job at a time:** the worker holds one lease at a time; the hub allows 2.
- **Leases:** it extends a lease at half its length while whisper-server decodes.
- **Pre-alert classes:** urgent, high, context-fallback and context have a hard deadline: a 4 s first lease,
  renewable up to 8 s from the claim. Past that the hub gives the clip to the next engine. The worker then drops its
  result and stops the decoding.
- **Decode:** every `/inference` request carries `language=en`, `suppress_nst=true`, `entropy_thold=2.4`,
  `logprob_thold=-1.0` and `no_speech_thold=0.6` (whisper.cpp reads each per request, so they hold whatever
  `whisper_server` says), and the temperature fallback: `temperature_inc=0.2`, or `0.4` for a pre-alert clip (at most
  two more passes, so it fits the lease). The hub sends no prompt for the airband.
- **Heartbeat:** every 5 s, also during a decode. The hub takes back the leases of a worker it hasn't heard from for
  12 s.
- **Gaming or pause:** the hub takes back the worker's pre-alert jobs itself when the state changes. A later complete,
  extend or release on them gets 409, which the worker treats as a lost lease.
- **Version:** every hello and heartbeat carries `self_update` (the `worker.py` SHA-256, its commit when known,
  `auto_update`, the update's state, the hashes marked bad). The answer carries `worker_update` (`mode`, `commit`,
  `sha256`): the version the hub wants it on ([Updating](#updating)). Health shows each worker's version, green when it
  is the hub's.
- **Keep awake:** on Windows, every hello and heartbeat carries `keep_awake` (`on`: the config key, `held`: no idle
  sleep now). A hub from before ignores it.
- **LLM jobs** (with `llm_url`): claimed on a thread and hub connection of their own (`/api/station/stt/infer/*`,
  the same tokens), one at a time, never while a pre-alert clip is held. The worker tells the hub its models with every
  heartbeat (`models: {stt, llm, llm_version}`); `llm` is empty while llama-server isn't answering, and the hub sends
  it no LLM job then. A hub without the infer lane answers 404: the worker asks again every 10 minutes.
- **Models the hub names:** `stt_models {catalog, want, try, llm, llm_priority}` in every hello and state answer
  ([Speech models](#speech-models), [The hub names the LLM](#the-hub-names-the-llm)). To such a hub, `models` also
  carries `stt_loaded`, `stt_try` (the try ids whose try server answers now), `stt_state`, `stt_catalog` and, with an LLM, `llm_state` (`ok` or why not) and `llm_id` (the
  catalog LLM in use, `""` for `worker.toml`'s).
- **Challenger jobs** (`challenger: true`, `greedy: true`): a clip for a try model, decoded only on that model's try
  server (`model not loaded` otherwise), never on a server that serves live traffic (`whisper_server`, a planned
  model's: whisper-server can't cut a decode short), and never without a model (`model serves live traffic`). The
  claim body says which try servers answer (`try_loaded`), and `caps` is `["challenger", "challenger_same_model"]`
  in hello, state and claim. greedily (`temperature_inc=0`). It gives way to
  the LLM (`busy: LLM`: an LLM job held or arriving) and never preempts it. While it decodes, the worker long-polls
  `POST /api/station/stt/jobs/{lease}/watch` `{"wait": 20}` on a connection of its own: `{"live": true}` after the
  wait; `{"live": false}`, 409 or 410 once the hub has taken the lease back, and the worker stops the decode at once
  and sends nothing more for it (no complete, no release), then claims again. A hub without the route (404): the
  worker extends every 0.5 s instead (409/410 there: taken back). Gaming or a pause hands it back at once.

## The local LLM (optional)

The hub's **infer lane** (`sizeup/cloud/infer.py`) asks a worker's local model for short JSON classifications of
radio traffic for the intel engine. The model runs here, on the gaming PC's GPU, in llama.cpp's `llama-server`, which
the worker starts, checks and stops. Nothing goes to a cloud model.

- **Clips first:** the hub gives the worker an LLM job only while it holds no pre-alert clip. A pre-alert clip that
  comes during a generation makes the worker abort the generation (it closes the connection, so llama-server stops)
  and hand the job back; the hub gives it out again within its deadline (about 15 s).
- **Constrained output:** each job carries a JSON schema; the worker sends it as `response_format: json_schema`,
  which llama-server turns into a grammar, so the answer is that JSON or nothing. The worker and the hub check it
  again; an answer that doesn't match is dropped (the intel engine gets None).
- **Privacy:** prompts and answers go only between the hub, the worker and llama-server on 127.0.0.1. The worker
  never logs them (job id, task, times and token counts only). llama-server runs without verbose logs and without its
  slots endpoint (the worker refuses `-v` and `--slots`).
- **Its own key:** llama-server answers any web page by default. The worker starts it with a random API key each time
  (in its environment, not on the command line), so no page in a browser on the PC can use it. `--no-webui` turns off
  its web page.
- **Only while online:** LLM jobs come while the worker is online (whisper-server answering, not gaming or paused).
- **Qwen3 without thinking:** `--reasoning off` (and the worker's `enable_thinking: false`). With thinking on, Qwen3
  spends its tokens thinking and returns an empty answer.
- **Pinned:** the worker hashes the GGUF before llama-server's first start and refuses one that isn't
  `llm_model_sha256` (the hash is remembered in `llm-model.json` until the file changes; before each restart the
  worker looks at the file's size and time again, and hashes it again when they changed).
- **No orphans:** on Windows the servers the worker starts join a job object that ends them when the worker ends,
  however it ends. Their pids are also kept in `children.json`: the next start ends one still running.

### The hub names the LLM

For a worker that runs its own llama-server (`llm_server`), the hub can name its LLM: `stt_models` `llm`, an id of
[`models.json`](models.json) `llm_models` (the three GGUFs above, each with its URL, size, SHA-256, `vram_mb` and
whether it needs `--reasoning off`). Nothing in `worker.toml` changes.

- **Same file as `worker.toml`'s** (by SHA-256: the 8B today): nothing downloaded, no restart; the hub sees the id.
- **Another one:** its GGUF is downloaded to `models\managed` through the same path as the speech models and the
  [runtime builds](#runtime-updates-llamacpp-whispercpp) (allowed hosts, exact size and SHA-256, a disk-space check,
  resumed, never while gaming or paused). Then, with no LLM job in hand or being claimed, not gaming, and no
  other switch under way (the one lock every switch takes), llama-server is stopped and started again with that `-m` and its `--reasoning` (every
  other option as `llm_server` says), its hash checked before its first start. It gets LLM jobs only after the same
  1-token test answer a new llama.cpp build passes. Health shows its id and hash; its `vram_mb` is the LLM's share
  in the VRAM plan.
- **Nothing named, `""`, an id the catalog doesn't have, or an older hub:** `worker.toml`'s LLM, as before.
- **A failure** (download, hash, an exit, no answer within 3 minutes, a failed test answer): `worker.toml`'s LLM again, the named one tried
  again after 5 minutes, then 10, 20, 30. Out of VRAM three times in a row: parked `no room (VRAM)` until the budget,
  the hub's models or the free VRAM change. Any other failure the third time in a row: marked bad for 6 hours.
- **Health:** `llm_state` is `ok`, or why not: `downloading`, `failed: out of VRAM (again in 5 min)`, `no room
  (VRAM)`, `model hash mismatch`, `unavailable: gave its VRAM to the speech models`. `llm` says which LLM answers
  meanwhile. The choice is kept in `state\models.json`: after a restart the worker starts the named one directly.

### VRAM budget (12 GB, RTX 3080 Ti)

| what | VRAM |
|---|---|
| Windows desktop, browser | 0.5-1 GB |
| whisper-server, large-v3-turbo q5_0 | about 1 GB |
| llama-server, Qwen3-8B Q4_K_M: weights 4.8 GB, KV cache at `-c 4096` 0.6 GB, buffers | about 6 GB |
| **together** | **7.5-8 GB of 12 GB** (about 4 GB free) |

`-np 1` (one slot: the worker sends one job at a time) keeps the KV cache to one context; `-c 4096` covers the system
prompt, the transmission and the answer with room to spare (`-c 2048` saves about 0.3 GB).

**With Gemma-3-12B instead:** about 8.3 GB for llama-server, plus whisper turbo (about 1 GB) and the Windows reserve,
comes to about 10.5-11 GB of 12. It fits, but it leaves no room for a second speech-to-text model next to them (a
challenger being compared with whisper turbo): with Gemma, a challenger has to run swap-per-job (loaded for its job,
unloaded after), never resident. With Qwen3 there are about 4 GB free.

A game needs the GPU more than either: the worker gives it all back (below).

### Gaming and the LLM

When gaming starts, the worker aborts a generation at once and hands the job back, as it does a clip. After
`llm_stop_gaming_s` of gaming (60 s for Qwen3-8B, 20 s for the bigger Gemma) it stops its llama-server, which gives
about 6 GB (Gemma: 8 GB) of VRAM back to the game
(whisper-server keeps its own rule: 10 minutes). When gaming ends, it starts llama-server again; the model takes a few
seconds to load, and the hub gets LLM jobs from the worker again once its health check passes. llama-server's own GPU
memory never counts as "another process" in the gaming check.

## Speech models

Besides the whisper-server in `worker.toml` (the **primary**), the worker can run more speech-to-text models, one server
each on its own loopback port (8181, 8182, ...), so each radio channel can have its own model (Settings > Radios, on
the hub). The models are listed in [`models.json`](models.json) (`stt_models`; apart from `pins.json`, the installer's,
so a model change never re-pins the installer): each one's files, their pinned URL, size
and SHA-256, the VRAM it holds, its services, and whether the channel's context goes with each clip by default.

| model | kind | VRAM | for |
|---|---|---|---|
| `large-v3-turbo-q5_0` | whisper-ggml | about 1 GB | today's model: the primary, and the control |
| `large-v3-turbo-q8_0` | whisper-ggml | about 1.4 GB | the same, less quantised (digits) |
| `qwen3-asr-1.7b-q8_0` | llama-asr (Qwen3-ASR, GGUF + mmproj) | about 3.8 GB | fire/EMS and police |
| `large-v3-turbo-us-atc-v3-f16` | whisper-ggml | about 2.3 GB | airband (no prompt by default) |

- **Two runtimes:** a `whisper-ggml` model runs in whisper-server (`whisper_bin`, default `whisper_server`'s
  program); a `llama-asr` model runs in llama-server (`llama_bin`, default `llm_server`'s program) with its audio
  projector (`--mmproj`), b12643 or later (the worker asks `llama-server --version` and says so in Health if it is
  older). Each llama-server gets its own random API key, as the LLM's does.
- **English, always:** every model is told the language, never left to detect one: whisper-server runs with `-l en`
  and each request says `language=en`; Qwen3-ASR's answer is begun `language English<asr_text>` (its own way to force
  a language). A catalog entry without `en` in `langs` is refused.
- **What the hub names:** every hello and state answer carries `stt_models {catalog: {commit, sha256}, want}`: the
  commit on main the hub was built from, the SHA-256 of `models.json` in it, and the models the channels use, most
  important first (fire/EMS, then police, then airband). Also `try` (challengers, below) and `llm` ([The hub names the
  LLM](#the-hub-names-the-llm)); `models.json`'s `llm_models` sit beside `stt_models`, which an older worker reads
  alone.
- **Where the list comes from:** the worker fetches `deploy/stt-worker/models.json` at that commit, only after GitHub's
  API says it is main's own (the same check as the [self-update](#updating)), and only with exactly that hash. It keeps
  it in `state\catalog.json`. One entry that breaks a rule (a URL not allowed, a bad hash, a missing file) refuses
  the whole list.
- **Where files come from:** `huggingface.co/<org>/<repo>/resolve/<40-digit revision>/...`, a `ggml-org` release
  on GitHub, or a release asset of this repo; redirects only to their CDNs (`*.hf.co`,
  `release-assets.githubusercontent.com`, `objects.githubusercontent.com`). These rules are written in `worker.py`:
  the hub can't send the worker anywhere else. Each file is downloaded to `models\managed\<hash>-<file>.part`
  (resumed after a stop), must be exactly its pinned size and SHA-256, then is renamed. Files in `models\managed`
  that no model in the list uses are deleted; nothing else in `models` is touched.
- **Built once, hosted here:** the airband model is a LoRA merged into whisper large-v3-turbo. Merging needs torch
  and peft, which the worker doesn't have, so it was built once by the recipe in `models.json` (pinned inputs and their
  hashes, [`recipes/merge_lora.py`](recipes/merge_lora.py), whisper.cpp's `convert-h5-to-ggml.py` at a pinned commit,
  the package versions; two runs from scratch gave the same file) and is a release asset of this repo (`stt-models-1`).
- **VRAM:** the budget is the GPU's total less `vram_reserve_mb` (1.5 GB for Windows and the desktop), or
  `vram_budget_mb`. The primary comes first, then the wanted models in the hub's order while each fits. A model that
  can't start on this PC (no program for it, or a llama-server older than it needs) isn't planned at all: nothing is
  downloaded or stopped for it, and Health says why.
- **The local LLM, `llm_priority`:** `yield` (the default: speech first). The LLM gets what the speech models leave.
  Once a model that needs its room can start now (files downloaded and checked, no retry pending), the worker stops
  llama-server between its jobs. Health shows `llm: gave its VRAM to the speech models`, the hub hears no LLM (intel
  falls back to its rules), and llama-server starts again once there is room. `keep`: the LLM's share comes off the
  budget first; a model that fits only without it isn't loaded, Health shows it `unavailable: no room beside the
  LLM`, and the hub keeps that channel on its champion or route. The hub can set it for its workers (`stt_models`
  `llm_priority`); worker.toml's is used otherwise.
- **12 GB (RTX 3080 Ti), the worker's own numbers:** 12,288 MB less the 1,536 MB reserve is **10,752 MB**. Speech models
  (catalog `vram_mb`): turbo q5_0 1,000, turbo q8_0 1,400, Qwen3-ASR 3,800, the airband model 2,300. The LLM (its
  GGUF's size x 1.05 + 900 MB; the 900 MB is the KV cache at `-c 4096`, 576 MiB for both Qwen3s (36 layers, 8 KV
  heads of 128, f16), and llama-server's buffers): Qwen3-8B Q4_K_M 6,179 MB, Qwen3-4B-Instruct-2507 Q4_K_M 3,522 MB.

  | primary + speech models | speech | + Qwen3-4B (3,522) | + Qwen3-8B (6,179) |
  |---|---|---|---|
  | q5_0 + Qwen3-ASR + airband | 7,100 | 10,622: fits (130 left) | 13,279: no |
  | q5_0 + Qwen3-ASR | 4,800 | 8,322: fits (2,430 left) | 10,979: no (227 over) |
  | q5_0 + airband | 3,300 | 6,822: fits (3,930 left) | 9,479: fits (1,273 left) |
  | q8_0 + Qwen3-ASR + airband | 7,500 | 11,022: no (270 over) | 13,679: no |
  | q8_0 + Qwen3-ASR | 5,200 | 8,722: fits (2,030 left) | 11,379: no |
  | q8_0 + airband | 3,700 | 7,222: fits (3,530 left) | 9,879: fits (873 left) |

  So with Qwen3-8B, Qwen3-ASR alone doesn't fit beside the LLM on 12 GB: under `yield` the LLM stops for it. With
  Qwen3-4B every speech model fits beside it with the q5_0 primary, only just (130 MB): with the q8_0 primary and all
  three, the LLM yields.
- **Only when idle:** downloads wait while gaming or paused (a stopped download resumes from its `.part`). A server
  starts or stops only while the worker holds no clip, one at a time; a server a clip is being decoded on is never
  stopped by the models thread. A new server must answer a test decode (a second of silence) within 3 minutes.
- **A server that fails to load:** out of VRAM or on a port another program holds (its own log says which) is never
  the model's fault: it is tried again in 5 minutes, then 10, 20, 30 (that port left alone for 30 minutes). Out of
  VRAM three times in a row: parked `no room (VRAM)`, not planned (so the LLM never stops for it) until the budget,
  the hub's models or `llm_priority` change, or the GPU has 1 GB more free than then (#940). Any other
  exit, or no answer, is tried again in 5 minutes, then 10; the third time in a row with the same hash, it is marked
  bad for 6 hours (`state\models.json`). A new version of a model that runs is downloaded beside it; once it is
  ready it takes the old one's place, and the old files are kept: while the new one waits to be tried again, and
  once it is marked bad, the old version runs. A ready server that exits is started again later (not marked bad).
- **Ports:** 8181 on, never `whisper_url`'s or `llm_url`'s; one another program holds is skipped. Each model's server
  output is in `state\model-<id>.log`. These servers listen on 127.0.0.1 only; whisper-server answers any web page
  there, as the primary does (each llama-server has its own key).
- **Gaming:** the LLM's rule, not whisper-server's longer one: after `llm_stop_gaming_s` of gaming (unset: 60 s, 20 s
  when they hold more than 6 GB together) the worker stops them all, and starts them again when gaming ends. The
  primary whisper-server keeps `gaming_stop_whisper_min` (10 minutes).
- **Try (challenger) models:** `stt_models` `try`, ids of the catalog. One the worker also serves live (its primary, or
  one in `want`) runs as a separate try instance of the same files on its own port, never on the live server; its
  `stt_state` stays the live model's (`stt_try` says when the instance answers). One at a time, loaded on
  demand only while the worker is idle (no clip, not gaming or paused) with every planned model settled, and only
  into what the planned models and the LLM's share leave (the LLM's share counted whenever it may run; once it has
  given way to the planned models, what they leave). A try model never counts in the plan, never takes the LLM's
  room from it, never stops a planned model. It is unloaded, while idle and never with a clip on it, when no longer
  in `try`, unused 10 minutes, or its room is needed: a planned model to start, or the LLM coming back (llama-server
  waits until it is out). Meanwhile it takes no new clip and its challenger clip ends at once. `stt_state` says
  `try: loading`, `try: ready`, `try: no room`, `try: unloaded (idle)`, `try: unloaded (preempted)`; `stt_try` (and
  `stt_loaded`, for a try model of its own) lists it only while it answers.
- **A clip:** the hub names the model it wants for each clip. The worker decodes it there if that model answers, else
  on the primary, and says which model wrote the text. Context: the clip's `prompt` (whisper) or `context` (a short
  list: a llama-asr model's system message), unless the hub says it is off for that model (`plain`).
- **A llama-asr request:** `/v1/chat/completions` with the context list as the system message (a short comma list of
  spoken forms), the WAV as an `input_audio` part and the answer begun `language English<asr_text>`, temperature 0.
  On llama.cpp b12728 (checked on a real server with synthetic speech) the transcript comes as the content and
  `language English` as `reasoning_content`; if a build ever leaves the content empty with the transcript after
  `<asr_text>` in `reasoning_content`, the worker takes it from there. Not `/v1/audio/transcriptions`: its prompt
  replaces the model's instruction and leaks into the text.
- **Silence:** a clip whose speech level is under `gate_dbfs` (-55 dBFS) isn't decoded at all (every model makes words
  up on silence; Qwen3-ASR, told English, answered a stray "The." on digital silence): the hub gets no text, which it
  reads as no speech. This holds for the primary whisper-server's clips too.
- `status` shows the models' states; Health on the hub shows each worker's.

## Gaming mode

When gaming or paused, the worker takes no new jobs. Its heartbeat goes on and the hub sends the clips elsewhere. If
gaming or a pause starts during a job, the worker stops whisper-server's decoding and hands the job back to the hub at
once. It finishes the job only when it is a short one whose lease ends within 3 s (`finish_s`).

**Automatic:** every 5 s the worker checks for any of these:

- Windows reports a full-screen game or app (the shell's notification state)
- a process other than our whisper-server or llama-server uses more than 1 GB of GPU memory (`nvidia-smi`)
- the GPU is more than 40 % busy while the worker is idle

Two checks in a row start gaming. Gaming ends after 60 s of quiet checks.

- **Our own load:** a check taken while the worker is decoding or generating (or just did) neither counts nor resets.
- **Windows:** per-process memory often shows as `[N/A]`, so the full-screen check and the load check do the work
  there.

While gaming, whisper-server is only checked for being there. It gets no test decoding and isn't restarted. After 10
minutes of gaming (`gaming_stop_whisper_min`), the worker stops the whisper-server it started, which gives its VRAM
back to the game. It starts it again when gaming ends.

**Manual:** these commands write `control.json`, which the running worker watches. The setting survives restarts.

```powershell
py -3 "$d\worker.py" gaming on      # / gaming off
py -3 "$d\worker.py" pause          # / resume (clears gaming and pause)
py -3 "$d\worker.py" status
```

For one-click toggles, make desktop shortcuts to `pythonw.exe "%LOCALAPPDATA%\sizeup-stt\worker.py" gaming on` (and
`off`). There is no tray icon or hotkey: the standard library has none on Windows.

## Keeping the PC awake

With `keep_awake = true` (the default), Windows doesn't idle-sleep the PC while the worker runs, so the pool doesn't
lose it overnight. A worker that has updated itself to this version does it with no step on the PC.

- **How:** `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` from a thread of the worker's own. No
  administrator rights and no `powercfg` change; nothing outlives the worker.
- **The screen** still turns off on its timer (never `ES_DISPLAY_REQUIRED`). Sleep from the Start menu, the lid and
  the power button still work.
- **Released** on `worker.py pause`, a stop or the exit, and when the worker crashes or is killed (Windows drops the
  flag with its thread). `worker.py resume` takes it again. Gaming, a self-update and whisper-server trouble don't
  change it. A stop (a self-update's restart too) first resets the idle timer, so the PC gets a whole sleep timeout
  before it may sleep: time for the task to start the worker again.
- **Modern Standby PCs** (`powercfg /a` says S0 Low Power Idle): check that `powercfg /requests` (an administrator
  prompt) lists the worker's `python`/`pythonw` under `SYSTEM` while it runs.
- **Off:** `keep_awake = false` in `worker.toml`, then restart the task. A `worker.py` from before this key refuses a
  `worker.toml` that has it: take the line out before an `update rollback` to such a version or a **Pin** to one.
- **Seen:** `worker.py status` (`keep awake: on, held`) and Health: the worker's state says `keeps PC awake`. A worker
  gone silent shows `offline since HH:MM`, with `(asleep?)` when its last heartbeat said keep-awake off or released,
  or its heartbeats just stopped while online.
- Not Windows: nothing (use the OS's own settings).

## Files

These are in `%LOCALAPPDATA%\sizeup-stt\state` (`state_dir`):

| file | what |
|---|---|
| `control.json` | manual gaming and pause (written by the CLI) |
| `status.json` | the running worker: state, hub link, job counts, GPU (read by `status`) |
| `worker.log` | the worker's log, errors included (1 MB, 3 old copies) |
| `whisper-server.log` | whisper-server's output, when the worker starts it (1 MB, 2 old copies) |
| `llama-server.log` | llama-server's output, when the worker starts it (1 MB, 2 old copies; token counts, never prompts) |
| `llm-model.json` | the GGUF's checked SHA-256, by path, size and time (so a restart doesn't hash 5 GB again) |
| `llm-key` | the API key llama-server was last started with (only you can read it; `check` uses it) |
| `children.json` | the pids of the servers the worker started: the next start ends one a worker that died left running |
| `update.json` | the self-update: hashes marked bad, the swap not confirmed yet, the last update or rollback |
| `runtimes.json` | llama.cpp's and whisper.cpp's active build, the one before, a switch not finished yet, failures and builds marked bad |
| `logspool\` | `worker.log`'s lines waiting for the hub, gzipped batches (5 MB at most, the oldest deleted first) |

Next to `worker.py`: `worker.py.prev` (the version before the last self-update, for a rollback) and, for a moment,
`worker.py.new` (the version being checked); `runtimes\` (llama.cpp's and whisper.cpp's builds the worker installed
itself, two of each at most, and their downloads in progress).

## Logs (CloudWatch)

The worker sends `worker.log`'s lines to the hub, which keeps them in CloudWatch Logs in the department's own AWS
account, like the stations' logs. They go the way the worker already talks to the hub: `POST /api/station/stt/logs`,
the worker token and its Cloudflare service token, the same Access application (`/api/station/stt/*`).

- **What:** the lines `worker.log` has (info and up), and nothing else: never whisper-server's or llama-server's
  output, so never a transcript, a prompt or an LLM answer. Every known secret and anything shaped like a token is
  redacted on the PC, and the hub scrubs every line again before it writes it.
- **Where:** log group `/sizeup/stt-worker`, one stream per worker id. Kept as long as the stations' logs
  (Settings > Station > Logs, 30 days by default). The hub makes the group the first time a worker sends logs.
- **Reading:** `/sizeup/stt-worker` is an Infrequent Access log group: read it with CloudWatch Logs Insights only
  (`aws logs start-query`, then `aws logs get-query-results`), never `aws logs tail` (not supported on that class).
- **How:** a gzipped batch every minute, through its own thread and connection, so a clip never waits for it. A batch
  is deleted on the PC only once the hub has taken it; until then it waits in `state\logspool` (5 MB at most) and is
  sent again, after a restart too. `worker.py status` shows the last shipment; so does Health (the Logs column).
- **Off:** `log_ship = false` in `worker.toml`. A `worker.py` from before this key refuses a `worker.toml` that has
  it: take the line out before an `update rollback` to such a version or a **Pin** to one.

**Admin, once (AWS):** run `bash 31-stt-worker-logs.sh` in sizeup-aws (step 31). It makes `/sizeup/stt-worker`
(Infrequent Access, 30 days) and gives the hub's task role an inline policy `stt-worker-logs`: `logs:CreateLogGroup`,
`PutRetentionPolicy`, `CreateLogStream` and `PutLogEvents` on that group only (`logs:DescribeLogGroups` comes from step
25). `bash 31-stt-worker-logs.sh undo` takes the policy away again.

Until it has run, Health shows `not shipped: permission`, the worker keeps its batches on the PC (and `worker.log` as
always), and nothing else is affected. All four actions are needed: without any one of them (the retention too)
nothing is written. The hub notices within 10 minutes; the workers send what they kept. (`SIZEUP_STT_WORKER_LOG_GROUP`
names another group: the policy must name that one.)

## Config

The config file is `worker.toml` (Python 3.11+) or `worker.json`. Keys go at the top level or under `[worker]`. The
environment overrides the file. Paths may use `%LOCALAPPDATA%`, and relative ones are from the config file's folder.
Without `-c`, the worker reads `SIZEUP_STT_WORKER_CONFIG`, else `worker.toml` or `worker.json` next to `worker.py`.
A key the worker doesn't know is ignored and named in `worker.log` (and by `check`), so an older `worker.py` (a
rollback) still starts with a newer one's keys; a known key with a bad value stops it with the reason.

| key / env | default | what |
|---|---|---|
| `hub_url` / `SIZEUP_STT_HUB_URL` | | the hub (https) |
| `SIZEUP_STT_WORKER_TOKEN` (or `token_file`) | | the worker token; the worker id comes from it |
| `SIZEUP_STT_CF_CLIENT_ID`, `SIZEUP_STT_CF_CLIENT_SECRET` | | the worker's Cloudflare Access service token |
| `whisper_url` | `http://127.0.0.1:8179/inference` | loopback only |
| `whisper_server` | | whisper-server's command line; when set, the worker runs it. Only these options: `-m` (one local model), `--host` (127.0.0.1 only), `--port`, `-t`, `-l` (`en` only), `-sns`, `-et`, `-lpt`, `-nth`, `-mc`, `--vad`, `-vm`, `-vt` (and their long names); anything else is refused (`-nf`, language detection, translate, a public folder, the ffmpeg converter). The worker adds `-l en` when it names no language |
| `whisper_process`, `whisper_pid` | `whisper-server` | how the GPU check recognises our whisper-server |
| `model` | `large-v3-turbo-q5_0` | the label sent with each transcript |
| `auto_gaming`, `fullscreen_gaming` | `true`, `true` | `false`: no GPU-load detection / no full-screen check |
| `gaming_mem_mb`, `gaming_util_pct`, `gaming_samples`, `gaming_clear_s`, `gpu_poll_s` | 1024, 40, 2, 60, 5 | gaming thresholds |
| `gaming_stop_whisper_min` | 10 | minutes of gaming before the worker stops its whisper-server; 0 never |
| `finish_s` | 3 | gaming mid-job: a short-class job is finished only if its lease ends this soon |
| `heartbeat_s`, `claim_wait_s` | 5, 25 | at most 5 and 25 |
| `whisper_fail_max`, `whisper_check_s`, `whisper_timeout_s` | 3, 10, 60 | failures in a row before pausing; health checks while paused |
| `unauthorized_retry_s` | 300 | retry interval after the hub or Cloudflare refuses |
| `state_dir`, `log_file` | see Files | `log_file = "-"`: stderr only |
| `llm_url` | | llama-server, loopback only (e.g. `http://127.0.0.1:8180`); unset: no LLM |
| `llm_server` | | llama-server's command line; when set, the worker runs it. Only these options: `-m` (exactly one local GGUF), `--host` (127.0.0.1 only), `--port` (`llm_url`'s), `-c`, `-ngl`, `-np`, `--jinja`, `--no-webui`, `--no-slots`, `--reasoning`, `-t`, `-fa`, `-b`, `-ub`, `--no-mmap`, `-ctk`, `-ctv` (and their long names); anything else is refused (verbose logs, a prompts directory, the slots endpoint, an API key, CORS, tools, other models). The worker always adds `--no-slots` and `--no-webui` |
| `llm_model` | | the label the hub shows (Health), e.g. `qwen3-8b-q4_k_m` |
| `llm_model_sha256` | | the GGUF's SHA-256, required with `llm_server`: checked before llama-server's first start, and again before a restart when the file's size or time changed; a mismatch means no LLM |
| `llm_process` | `llama-server` | how the GPU check recognises our llama-server |
| `llm_stop_gaming_s` | auto | seconds of gaming before the worker stops its llama-server (its VRAM back to the game); 0 at once; unset: 60 for a model up to 6 GB (Qwen3-8B), 20 for a bigger one (Gemma-3-12B) |
| `llm_timeout_s`, `llm_check_s`, `llm_fail_max` | 30, 10, 3 | per answer; health checks; failures in a row before no LLM until healthy |
| `llm_no_think` | `true` | sends `enable_thinking: false` to the chat template (Qwen3); other templates ignore it |
| `auto_update` | `true` | updates itself to the version the hub names (see [Updating](#updating)), and llama.cpp and whisper.cpp to the builds that version pins ([Runtime updates](#runtime-updates-llamacpp-whispercpp)); `false`: never |
| `log_ship` | `true` | `worker.log`'s lines to the hub too (see [Logs](#logs-cloudwatch)); `false`: they stay on the PC |
| `managed_models` | `true` | runs the hub's [speech models](#speech-models) beside `whisper_server`; `false`: only `whisper_server` |
| `models_dir` | `models` | the models' files go in `<models_dir>\\managed` |
| `whisper_bin`, `llama_bin` | `whisper_server`'s, `llm_server`'s program | the programs for whisper-ggml and llama-asr models |
| `vram_budget_mb`, `vram_reserve_mb` | 0 (auto), 1536 | VRAM for all the servers: `vram_budget_mb`, else the GPU's total less the reserve |
| `llm_vram_mb`, `primary_vram_mb` | auto, 1000 | the LLM's share (auto: its GGUF's size and a context's worth); `whisper_server`'s when its model isn't in the list |
| `gate_dbfs` | -55 | clips quieter than this aren't decoded (no speech), on every model and the primary; -120 never |
| `llm_priority` | `yield` | the local LLM vs a speech model that doesn't fit beside it: `yield` (the LLM stops for it) or `keep` (the model isn't loaded); the hub's word wins ([Speech models](#speech-models)) |
| `catalog_file` | | a local `models.json` instead of the hub's (testing only) |
| `keep_awake` | `true` | Windows: no idle sleep while the worker runs and isn't paused (see [Keeping the PC awake](#keeping-the-pc-awake)); `false`: Windows' own sleep settings. `true` or `false` only |

## Troubleshooting

| `status` / log says | do |
|---|---|
| `hub refused the token (token_revoked)` / `(token_unknown)` | Get a new worker token (step 6). The worker retries every 5 min. |
| `refused (302)` or `(403): is the Cloudflare Access service token right?` | Check the CF client id and secret, and the `/api/station/stt/*` application's policy. |
| `logs: not shipped (hub: not configured (permission))` | The hub's task role may not write `/sizeup/stt-worker` yet: [Logs](#logs-cloudwatch). The batches wait on the PC. |
| `paused (whisper-server: ...)` | Read `whisper-server.log`. The worker checks every 10 s and resumes by itself. |
| `gaming (GPU 55% busy)` when nothing runs | Raise `gaming_util_pct` or set `whisper_pid`. |
| `gaming (a full-screen app)` while watching a full-screen video | Expected. Set `fullscreen_gaming = false` to turn it off. |
| `another worker (pid N) is running` | A second copy was refused. `status` shows the one that runs. |
| `llm: ..., model hash mismatch` | The GGUF isn't the pinned one: download it again (step 4b) and check its hash. |
| `llm: ..., loading` for minutes | Read `llama-server.log`. `CUDA error: out of memory`: something else holds VRAM; lower `-c` or use the fallback model. |
| `llm: ..., not answering` | llama-server exited or never started: `llama-server.log`; `check` tries a constrained answer. |
| `llm: ..., port 8180 answered 401: not this worker's llama-server` | A leftover from a worker that ended (its key is gone). The worker stops the program that holds the port when it is a llama-server (never another one you run), then starts its own; anything else on the port is only reported (no LLM jobs until it's gone). On Windows the worker's servers end with it (a kill-on-close job), so this is rare. |
| `check` says `llama-server said 401` | `check` uses the running worker's key (`state\llm-key`); a llama-server you started by hand for step 4b has none: run `check` against that one before the worker starts its own. |
| `the hub has no LLM lane (an older hub)` | The hub predates the infer lane. Clips work as before; the worker asks again every 10 min. |
| `infer ... not valid under its schema` often, with Qwen3 | Check `--reasoning off` is in `llm_server` (thinking eats the answer). |
| `infer ... not valid under its schema` | The model's answer didn't match the job's schema; the hub drops it (None). Many: tell the admin. |
| Health: LLM `none` while online | `status` on the PC: `llm:` says why (loading, gaming stopped it, hash mismatch). |
| `update: refused ...: not on main` / `not a pull request's merge into main` | The hub names a commit that isn't main's own (a pin of a branch commit). Pin a merge commit on main. |
| `update: refused ...: hash mismatch` | The pinned SHA-256 isn't that commit's `worker.py`. Check the pin. |
| `update: refused ...: its self-check failed: exit 2` | The new version refuses this `worker.toml`: `worker.log` names the key. Fix it; the worker tries again within an hour. |
| `update: refused ...: rolled back before` | That version never reached the hub (`update` says why). It is tried again after 6 hours, or at once after `update retry`. |
| `update: GitHub: ...; again in N min` | GitHub is unreachable or limited this address. The worker waits; nothing else changes. |
| `update: staged ..., waiting: gaming` | Expected. The swap waits until gaming has been over for 2 minutes. |

## Mac (Metal) later

The same `worker.py` runs on Apple silicon:

- **whisper-server:** `brew install whisper-cpp`, or build it with `cmake -B build -DGGML_METAL=1`.
- **Model:** the same one.
- **Gaming mode:** manual only (there is no `nvidia-smi` and no full-screen check). A dedicated box simply stays
  `online`.
- **Startup:** a launchd agent will replace Task Scheduler. It isn't written yet.
