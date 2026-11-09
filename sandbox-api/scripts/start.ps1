# sandbox-api - one-command start (Windows host)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path .env)) { Copy-Item .env.example .env }

docker compose up --build -d
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }

$apiPort = (Select-String -Path .env -Pattern '^API_PORT=(\d+)' |
    ForEach-Object { $_.Matches[0].Groups[1].Value })
if (-not $apiPort) { $apiPort = "9000" }

Write-Host "`nWaiting for API health..."
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $h = Invoke-RestMethod -Uri "http://localhost:$apiPort/healthz" -TimeoutSec 3
        if ($h.api -eq "up" -and $h.dind -eq "up") { $ok = $true; break }
    } catch { Start-Sleep -Seconds 2 }
}
if (-not $ok) { throw "API did not become healthy" }

Write-Host @"

============================================================
 sandbox-api v2 is UP
   Console:       http://localhost:$apiPort        (live events, terminal, files)
   OpenAPI docs:  http://localhost:$apiPort/docs
   Sandbox apps:  http://localhost:9200-9209 (allocated on create)
   Templates:     web . blank . coreapp . desktop
   Isolation:     container (runc) . gvisor (runsc)

 Try:
   curl -X POST http://localhost:$apiPort/sandboxes \
     -H 'content-type: application/json' \
     -d '{\"name\":\"demo\",\"template\":\"web\",\"isolation\":\"gvisor\"}'

 Verify everything (89 assertions, ~10 min):
   bash scripts/verify.sh
 Simulate the lifecycle core (no docker needed):
   cd ..\sandbox-sim; scripts\run.ps1
============================================================
"@
