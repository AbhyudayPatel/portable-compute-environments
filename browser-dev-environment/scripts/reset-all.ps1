#requires -Version 5.1
<#
.SYNOPSIS
  FACTORY RESET: destroys everything, including the seeded Git server
  history. The next start re-seeds core-app.git from the seed\company-app
  folder.
#>
$ErrorActionPreference = 'Stop'
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $ProjectRoot

$confirm = Read-Host 'This destroys EVERYTHING including all pushed Git history. Type YES to continue'
if ($confirm -ne 'YES') {
  Write-Host 'Aborted.'
  exit 0
}

docker compose down -v
if ($LASTEXITCODE -ne 0) { throw 'docker compose down failed' }

Write-Host ''
Write-Host '[OK] Factory reset complete. Next start re-seeds the Git server from seed\company-app.' -ForegroundColor Green
