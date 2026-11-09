# sandbox-egress - stop, keep the cassette catalogue
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
docker compose down
Write-Host "Stopped. Cassette catalogue kept."
