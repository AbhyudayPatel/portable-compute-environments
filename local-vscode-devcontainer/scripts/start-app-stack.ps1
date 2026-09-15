#requires -Version 5.1
<#
.SYNOPSIS
  Runs ONLY the application stack (backend + frontend + db) directly,
  without the VS Code Dev Containers flow. Good for a quick smoke test.
#>
$ErrorActionPreference = 'Stop'
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location (Join-Path $ProjectRoot 'company-app')

function Wait-ForUrl([string]$Url, [string]$Name, [int]$TimeoutSeconds = 180) {
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    try {
      $null = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
      Write-Host "  [OK] $Name ($Url)" -ForegroundColor Green
      return
    } catch {
      Start-Sleep -Seconds 3
    }
  }
  throw "$Name did not become ready at $Url. Investigate with: docker compose logs"
}

docker info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Docker Desktop is not running.' }

Write-Host '==> Building and starting the application stack' -ForegroundColor Cyan
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { throw 'docker compose up failed. Run "docker compose logs" for details.' }

Write-Host '==> Waiting for services' -ForegroundColor Cyan
Wait-ForUrl 'http://localhost:8000/api/health' 'Backend API'
Wait-ForUrl 'http://localhost:3000/' 'Frontend'

Write-Host ''
Write-Host '  Frontend: http://localhost:3000'
Write-Host '  Backend:  http://localhost:8000/api/health'
Write-Host '  Stop:     scripts\stop.ps1'
Write-Host ''

Start-Process 'http://localhost:3000'
