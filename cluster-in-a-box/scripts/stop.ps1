# cluster-in-a-box - stop the control plane (workers are outer sandboxes; delete them via the API or sandbox-api)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
docker compose down
Write-Host "Stopped. Worker sandboxes (if any) still exist on sandbox-api."
