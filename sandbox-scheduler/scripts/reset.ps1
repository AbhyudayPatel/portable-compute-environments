# sandbox-scheduler - stop and WIPE the job DB volume
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
docker compose down -v
Write-Host "Stopped and wiped. sched-* sandboxes left in sandbox-api will be"
Write-Host "reaped as orphans on the next start (janitor sweep)."
