<#
.SYNOPSIS
  sizeup GPU STT worker: one-shot Windows installer (README.md, "Windows setup (NVIDIA GPU)", steps 1-8).

.DESCRIPTION
  Asks its questions first (the local LLM, the hub URL, the three secrets), then downloads, checks and registers the
  task without further input.

  - Every download is pinned: URL, size and SHA-256 (the $PinsJson below is deploy/stt-worker/pins.json). A file is
    used only after its hash matched; a mismatch deletes it and stops.
  - The secrets are read hidden (Read-Host -AsSecureString) and stored only as user environment variables, as in the
    README. They are never printed, logged or written anywhere else.
  - whisper-server and llama-server listen on 127.0.0.1 only.
  - Safe to run again: what is already there with the pinned hash is kept. It asks before it replaces worker.toml, a
    secret, the task, a self-updated worker.py or a whisper/llama folder it didn't install.

  Windows PowerShell 5.1 or PowerShell 7. Run it in an ordinary (not elevated) window as the user who games on the PC.

.PARAMETER Uninstall
  Remove the task, %LOCALAPPDATA%\sizeup-stt and the three user environment variables (after you confirm).

.PARAMETER DryRun
  Print what it would do. No prompts, downloads or changes.

.PARAMETER Llm
  yes / no: install the local LLM (or not) without asking. Default: ask.
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$DryRun,
    [ValidateSet('ask', 'yes', 'no')][string]$Llm = 'ask'
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# deploy/stt-worker/pins.json, verbatim (tests/test_stt_worker_install.py keeps the two the same)
$PinsJson = @'
{
  "_what": "Every download of the GPU STT worker's Windows setup: URL, size and SHA-256. README.md and install.ps1 (its $PinsJson) must say the same (tests/test_stt_worker_install.py).",
  "nvidia_driver_min": "551.61",
  "artifacts": {
    "whisper_zip": {
      "what": "whisper.cpp b5454, CUDA 12.4 (whisper-server.exe and the CUDA runtime)",
      "url": "https://github.com/ggml-org/whisper.cpp/releases/download/b5454/whisper-bin-win-cuda-12.4.0-x64.zip",
      "bytes": 684913404,
      "sha256": "AFEF0B881C500958921C3F5523B50E59EE2EC9B6F5CBD25B324C51ED308A957A"
    },
    "whisper_model": {
      "what": "ggml-large-v3-turbo-q5_0.bin",
      "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-large-v3-turbo-q5_0.bin",
      "bytes": 574041195,
      "sha256": "394221709CD5AD1F40C46E6031CA61BCE88931E6E088C188294C6D5A55FFA7E2"
    },
    "worker_py": {
      "what": "worker.py (first install; it updates itself afterwards)",
      "url": "https://raw.githubusercontent.com/actualsecurity/sizeup/36608ba643a74cc46e9ac8db54e88afc2b474975/deploy/stt-worker/worker.py",
      "bytes": 236911,
      "sha256": "4361607043AE5416F4FF0123DEA44518A5346E93E9822B687EE5489A6BD34C3F"
    },
    "llama_zip": {
      "what": "llama.cpp b12359, CUDA 12.4 (llama-server.exe)",
      "url": "https://github.com/ggml-org/llama.cpp/releases/download/b12359/llama-b12359-bin-win-cuda-12.4-x64.zip",
      "bytes": 318976332,
      "sha256": "E9C0ABF442E8339C06728366C1AD2C711332F9939C0C00603FB75D85FD44C914"
    },
    "llama_cudart_zip": {
      "what": "llama.cpp b12359, the CUDA 12.4 runtime",
      "url": "https://github.com/ggml-org/llama.cpp/releases/download/b12359/cudart-llama-bin-win-cuda-12.4-x64.zip",
      "bytes": 391443627,
      "sha256": "8C79A9B226DE4B3CACFD1F83D24F962D0773BE79F1E7B75C6AF4DED7E32AE1D6"
    },
    "qwen3_8b": {
      "what": "Qwen3-8B Q4_K_M GGUF (the default local LLM)",
      "url": "https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/7c41481f57cb95916b40956ab2f0b139b296d974/Qwen3-8B-Q4_K_M.gguf",
      "bytes": 5027783488,
      "sha256": "D98CDCBD03E17CE47681435B5150E34C1417F50B5C0019DD560E4882C5745785"
    },
    "gemma3_12b": {
      "what": "Gemma-3-12B-it Q4_K_M GGUF (the other choice; install.ps1 doesn't fetch it)",
      "url": "https://huggingface.co/bartowski/google_gemma-3-12b-it-GGUF/resolve/648e3a36a77c8a9f12d86e741f9dcb9089c769c4/google_gemma-3-12b-it-Q4_K_M.gguf",
      "bytes": 7300575264,
      "sha256": "FC57F67EFA46D711C346E587CBEF7D049E95F3DF8DB2EB2271153343EF0ACC7B"
    },
    "qwen3_4b": {
      "what": "Qwen3-4B-Instruct-2507 Q4_K_M GGUF (the small choice: beside every speech model on 12 GB; install.ps1 doesn't fetch it)",
      "url": "https://huggingface.co/bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF/resolve/ae44f08e1392f39c0e474af10c3ff8355c8b6688/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
      "bytes": 2497280736,
      "sha256": "2FDE00CE69DD4899C70D020845E2638353015BBA0FDF161B3EB965F2BCA4464E"
    }
  }
}
'@

# worker.toml (README step 5); relative paths are from the folder worker.toml is in
$TomlStt = @'
# written by install.ps1 (deploy/stt-worker/README.md, step 5); relative paths are from this folder
hub_url = "{{HUB_URL}}"
model = "large-v3-turbo-q5_0"
# the worker starts whisper-server itself, restarts it when it exits, and knows its pid (its GPU use isn't a game)
whisper_server = ['whisper\Release\whisper-server.exe', '-m', 'models\ggml-large-v3-turbo-q5_0.bin',
                  '--host', '127.0.0.1', '--port', '8179', '-t', '4',
                  '-l', 'en', '-sns', '-et', '2.4', '-lpt', '-1.0', '-nth', '0.6']
# no idle sleep while the worker runs (README "Keeping the PC awake"); false: Windows' own sleep settings
keep_awake = true
'@
$TomlLlm = @'
# the local LLM (README "The local LLM"): Qwen3-8B, the default
llm_url = "http://127.0.0.1:8180"
llm_model = "qwen3-8b-q4_k_m"
llm_model_sha256 = "{{LLM_SHA256}}"
llm_server = ['llama\llama-server.exe', '-m', 'models\Qwen3-8B-Q4_K_M.gguf', '--host', '127.0.0.1', '--port', '8180',
              '-c', '4096', '-ngl', '99', '-np', '1', '--jinja', '--no-webui', '--no-slots', '--reasoning', 'off']
'@

$TaskName = 'sizeup-stt-worker'
$SecretNames = @('SIZEUP_STT_WORKER_TOKEN', 'SIZEUP_STT_CF_CLIENT_ID', 'SIZEUP_STT_CF_CLIENT_SECRET')
$DefaultHub = 'https://sizeup.actualsecurity.org'
$TokenShape = '^sw1\.[a-z0-9][a-z0-9_-]{0,39}\.[A-Za-z0-9_-]+$'
$CfIdShape = '^[A-Za-z0-9._-]{8,}$'
$CfSecretShape = '^[A-Za-z0-9._~+/=-]{16,}$'
$HubShape = '^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$'
$WhisperArgs = '-m models\ggml-large-v3-turbo-q5_0.bin --host 127.0.0.1 --port 8179 -t 4 -l en -sns -et 2.4 -lpt -1.0 -nth 0.6'
$LlamaArgs = '-m models\Qwen3-8B-Q4_K_M.gguf --host 127.0.0.1 --port 8180 -c 4096 -ngl 99 -np 1 --jinja --no-webui --no-slots --reasoning off'

$Pins = $PinsJson | ConvertFrom-Json
$Art = $Pins.artifacts

$OnWindows = $true
if ($PSVersionTable.PSEdition -eq 'Core') { $OnWindows = [bool](Get-Variable -Name IsWindows -ValueOnly) }
if ($env:LOCALAPPDATA) { $D = Join-Path $env:LOCALAPPDATA 'sizeup-stt' }
elseif ($DryRun) { $D = Join-Path $HOME 'AppData/Local/sizeup-stt' }
else { Write-Host 'STOPPED: %LOCALAPPDATA% is not set (this installer is for Windows).' -ForegroundColor Red; exit 1 }
# the secrets stay out of this process's environment (and so out of every program it starts) until the check
foreach ($n in $SecretNames) { [Environment]::SetEnvironmentVariable($n, $null, 'Process') }
$script:TaskDisabled = $false
$Toml = Join-Path $D 'worker.toml'
$WorkerPy = Join-Path $D 'worker.py'
$StateDir = Join-Path $D 'state'

# --- output and prompts --------------------------------------------------------------------------------------------

function Say([string]$m) { Write-Host $m }
function Step([string]$m) { Write-Host ''; Write-Host "== $m" -ForegroundColor Cyan }
function Ok([string]$m) { Write-Host "  ok: $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "  warning: $m" -ForegroundColor Yellow }
function Would([string]$m) { Write-Host "  [dry run] would $m" -ForegroundColor DarkGray }
function Fail([string]$m) { Write-Host ''; Write-Host "STOPPED: $m" -ForegroundColor Red; exit 1 }

function Read-Line([string]$q) {
    $ans = Read-Host $q
    if ($null -eq $ans) { return '' }
    return $ans.Trim()
}

function Ask-YesNo([string]$q, [bool]$default) {
    $hint = '[y/N]'
    if ($default) { $hint = '[Y/n]' }
    if ($DryRun) { Would "ask: $q $hint (taking the default)"; return $default }
    while ($true) {
        $ans = (Read-Line "$q $hint").ToLowerInvariant()
        if ($ans -eq '') { return $default }
        if ($ans -eq 'y' -or $ans -eq 'yes') { return $true }
        if ($ans -eq 'n' -or $ans -eq 'no') { return $false }
    }
}

# a problem the admin may choose to go past; a dry run only reports it
function Confirm-Continue([string]$problem) {
    Warn $problem
    if ($DryRun) { return }
    if (-not (Ask-YesNo 'Continue anyway?' $false)) { Fail $problem }
}

# a native program's output, without PowerShell 5.1 turning its stderr into a terminating error
function Invoke-Quiet([scriptblock]$sb) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $sb } finally { $ErrorActionPreference = $old }
}

function Format-Size([double]$b) {
    if ($b -ge 1GB) { return ('{0:N1} GB' -f ($b / 1GB)) }
    return ('{0:N0} MB' -f ($b / 1MB))
}

function Get-Sha256([string]$path) { return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToUpperInvariant() }

function Get-Task {
    if (-not (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue)) { return $null }
    try { return Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch { return $null }
}

# --- downloads: pinned URL, size and SHA-256 -----------------------------------------------------------------------

function Receive-File([string]$url, [string]$out, [long]$max) {
    Add-Type -AssemblyName System.Net.Http
    $client = New-Object System.Net.Http.HttpClient
    $client.Timeout = [TimeSpan]::FromHours(12)
    $client.DefaultRequestHeaders.UserAgent.ParseAdd('sizeup-stt-install/1')
    $r = $null
    $inStream = $null
    $fs = $null
    try {
        $r = $client.GetAsync($url, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        if (-not $r.IsSuccessStatusCode) { throw "HTTP $([int]$r.StatusCode) from $(([Uri]$url).Host)" }
        $inStream = $r.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $fs = [IO.File]::Open($out, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $buf = New-Object byte[] 1048576
            $total = [long]0
            $stepBytes = [long][Math]::Max($max / 10, 1)
            $next = $stepBytes
            while ($true) {
                $read = $inStream.ReadAsync($buf, 0, $buf.Length)
                if (-not $read.Wait([TimeSpan]::FromMinutes(2))) { throw 'the download stalled (nothing for 2 minutes)' }
                $n = $read.Result
                if ($n -le 0) { break }
                $total += $n
                if ($total -gt $max) { throw "the server sent more than the pinned $max bytes" }
                $fs.Write($buf, 0, $n)
                if ($total -ge $next -and $max -ge 50MB) {
                    Say ('    {0,3}%  {1}' -f [int](100 * $total / $max), (Format-Size $total))
                    $next += $stepBytes
                }
            }
        } finally { }
    } finally {
        if ($fs) { $fs.Dispose() }
        if ($inStream) { $inStream.Dispose() }
        if ($r) { $r.Dispose() }
        $client.Dispose()
    }
}

# $dest stays when it already is the pinned file; else a download to $dest.partial, checked, then moved into place
function Save-Pinned($pin, [string]$dest) {
    $name = Split-Path $dest -Leaf
    $want = ([string]$pin.sha256).ToUpperInvariant()
    $size = [long]$pin.bytes
    if (Test-Path -LiteralPath $dest) {
        if ((Get-Item -LiteralPath $dest).Length -eq $size) {
            Say "  checking the $name that is there ..."
            if ((Get-Sha256 $dest) -eq $want) { Ok "$name already there (SHA-256 matches)"; return }
        }
        Warn "$name is there but isn't the pinned file: downloading it again"
    }
    if ($DryRun) { Would "download $($pin.url) ($(Format-Size $size)) -> $dest, SHA-256 must be $want"; return }
    $tmp = "$dest.partial"
    if ((Test-Path -LiteralPath $tmp) -and (Get-Item -LiteralPath $tmp).Length -eq $size -and (Get-Sha256 $tmp) -eq $want) {
        Move-Item -LiteralPath $tmp -Destination $dest -Force
        Ok "$name (an earlier run's download, SHA-256 $want)"
        return
    }
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    Say "  downloading $name ($(Format-Size $size)) from $(([Uri]$pin.url).Host) ..."
    try {
        Receive-File ([string]$pin.url) $tmp $size
    } catch {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        Fail "${name}: the download failed ($($_.Exception.Message)). Run the script again; what is done is kept."
    }
    $len = (Get-Item -LiteralPath $tmp).Length
    if ($len -ne $size) {
        Remove-Item -LiteralPath $tmp -Force
        Fail "${name}: got $len bytes, the pin says $size. The download was deleted. Run the script again."
    }
    $got = Get-Sha256 $tmp
    if ($got -ne $want) {
        Remove-Item -LiteralPath $tmp -Force
        Fail "${name}: SHA-256 $got is not the pinned $want. The download was deleted and nothing was installed from it. Don't use a copy from elsewhere; tell the admin if it happens again."
    }
    try { Move-Item -LiteralPath $tmp -Destination $dest -Force }
    catch { Fail "${name}: checked, but it couldn't replace $dest ($($_.Exception.Message)). Close what uses it and run this again (the checked download is kept)." }
    Ok "$name (SHA-256 $want)"
}

# a zip into $dir; an entry that would land outside $dir is refused
function Expand-Checked([string]$zip, [string]$dir) {
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $root = [IO.Path]::GetFullPath($dir).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $z = [IO.Compression.ZipFile]::OpenRead($zip)
    try {
        foreach ($e in $z.Entries) {
            $t = [IO.Path]::GetFullPath([IO.Path]::Combine($dir, $e.FullName))
            if (-not $t.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { throw "a zip entry outside its folder: $($e.FullName)" }
            if ($e.FullName.EndsWith('/') -or $e.FullName.EndsWith('\')) { [void][IO.Directory]::CreateDirectory($t); continue }
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($t))
            [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $t, $true)
        }
    } finally { $z.Dispose() }
}

# pinned zips unpacked into $dir; $dir\.sizeup-pins names the zips' hashes and the program's own, so a second run
# keeps a folder that is still what the pins made
function Test-ZipsInstalled([object[]]$zipPins, [string]$dir, [string]$exe) {
    $marker = Join-Path $dir '.sizeup-pins'
    $exePath = Join-Path $dir $exe
    if (-not ((Test-Path -LiteralPath $exePath) -and (Test-Path -LiteralPath $marker))) { return $false }
    $zipHashes = ($zipPins | ForEach-Object { ([string]$_.sha256).ToUpperInvariant() }) -join ','
    return (([IO.File]::ReadAllText($marker)).Trim() -eq "$zipHashes|$(Get-Sha256 $exePath)")
}

# a folder there that this script didn't make (a README install by hand, an own build): replace it only if the admin says so
function Ask-KeepFolder([object[]]$zipPins, [string]$dir, [string]$exe) {
    if (-not (Test-Path -LiteralPath (Join-Path $dir $exe))) { return $false }
    if (Test-Path -LiteralPath (Join-Path $dir '.sizeup-pins')) { return $false }    # ours: updated to the pins if they changed
    return (Ask-YesNo "$dir\$exe is there but this script didn't install it. Keep it as it is?" $true)
}

function Install-Zips([string]$label, [object[]]$zipPins, [string]$dir, [string]$exe, [bool]$keep) {
    if ($keep) { Ok "keeping $dir as it is"; return }
    $marker = Join-Path $dir '.sizeup-pins'
    $exePath = Join-Path $dir $exe
    $zipHashes = ($zipPins | ForEach-Object { ([string]$_.sha256).ToUpperInvariant() }) -join ','
    if (Test-ZipsInstalled $zipPins $dir $exe) { Ok "$label already installed ($exe)"; return }
    $zips = @()
    $i = 0
    foreach ($p in $zipPins) {
        $z = Join-Path $D "$label-$i.zip"
        Save-Pinned $p $z
        $zips += $z
        $i++
    }
    if ($DryRun) { Would "unpack them into $dir (-> $exe), then delete the zips"; return }
    $new = "$dir.new"
    if (Test-Path -LiteralPath $new) { Remove-Item -LiteralPath $new -Recurse -Force }
    New-Item -ItemType Directory -Path $new | Out-Null
    Say "  unpacking into $dir ..."
    foreach ($z in $zips) { Expand-Checked $z $new }
    if (-not (Test-Path -LiteralPath (Join-Path $new $exe))) { Fail "${label}: $exe isn't in the pinned zip" }
    if (Test-Path -LiteralPath $dir) { Remove-Item -LiteralPath $dir -Recurse -Force }
    Rename-Item -LiteralPath $new -NewName (Split-Path $dir -Leaf)
    [IO.File]::WriteAllText($marker, "$zipHashes|$(Get-Sha256 $exePath)")
    foreach ($z in $zips) { Remove-Item -LiteralPath $z -Force }
    Ok "$label -> $exePath"
}

# --- Python --------------------------------------------------------------------------------------------------------

# the py launcher's Python 3: @(major, minor, python.exe), or $null
function Find-Python {
    $launcher = $null
    $cmd = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { $launcher = $cmd.Path }
    else {
        foreach ($c in @(@([string]$env:LOCALAPPDATA, 'Programs\Python\Launcher\py.exe'), @([string]$env:SystemRoot, 'py.exe'))) {
            if ($c[0] -and (Test-Path -LiteralPath ([IO.Path]::Combine($c[0], $c[1])))) { $launcher = [IO.Path]::Combine($c[0], $c[1]); break }
        }
    }
    if (-not $launcher) { return $null }
    $out = @(Invoke-Quiet { & $launcher -3 -c 'import sys; print(sys.version_info[0], sys.version_info[1]); print(sys.executable)' 2>$null })
    if ($LASTEXITCODE -ne 0 -or $out.Count -lt 2) { return $null }
    $v = ([string]$out[0]).Trim().Split(' ')
    return @([int]$v[0], [int]$v[1], ([string]$out[1]).Trim())
}

# --- the secrets ---------------------------------------------------------------------------------------------------

# stored as a user environment variable (README step 6); never printed, logged or written anywhere else
function Read-Secret([string]$name, [string]$shape, [string]$hint) {
    $have = [Environment]::GetEnvironmentVariable($name, 'User')
    if ($have) {
        if ($have -cmatch $shape) {
            if (Ask-YesNo "$name is already set. Keep it?" $true) {
                $have = $null
                Ok "${name}: kept"
                return
            }
        } else {
            Warn "$name is set but doesn't look like ${hint}: enter it again"
        }
    }
    $have = $null
    if ($DryRun) { Would "ask for $name (hidden) and store it as a user environment variable"; return }
    for ($try = 1; $try -le 5; $try++) {
        $s = Read-Host "$name ($hint; hidden)" -AsSecureString
        $plain = [Net.NetworkCredential]::new('', $s).Password
        $s.Dispose()
        if ($null -ne $plain -and $plain.Trim() -cmatch $shape) {
            [Environment]::SetEnvironmentVariable($name, $plain.Trim(), 'User')
            $plain = $null
            Ok "${name}: stored (user environment variable)"
            return
        }
        $plain = $null
        Warn "that doesn't look like $hint (not shown). Try again."
    }
    Fail "${name}: no valid value entered"
}

# --- the check (README steps 4 and 7) ------------------------------------------------------------------------------

function Test-Port([int]$port) {
    $c = New-Object Net.Sockets.TcpClient
    try {
        $iar = $c.BeginConnect('127.0.0.1', $port, $null, $null)
        return [bool]($iar.AsyncWaitHandle.WaitOne(500) -and $c.Connected)
    } catch { return $false } finally { $c.Close() }
}

# a server for the check, on 127.0.0.1, started without the secrets in its environment (README step 6)
function Start-Loopback([string]$exe, [string]$argLine, [string]$logName, [string]$apiKey) {
    foreach ($n in $SecretNames) { [Environment]::SetEnvironmentVariable($n, $null, 'Process') }
    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    $out = Join-Path $StateDir "install-check-$logName.log"
    # llama-server's key goes in its environment (as the worker does), never on its command line
    if ($apiKey) { [Environment]::SetEnvironmentVariable('LLAMA_API_KEY', $apiKey, 'Process') }
    try {
        return Start-Process -FilePath $exe -ArgumentList $argLine -WorkingDirectory $D -NoNewWindow -PassThru `
            -RedirectStandardOutput $out -RedirectStandardError "$out.err"
    } finally { [Environment]::SetEnvironmentVariable('LLAMA_API_KEY', $null, 'Process') }
}

function New-Key {
    $b = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($b) } finally { $rng.Dispose() }
    return (($b | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Wait-Ready($proc, [scriptblock]$ready, [int]$seconds, [string]$label) {
    $until = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $until) {
        if ($proc.HasExited) { Warn "$label exited (code $($proc.ExitCode)): see $StateDir\install-check-$label.log*"; return $false }
        if (& $ready) { return $true }
        Start-Sleep -Seconds 1
    }
    Warn "$label didn't answer within $seconds s: see $StateDir\install-check-$label.log*"
    return $false
}

function Invoke-Check([string]$python, [bool]$withLlm) {
    if ($DryRun) {
        Would "start whisper-server $WhisperArgs (in $D, no secrets in its environment)"
        if ($withLlm) { Would "start llama-server $LlamaArgs" }
        Would "run: `"$python`" `"$WorkerPy`" check -c `"$Toml`", then stop the servers it started"
        return $true
    }
    $procs = @()
    try {
        $ports = @(8179)
        if ($withLlm) { $ports += 8180 }
        foreach ($port in $ports) {
            if (Test-Port $port) { Warn "something already listens on 127.0.0.1:$port (a whisper-server or llama-server you started?): the check uses it" }
        }
        if (-not (Test-Port 8179)) {
            Say '  starting whisper-server on 127.0.0.1:8179 ...'
            $w = Start-Loopback (Join-Path $D 'whisper\Release\whisper-server.exe') $WhisperArgs 'whisper-server' ''
            $procs += $w
            [void](Wait-Ready $w { Test-Port 8179 } 120 'whisper-server')
        }
        if ($withLlm -and -not (Test-Port 8180) -and (Test-Path -LiteralPath (Join-Path $D 'llama\llama-server.exe'))) {
            Say '  starting llama-server on 127.0.0.1:8180 (loading the model takes a while) ...'
            # `check` sends the key in state\llm-key (the running worker's; the worker writes its own when it starts)
            $key = New-Key
            [IO.File]::WriteAllText((Join-Path $StateDir 'llm-key'), $key)
            $l = Start-Loopback (Join-Path $D 'llama\llama-server.exe') $LlamaArgs 'llama-server' $key
            $key = $null
            $procs += $l
            [void](Wait-Ready $l {
                    try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri 'http://127.0.0.1:8180/health').StatusCode -eq 200 } catch { $false }
                } 300 'llama-server')
        }
        # the worker reads the secrets from its environment: this process's, from the user variables just stored
        # (.NET, not Set-Item: PowerShell module logging records cmdlet parameters)
        foreach ($n in $SecretNames) {
            [Environment]::SetEnvironmentVariable($n, [Environment]::GetEnvironmentVariable($n, 'User'), 'Process')
        }
        Say "  py worker.py check:"
        Invoke-Quiet { & $python $WorkerPy check -c $Toml 2>&1 } | ForEach-Object { Say "    $_" }
        return ($LASTEXITCODE -eq 0)
    } finally {
        foreach ($n in $SecretNames) { [Environment]::SetEnvironmentVariable($n, $null, 'Process') }
        foreach ($p in $procs) { if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } }
    }
}

# the worker task and the servers it started from this folder: stopped before files change or the check runs
function Stop-Ours {
    $t = Get-Task
    if ($t) {
        if ($DryRun) { Would "disable and stop the task $TaskName while files change (enabled and started again at the end)"; return }
        try {
            Disable-ScheduledTask -TaskName $TaskName | Out-Null
            $script:TaskDisabled = $true
        } catch { Warn "couldn't disable the task $TaskName ($($_.Exception.Message)): its 5-minute trigger may start the worker during the install" }
        if ($t.State -eq 'Running') { Stop-ScheduledTask -TaskName $TaskName }
        Say "  the task $TaskName is off while files change (on again at the end)"
        Start-Sleep -Seconds 2
    }
    if ($DryRun -or -not $OnWindows) { return }
    Get-Process -Name 'whisper-server', 'llama-server' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith("$D\", [StringComparison]::OrdinalIgnoreCase) } |
        ForEach-Object { Say "  stopping $($_.Name) (pid $($_.Id)) from $D"; Stop-Process -Id $_.Id -Force }
}

# --- uninstall -----------------------------------------------------------------------------------------------------

function Invoke-Uninstall {
    Step 'Uninstall'
    Say "This removes the task $TaskName, the folder $D (programs, models, worker.toml, logs)"
    Say "and the user environment variables $($SecretNames -join ', ')."
    if ($DryRun) {
        Would "stop and unregister the task $TaskName (Unregister-ScheduledTask)"
        Would "stop whisper-server / llama-server / worker.py running from $D"
        Would "delete $D"
        foreach ($n in $SecretNames) { Would "clear the user environment variable $n" }
        return
    }
    if (-not (Ask-YesNo 'Remove the sizeup STT worker from this PC?' $false)) { Say 'Nothing was removed.'; return }
    if (Get-Task) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Ok "task $TaskName removed"
    }
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($WorkerPy) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Stop-Ours
    if (Test-Path -LiteralPath $D) {
        Start-Sleep -Seconds 1
        Remove-Item -LiteralPath $D -Recurse -Force
        Ok "$D removed"
    }
    foreach ($n in $SecretNames) {
        [Environment]::SetEnvironmentVariable($n, $null, 'User')
        Remove-Item -LiteralPath "Env:$n" -ErrorAction SilentlyContinue
    }
    Ok "$($SecretNames -join ', ') cleared"
    Say ''
    Say 'Also revoke the worker token (Settings > Station > STT workers) and delete its Cloudflare service token.'
}

# --- install -------------------------------------------------------------------------------------------------------

function Invoke-Install {
    if ($DryRun) { Say 'DRY RUN: nothing is asked, downloaded or changed.' }

    Step '1/9 This PC'
    if (-not $OnWindows) { Confirm-Continue 'this is not Windows (Windows 10/11 x64 needed)' }
    else {
        $v = [Environment]::OSVersion.Version
        $arch = $env:PROCESSOR_ARCHITEW6432
        if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
        if ($v.Major -lt 10 -or $arch -ne 'AMD64') { Fail "Windows 10/11 x64 needed (this is Windows $v, $arch)" }
        Ok "Windows $v $arch, user $env:USERDOMAIN\$env:USERNAME"
        $me = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
        if ($me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            Confirm-Continue ("this window is elevated (Run as administrator). Run it in an ordinary window as the user who " +
                "games on this PC: everything goes in that user's folder and the task runs as that user. Elevated is " +
                "only for when Windows refuses to register the task.")
        }
    }
    $smi = Get-Command nvidia-smi -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $vramMb = 0
    if (-not $smi) { Confirm-Continue 'no nvidia-smi: no NVIDIA GPU or driver (README step 2)' }
    else {
        $smiPath = $smi.Path
        $gpus = @(Invoke-Quiet { & $smiPath '--query-gpu=name,memory.total,driver_version' '--format=csv,noheader,nounits' 2>$null })
        if ($LASTEXITCODE -ne 0 -or $gpus.Count -eq 0) { Confirm-Continue 'nvidia-smi found no GPU' }
        else {
            $f = ([string]$gpus[0]).Split(',') | ForEach-Object { $_.Trim() }
            try { $vramMb = [int]$f[1] } catch { $vramMb = 0 }
            Ok "GPU $($f[0]), $([Math]::Round($vramMb / 1024, 1)) GB VRAM, driver $($f[2])"
            if ($gpus.Count -gt 1) { Warn "$($gpus.Count) GPUs: whisper-server uses the first" }
            try {
                if ([version]$f[2] -lt [version]$Pins.nvidia_driver_min) {
                    Confirm-Continue "driver $($f[2]) is older than $($Pins.nvidia_driver_min) (CUDA 12.4's minimum): update the NVIDIA driver"
                }
            } catch { Warn "couldn't read the driver version '$($f[2])'" }
        }
    }

    Step '2/9 Python'
    $py = Find-Python
    if ($py -and ($py[0] -gt 3 -or ($py[0] -eq 3 -and $py[1] -ge 11))) { Ok "Python $($py[0]).$($py[1]) ($($py[2]))" }
    else {
        if ($py) { Warn "py -3 is Python $($py[0]).$($py[1]); worker.toml needs 3.11 or newer" } else { Warn 'no Python (py -3)' }
        if ($DryRun) { Would 'offer: winget install -e --id Python.Python.3.12'; $py = @(3, 12, 'python.exe') }
        else {
            if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { Fail 'install Python 3.12 from python.org (with the py launcher), then run this again' }
            if (-not (Ask-YesNo 'Install Python 3.12 now (winget install -e --id Python.Python.3.12)?' $true)) { Fail 'Python 3.11 or newer is needed' }
            Invoke-Quiet { & winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements } | Out-Host
            $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
            $py = Find-Python
            if (-not $py -or ($py[0] -eq 3 -and $py[1] -lt 11)) { Fail 'Python still not found: open a new PowerShell window and run this script again' }
            Ok "Python $($py[0]).$($py[1]) ($($py[2]))"
        }
    }
    $Python = $py[2]

    Step '3/9 Questions (then the rest runs by itself)'
    $withLlm = $false
    if ($Llm -eq 'yes') { $withLlm = $true }
    elseif ($Llm -eq 'ask') { $withLlm = Ask-YesNo 'Install the local AI model for intel (~5.3 GB download, ~6 GB VRAM)?' $false }
    if ($withLlm) { Ok 'the local LLM (Qwen3-8B) will be installed' } else { Ok 'speech-to-text only (no local LLM)' }
    if ($withLlm -and $vramMb -gt 0 -and $vramMb -lt 10240) { Warn "whisper + the LLM need about 7.5-8 GB VRAM; this GPU has $([Math]::Round($vramMb / 1024, 1)) GB" }

    $need = 3 * [long]$Art.whisper_zip.bytes + [long]$Art.whisper_model.bytes
    if ($withLlm) { $need += 3 * ([long]$Art.llama_zip.bytes + [long]$Art.llama_cudart_zip.bytes) + [long]$Art.qwen3_8b.bytes }
    try {
        $free = (New-Object IO.DriveInfo ([IO.Path]::GetPathRoot($D))).AvailableFreeSpace
        if ($free -lt $need) { Confirm-Continue "only $(Format-Size $free) free on $([IO.Path]::GetPathRoot($D)); the install needs up to $(Format-Size $need)" }
        else { Ok "$(Format-Size $free) free (up to $(Format-Size $need) needed)" }
    } catch { Warn "couldn't read the free disk space" }

    $tomlAction = 'write'
    if (Test-Path -LiteralPath $Toml) {
        if ($DryRun) { Would "ask whether to keep, replace or back up and replace $Toml (default: keep)"; $tomlAction = 'keep' }
        else {
            while ($true) {
                $ans = (Read-Line "$Toml exists. [K]eep it, [R]eplace it, or [B]ack it up and replace it? [K/r/b]").ToLowerInvariant()
                if ($ans -eq '' -or $ans -eq 'k') { $tomlAction = 'keep'; break }
                if ($ans -eq 'r') { $tomlAction = 'write'; break }
                if ($ans -eq 'b') { $tomlAction = 'backup'; break }
            }
        }
    }
    $hub = $DefaultHub
    if ($tomlAction -ne 'keep') {
        if ($DryRun) { Would "ask for the hub URL (default $DefaultHub)" }
        else {
            while ($true) {
                $u = Read-Line "Hub URL [$DefaultHub]"
                if ($u -eq '') { $u = $DefaultHub }
                $u = $u.TrimEnd('/')
                if ($u -cmatch $HubShape) { $hub = $u; break }
                Warn 'use https://host (lower-case https, no spaces, quotes, query or user name)'
            }
        }
        Ok "hub $hub"
    } else {
        Ok "keeping $Toml"
        if ($withLlm -and -not (Select-String -LiteralPath $Toml -Pattern '^\s*llm_url' -Quiet)) {
            Warn 'the worker.toml you keep has no llm_* keys: the LLM runs only once you add them (README step 5) or run this again and replace it'
        }
    }

    Say '  The secrets are read hidden and stored as user environment variables (never shown or written anywhere else).'
    Say '  Worker token: Settings > Station > STT workers > Add. Cloudflare: the service token of the /api/station/stt/* application.'
    Read-Secret 'SIZEUP_STT_WORKER_TOKEN' $TokenShape 'a worker token, sw1.<worker>.<secret>'
    Read-Secret 'SIZEUP_STT_CF_CLIENT_ID' $CfIdShape 'the Cloudflare Access Client ID (...access)'
    Read-Secret 'SIZEUP_STT_CF_CLIENT_SECRET' $CfSecretShape 'the Cloudflare Access Client Secret'

    $keepWorker = $false
    if (Test-Path -LiteralPath $WorkerPy) {
        if ((Get-Sha256 $WorkerPy) -ne ([string]$Art.worker_py.sha256).ToUpperInvariant()) {
            Say "  $WorkerPy isn't the first-install pin: it has updated itself since (expected)."
            $keepWorker = Ask-YesNo 'Keep that worker.py (it keeps updating itself)?' $true
        }
    }

    $keepWhisper = Ask-KeepFolder @($Art.whisper_zip) (Join-Path $D 'whisper') 'Release\whisper-server.exe'
    $keepLlama = $false
    if ($withLlm) { $keepLlama = Ask-KeepFolder @($Art.llama_zip, $Art.llama_cudart_zip) (Join-Path $D 'llama') 'llama-server.exe' }

    $task = Get-Task
    $writeTask = $true
    if ($task) { $writeTask = Ask-YesNo "The task $TaskName exists. Replace it with the README's?" $true }

    Step '4/9 Folders'
    Stop-Ours
    if ($DryRun) { Would "create $D and $D\models" }
    else { New-Item -ItemType Directory -Path (Join-Path $D 'models') -Force | Out-Null; Ok $D }

    Step '5/9 whisper.cpp b5454 (CUDA) and the model'
    Install-Zips 'whisper' @($Art.whisper_zip) (Join-Path $D 'whisper') 'Release\whisper-server.exe' $keepWhisper
    Save-Pinned $Art.whisper_model (Join-Path $D 'models\ggml-large-v3-turbo-q5_0.bin')
    if ($withLlm) {
        Step '5b/9 llama.cpp b12359 (CUDA) and Qwen3-8B'
        Install-Zips 'llama' @($Art.llama_zip, $Art.llama_cudart_zip) (Join-Path $D 'llama') 'llama-server.exe' $keepLlama
        Save-Pinned $Art.qwen3_8b (Join-Path $D 'models\Qwen3-8B-Q4_K_M.gguf')
    }

    Step '6/9 worker.py and worker.toml'
    if ($keepWorker) { Ok "keeping $WorkerPy (self-updated)" }
    else {
        if ((Test-Path -LiteralPath $WorkerPy) -and -not $DryRun) { Copy-Item -LiteralPath $WorkerPy -Destination "$WorkerPy.before-install" -Force }
        Save-Pinned $Art.worker_py $WorkerPy
    }
    if ($tomlAction -ne 'keep') {
        $text = $TomlStt.Replace('{{HUB_URL}}', $hub) + "`n"
        if ($withLlm) { $text += "`n" + $TomlLlm.Replace('{{LLM_SHA256}}', ([string]$Art.qwen3_8b.sha256).ToLowerInvariant()) + "`n" }
        if ($DryRun) { Would "write $Toml :"; $text.Split("`n") | ForEach-Object { Say "      $_" } }
        else {
            if ($tomlAction -eq 'backup') {
                $bak = "$Toml.$(Get-Date -Format 'yyyyMMdd-HHmmss').bak"
                Copy-Item -LiteralPath $Toml -Destination $bak
                Ok "backed up to $bak"
            }
            [IO.File]::WriteAllText($Toml, $text, (New-Object Text.UTF8Encoding($false)))
            Ok "wrote $Toml"
        }
    }
    if ($PSCommandPath -and -not $DryRun) {
        $self = Join-Path $D 'install.ps1'
        if ([IO.Path]::GetFullPath($PSCommandPath) -ne [IO.Path]::GetFullPath($self)) { Copy-Item -LiteralPath $PSCommandPath -Destination $self -Force }
    }

    Step '7/9 Check (whisper-server on 127.0.0.1, then worker.py check)'
    # the check tries llama-server when worker.toml has llm_url (a kept worker.toml decides, not the answer above)
    $checkLlm = $withLlm
    if ($tomlAction -eq 'keep' -and -not $DryRun) { $checkLlm = [bool](Select-String -LiteralPath $Toml -Pattern '^\s*llm_url' -Quiet) }
    if ($checkLlm -and -not $DryRun -and -not (Test-Path -LiteralPath (Join-Path $D 'llama\llama-server.exe'))) {
        Warn 'worker.toml has llm_url but llama-server.exe is not installed (run this again and answer y to the local AI model)'
    }
    $checked = Invoke-Check $Python $checkLlm
    if ($checked) { Ok 'check passed' }
    else {
        Warn "the check failed: README 'Troubleshooting'. A wrong token or Cloudflare setting can be fixed later (run this again); the task retries by itself."
        if (-not (Ask-YesNo 'Register and start the task anyway?' $true)) { Fail 'stopped before the task; run this again after the fix' }
    }

    Step '8/9 Task Scheduler'
    $pyw = [IO.Path]::Combine([IO.Path]::GetDirectoryName($Python), 'pythonw.exe')
    if ($writeTask) {
        if ($DryRun) { Would "register the task $TaskName (README step 8): $pyw `"$WorkerPy`" run -c `"$Toml`", at logon + every 5 min, IgnoreNew, no time limit, Limited" }
        else {
            if (-not (Test-Path -LiteralPath $pyw)) { Fail "no pythonw.exe next to $Python" }
            # README step 8, as it is
            # (PowerShell names ignore case: the README's $d is this script's $D)
            $me = "$env:USERDOMAIN\$env:USERNAME"
            $act = New-ScheduledTaskAction -Execute $pyw -Argument "`"$d\worker.py`" run -c `"$d\worker.toml`"" -WorkingDirectory $d
            $trg = @(
                New-ScheduledTaskTrigger -AtLogOn -User $me
                New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) `
                    -RepetitionDuration (New-TimeSpan -Days 3650)
            )
            $set = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
                -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
            $who = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
            try {
                Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger $trg -Settings $set -Principal $who -Force | Out-Null
            } catch {
                Fail ("Windows refused to register the task ($($_.Exception.Message)). Run this script again from an elevated " +
                    "PowerShell (Run as administrator): everything else is done and is kept, and the task still runs as $me, Limited.")
            }
            Ok "task $TaskName registered"
        }
    } else { Ok "keeping the task $TaskName" }
    if ($DryRun) { Would "start the task $TaskName and show its state" }
    else {
        if ($script:TaskDisabled) { Enable-ScheduledTask -TaskName $TaskName | Out-Null; $script:TaskDisabled = $false }
        Start-ScheduledTask -TaskName $TaskName
        Start-Sleep -Seconds 8
        $t = Get-ScheduledTask -TaskName $TaskName
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Ok ("task {0}: {1}, last run {2}, last result 0x{3:X}" -f $TaskName, $t.State, $info.LastRunTime, $info.LastTaskResult)
        Invoke-Quiet { & $Python $WorkerPy status -c $Toml 2>&1 } | ForEach-Object { Say "    $_" }
    }

    Step '9/9 Done'
    Say "Installed in $D :"
    Say "  whisper.cpp b5454 CUDA   whisper\Release\whisper-server.exe   zip SHA-256 $($Art.whisper_zip.sha256)"
    Say "  model                    models\ggml-large-v3-turbo-q5_0.bin   SHA-256 $($Art.whisper_model.sha256)"
    if ($withLlm) {
        Say "  llama.cpp b12359 CUDA    llama\llama-server.exe                zip SHA-256 $($Art.llama_zip.sha256), $($Art.llama_cudart_zip.sha256)"
        Say "  Qwen3-8B Q4_K_M          models\Qwen3-8B-Q4_K_M.gguf          SHA-256 $($Art.qwen3_8b.sha256)"
    }
    if ($keepWorker) { Say '  worker.py                kept (self-updated)' }
    else { Say "  worker.py                first-install pin                     SHA-256 $($Art.worker_py.sha256) (updates itself from now on)" }
    Say "  config                   $Toml"
    Say "  secrets                  user environment variables $($SecretNames -join ', ')"
    Say ''
    Say "Logs:     $StateDir\worker.log (and whisper-server.log, llama-server.log)"
    Say "Status:   py -3 `"$WorkerPy`" status      Task: schtasks /Query /TN $TaskName /V /FO LIST"
    Say 'Hub:      the worker shows up in Settings > Station > STT workers (and Health) once it has said hello.'
    Say 'If status says there is no token: sign out of Windows and back in, so the task sees the new variables.'
    Say "Uninstall: powershell -ExecutionPolicy Bypass -File `"$D\install.ps1`" -Uninstall"
    Say "           (= Unregister-ScheduledTask $TaskName, delete $D, clear the 3 variables)"
}

try {
    if ($OnWindows -and $PSVersionTable.PSEdition -ne 'Core') {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    }
    if ($Uninstall) { Invoke-Uninstall } else { Invoke-Install }
} catch {
    Write-Host ''
    Write-Host "STOPPED: $($_.Exception.Message) (install.ps1 line $($_.InvocationInfo.ScriptLineNumber))" -ForegroundColor Red
    exit 1
} finally {
    # a task this run turned off (Stop-Ours) is on again, also when the install stopped early (Fail exits through here)
    if ($script:TaskDisabled) {
        try { Enable-ScheduledTask -TaskName $TaskName | Out-Null; Write-Host "  the task $TaskName is on again" } catch { }
    }
    foreach ($n in $SecretNames) { [Environment]::SetEnvironmentVariable($n, $null, 'Process') }
}
exit 0
