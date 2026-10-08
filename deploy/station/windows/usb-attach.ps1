# sizeup: keep every RTL-SDR attached to WSL (scheduled task "sizeup-usb", installed by
# install-windows.ps1 to C:\ProgramData\sizeup\usb-attach.ps1). Every 10 s: each RTL-SDR (USB id
# 0bda:2838 or 0bda:2832) that usbipd lists is shared (bind, once) and attached to WSL. A dongle
# unplugged and plugged back in, a WSL restart or a reboot: attached again within ~10 s.
# Log: C:\ProgramData\sizeup\usb.log (1 MB, one old file kept); only changes are logged.
param(
    [int]$IntervalSeconds = 10
)
$ErrorActionPreference = "Continue"
$Dir = Join-Path $env:ProgramData "sizeup"
$Log = Join-Path $Dir "usb.log"
$Ids = @("0bda:2838", "0bda:2832")
$env:WSL_UTF8 = "1"

function Write-SizeupLog([string]$Message) {
    try {
        if ((Test-Path $Log) -and ((Get-Item $Log).Length -gt 1MB)) {
            Move-Item -Force $Log "$Log.1"
        }
        Add-Content -Path $Log -Value ("{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message)
    } catch { }
}

function Find-Usbipd {
    $cmd = Get-Command usbipd.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $path = Join-Path $env:ProgramFiles "usbipd-win\usbipd.exe"
    if (Test-Path $path) { return $path }
    return $null
}

# The RTL-SDRs in "usbipd list" (Connected section): @{BusId; Id; State}
function Get-RtlDevice([string]$Usbipd) {
    $out = & $Usbipd list 2>&1 | Out-String
    $devices = @()
    foreach ($line in ($out -split "`r?`n")) {
        if ($line -match '^\s*Persisted') { break }
        if ($line -match '^\s*(\d+-[\d.]+)\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s+(.+?)\s{2,}(\S.*?)\s*$') {
            if ($Ids -contains $Matches[2].ToLower()) {
                $devices += [pscustomobject]@{ BusId = $Matches[1]; Id = $Matches[2].ToLower(); State = $Matches[4] }
            }
        }
    }
    return $devices
}

$created = $false
# held (by $mutex) for the life of this process: a second copy exits at once
$mutex = [System.Threading.Mutex]::new($true, "Global\sizeup-usb-attach", [ref]$created)
if (-not $created) { exit 0 }

Write-SizeupLog "usb: watching for RTL-SDRs ($($Ids -join ', '))"
$last = @{}
while ($true) {
    $usbipd = Find-Usbipd
    if (-not $usbipd) {
        if ($last["usbipd"] -ne "missing") { Write-SizeupLog "usb: usbipd-win is not installed (run install-windows.ps1 again)" }
        $last["usbipd"] = "missing"
        Start-Sleep -Seconds 60
        continue
    }
    $last["usbipd"] = "ok"
    $seen = @{}
    foreach ($d in (Get-RtlDevice $usbipd)) {
        $seen[$d.BusId] = $true
        $msg = ""
        if ($d.State -match 'Not shared') {
            $r = & $usbipd bind --busid $d.BusId 2>&1 | Out-String
            $msg = "bind $($d.BusId) ($($d.Id)): exit $LASTEXITCODE $($r.Trim())"
        }
        if ($d.State -notmatch 'Attached') {
            $r = & $usbipd attach --wsl --busid $d.BusId 2>&1 | Out-String
            if ($LASTEXITCODE -eq 0) {
                $msg = "$msg attached $($d.BusId) ($($d.Id)) to WSL".Trim()
            } else {
                $msg = "$msg attach $($d.BusId) ($($d.Id)) failed (exit $LASTEXITCODE): $($r.Trim())".Trim()
            }
        } else {
            $msg = "$($d.BusId) ($($d.Id)) attached"
        }
        if ($last[$d.BusId] -ne $msg) { Write-SizeupLog "usb: $msg" }
        $last[$d.BusId] = $msg
    }
    foreach ($k in @($last.Keys)) {
        if ($k -ne "usbipd" -and -not $seen.ContainsKey($k)) {
            Write-SizeupLog "usb: $k unplugged"
            $last.Remove($k)
        }
    }
    Start-Sleep -Seconds $IntervalSeconds
}
