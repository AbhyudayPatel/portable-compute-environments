# sandbox-checkpoint - stop, KEEP the manifest index
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
docker compose down
Write-Host "Stopped. Checkpoint layers live on sandbox-api's sbx-state volume; the index was kept."
