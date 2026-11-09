# sandbox-checkpoint - one-command start (Windows host)
# Prerequisites: ..\sandbox-api and ..\sandbox-blobs running.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
foreach ($net in @("sandbox-api_sandnet", "sandbox-blobs")) {
  docker network inspect $net *> $null
  if ($LASTEXITCODE -ne 0) { throw "network $net missing - start ..\sandbox-api and ..\sandbox-blobs first" }
}
docker compose up --build -d
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }
$port = (Select-String -Path .env -Pattern '^CHECKPOINT_PORT=(\d+)' | ForEach-Object { $_.Matches[0].Groups[1].Value })
if (-not $port) { $port = "9040" }
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
  try { $h = Invoke-RestMethod -Uri "http://localhost:$port/healthz" -TimeoutSec 3; if ($h.checkpoint -eq "up") { $ok = $true; break } } catch { Start-Sleep -Seconds 2 }
}
if (-not $ok) { throw "checkpoint service did not become healthy" }
Write-Host @"

============================================================
 sandbox-checkpoint is UP
   API:  http://localhost:$port        docs: /docs
   Flow: create a STATEFUL sandbox on sandbox-api:
           POST /sandboxes {"name":"s","template":"coreapp","options":{"stateful":true}}
         then  POST http://localhost:$port/sandboxes/<id>/checkpoints {"label":"v1"}
               POST http://localhost:$port/checkpoints/<cid>/fork {"name":"s-fork"}
               POST http://localhost:$port/checkpoints/<cid>/restore
   Verify: bash scripts/verify.sh
============================================================
"@
