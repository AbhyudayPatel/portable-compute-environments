# cluster-in-a-box - one-command start (Windows host). Prereq: ..\sandbox-api up (dind publishing CLUSTER_PORTS).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker network inspect sandbox-api_sandnet *> $null
if ($LASTEXITCODE -ne 0) { throw "network sandbox-api_sandnet missing - start ..\sandbox-api first" }
docker compose up --build -d
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }
$port = (Select-String -Path .env -Pattern '^CLUSTER_PORT=(\d+)' | ForEach-Object { $_.Matches[0].Groups[1].Value })
if (-not $port) { $port = "9430" }
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
  try { $h = Invoke-RestMethod -Uri "http://localhost:$port/healthz" -TimeoutSec 3; if ($h.cluster -eq "up") { $ok = $true; break } } catch { Start-Sleep -Seconds 2 }
}
if (-not $ok) { throw "cluster control plane did not become healthy" }
Write-Host @"

============================================================
 cluster-in-a-box is UP
   API:      http://localhost:$port   docs: /docs
   Workers:  curl -X POST http://localhost:$port/workers -H 'content-type: application/json' -d '{"count":2}'
             (first time: builds the worker image inside the outer engine, ~3-5 min)
   Place:    curl -X POST http://localhost:$port/sandboxes -H 'content-type: application/json' -d '{"name":"demo","template":"web"}'
   Verify:   bash scripts/verify.sh
============================================================
"@
