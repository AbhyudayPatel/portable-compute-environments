# sandbox-checkpoint - stop and WIPE the manifest index (layers on the node
# stay until sandbox-api's volumes are reset or /gc runs)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
docker compose down -v
Write-Host "Stopped and wiped the index."
