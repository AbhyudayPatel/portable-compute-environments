#requires -Version 5.1
<#
.SYNOPSIS
  Starts the company Linux desktop environment (full OS in the browser).

.DESCRIPTION
  1. Verifies Docker Desktop is running
  2. Creates .env from .env.example on first run
  3. Builds images and starts all containers
  4. Waits until the desktop, backend and frontend are up
  5. Opens the browser desktop
#>
$ErrorActionPreference = 'Stop'
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $ProjectRoot

function Write-Step([string]$Message) {
  Write-Host ''
  Write-Host "==> $Message" -ForegroundColor Cyan
}

function Wait-ForUrl([string]$Url, [string]$Name, [int]$TimeoutSeconds = 420) {
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
  throw "$Name did not become ready at $Url within $TimeoutSeconds s. Investigate with: docker compose logs"
}

Write-Step 'Checking Docker Desktop'
docker info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
  throw 'Docker Desktop is not running. Start Docker Desktop, wait until it reports running, then re-run this script.'
}
Write-Host '  [OK] Docker is running' -ForegroundColor Green

if (-not (Test-Path '.env')) {
  Copy-Item '.env.example' '.env'
  Write-Host '  [OK] Created .env from .env.example (edit it to change ports or the desktop password)'
}

Write-Step 'Building images and starting containers (first build of the desktop image is large)'
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { throw 'docker compose up failed. Run "docker compose logs" for details.' }

Write-Step 'Waiting for services to become healthy'
Wait-ForUrl 'http://localhost:8000/api/health' 'Backend API'
Wait-ForUrl 'http://localhost:3000/' 'Company app (frontend)'

$DesktopPort = '8080'
if (Test-Path '.env') {
  foreach ($line in (Get-Content '.env')) {
    if ($line -match '^DESKTOP_PORT=([^#]+)') { $DesktopPort = $Matches[1].Trim() }
  }
}
Wait-ForUrl "http://localhost:$DesktopPort/" 'Linux desktop'

Write-Host ''
Write-Host '==============================================================' -ForegroundColor DarkCyan
Write-Host '    COMPANY LINUX DESKTOP IS READY' -ForegroundColor DarkCyan
Write-Host '==============================================================' -ForegroundColor DarkCyan
Write-Host ''
Write-Host "  Linux desktop (browser):  http://localhost:$DesktopPort"
Write-Host ''
Write-Host '  Inside the desktop you get: VS Code, Chromium, terminal,'
Write-Host '  file manager, git, python - a full Debian XFCE machine.'
Write-Host ''
Write-Host '  Company app (frontend):   http://localhost:3000'
Write-Host '  Backend API:              http://localhost:8000/api/health'
Write-Host '  PostgreSQL:               localhost:5432 (company / company)'
Write-Host ''
Write-Host '  Workspace (in desktop):   /config/workspace/core-app'
Write-Host '  Git remote:               git://gitserver/core-app.git (simulated company Git)'
Write-Host ''
Write-Host '  Stop:           scripts\stop.ps1'
Write-Host '  Reset:          scripts\reset.ps1      (keeps pushed Git history + desktop settings)'
Write-Host '  Factory reset:  scripts\reset-all.ps1  (re-seeds everything)'
Write-Host ''

Start-Process "http://localhost:$DesktopPort"
