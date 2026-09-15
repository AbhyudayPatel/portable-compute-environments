#requires -Version 5.1
<#
.SYNOPSIS
  Stops the environment. The workspace, database and Git history are all
  preserved in Docker volumes — the next start picks up exactly where you
  left off.
#>
$ErrorActionPreference = 'Stop'
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $ProjectRoot

docker compose down
if ($LASTEXITCODE -ne 0) { throw 'docker compose down failed' }

Write-Host '[OK] Environment stopped. Workspace, database and Git history are preserved.' -ForegroundColor Green
