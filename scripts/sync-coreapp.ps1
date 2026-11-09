# sync-coreapp.ps1 - the canonical CoreApp lives in <repo>/coreapp.
# Every project that embeds a copy gets it from here, so the five copies
# can never drift again (they had: the inline copy in sandbox-api used a
# POST toggle route while the seed used PATCH).
#
#   scripts\sync-coreapp.ps1          copy canonical -> all consumers
#   scripts\sync-coreapp.ps1 -Check   exit 1 if any consumer differs
param([switch]$Check)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $root "coreapp"
$targets = @(
  @{ path = "sandbox-api\api\coreapp";                  parts = @("backend", "frontend", "db") },
  @{ path = "browser-dev-environment\seed\company-app"; parts = @("backend", "frontend", "db", "docker-compose.yml", "README.md", ".gitattributes", ".gitignore") },
  @{ path = "browser-linux-desktop\seed\company-app";   parts = @("backend", "frontend", "db", "docker-compose.yml", "README.md", ".gitattributes", ".gitignore") },
  @{ path = "local-vscode-devcontainer\company-app";    parts = @("backend", "frontend", "db", "docker-compose.yml", ".gitattributes", ".gitignore") }
)

function Hash-Tree($dir) {
  if (-not (Test-Path $dir)) { return "" }
  $files = Get-ChildItem -Path $dir -Recurse -File | Where-Object { $_.FullName -notmatch "__pycache__" } | Sort-Object FullName
  $sb = New-Object System.Text.StringBuilder
  foreach ($f in $files) {
    $rel = $f.FullName.Substring($dir.Length).TrimStart("\")
    $h = (Get-FileHash -Algorithm SHA256 -Path $f.FullName).Hash
    [void]$sb.AppendLine("$rel $h")
  }
  return $sb.ToString()
}

$drift = 0
foreach ($t in $targets) {
  $dst = Join-Path $root $t.path
  foreach ($p in $t.parts) {
    $s = Join-Path $src $p; $d = Join-Path $dst $p
    if ($Check) {
      $hs = if (Test-Path $s -PathType Container) { Hash-Tree $s } else { (Get-FileHash -Algorithm SHA256 -Path $s).Hash }
      $hd = if (Test-Path $d -PathType Container) { Hash-Tree $d } elseif (Test-Path $d) { (Get-FileHash -Algorithm SHA256 -Path $d).Hash } else { "" }
      if ($hs -ne $hd) { Write-Host "DRIFT  $($t.path)\$p"; $drift++ } else { Write-Host "ok     $($t.path)\$p" }
    } else {
      if (Test-Path $s -PathType Container) {
        if (Test-Path $d) { Remove-Item -Recurse -Force $d }
        Copy-Item -Recurse -Force $s $d
        Get-ChildItem -Path $d -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
      } else {
        New-Item -ItemType Directory -Force (Split-Path $d) | Out-Null
        Copy-Item -Force $s $d
      }
      Write-Host "synced $($t.path)\$p"
    }
  }
}
if ($Check) { if ($drift -gt 0) { Write-Host "$drift drifted part(s)"; exit 1 } else { Write-Host "all copies match coreapp/"; exit 0 } }
