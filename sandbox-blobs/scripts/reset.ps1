# sandbox-blobs - stop and WIPE the blob volume
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
docker compose down -v
Write-Host "Stopped and wiped."
