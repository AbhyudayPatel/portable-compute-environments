# sandbox-egress - stop and wipe the cassette catalogue (blobs stay in the store until GC)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
docker compose down -v
Write-Host "Stopped and wiped."
