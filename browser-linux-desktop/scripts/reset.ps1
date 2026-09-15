#requires -Version 5.1
<#
.SYNOPSIS
  Resets the environment: destroys the containers, the workspace volume and
  the database — but KEEPS the company Git server and your desktop settings.

.DESCRIPTION
  The "disposable machine" demo: the next start re-clones the repository
  from the Git server, so anything you committed AND PUSHED comes back.
  Git is the source of truth; the machine is disposable.
#>
$ErrorActionPreference = 'Stop'
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $ProjectRoot

Write-Host '==> Stopping containers' -ForegroundColor Cyan
docker compose down

Write-Host '==> Removing workspace and database volumes (Git history + desktop settings kept)' -ForegroundColor Cyan
docker volume rm -f linux-desktop-env_workspace-data 2>$null | Out-Null
docker volume rm -f linux-desktop-env_pgdata 2>$null | Out-Null

Write-Host ''
Write-Host '[OK] Reset complete.' -ForegroundColor Green
Write-Host '     Next start (scripts\start.ps1) re-clones core-app from the Git server.'
Write-Host '     Commits you pushed will come back; unpushed work is gone.'
Write-Host ''
