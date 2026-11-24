#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Installs Social Agent on Windows Server and registers it to start at boot (and restart if it crashes).

.DESCRIPTION
  Run from an ELEVATED PowerShell:   .\deploy\windows\install.ps1
  It creates the virtual environment and a locked-down .env, then runs the guided setup (`socialagent setup`):
  you paste your Claude key and your Telegram bot token (hidden; checked live before they are saved), message the
  bot once so it learns your Telegram id, and answer a few questions about the account. Then it runs
  `socialagent selfcheck --live` and registers + starts the "SocialAgent" scheduled task.
  Re-run any time: finished steps are skipped (-Reconfigure runs the guided setup again).

  NOT TESTED: written without access to a Windows machine. Read it first; every step is also listed
  in README.md so you can run the commands by hand. Safe to re-run.

.PARAMETER Python    Path to python.exe (3.10+). Auto-detected if omitted.
.PARAMETER TaskName  Scheduled-task name (default SocialAgent).
.PARAMETER SkipTask  Do everything except registering the scheduled task.
.PARAMETER SkipSetup Do not run the guided setup (you will edit .env yourself).
.PARAMETER Reconfigure  Run the guided setup again even if .env is already filled in.
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$TaskName = "SocialAgent",
    [switch]$SkipTask,
    [switch]$SkipSetup,
    [switch]$Reconfigure
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
function Lock-EnvFile { icacls $envFile /inheritance:r /grant:r "*S-1-5-32-544:(F)" "*S-1-5-18:(F)" | Out-Null }
if (-not (Test-Path $envFile)) { Copy-Item (Join-Path $root ".env.example") $envFile }
Lock-EnvFile

# --- guided setup: validates the keys, finds your Telegram id, writes .env + config\brand.yaml ---------
# [^\s#] = a real value, not the "KEY=   # comment" placeholder of the template.
function Test-EnvValue([string]$key) { Select-String -Path $envFile -Pattern "^\s*$key\s*=\s*[^\s#]" -Quiet }
$configured = (Test-EnvValue "ANTHROPIC_API_KEY") -and (Test-EnvValue "TELEGRAM_BOT_TOKEN") -and (Test-EnvValue "OWNER_TELEGRAM_ID")
if (-not $SkipSetup -and ($Reconfigure -or -not $configured)) {
    # A running agent would be reading the bot's messages, which the setup needs to do itself.
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    & $venvPy -m socialagent setup
    $setupCode = $LASTEXITCODE
    Lock-EnvFile   # the setup rewrites .env; make sure it is still private afterwards
    if ($setupCode -ne 0) {
        Write-Host "Setup did not finish (nothing was posted or published). Re-run this script to try again." -ForegroundColor Yellow
        exit 1
    }
}
elseif (-not $configured) {
    Write-Host "Skipped the guided setup (-SkipSetup). Edit .env now (notepad .env), then run this script again." -ForegroundColor Yellow
    exit 0
}

# --- studio login: the web app needs a password before it can be opened from your phone ---------------
if (-not $SkipSetup) {
    & $venvPy -m socialagent studio-password --if-missing
    if ($LASTEXITCODE -ne 0) { Write-Host "No studio password was set. Run: .venv\Scripts\python.exe -m socialagent studio-password" -ForegroundColor Yellow }
}

# --- ffmpeg reminder ------------------------------------------------------------------------------
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -and -not (Test-EnvValue "FFMPEG_BIN")) {
    Write-Host "ffmpeg was not found on PATH and FFMPEG_BIN is empty in .env - set it to the full path of ffmpeg.exe." -ForegroundColor Yellow
}

# --- self-check -----------------------------------------------------------------------------------
& $venvPy -m socialagent selfcheck --live
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
