#!/usr/bin/env bash
# Stops the environment. Volumes (workspace, db, git history, desktop
# settings) are preserved.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose down
echo '[OK] Environment stopped. Workspace, database, desktop settings and Git history are preserved.'
