# sandbox-blobs - one-command start (Windows host)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose up --build -d
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }
$port = (Select-String -Path .env -Pattern '^BLOBS_PORT=(\d+)' | ForEach-Object { $_.Matches[0].Groups[1].Value })
if (-not $port) { $port = "9400" }
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
  try { $h = Invoke-RestMethod -Uri "http://localhost:$port/healthz" -TimeoutSec 3; if ($h.blobs -eq "up") { $ok = $true; break } } catch { Start-Sleep -Seconds 2 }
}
if (-not $ok) { throw "blob store did not become healthy" }
Write-Host @"

============================================================
 sandbox-blobs is UP
   API:     http://localhost:$port        docs: /docs
   Try:     curl -T somefile http://localhost:$port/blobs
            curl http://localhost:$port/stats
   Verify:  bash scripts/verify.sh
============================================================
"@
