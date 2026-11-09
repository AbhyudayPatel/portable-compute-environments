# sandbox-mcp - one-command start (Windows host). Prereq: ..\sandbox-api up.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker network inspect sandbox-api_sandnet *> $null
if ($LASTEXITCODE -ne 0) { throw "network sandbox-api_sandnet missing - start ..\sandbox-api first" }
docker compose up --build -d
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }
$port = (Select-String -Path .env -Pattern '^MCP_PORT=(\d+)' | ForEach-Object { $_.Matches[0].Groups[1].Value })
if (-not $port) { $port = "9420" }
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
  try { $h = Invoke-RestMethod -Uri "http://localhost:$port/healthz" -TimeoutSec 3; if ($h.mcp -eq "up") { $ok = $true; break } } catch { Start-Sleep -Seconds 2 }
}
if (-not $ok) { throw "mcp server did not become healthy" }
Write-Host @"

============================================================
 sandbox-mcp is UP
   MCP endpoint:  http://localhost:$port/mcp   (Streamable HTTP, Bearer token)
   Tokens:        dev-token -> tenant dev, alice-token, bob-token (see .env)
   Catalogue:     http://localhost:$port/tools     Audit: /calls
   Claude Code:   claude mcp add --transport http agentos http://localhost:$port/mcp --header "Authorization: Bearer dev-token"
   Verify:        bash scripts/verify.sh
============================================================
"@
