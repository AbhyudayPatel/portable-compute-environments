#!/usr/bin/env bash
# Stops the environment. Volumes (workspace, db, git history) are preserved.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose down
echo '[OK] Environment stopped. Workspace, database and Git history are preserved.'
