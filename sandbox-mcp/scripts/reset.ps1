# sandbox-mcp - stop and wipe the audit trail
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
docker compose --profile test down -v
Write-Host "Stopped and wiped."
