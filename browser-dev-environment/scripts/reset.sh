#!/usr/bin/env bash
# Resets the environment but KEEPS the Git server: the next start re-clones
# the repo, so pushed commits come back and unpushed work is gone.
set -euo pipefail
cd "$(dirname "$0")/.."

echo '==> Stopping containers'
docker compose down

echo '==> Removing workspace and database volumes (Git history is kept)'
docker volume rm -f company-dev-env_workspace-data 2>/dev/null || true
docker volume rm -f company-dev-env_pgdata 2>/dev/null || true

echo ''
echo '[OK] Reset complete. Next start re-clones core-app from the Git server.'
