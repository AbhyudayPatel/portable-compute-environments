# sandbox-blobs - stop, KEEP the blob volume
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
docker compose down
Write-Host "Stopped. Blobs and index kept (scripts/reset.ps1 wipes them)."
