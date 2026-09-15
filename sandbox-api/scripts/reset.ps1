# sandbox-api - stop and WIPE volumes (inner images, SQLite store)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
docker compose down -v
Write-Host "Stopped and wiped. Next start re-pulls/rebuilds inner images."
