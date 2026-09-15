#requires -Version 5.1
<#
.SYNOPSIS
  Stops the environment. Workspace, database, desktop settings and Git
  history are all preserved in Docker volumes.
#>
$ErrorActionPreference = 'Stop'
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $ProjectRoot

docker compose down
if ($LASTEXITCODE -ne 0) { throw 'docker compose down failed' }

Write-Host '[OK] Environment stopped. Workspace, database, desktop settings and Git history are preserved.' -ForegroundColor Green
