# sandbox-egress - one-command start (Windows host). Prereqs: sandbox-api + sandbox-blobs up.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
foreach ($net in @("sandbox-api_sandnet", "sandbox-blobs")) {
  docker network inspect $net *> $null
  if ($LASTEXITCODE -ne 0) { throw "network $net missing - start ..\sandbox-api and ..\sandbox-blobs first" }
}
docker compose up --build -d
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }
$port = (Select-String -Path .env -Pattern '^EGRESS_PORT=(\d+)' | ForEach-Object { $_.Matches[0].Groups[1].Value })
if (-not $port) { $port = "9410" }
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
  try { $h = Invoke-RestMethod -Uri "http://localhost:$port/healthz" -TimeoutSec 3; if ($h.egress -eq "up") { $ok = $true; break } } catch { Start-Sleep -Seconds 2 }
}
if (-not $ok) { throw "egress service did not become healthy" }
Write-Host @"

============================================================
 sandbox-egress is UP
   API: http://localhost:$port   docs: /docs
   A sandbox with a policy (on sandbox-api):
     POST /sandboxes {"name":"s","template":"coreapp",
                      "options":{"egress":{"mode":"allow","allow":["example.com"]}}}
   Live policy:   GET/PUT http://localhost:9000/sandboxes/<id>/egress
   Cassettes:     POST http://localhost:$port/sandboxes/<id>/cassettes/ship
   Verify:        bash scripts/verify.sh
============================================================
"@
