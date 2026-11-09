# sandbox-sim - verification (Windows host wrapper around scripts/verify.sh)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$bash = Get-Command bash -ErrorAction SilentlyContinue
if (-not $bash) { throw "bash not found (install Git for Windows); or run: docker compose run --rm sim pytest -q" }
& bash scripts/verify.sh
exit $LASTEXITCODE
