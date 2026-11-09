#!/usr/bin/env bash
# sync-coreapp.sh - canonical CoreApp (<repo>/coreapp) -> every embedded copy.
#   bash scripts/sync-coreapp.sh          copy
#   bash scripts/sync-coreapp.sh --check  exit 1 on drift
set -euo pipefail
cd "$(dirname "$0")/.."
SRC=coreapp
declare -A PARTS=(
  ["sandbox-api/api/coreapp"]="backend frontend db"
  ["browser-dev-environment/seed/company-app"]="backend frontend db docker-compose.yml README.md .gitattributes .gitignore"
  ["browser-linux-desktop/seed/company-app"]="backend frontend db docker-compose.yml README.md .gitattributes .gitignore"
  ["local-vscode-devcontainer/company-app"]="backend frontend db docker-compose.yml .gitattributes .gitignore"
)
hash_tree() { # directory or file -> one hash (pycache ignored)
  if [ -d "$1" ]; then (cd "$1" && find . -type f -not -path '*/__pycache__/*' | sort | xargs sha256sum) | sha256sum | cut -c1-16
  elif [ -f "$1" ]; then sha256sum "$1" | cut -c1-16; else echo missing; fi
}
drift=0
for dst in "${!PARTS[@]}"; do
  for p in ${PARTS[$dst]}; do
    if [ "${1:-}" = "--check" ]; then
      a=$(hash_tree "$SRC/$p"); b=$(hash_tree "$dst/$p")
      if [ "$a" = "$b" ]; then echo "ok     $dst/$p"; else echo "DRIFT  $dst/$p"; drift=$((drift+1)); fi
    else
      mkdir -p "$dst"
      if [ -d "$SRC/$p" ]; then rm -rf "${dst:?}/$p"; cp -r "$SRC/$p" "$dst/$p"; find "$dst/$p" -name __pycache__ -type d -prune -exec rm -rf {} +; else cp "$SRC/$p" "$dst/$p"; fi
      echo "synced $dst/$p"
    fi
  done
done
if [ "${1:-}" = "--check" ]; then [ "$drift" -eq 0 ] && echo "all copies match coreapp/" || { echo "$drift drifted part(s)"; exit 1; }; fi
