<#
.SYNOPSIS
  Deploy Social Studio from YOUR PC to the AWS server, the same way as bbtracker: copy with scp, then start it.

.DESCRIPTION
  Run on your PC (Windows PowerShell 5.1+ or PowerShell 7), from anywhere inside your local clone of the repository:
      .\social-agent\deploy\deploy.ps1
  What it does, every time:
    1. Gets the latest code from GitHub into your local clone (git pull, fast-forward only).
    2. Packs the social-agent folder with `git archive` - only files in git, so your .env, data\ and .venv are never
       copied or overwritten (secrets stay on the server).
    3. Copies the package to the server with scp and unpacks it into the server folder.
    4. On the server: first time -> runs install.ps1 (creates the Python environment, asks the setup questions in this
       window, registers the start-at-boot task and starts it); later -> runs update.ps1 -NoGit (install + restart +
       health check).
  Settings come from deploy\deploy.local.json next to this script (git-ignored; copy deploy.example.json).
  Needs the same things bbtracker's deploy needs: ssh and scp on your PC, and SSH access to the server.
  NOT TESTED against your server (written without access to it). Every step prints what it runs.

.PARAMETER Config  Path to the settings file (default: deploy.local.json next to this script).
.PARAMETER NoPull  Deploy what is in your local clone without pulling from GitHub first.
#>
[CmdletBinding()]
param([string]$Config = "", [switch]$NoPull)

$ErrorActionPreference = "Stop"
if (-not $Config) { $Config = Join-Path $PSScriptRoot "deploy.local.json" }
if (-not (Test-Path $Config)) {
    throw "Missing $Config. Copy deploy.example.json to deploy.local.json and fill in your server (same host, user and key as bbtracker)."
}
$cfg = Get-Content $Config -Raw | ConvertFrom-Json
foreach ($k in "host", "user", "remote_dir") {
    if (-not $cfg.$k) { throw "deploy.local.json: '$k' is empty." }
}
$port = if ($cfg.port) { [string]$cfg.port } else { "22" }
$branch = if ($cfg.branch) { [string]$cfg.branch } else { "" }
$target = "$($cfg.user)@$($cfg.host)"
$remoteDir = ([string]$cfg.remote_dir).TrimEnd('\', '/')
$remoteScp = $remoteDir -replace '\\', '/'
$sshArgs = @("-p", $port, "-o", "ServerAliveInterval=30")
$scpArgs = @("-P", $port)
if ($cfg.key) {
    if (-not (Test-Path $cfg.key)) { throw "SSH key not found: $($cfg.key)" }
    $sshArgs += @("-i", $cfg.key); $scpArgs += @("-i", $cfg.key)
}
foreach ($tool in "git", "ssh", "scp") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool is not installed on this PC." }
}

# Remote commands go as -EncodedCommand (base64 UTF-16LE): no quoting rules of cmd.exe, ssh or PowerShell 5.1 can mangle them.
function Remote([string]$script) {
    return "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand " + [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
}

function Run([string]$what, [string]$exe, [string[]]$argList) {
    Write-Host "-> $what" -ForegroundColor Cyan
    & $exe @argList
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)." }
}

# 1. latest code from GitHub ---------------------------------------------------------------------------
$repo = (& git -C $PSScriptRoot rev-parse --show-toplevel).Trim()
if (-not $NoPull) {
    if (-not $branch) { $branch = (& git -C $repo rev-parse --abbrev-ref HEAD).Trim() }
    Run "git fetch $branch" "git" @("-C", $repo, "fetch", "origin", $branch)
    Run "git checkout $branch" "git" @("-C", $repo, "checkout", $branch)
    Run "git pull" "git" @("-C", $repo, "pull", "--ff-only", "origin", $branch)
}
$sha = (& git -C $repo rev-parse --short HEAD).Trim()

# 2. package (tracked files only: no .env, data or .venv) -----------------------------------------------
$zip = Join-Path ([IO.Path]::GetTempPath()) "social-agent-$sha.zip"
Run "pack social-agent @ $sha" "git" @("-C", $repo, "archive", "--format=zip", "-o", $zip, "HEAD", "social-agent")

# 3. copy + unpack on the server --------------------------------------------------------------------------
$mk = "New-Item -ItemType Directory -Force -Path '$remoteDir' | Out-Null"
Run "create $remoteDir on the server" "ssh" ($sshArgs + @($target, (Remote $mk)))
Run "scp package to the server" "scp" ($scpArgs + @($zip, "${target}:$remoteScp/social-agent-deploy.zip"))
$unpack = "Expand-Archive -Force -Path '$remoteDir\social-agent-deploy.zip' -DestinationPath '$remoteDir'; " +
          "Remove-Item -Force '$remoteDir\social-agent-deploy.zip'; Set-Content -Path '$remoteDir\social-agent\DEPLOYED_VERSION' -Value '$sha'"
Run "unpack on the server" "ssh" ($sshArgs + @($target, (Remote $unpack)))
Remove-Item -Force $zip

# 4. install (first time) or update + restart ------------------------------------------------------------
$apply = "$remoteDir\social-agent\deploy\windows\remote-apply.ps1"
Write-Host "-> install/update and start on the server (the first time it asks the setup questions here)" -ForegroundColor Cyan
& ssh -t @sshArgs $target (Remote "& '$apply'; exit `$LASTEXITCODE")
if ($LASTEXITCODE -ne 0) { throw "The server-side install/update reported a problem (exit $LASTEXITCODE). See the messages above." }
Write-Host "Deployed $sha to ${target}:$remoteDir\social-agent" -ForegroundColor Green
