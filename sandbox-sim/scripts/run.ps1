# sandbox-sim - run the fuzzer (Windows host). Examples:
#   scripts\run.ps1                       # 300 seeds x 300 steps
#   scripts\run.ps1 -Seeds 2000 -Steps 500
#   scripts\run.ps1 -Seed 42 -Verbose     # replay one seed step by step
#   scripts\run.ps1 -Pytest               # unit + property + mutation tests
param(
  [int]$Seeds = 300,
  [int]$Steps = 300,
  [int]$Seed = -1,
  [switch]$Verbose,
  [switch]$Pytest
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
docker compose build | Out-Null
if ($Pytest) { docker compose run --rm sim pytest -q; exit $LASTEXITCODE }
if ($Seed -ge 0) {
  $args = @("python", "-m", "sim.run", "--seed", "$Seed", "--steps", "$Steps")
  if ($Verbose) { $args += "--verbose" }
  docker compose run --rm sim @args
} else {
  docker compose run --rm sim python -m sim.run --seeds $Seeds --steps $Steps
}
exit $LASTEXITCODE
