# sandbox-scheduler - stop, KEEP volumes (job DB survives)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
docker compose down
Write-Host "Stopped. Job DB kept. Use scripts/reset.ps1 to also wipe it."
Write-Host "Note: sandboxes it created live in sandbox-api and keep running;"
Write-Host "on next start the janitor reconciles them."
