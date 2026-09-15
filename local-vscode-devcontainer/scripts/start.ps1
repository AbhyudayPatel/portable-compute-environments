#requires -Version 5.1
<#
.SYNOPSIS
  Prepares and opens the Dev Containers (local VS Code) environment.

.DESCRIPTION
  1. Verifies Docker Desktop is running
  2. Verifies VS Code + the Dev Containers extension are installed
  3. Opens the company-app repository in VS Code

  You then run "Dev Containers: Reopen in Container" inside VS Code.
#>
$ErrorActionPreference = 'Stop'
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $ProjectRoot

function Write-Step([string]$Message) {
  Write-Host ''
  Write-Host "==> $Message" -ForegroundColor Cyan
}

Write-Step 'Checking Docker Desktop'
docker info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
  throw 'Docker Desktop is not running. Start Docker Desktop, wait until it reports running, then re-run this script.'
}
Write-Host '  [OK] Docker is running' -ForegroundColor Green

Write-Step 'Checking VS Code'
$code = Get-Command code -ErrorAction SilentlyContinue
if (-not $code) {
  throw 'VS Code CLI "code" not found in PATH. Install VS Code, then in VS Code run "Shell Command: Install code command in PATH".'
}
Write-Host '  [OK] VS Code found' -ForegroundColor Green

Write-Step 'Ensuring the Dev Containers extension is installed'
code --install-extension ms-vscode-remote.remote-containers --force | Out-Null
Write-Host '  [OK] ms-vscode-remote.remote-containers installed' -ForegroundColor Green

Write-Step 'Opening company-app in VS Code'
code (Join-Path $ProjectRoot 'company-app')

Write-Host ''
Write-Host '==============================================================' -ForegroundColor DarkCyan
Write-Host '  NEXT STEP (inside VS Code):' -ForegroundColor DarkCyan
Write-Host '==============================================================' -ForegroundColor DarkCyan
Write-Host ''
Write-Host '  Press F1 and run:  Dev Containers: Reopen in Container' -ForegroundColor Yellow
Write-Host ''
Write-Host '  VS Code will build the dev image and start the whole stack'
Write-Host '  (dev container + backend + frontend + postgres).'
Write-Host ''
Write-Host '  When it is ready:'
Write-Host '    Frontend:  http://localhost:3000'
Write-Host '    Backend:   http://localhost:8000/api/health'
Write-Host '    Postgres:  localhost:5432  (company / company)'
Write-Host ''
Write-Host '  The integrated terminal in VS Code runs INSIDE the Linux'
Write-Host '  dev container. The repo on your disk is mounted at /workspace.'
Write-Host ''
