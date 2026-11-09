# cluster-in-a-box - stop and wipe the cluster database
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
docker compose down -v
Write-Host "Stopped and wiped."
