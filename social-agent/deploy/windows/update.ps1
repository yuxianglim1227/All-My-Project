#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Pull the latest code and restart Social Studio on the server (run after the first install.ps1).

.DESCRIPTION
  Run from an ELEVATED PowerShell inside the social-agent folder:   .\deploy\windows\update.ps1
  1. git pull (fast-forward only: it never overwrites local edits; .env and data\ are untouched, they are not in git)
  2. pip install -e .   (picks up new dependencies)
  3. restarts the SocialAgent scheduled task and checks /healthz on the configured port.
  NOT TESTED on a real Windows Server (written without one). Every step is an ordinary command you can run by hand.

.PARAMETER Branch    Git branch to deploy (default: the one currently checked out).
.PARAMETER TaskName  Scheduled-task name (default SocialAgent).
.PARAMETER NoGit     The code was copied in by deploy.ps1 (scp from your PC): skip the git pull.
#>
[CmdletBinding()]
param([string]$Branch = "", [string]$TaskName = "SocialAgent", [switch]$NoGit)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { throw "No virtual environment found: run .\deploy\windows\install.ps1 first." }

if ($NoGit) {
    Write-Host "Code was copied here by deploy.ps1 (scp); skipping git."
} else {
    if (-not $Branch) { $Branch = (git rev-parse --abbrev-ref HEAD).Trim() }
    Write-Host "Updating branch $Branch ..."
    git fetch origin $Branch
    if ($LASTEXITCODE -ne 0) { throw "git fetch failed (network or credentials)." }
    git checkout $Branch
    git pull --ff-only origin $Branch
    if ($LASTEXITCODE -ne 0) { throw "git pull could not fast-forward (local changes?). Nothing was restarted." }
}

& $venvPy -m pip install -e $root
if ($LASTEXITCODE -ne 0) { throw "pip install failed. Nothing was restarted." }

& $venvPy -m socialagent selfcheck
Write-Host "Restarting $TaskName ..."
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName $TaskName

$port = 8787
$line = Select-String -Path (Join-Path $root ".env") -Pattern '^\s*HTTP_PORT\s*=\s*(\d+)' | Select-Object -First 1
if ($line) { $port = [int]$line.Matches[0].Groups[1].Value }
$ok = $false
for ($i = 0; $i -lt 15 -and -not $ok; $i++) {
    Start-Sleep -Seconds 2
    try { $ok = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$port/healthz" -TimeoutSec 3).Content -eq "ok" } catch { }
}
if ($ok) { Write-Host "Up: http://127.0.0.1:$port/studio/ (and your public hostname)" -ForegroundColor Green }
else { Write-Host "The agent did not answer on port $port yet. Check: Get-Content data\agent.log -Tail 50" -ForegroundColor Yellow }
