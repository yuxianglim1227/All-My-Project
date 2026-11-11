#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Installs Social Agent on Windows Server and registers it to start at boot (and restart if it crashes).

.DESCRIPTION
  Run from an ELEVATED PowerShell:   .\deploy\windows\install.ps1
  1st run: creates the virtual environment and a locked-down .env, then stops so you can edit .env.
  2nd run: runs `socialagent selfcheck`, then registers + starts the "SocialAgent" scheduled task.

  NOT TESTED: written without access to a Windows machine. Read it first; every step is also listed
  in README.md so you can run the commands by hand. Safe to re-run.

.PARAMETER Python    Path to python.exe (3.10+). Auto-detected if omitted.
.PARAMETER TaskName  Scheduled-task name (default SocialAgent).
.PARAMETER SkipTask  Do everything except registering the scheduled task.
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$TaskName = "SocialAgent",
    [switch]$SkipTask
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
Write-Host "Social Agent folder: $root"

# --- Python 3.10+ -------------------------------------------------------------------------------
if (-not $Python) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $Python = (& py -3 -c "import sys; print(sys.executable)").Trim()
    } else {
        $found = Get-Command python -ErrorAction SilentlyContinue
        if ($found) { $Python = $found.Source }
    }
}
if (-not $Python -or -not (Test-Path $Python)) {
    throw "Python 3.10+ not found. Install it from python.org (tick 'Add python.exe to PATH') and re-run."
}
$version = (& $Python -c "import sys; print('{}.{}'.format(*sys.version_info[:2]))").Trim()
if ([version]$version -lt [version]"3.10") { throw "Python $version found; 3.10 or newer is required." }
Write-Host "Using Python $version at $Python"

# --- virtual environment + package ---------------------------------------------------------------
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    & $Python -m venv (Join-Path $root ".venv")
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment." }
}
& $venvPy -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
& $venvPy -m pip install -e $root
if ($LASTEXITCODE -ne 0) { throw "pip install failed." }

# --- .env (secrets): readable only by Administrators and SYSTEM -----------------------------------
# SIDs instead of names so this also works on non-English Windows: S-1-5-32-544 = Administrators, S-1-5-18 = SYSTEM.
$envFile = Join-Path $root ".env"
$firstTime = -not (Test-Path $envFile)
if ($firstTime) { Copy-Item (Join-Path $root ".env.example") $envFile }
icacls $envFile /inheritance:r /grant:r "*S-1-5-32-544:(F)" "*S-1-5-18:(F)" | Out-Null
if ($firstTime) {
    Write-Host ""
    Write-Host "Created .env (kept private to Administrators). Edit it now:  notepad .env" -ForegroundColor Yellow
    Write-Host "Then run this script again to run the self-check and start the service." -ForegroundColor Yellow
    exit 0
}

# --- ffmpeg reminder ------------------------------------------------------------------------------
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -and -not (Select-String -Path $envFile -Pattern '^\s*FFMPEG_BIN\s*=\s*\S' -Quiet)) {
    Write-Host "ffmpeg was not found on PATH and FFMPEG_BIN is empty in .env - set it to the full path of ffmpeg.exe." -ForegroundColor Yellow
}

# --- self-check -----------------------------------------------------------------------------------
& $venvPy -m socialagent selfcheck
if ($LASTEXITCODE -ne 0) {
    Write-Host "selfcheck reported problems (see above). Fix them, then re-run." -ForegroundColor Red
    exit 1
}
if ($SkipTask) { Write-Host "Skipped the scheduled task (-SkipTask)."; exit 0 }

# --- start at boot, restart on failure, run as SYSTEM, no time limit -------------------------------
$action    = New-ScheduledTaskAction -Execute $venvPy -Argument "-m socialagent run" -WorkingDirectory $root
$trigger   = New-ScheduledTaskTrigger -AtStartup
$settings  = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
                 -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "Started scheduled task '$TaskName'. It will also start automatically after every reboot." -ForegroundColor Green
Write-Host "Logs:    Get-Content `"$root\data\agent.log`" -Tail 50 -Wait"
Write-Host "Status:  Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "Stop:    Stop-ScheduledTask -TaskName $TaskName      Start: Start-ScheduledTask -TaskName $TaskName"
