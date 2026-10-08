# sizeup: keep the station's WSL distro running (scheduled task "sizeup-wsl", installed by
# install-windows.ps1 to C:\ProgramData\sizeup\wsl-keepalive.ps1).
#
# Why this pattern: WSL stops a distro (and then the VM) when no wsl.exe session is attached to it,
# whatever systemd is running inside. One long-lived "sleep infinity" session keeps it up, and with
# it systemd, which starts sizeup-station and restarts it on failure. If that session ends (WSL
# update, wsl --shutdown, a crash of the VM), this loop starts it again within seconds; if this loop
# itself dies, the task's 5-minute repeat trigger and restart-on-failure bring it back.
param(
    [string]$Distro = "Ubuntu-24.04"
)
$ErrorActionPreference = "Continue"
$Dir = Join-Path $env:ProgramData "sizeup"
$Log = Join-Path $Dir "wsl.log"
$env:WSL_UTF8 = "1"

function Write-SizeupLog([string]$Message) {
    try {
        if ((Test-Path $Log) -and ((Get-Item $Log).Length -gt 1MB)) {
            Move-Item -Force $Log "$Log.1"
        }
        Add-Content -Path $Log -Value ("{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message)
    } catch { }
}

# One copy at a time (a manual run next to the task's would only fight it).
$created = $false
# held (by $mutex) for the life of this process: a second copy exits at once
$mutex = [System.Threading.Mutex]::new($true, "Global\sizeup-wsl-keepalive", [ref]$created)
if (-not $created) { exit 0 }

$wsl = Join-Path $env:SystemRoot "System32\wsl.exe"
Write-SizeupLog "keepalive: starting for $Distro"
while ($true) {
    try {
        $p = Start-Process -FilePath $wsl -ArgumentList @("-d", $Distro, "-u", "root", "--exec", "/bin/sleep", "infinity") `
            -WindowStyle Hidden -PassThru
        Write-SizeupLog "keepalive: wsl.exe running (pid $($p.Id))"
        $p.WaitForExit()
        Write-SizeupLog "keepalive: wsl.exe ended (exit $($p.ExitCode)); starting it again in 10 s"
    } catch {
        Write-SizeupLog "keepalive: could not start wsl.exe: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds 10
}
