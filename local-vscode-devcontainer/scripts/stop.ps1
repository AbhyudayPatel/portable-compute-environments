#requires -Version 5.1
<#
.SYNOPSIS
  Stops the standalone application stack. (The Dev Containers stack stops
  automatically when you close VS Code — devcontainer.json sets
  "shutdownAction": "stopCompose".)
#>
$ErrorActionPreference = 'Stop'
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location (Join-Path $ProjectRoot 'company-app')

docker compose down
Write-Host '[OK] Application stack stopped. The postgres volume (coreapp_pgdata) is preserved.' -ForegroundColor Green
