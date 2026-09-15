#!/bin/sh
# IDE container entrypoint: configure git identity, then start code-server.
set -e

git config --global user.name  "${GIT_USER_NAME:-Company Developer}"
git config --global user.email "${GIT_USER_EMAIL:-dev@company.local}"
git config --global init.defaultBranch main
git config --global credential.helper cache

echo "[ide] Starting code-server on 0.0.0.0:8080 ..."
echo "[ide] Workspace: /home/coder/workspace"

exec code-server \
  --bind-addr 0.0.0.0:8080 \
  --auth password \
  /home/coder/workspace
