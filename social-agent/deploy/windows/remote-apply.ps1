<#
.SYNOPSIS
  Runs ON THE SERVER after deploy.ps1 has copied new code in: first install, or update + restart.
  You can also run it by hand in an elevated PowerShell on the server.
#>
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if ((Test-Path $venvPy) -and (Test-Path (Join-Path $root ".env"))) {
    Write-Host "Existing install found: updating and restarting."
    & (Join-Path $PSScriptRoot "update.ps1") -NoGit
} else {
    Write-Host "First install: creating the environment, asking the setup questions, registering the start-at-boot task."
    & (Join-Path $PSScriptRoot "install.ps1")
}
exit $LASTEXITCODE
