<#
.SYNOPSIS
  Install the sizeup station on Windows 10/11: WSL2 + Ubuntu 24.04 + usbipd-win, the station inside
  it (install-linux.sh), and the scheduled tasks that bring it all back after any reboot.

.DESCRIPTION
  Run it in an elevated PowerShell (Run as administrator), from the deploy\station folder copied to
  this PC, as the Windows account that will own the station:

      powershell -ExecutionPolicy Bypass -File .\install-windows.ps1

  Safe to run again at any time (= repair). If Windows needs a restart to turn WSL on, it says so,
  restarts when you press Enter, and carries on by itself when you sign in again.
  Log: C:\ProgramData\sizeup\install.log. deploy\station\README.md has the whole story.

.PARAMETER Distro
  The WSL distribution the station runs in (default Ubuntu-24.04).
.PARAMETER Model
  whisper model: small.en-q5_1 (default), large-v3-turbo-q5_0 or base.en.
.PARAMETER Ref
  Branch or tag of the sizeup repo to run (default main).
.PARAMETER MemoryGB
  Memory for the WSL VM (default 8 of the 16 GB).
.PARAMETER Processors
  CPUs for the WSL VM (default: all of them).
.PARAMETER NoPassword
  Register the tasks without storing your Windows password (S4U). Then they may only start once you
  sign in: see the README ("Starting before anyone signs in").
#>
[CmdletBinding()]
param(
    [string]$Distro = "Ubuntu-24.04",
    [ValidateSet("small.en-q5_1", "large-v3-turbo-q5_0", "base.en")]
    [string]$Model = "small.en-q5_1",
    [string]$Ref = "main",
    [string]$Repo = "git@github.com:actualsecurity/sizeup.git",
    [int]$MemoryGB = 8,
    [int]$Processors = 0,
    [switch]$NoPassword,
    [switch]$Resume
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$env:WSL_UTF8 = "1"

$Home_ = Join-Path $env:ProgramData "sizeup"
$Stage = Join-Path $Home_ "station"
$LogFile = Join-Path $Home_ "install.log"
$Wsl = Join-Path $env:SystemRoot "System32\wsl.exe"
$RtlIds = @("0bda:2838", "0bda:2832")
# usbipd-win, if winget isn't there: pinned MSI, SHA-256 from the release's asset digest.
$UsbipdVersion = "5.3.0"
$UsbipdMsiSha256 = @{
    "x64"   = "1c984914aec944de19b64eff232421439629699f8138e3ddc29301175bc6d938"
    "arm64" = "efd7c4eb99b144c1623e616064a7b262f83d0994b0d7fde16c95d4b07528b24d"
}

New-Item -ItemType Directory -Force -Path $Home_ | Out-Null

function Write-SizeupLog([string]$Message, [string]$Color = "") {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $LogFile -Value $line
    if ($Color) { Write-Host $Message -ForegroundColor $Color } else { Write-Host $Message }
}
function Step([string]$Message) { Write-SizeupLog ""; Write-SizeupLog "== $Message" "Cyan" }
function Fail([string]$Message) {
    Write-SizeupLog "FAILED: $Message" "Red"
    Write-SizeupLog "Fix it and run install-windows.ps1 again: it picks up where it left off. Log: $LogFile" "Red"
    exit 1
}

# wsl.exe on this console (a real terminal inside: the Linux installer's hidden prompts work), its
# exit code back. Nothing typed there passes through PowerShell or this log.
function Invoke-Wsl([string[]]$Arguments) {
    $p = Start-Process -FilePath $Wsl -ArgumentList $Arguments -NoNewWindow -Wait -PassThru
    return $p.ExitCode
}
function Get-WslText([string[]]$Arguments) {
    $out = & $Wsl @Arguments 2>&1 | Out-String
    return ($out -replace "`0", "")
}
function Test-Distro {
    $names = (Get-WslText @("--list", "--quiet")) -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    return ($names -contains $Distro)
}
function Find-Usbipd {
    $cmd = Get-Command usbipd.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $path = Join-Path $env:ProgramFiles "usbipd-win\usbipd.exe"
    if (Test-Path $path) { return $path }
    return $null
}

# -- 0. administrator, who, where -----------------------------------------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Run this in an elevated PowerShell: right-click PowerShell > Run as administrator." -ForegroundColor Red
    exit 1
}
$User = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$os = Get-CimInstance Win32_OperatingSystem
Write-SizeupLog ""
Write-SizeupLog "sizeup station install for Windows ($($os.Caption) $($os.Version), as $User)$(if ($Resume) { ' - resuming after the restart' })" "Cyan"
if ([int]$os.BuildNumber -lt 19041) { Fail "WSL2 needs Windows 10 version 2004 (build 19041) or later; this is build $($os.BuildNumber). Run Windows Update first." }
$Arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "x64" }
if ($Processors -le 0) { $Processors = [Environment]::ProcessorCount }

# The scripts in here run elevated at every boot: only administrators, SYSTEM and you may change them.
& icacls.exe $Home_ /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "${User}:(OI)(CI)F" "*S-1-5-32-545:(OI)(CI)RX" | Out-Null

# A copy of deploy\station that outlives the folder you ran this from (and the restart).
Step "Copying the station files to $Stage"
$here = $PSScriptRoot
if ((Resolve-Path $here).Path.TrimEnd('\') -ne $Stage.TrimEnd('\')) {
    if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
    Copy-Item -Path $here -Destination $Stage -Recurse -Force     # (a new folder: no nesting surprises)
}
Get-ChildItem -Path $Stage -Recurse -File | Unblock-File -ErrorAction SilentlyContinue
# Linux reads these: LF line endings, UTF-8 without BOM, whatever the copy did to them.
$utf8 = New-Object System.Text.UTF8Encoding($false)
Get-ChildItem -Path $Stage -Recurse -File | Where-Object { $_.Extension -in @(".sh", ".toml", ".service", ".timer", ".conf", "") } | ForEach-Object {
    $text = [IO.File]::ReadAllText($_.FullName)
    if ($text.Contains("`r`n")) { [IO.File]::WriteAllText($_.FullName, $text.Replace("`r`n", "`n"), $utf8) }
}
foreach ($f in @("install-linux.sh", "config.station.toml", "windows\wsl-keepalive.ps1", "windows\usb-attach.ps1")) {
    if (-not (Test-Path (Join-Path $Stage $f))) { Fail "$f is missing from ${here}: copy the whole deploy\station folder" }
}
Copy-Item -Force (Join-Path $Stage "windows\wsl-keepalive.ps1") (Join-Path $Home_ "wsl-keepalive.ps1")
Copy-Item -Force (Join-Path $Stage "windows\usb-attach.ps1") (Join-Path $Home_ "usb-attach.ps1")

# -- 1. Windows features ---------------------------------------------------------------------------
Step "Windows features: WSL and Virtual Machine Platform"
$cs = Get-CimInstance Win32_ComputerSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
if (-not $cs.HypervisorPresent -and -not $cpu.VirtualizationFirmwareEnabled) {
    Write-SizeupLog "Virtualization (Intel VT-x) looks OFF in the BIOS/UEFI: WSL2 needs it. Turn it on there if WSL fails to start." "Yellow"
}
$restart = $false
foreach ($feature in @("Microsoft-Windows-Subsystem-Linux", "VirtualMachinePlatform")) {
    $state = (Get-WindowsOptionalFeature -Online -FeatureName $feature).State
    if ($state -eq "Enabled") { Write-SizeupLog "$feature`: on"; continue }
    if ($state -eq "EnablePending") { Write-SizeupLog "$feature`: on after a restart"; $restart = $true; continue }
    Write-SizeupLog "$feature`: turning it on"
    & dism.exe /online /enable-feature "/featurename:$feature" /all /norestart | Out-Null
    if ($LASTEXITCODE -eq 3010) { $restart = $true }
    elseif ($LASTEXITCODE -ne 0) { Fail "dism could not enable $feature (exit $LASTEXITCODE)" }
    else { $restart = $true }      # a feature just turned on: WSL2 works after the restart
}

$ResumeTask = "sizeup-install-resume"
if ($restart) {
    Step "A restart is needed"
    $argList = "-NoProfile -ExecutionPolicy Bypass -NoExit -File `"$Stage\install-windows.ps1`" -Resume -Distro `"$Distro`" -Model $Model -Ref `"$Ref`" -Repo `"$Repo`" -MemoryGB $MemoryGB -Processors $Processors"
    if ($NoPassword) { $argList += " -NoPassword" }
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argList
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
    $p = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
    Register-ScheduledTask -TaskName $ResumeTask -Action $action -Trigger $trigger -Principal $p -Force | Out-Null
    Write-SizeupLog "After the restart, sign in as ${User}: the install carries on in a PowerShell window by itself." "Yellow"
    Read-Host "Press Enter to restart now (or Ctrl+C and restart later yourself)"
    Restart-Computer -Force
    exit 0
}
if (Get-ScheduledTask -TaskName $ResumeTask -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $ResumeTask -Confirm:$false
}

# -- 2. WSL itself ---------------------------------------------------------------------------------
Step "WSL: update, version 2"
if ((Invoke-Wsl @("--update")) -ne 0) { Write-SizeupLog "wsl --update didn't finish cleanly; carrying on (it may already be current)" "Yellow" }
Invoke-Wsl @("--set-default-version", "2") | Out-Null

# The VM: memory, CPUs, and never shut down for being idle (the station is always on).
$wslconfig = Join-Path $env:USERPROFILE ".wslconfig"
$want = [ordered]@{ "memory" = "$($MemoryGB)GB"; "processors" = "$Processors"; "vmIdleTimeout" = "-1" }
$lines = @()
if (Test-Path $wslconfig) { $lines = @(Get-Content $wslconfig) }
$out = New-Object System.Collections.Generic.List[string]
$section = ""; $done = @{}
foreach ($line in $lines) {
    if ($line -match '^\s*\[(.+)\]\s*$') {
        if ($section -eq "wsl2") { foreach ($k in $want.Keys) { if (-not $done[$k]) { $out.Add("$k=$($want[$k])"); $done[$k] = $true } } }
        $section = $Matches[1].Trim().ToLower()
        $out.Add($line); continue
    }
    if ($section -eq "wsl2" -and $line -match '^\s*([A-Za-z0-9]+)\s*=') {
        $k = $Matches[1]
        $hit = $want.Keys | Where-Object { $_ -ieq $k } | Select-Object -First 1
        if ($hit) { $out.Add("$hit=$($want[$hit])"); $done[$hit] = $true; continue }
    }
    $out.Add($line)
}
if ($section -eq "wsl2") { foreach ($k in $want.Keys) { if (-not $done[$k]) { $out.Add("$k=$($want[$k])"); $done[$k] = $true } } }
if (-not ($lines | Where-Object { $_ -match '^\s*\[wsl2\]\s*$' })) {
    $out.Add("[wsl2]")
    foreach ($k in $want.Keys) { $out.Add("$k=$($want[$k])") }
}
$newText = ($out -join "`r`n") + "`r`n"
$oldText = if (Test-Path $wslconfig) { [IO.File]::ReadAllText($wslconfig) } else { "" }
$vmChanged = $newText -ne $oldText
if ($vmChanged) { [IO.File]::WriteAllText($wslconfig, $newText, $utf8); Write-SizeupLog ".wslconfig: memory $($MemoryGB)GB, $Processors CPUs, never idle-stopped" }

# -- 3. the distribution ---------------------------------------------------------------------------
Step "WSL distribution $Distro"
if (Test-Distro) {
    Write-SizeupLog "$Distro is installed"
} else {
    Write-SizeupLog "Installing $Distro (a few minutes)"
    $rc = Invoke-Wsl @("--install", "-d", $Distro, "--no-launch", "--web-download")
    if ($rc -ne 0) { $rc = Invoke-Wsl @("--install", "-d", $Distro, "--no-launch") }
    if (-not (Test-Distro)) {
        # Store-style package installed but not registered yet: register it with root, no questions.
        $launcher = Get-Command (($Distro -replace '[-.]', '').ToLower() + ".exe") -ErrorAction SilentlyContinue
        if ($launcher) { & $launcher.Source install --root | Out-Null }
    }
    if (-not (Test-Distro) -and $Distro -eq "Ubuntu-24.04") {
        # Last resort: Ubuntu's own WSL image, checked against its SHA256SUMS.
        $base = "https://cloud-images.ubuntu.com/wsl/releases/24.04/current"
        $arch = if ($Arch -eq "arm64") { "arm64" } else { "amd64" }
        $file = "ubuntu-noble-wsl-$arch-wsl.rootfs.tar.gz"
        $tar = Join-Path $Home_ $file
        Write-SizeupLog "Downloading $base/$file"
        Invoke-WebRequest -UseBasicParsing -Uri "$base/$file" -OutFile $tar
        $sums = (Invoke-WebRequest -UseBasicParsing -Uri "$base/SHA256SUMS").Content
        $line = ($sums -split "`n") | Where-Object { $_ -match [regex]::Escape($file) + '$' } | Select-Object -First 1
        if (-not $line) { Fail "no checksum for $file in SHA256SUMS" }
        $sum = ($line -split '\s+')[0].ToLower()
        if ((Get-FileHash -Algorithm SHA256 $tar).Hash.ToLower() -ne $sum) { Remove-Item -Force $tar; Fail "${file}: checksum mismatch" }
        $dir = Join-Path $Home_ "wsl\$Distro"
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        if ((Invoke-Wsl @("--import", $Distro, $dir, $tar, "--version", "2")) -ne 0) { Fail "wsl --import failed" }
        Remove-Item -Force $tar
    }
    if (-not (Test-Distro)) { Fail "$Distro didn't install. Try: wsl --install -d $Distro, then run this again." }
    Write-SizeupLog "$Distro installed"
}

# systemd on inside it (wsl.conf), then restart WSL so systemd and .wslconfig take effect.
$stageLinux = (Get-WslText @("-d", $Distro, "-u", "root", "--exec", "wslpath", "-a", $Stage)).Trim()
if (-not $stageLinux.StartsWith("/")) { $stageLinux = "/mnt/" + $Stage.Substring(0, 1).ToLower() + ($Stage.Substring(2) -replace '\\', '/') }
if ((Invoke-Wsl @("-d", $Distro, "-u", "root", "--exec", "bash", "$stageLinux/install-linux.sh", "--wsl-prepare")) -ne 0) {
    Fail "could not write /etc/wsl.conf in $Distro"
}
$systemd = (Get-WslText @("-d", $Distro, "-u", "root", "--exec", "sh", "-c", "test -d /run/systemd/system && echo yes")).Trim()
if ($systemd -ne "yes" -or $vmChanged) {
    Write-SizeupLog "Restarting WSL so systemd and the VM settings take effect"
    Invoke-Wsl @("--shutdown") | Out-Null
    Start-Sleep -Seconds 8
}

# -- 4. usbipd-win: the RTL-SDRs into WSL ----------------------------------------------------------
Step "usbipd-win (USB into WSL)"
$usbipd = Find-Usbipd
if (-not $usbipd) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
        & $winget.Source install --exact --id dorssel.usbipd-win --silent --accept-source-agreements --accept-package-agreements | Out-Null
    }
    $usbipd = Find-Usbipd
}
if (-not $usbipd) {
    $msi = Join-Path $Home_ "usbipd-win_$($UsbipdVersion)_$Arch.msi"
    $url = "https://github.com/dorssel/usbipd-win/releases/download/v$UsbipdVersion/usbipd-win_$($UsbipdVersion)_$Arch.msi"
    Write-SizeupLog "Downloading $url"
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $msi
    if ((Get-FileHash -Algorithm SHA256 $msi).Hash.ToLower() -ne $UsbipdMsiSha256[$Arch]) { Remove-Item -Force $msi; Fail "usbipd-win MSI checksum mismatch" }
    $p = Start-Process msiexec.exe -ArgumentList @("/i", "`"$msi`"", "/qn", "/norestart") -Wait -PassThru
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { Fail "usbipd-win MSI install failed (exit $($p.ExitCode))" }
    $usbipd = Find-Usbipd
}
if (-not $usbipd) { Fail "usbipd-win isn't installed: winget install --exact dorssel.usbipd-win, then run this again" }
Write-SizeupLog "usbipd: $usbipd ($((& $usbipd --version 2>&1 | Out-String).Trim()))"
$listing = & $usbipd list 2>&1 | Out-String
$found = 0
foreach ($line in ($listing -split "`r?`n")) {
    if ($line -match '^\s*Persisted') { break }
    if ($line -match '^\s*(\d+-[\d.]+)\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s+(.+?)\s{2,}(\S.*?)\s*$' -and ($RtlIds -contains $Matches[2].ToLower())) {
        $found++
        $busid = $Matches[1]
        if ($Matches[4] -match 'Not shared') {
            & $usbipd bind --busid $busid | Out-Null
            Write-SizeupLog "shared RTL-SDR $busid ($($Matches[2]))"
        } else {
            Write-SizeupLog "RTL-SDR $busid ($($Matches[2])): $($Matches[4])"
        }
    }
}
if ($found -eq 0) { Write-SizeupLog "No RTL-SDR plugged in right now: plug them in any time, they're attached within ~10 s." "Yellow" }

# -- 5. power: never asleep -------------------------------------------------------------------------
Step "Power: no sleep, no hibernate, USB never suspended"
& powercfg.exe /change standby-timeout-ac 0 | Out-Null
& powercfg.exe /change hibernate-timeout-ac 0 | Out-Null
& powercfg.exe /hibernate off | Out-Null      # also turns off Fast Startup: a real boot runs the startup tasks
# USB selective suspend: off (subgroup USB settings / USB selective suspend setting)
& powercfg.exe /setacvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0 | Out-Null
& powercfg.exe /setdcvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0 | Out-Null
& powercfg.exe /setactive SCHEME_CURRENT | Out-Null
Write-SizeupLog "done"

# -- 6. scheduled tasks: WSL kept running, dongles kept attached -----------------------------------
Step "Scheduled tasks sizeup-wsl and sizeup-usb"
$password = $null
if (-not $NoPassword) {
    Write-SizeupLog "The tasks run as $User whether or not anyone is signed in, so Windows needs that account's password once (stored by Task Scheduler, not by sizeup)."
    $cred = Get-Credential -UserName $User -Message "Password of $User for the sizeup tasks (Cancel = only while signed in)"
    if ($cred) { $password = $cred.GetNetworkCredential().Password }
}
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
$settings.DisallowStartIfOnBatteries = $false
$settings.StopIfGoingOnBatteries = $false
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$triggers = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn -User $User), $repeat)
$tasks = [ordered]@{
    "sizeup-wsl" = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Home_\wsl-keepalive.ps1`" -Distro `"$Distro`""
    "sizeup-usb" = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Home_\usb-attach.ps1`""
}
foreach ($name in $tasks.Keys) {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $tasks[$name] -WorkingDirectory $Home_
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
    $desc = "sizeup station ($name): see C:\ProgramData\sizeup and deploy\station\README.md"
    if ($password) {
        Register-ScheduledTask -TaskName $name -Description $desc -Action $action -Trigger $triggers -Settings $settings `
            -User $User -Password $password -RunLevel Highest | Out-Null
        $mode = "whether or not anyone is signed in"
    } else {
        $p = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Highest
        Register-ScheduledTask -TaskName $name -Description $desc -Action $action -Trigger $triggers -Settings $settings `
            -Principal $p | Out-Null
        $mode = "without a stored password (S4U)"
    }
    Start-ScheduledTask -TaskName $name
    Write-SizeupLog "$name`: registered ($mode) and started"
}
$password = $null
$cred = $null

# Give the keep-alive a moment to bring WSL (and systemd) up, then check.
$up = $false
for ($i = 0; $i -lt 30; $i++) {
    if ((Get-WslText @("-d", $Distro, "-u", "root", "--exec", "sh", "-c", "test -d /run/systemd/system && echo yes")).Trim() -eq "yes") { $up = $true; break }
    Start-Sleep -Seconds 2
}
if (-not $up) { Fail "systemd isn't running in $Distro (check /etc/wsl.conf, then wsl --shutdown and run this again)" }

# -- 7. the station inside WSL ---------------------------------------------------------------------
Step "The station inside $Distro (install-linux.sh: builds take 20-40 minutes the first time)"
Write-SizeupLog "It will show a GitHub deploy key to add, then ask for the hub token: answer in this window."
$linuxArgs = @("-d", $Distro, "-u", "root", "--exec", "bash", "$stageLinux/install-linux.sh", "--model", $Model, "--ref", $Ref, "--repo", $Repo)
if ((Invoke-Wsl $linuxArgs) -ne 0) { Fail "install-linux.sh failed inside $Distro (its log: /var/log/sizeup-install.log in WSL)" }

# -- 8. done ---------------------------------------------------------------------------------------
Step "Done"
Invoke-Wsl @("-d", $Distro, "-u", "root", "--exec", "/usr/local/bin/sizeup-station", "doctor") | Out-Null
Write-SizeupLog "The station runs inside WSL and starts by itself after every restart or power cut." "Green"
Write-SizeupLog "  Diagnostics page:  http://localhost:8091" "Green"
Write-SizeupLog "  Status / checks:   wsl -d $Distro -u root sizeup-station status   (or: doctor, logs, update, secrets)" "Green"
Write-SizeupLog ""
Write-SizeupLog "Two things to set by hand, once:" "Yellow"
Write-SizeupLog "  1. BIOS/UEFI: 'Restore on AC power loss' (or 'After power failure') = Power On, so it boots after an outage." "Yellow"
Write-SizeupLog "  2. Settings > Accounts > Sign-in options: if the station only comes up after someone signs in, turn on automatic sign-in (README)." "Yellow"
Write-SizeupLog "Windows Update restarts are fine: the tasks bring everything back after each one." "Yellow"
