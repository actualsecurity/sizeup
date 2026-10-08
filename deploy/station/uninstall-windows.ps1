<#
.SYNOPSIS
  Remove the sizeup station's Windows side: the scheduled tasks (sizeup-wsl, sizeup-usb,
  sizeup-install-resume) and their scripts. WSL, the distribution (with the station, its data and its
  secrets inside) and usbipd-win stay; so do the power settings.

.DESCRIPTION
  Run in an elevated PowerShell:  powershell -ExecutionPolicy Bypass -File .\uninstall-windows.ps1
  -Unshare also stops sharing the RTL-SDRs with WSL (usbipd unbind), so Windows apps can use them.
  To remove the station itself as well (everything in it, for good):  wsl --unregister Ubuntu-24.04
#>
[CmdletBinding()]
param(
    [switch]$Unshare
)
$ErrorActionPreference = "Continue"
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Run this in an elevated PowerShell (Run as administrator)." -ForegroundColor Red
    exit 1
}
$Dir = Join-Path $env:ProgramData "sizeup"
$LogFile = Join-Path $Dir "install.log"
function Write-SizeupLog([string]$Message) {
    Write-Host $Message
    if (Test-Path $Dir) { Add-Content -Path $LogFile -Value ("{0}  uninstall: {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message) }
}

foreach ($name in @("sizeup-wsl", "sizeup-usb", "sizeup-install-resume")) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-SizeupLog "removed task $name"
    }
}
# The loops the tasks ran (they hold these script paths on their command lines).
Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" | Where-Object {
    $_.CommandLine -match 'sizeup\\(wsl-keepalive|usb-attach)\.ps1'
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    Write-SizeupLog "stopped $($_.ProcessId)"
}
# ...and the keep-alive's wsl.exe session (WSL may then stop when nothing else uses it).
Get-CimInstance Win32_Process -Filter "Name = 'wsl.exe'" | Where-Object { $_.CommandLine -match '/bin/sleep infinity' } | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
foreach ($f in @("wsl-keepalive.ps1", "usb-attach.ps1")) {
    $path = Join-Path $Dir $f
    if (Test-Path $path) { Remove-Item -Force $path }
}

if ($Unshare) {
    $usbipd = Get-Command usbipd.exe -ErrorAction SilentlyContinue
    $exe = if ($usbipd) { $usbipd.Source } else { Join-Path $env:ProgramFiles "usbipd-win\usbipd.exe" }
    if (Test-Path $exe) {
        foreach ($id in @("0bda:2838", "0bda:2832")) {
            & $exe unbind --hardware-id $id 2>&1 | Out-Null
        }
        Write-SizeupLog "RTL-SDRs no longer shared with WSL"
    }
}

Write-SizeupLog "Done. The station no longer starts with Windows; WSL and its data are untouched."
Write-SizeupLog "It still runs until WSL stops (wsl --shutdown). Logs stay in $Dir."
