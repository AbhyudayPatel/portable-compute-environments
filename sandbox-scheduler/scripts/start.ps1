# sandbox-scheduler - one-command start (Windows host)
# PREREQUISITE: the sandbox-api stack (../sandbox-api) must be running.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path .env)) { Copy-Item .env.example .env }

# prerequisite check: sandbox-api must answer
try {
    $h = Invoke-RestMethod -Uri "http://localhost:9000/healthz" -TimeoutSec 4
    if ($h.dind -ne "up") { throw "sandbox-api dind is down" }
} catch {
    throw @"
sandbox-api is not reachable at http://localhost:9000.
The scheduler needs it (it schedules sandboxes THROUGH it).
Start it first:
    cd ..\sandbox-api
    powershell -ExecutionPolicy Bypass -File scripts\start.ps1
"@
}

docker compose up --build -d
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }

$apiPort = (Select-String -Path .env -Pattern '^API_PORT=(\d+)' |
    ForEach-Object { $_.Matches[0].Groups[1].Value })
if (-not $apiPort) { $apiPort = "9010" }

Write-Host "`nWaiting for scheduler health..."
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $h = Invoke-RestMethod -Uri "http://localhost:$apiPort/healthz" -TimeoutSec 3
        if ($h.scheduler -eq "up") { $ok = $true; break }
    } catch { Start-Sleep -Seconds 2 }
}
if (-not $ok) { throw "scheduler did not become healthy" }

Write-Host @"

============================================================
 sandbox-scheduler is UP

   Console (start here):  http://localhost:$apiPort
   Swagger API docs:      http://localhost:$apiPort/docs
   Upstream sandbox-api:  http://localhost:9000

 Try:
   - click "herd x12" in the console and watch the queue drain
   - guided tour:  powershell -ExecutionPolicy Bypass -File scripts\demo.ps1
   - edge battery: bash scripts/verify.sh
   - practice:     docs\EXAMPLES.md
============================================================
"@
