#!/bin/sh
# Simulated company Git server.
#
# First boot: create a bare repo /srv/git/core-app.git and seed it with the
# contents of /seed (the ./seed/company-app folder from this project).
# Subsequent boots: the repo already exists in the git-data volume, so any
# commits developers pushed survive even a full environment reset.
#
# DEMO ONLY: git daemon with --enable=receive-pack allows anonymous push.
# A real deployment uses SSH/HTTPS with authentication — see docs/SECURITY.md.
set -e

REPO=/srv/git/core-app.git

if [ ! -d "$REPO" ]; then
  echo "[gitserver] First boot — creating bare repository"
  git init --bare --initial-branch=main "$REPO"

  TMP=$(mktemp -d)
  git clone "$REPO" "$TMP/repo" 2>/dev/null || true
  cp -r /seed/. "$TMP/repo/"

  cd "$TMP/repo"
  git config user.name "Platform Bot"
  git config user.email "platform@company.local"
  git checkout -b main 2>/dev/null || git switch -c main
  git add -A
  git commit -m "Initial import of CoreApp"
  git push origin main

  cd /
  rm -rf "$TMP"
  echo "[gitserver] Seeded core-app.git ($(git --git-dir=$REPO rev-parse --short HEAD))"
else
  echo "[gitserver] Repository exists — pushed history preserved"
fi

echo "[gitserver] Serving git://gitserver/core-app.git"
exec git daemon \
  --reuseaddr \
  --verbose \
  --export-all \
  --enable=receive-pack \
  --base-path=/srv/git \
  --listen=0.0.0.0 \
  --port=9418 \
  /srv/git
