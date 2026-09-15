# sandbox-api - stop everything, KEEP volumes (inner images + metadata survive)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
docker compose down
Write-Host "Stopped. Inner engine images and the SQLite store were kept."
Write-Host "Use scripts/reset.ps1 to also wipe volumes."
