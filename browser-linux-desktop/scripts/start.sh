#!/usr/bin/env bash
# Starts the company Linux desktop environment (Linux/macOS).
set -euo pipefail
cd "$(dirname "$0")/.."

step() { printf '\n==> %s\n' "$1"; }

wait_for_url() {
  local url="$1" name="$2" timeout="${3:-420}" deadline=$((SECONDS + timeout))
  while [ $SECONDS -lt $deadline ]; do
    if curl -fsS -m 5 "$url" >/dev/null 2>&1; then
      printf '  [OK] %s (%s)\n' "$name" "$url"
      return 0
    fi
    sleep 3
  done
  echo "ERROR: $name did not become ready at $url. Try: docker compose logs" >&2
  exit 1
}

step 'Checking Docker'
docker info >/dev/null 2>&1 || { echo 'Docker is not running.' >&2; exit 1; }
echo '  [OK] Docker is running'

if [ ! -f .env ]; then
  cp .env.example .env
  echo '  [OK] Created .env from .env.example'
fi

step 'Building images and starting containers (first build of the desktop image is large)'
docker compose up -d --build

step 'Waiting for services to become healthy'
wait_for_url 'http://localhost:8000/api/health' 'Backend API'
wait_for_url 'http://localhost:3000/' 'Company app (frontend)'

DESKTOP_PORT=$(grep -E '^DESKTOP_PORT=' .env 2>/dev/null | cut -d= -f2 | cut -d# -f1 | tr -d ' ' || true)
DESKTOP_PORT="${DESKTOP_PORT:-8080}"
wait_for_url "http://localhost:${DESKTOP_PORT}/" 'Linux desktop'

cat <<EOF

==============================================================
   COMPANY LINUX DESKTOP IS READY
==============================================================

  Linux desktop (browser):  http://localhost:${DESKTOP_PORT}

  Inside the desktop: VS Code, Chromium, terminal,
  file manager, git, python — a full Debian XFCE machine.

  Company app (frontend):   http://localhost:3000
  Backend API:              http://localhost:8000/api/health
  PostgreSQL:               localhost:5432 (company / company)

  Workspace (in desktop):   /config/workspace/core-app
  Git remote:               git://gitserver/core-app.git

  Stop:          scripts/stop.sh
  Reset:         scripts/reset.sh      (keeps pushed Git history)
  Factory reset: docker compose down -v
EOF

if command -v xdg-open >/dev/null 2>&1; then xdg-open "http://localhost:${DESKTOP_PORT}"; fi
if command -v open >/dev/null 2>&1; then open "http://localhost:${DESKTOP_PORT}"; fi
