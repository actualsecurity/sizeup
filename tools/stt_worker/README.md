# GPU STT worker

`worker.py` makes a PC with a GPU part of the hub's speech-to-text pool (#639). It takes clips from the hub, runs them
through a local whisper.cpp `whisper-server` and sends the text back. When the PC is gaming, paused or off, the hub
sends the clips the way it would without the worker (AWS Transcribe, then the station's whisper). Nothing is dropped.

- **Connections:** the worker only connects out, to the hub over HTTPS through Cloudflare Access and to
  `whisper-server` on 127.0.0.1. Nothing on the PC listens on the network.
- **Python:** standard library only, Python 3.10 or newer. `worker.toml` needs 3.11+. On 3.10, use `worker.json`
  with the same keys.
- **Logs:** they hold job ids and timings. They never hold transcripts or the token.

## Windows setup (NVIDIA GPU)

Everything below goes in `C:\sizeup-stt`. Run the commands in PowerShell.

### 1. Python

```powershell
winget install -e --id Python.Python.3.12
py -3 --version
```

The python.org installer works too. It also installs the `py` launcher.

### 2. whisper.cpp with CUDA

You need a current NVIDIA driver. `nvidia-smi` should print the GPU.

Get the newest release from <https://github.com/ggml-org/whisper.cpp/releases> that has a Windows CUDA zip:
`whisper-bin-win-cuda-12.4.0-x64.zip` (older releases name it `whisper-cublas-12.4.0-bin-x64.zip`). If the
driver is too old for CUDA 12, use the `cuda-11.8.0` zip. The zip includes the CUDA runtime DLLs, so you don't need
the CUDA Toolkit.

```powershell
New-Item -ItemType Directory C:\sizeup-stt\whisper, C:\sizeup-stt\models -Force
Expand-Archive $HOME\Downloads\whisper-bin-win-cuda-12.4.0-x64.zip C:\sizeup-stt\whisper
Get-ChildItem C:\sizeup-stt\whisper -Recurse -Filter whisper-server.exe   # some zips put it in a Release folder
```

To build it yourself instead, install Visual Studio 2022 Build Tools (C++), the CUDA Toolkit and CMake, then run this
in a whisper.cpp checkout:

```powershell
cmake -B build -DGGML_CUDA=1
cmake --build build -j --config Release --target whisper-server   # -> build\bin\Release\whisper-server.exe
```

### 3. The model

```powershell
Invoke-WebRequest https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin `
  -OutFile C:\sizeup-stt\models\ggml-large-v3-turbo-q5_0.bin                  # ~550 MB
nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv
```

`large-v3-turbo-q5_0` needs about 1 GB of free VRAM while it is loaded. The f16 model (`ggml-large-v3-turbo.bin`,
1.6 GB) needs about 2 GB. If you use f16, set `model = "large-v3-turbo"` in `worker.toml`.

### 4. Try whisper-server

```powershell
C:\sizeup-stt\whisper\whisper-server.exe -m C:\sizeup-stt\models\ggml-large-v3-turbo-q5_0.bin --host 127.0.0.1 --port 8179 -t 4
```

The output should list the GPU (`ggml_cuda_init: found 1 CUDA devices`). Always bind to `127.0.0.1`, never
`0.0.0.0`. Leave it running for step 7, then stop it with Ctrl+C.

### 5. The worker

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/actualsecurity/sizeup/main/tools/stt_worker/worker.py `
  -OutFile C:\sizeup-stt\worker.py
notepad C:\sizeup-stt\worker.toml
```

`worker.toml` (single quotes, so the backslashes stay as they are):

```toml
hub_url = "https://hub.example.org"          # the hub's address (the one the station uses)
model = "large-v3-turbo-q5_0"
# the worker starts whisper-server itself, restarts it when it exits, and knows its pid (its GPU use isn't a game)
whisper_server = ['C:\sizeup-stt\whisper\whisper-server.exe', '-m', 'C:\sizeup-stt\models\ggml-large-v3-turbo-q5_0.bin',
                  '--host', '127.0.0.1', '--port', '8179', '-t', '4']
```

### 6. The two tokens

1. **Worker token:** the admin creates it in **Settings > Station > STT workers > Add**. It looks like
   `sw1.<worker>.<secret>` and is shown only once. Rotate keeps the old one working for 10 minutes. Revoke stops it
   at once.
2. **Cloudflare Access service token:** create it in Zero Trust > Access > Service Auth > Service Tokens > Create,
   one per worker. Add it to the policy (Service Auth) that lets `/api/station/*` through, the same one the station
   uses.

Store the tokens as user environment variables. `Read-Host` keeps them out of the PowerShell history:

```powershell
[Environment]::SetEnvironmentVariable('SIZEUP_STT_WORKER_TOKEN', (Read-Host 'worker token'), 'User')
[Environment]::SetEnvironmentVariable('SIZEUP_STT_CF_CLIENT_ID', (Read-Host 'CF client id'), 'User')
[Environment]::SetEnvironmentVariable('SIZEUP_STT_CF_CLIENT_SECRET', (Read-Host 'CF client secret'), 'User')
```

Open a new PowerShell window so it sees them. Sign out and back in before the task in step 8 runs.

### 7. Check

With whisper-server from step 4 running:

```powershell
py -3 C:\sizeup-stt\worker.py check
```

It prints `whisper-server: ok (...)`, `hub: ok (worker ...)` and the GPU. If something fails, see
[Troubleshooting](#troubleshooting).

### 8. Run it at logon (Task Scheduler)

Run this in PowerShell as administrator, signed in as the user who games on the PC:

```powershell
$py  = py -3 -c "import sys, pathlib; print(pathlib.Path(sys.executable).with_name('pythonw.exe'))"   # no console window
$act = New-ScheduledTaskAction -Execute $py -Argument 'C:\sizeup-stt\worker.py run -c C:\sizeup-stt\worker.toml' `
         -WorkingDirectory 'C:\sizeup-stt'
$trg = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$set = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
         -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$who = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'sizeup-stt-worker' -Action $act -Trigger $trg -Settings $set -Principal $who
Start-ScheduledTask -TaskName 'sizeup-stt-worker'
```

How the task behaves:

- **Restarts:** it restarts every minute if it fails. The worker itself never exits on hub or whisper-server
  errors: it reconnects (1, 2, 5, 10, 30 s) and restarts whisper-server.
- **No time limit:** `ExecutionTimeLimit` zero turns off Task Scheduler's 72-hour default.
- **No schtasks:** `schtasks /Create` can set neither of those, so use the PowerShell commands above to create the task.

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
$wact = New-ScheduledTaskAction -Execute 'C:\sizeup-stt\whisper\whisper-server.exe' -WorkingDirectory 'C:\sizeup-stt\whisper' `
          -Argument '-m C:\sizeup-stt\models\ggml-large-v3-turbo-q5_0.bin --host 127.0.0.1 --port 8179 -t 4'
Register-ScheduledTask -TaskName 'sizeup-whisper-server' -Action $wact -Trigger $trg -Settings $set -Principal $who
```

### Updating

Download `worker.py` again (step 5), then `schtasks /End` and `schtasks /Run` the task.

## Gaming mode

When gaming or paused, the worker takes no new jobs. Its heartbeat goes on and the hub sends the clips elsewhere. If
gaming starts during a job, the worker finishes the job if it fits in the job's lease. Otherwise it hands the job
back to the hub at once.

**Automatic:** every 5 s the worker runs `nvidia-smi`. Gaming starts after two samples in a row where either of these
is true:

- a process other than our whisper-server uses more than 1 GB of GPU memory
- the GPU is more than 40 % busy while the worker is idle

Gaming ends after 60 s below both. Windows often can't report per-process memory (nvidia-smi shows `[N/A]`), so there
the GPU-load check does the work.

**Manual:** these commands write `control.json`, which the running worker watches. The setting survives restarts.

```powershell
py -3 C:\sizeup-stt\worker.py gaming on      # / gaming off
py -3 C:\sizeup-stt\worker.py pause          # / resume (clears gaming and pause)
py -3 C:\sizeup-stt\worker.py status
```

For one-click toggles, make desktop shortcuts to `pythonw.exe C:\sizeup-stt\worker.py gaming on` (and `off`). There is
no tray icon or hotkey: the standard library has none on Windows.

## Files

These are in `%LOCALAPPDATA%\sizeup-stt-worker` (`state_dir`):

| file | what |
|---|---|
| `control.json` | manual gaming and pause (written by the CLI) |
| `status.json` | the running worker: state, hub link, job counts, GPU (read by `status`) |
| `worker.log` | the worker's log (1 MB, 3 old copies) |
| `whisper-server.log` | whisper-server's output, when the worker starts it |

## Config

The config file is `worker.toml` (Python 3.11+) or `worker.json`. Keys go at the top level or under `[worker]`. The
environment overrides the file. Without `-c`, the worker reads `SIZEUP_STT_WORKER_CONFIG`, else `worker.toml` or
`worker.json` next to `worker.py`.

| key / env | default | what |
|---|---|---|
| `hub_url` / `SIZEUP_STT_HUB_URL` | | the hub (https) |
| `SIZEUP_STT_WORKER_TOKEN` (or `token_file`) | | the worker token; the worker id comes from it |
| `SIZEUP_STT_CF_CLIENT_ID`, `SIZEUP_STT_CF_CLIENT_SECRET` | | the Cloudflare Access service token |
| `whisper_url` | `http://127.0.0.1:8179/inference` | |
| `whisper_server` | | whisper-server's command line; when set, the worker runs it |
| `whisper_process`, `whisper_pid` | `whisper-server` | how the GPU check recognises our whisper-server |
| `model` | `large-v3-turbo-q5_0` | the label sent with each transcript |
| `auto_gaming` | `true` | `false`: manual gaming mode only |
| `gaming_mem_mb`, `gaming_util_pct`, `gaming_samples`, `gaming_clear_s`, `gpu_poll_s` | 1024, 40, 2, 60, 5 | gaming thresholds |
| `heartbeat_s`, `claim_wait_s` | 5, 25 | at most 10 and 25 |
| `whisper_fail_max`, `whisper_check_s`, `whisper_timeout_s` | 3, 10, 60 | failures in a row before pausing; health checks while paused |
| `unauthorized_retry_s` | 300 | retry interval after the hub or Cloudflare refuses |
| `state_dir`, `log_file` | see Files | `log_file = "-"`: stderr only |

## Troubleshooting

| `status` / log says | do |
|---|---|
| `hub refused the token (token_revoked)` / `(token_unknown)` | Get a new worker token (step 6). The worker retries every 5 min. |
| `refused (302)` or `(403): is the Cloudflare Access service token right?` | Check the CF client id and secret, and the policy. |
| `paused (whisper-server: ...)` | Read `whisper-server.log`. The worker checks every 10 s and resumes by itself. |
| `gaming (GPU 55% busy)` when nothing runs | Raise `gaming_util_pct` or set `whisper_pid`. |

## Mac (Metal) later

The same `worker.py` runs on Apple silicon:

- **whisper-server:** `brew install whisper-cpp`, or build it with `cmake -B build -DGGML_METAL=1`.
- **Model:** the same one.
- **Gaming mode:** manual only (there is no `nvidia-smi`). A dedicated box simply stays `online`.
- **Startup:** a launchd agent will replace Task Scheduler. It isn't written yet.
