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

Everything goes in `%LOCALAPPDATA%\sizeup-stt`. Only your Windows user can write there, so other local users can't
swap the programs the worker runs. Run the commands in PowerShell as that user:

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

You need an NVIDIA driver from 2024 or later. `nvidia-smi` should print the GPU.

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
& "$d\whisper\Release\whisper-server.exe" -m "$d\models\ggml-large-v3-turbo-q5_0.bin" --host 127.0.0.1 --port 8179 -t 4
```

The output should list the GPU (`ggml_cuda_init: found 1 CUDA devices`).

whisper-server answers any web page (`Access-Control-Allow-Origin: *`), so it must listen on `127.0.0.1` only, never
`0.0.0.0`. Otherwise any site open in a browser on the network could use the GPU. The worker refuses a config that
binds it anywhere else.

Leave it running for step 7, then stop it with Ctrl+C.

### 5. The worker

`worker.py` is pinned to a commit. Check its hash after the download:

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/actualsecurity/sizeup/4947a44d0fef9eda746f17e486c02c6d68754981/tools/stt_worker/worker.py -OutFile "$d\worker.py"
(Get-FileHash "$d\worker.py").Hash -eq '6D4B799630BC61A86DFA1B117C9FC9716A19CD89F6D99EED1563DF761A0446BC'   # True
notepad "$d\worker.toml"
```

In `worker.toml`, relative paths are from the folder `worker.toml` is in. The worker also runs whisper-server from
that folder. Use single quotes, so the backslashes stay as they are:

```toml
hub_url = "https://hub.example.org"          # the hub's address (the one the station uses)
model = "large-v3-turbo-q5_0"
# the worker starts whisper-server itself, restarts it when it exits, and knows its pid (its GPU use isn't a game)
whisper_server = ['whisper\Release\whisper-server.exe', '-m', 'models\ggml-large-v3-turbo-q5_0.bin',
                  '--host', '127.0.0.1', '--port', '8179', '-t', '4']
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

1. Download `worker.py` from the commit this README names, and check its hash (step 5).
2. `schtasks /End` and `schtasks /Run` the task.

## How it works with the hub

- **One job at a time:** the worker holds one lease at a time; the hub allows 2.
- **Leases:** it extends a lease at half its length while whisper-server decodes.
- **Pre-alert classes:** urgent, high, context-fallback and context have a hard deadline: a 4 s first lease,
  renewable up to 8 s from the claim. Past that the hub gives the clip to the next engine. The worker then drops its
  result and stops the decoding.
- **Heartbeat:** every 5 s, also during a decode. The hub takes back the leases of a worker it hasn't heard from for
  12 s.
- **Gaming or pause:** the hub takes back the worker's pre-alert jobs itself when the state changes. A later complete,
  extend or release on them gets 409, which the worker treats as a lost lease.

## Gaming mode

When gaming or paused, the worker takes no new jobs. Its heartbeat goes on and the hub sends the clips elsewhere. If
gaming or a pause starts during a job, the worker stops whisper-server's decoding and hands the job back to the hub at
once. It finishes the job only when it is a short one whose lease ends within 3 s (`finish_s`).

**Automatic:** every 5 s the worker checks for any of these:

- Windows reports a full-screen game or app (the shell's notification state)
- a process other than our whisper-server uses more than 1 GB of GPU memory (`nvidia-smi`)
- the GPU is more than 40 % busy while the worker is idle

Two checks in a row start gaming. Gaming ends after 60 s of quiet checks.

- **Our own load:** a check taken while the worker is decoding (or just did) neither counts nor resets.
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

## Files

These are in `%LOCALAPPDATA%\sizeup-stt\state` (`state_dir`):

| file | what |
|---|---|
| `control.json` | manual gaming and pause (written by the CLI) |
| `status.json` | the running worker: state, hub link, job counts, GPU (read by `status`) |
| `worker.log` | the worker's log, errors included (1 MB, 3 old copies) |
| `whisper-server.log` | whisper-server's output, when the worker starts it (1 MB, 2 old copies) |

## Config

The config file is `worker.toml` (Python 3.11+) or `worker.json`. Keys go at the top level or under `[worker]`. The
environment overrides the file. Paths may use `%LOCALAPPDATA%`, and relative ones are from the config file's folder.
Without `-c`, the worker reads `SIZEUP_STT_WORKER_CONFIG`, else `worker.toml` or `worker.json` next to `worker.py`.

| key / env | default | what |
|---|---|---|
| `hub_url` / `SIZEUP_STT_HUB_URL` | | the hub (https) |
| `SIZEUP_STT_WORKER_TOKEN` (or `token_file`) | | the worker token; the worker id comes from it |
| `SIZEUP_STT_CF_CLIENT_ID`, `SIZEUP_STT_CF_CLIENT_SECRET` | | the worker's Cloudflare Access service token |
| `whisper_url` | `http://127.0.0.1:8179/inference` | loopback only |
| `whisper_server` | | whisper-server's command line (`--host 127.0.0.1` only); when set, the worker runs it |
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

## Troubleshooting

| `status` / log says | do |
|---|---|
| `hub refused the token (token_revoked)` / `(token_unknown)` | Get a new worker token (step 6). The worker retries every 5 min. |
| `refused (302)` or `(403): is the Cloudflare Access service token right?` | Check the CF client id and secret, and the `/api/station/stt/*` application's policy. |
| `paused (whisper-server: ...)` | Read `whisper-server.log`. The worker checks every 10 s and resumes by itself. |
| `gaming (GPU 55% busy)` when nothing runs | Raise `gaming_util_pct` or set `whisper_pid`. |
| `gaming (a full-screen app)` while watching a full-screen video | Expected. Set `fullscreen_gaming = false` to turn it off. |
| `another worker (pid N) is running` | A second copy was refused. `status` shows the one that runs. |

## Mac (Metal) later

The same `worker.py` runs on Apple silicon:

- **whisper-server:** `brew install whisper-cpp`, or build it with `cmake -B build -DGGML_METAL=1`.
- **Model:** the same one.
- **Gaming mode:** manual only (there is no `nvidia-smi` and no full-screen check). A dedicated box simply stays
  `online`.
- **Startup:** a launchd agent will replace Task Scheduler. It isn't written yet.
