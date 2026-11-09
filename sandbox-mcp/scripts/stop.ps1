# sandbox-mcp - stop, keep the audit trail
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
docker compose --profile test down
Write-Host "Stopped. Audit trail kept (scripts/reset.ps1 wipes it)."
