#!/usr/bin/env bash
# Starts the company browser-based development environment (Linux/macOS).
set -euo pipefail
cd "$(dirname "$0")/.."

step() { printf '\n==> %s\n' "$1"; }

wait_for_url() {
  local url="$1" name="$2" timeout="${3:-300}" deadline=$((SECONDS + timeout))
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

step 'Building images and starting containers'
docker compose up -d --build

step 'Waiting for services to become healthy'
wait_for_url 'http://localhost:8000/api/health' 'Backend API'
wait_for_url 'http://localhost:3000/' 'Company app (frontend)'

IDE_PORT=$(grep -E '^IDE_PORT=' .env 2>/dev/null | cut -d= -f2 | cut -d# -f1 | tr -d ' ' || true)
IDE_PASSWORD=$(grep -E '^IDE_PASSWORD=' .env 2>/dev/null | cut -d= -f2 | cut -d# -f1 | tr -d ' ' || true)
IDE_PORT="${IDE_PORT:-8080}"
IDE_PASSWORD="${IDE_PASSWORD:-dev123}"
wait_for_url "http://localhost:${IDE_PORT}/healthz" 'Browser IDE'

cat <<EOF

==============================================================
   COMPANY DEVELOPMENT ENVIRONMENT IS READY
==============================================================

  Browser IDE (VS Code):   http://localhost:${IDE_PORT}
  IDE password:            ${IDE_PASSWORD}

  Company app (frontend):  http://localhost:3000
  Backend API:             http://localhost:8000/api/health
  PostgreSQL:              localhost:5432 (company / company)

  Workspace (in the IDE):  /home/coder/workspace/core-app
  Git remote:              git://gitserver/core-app.git

  Stop:          scripts/stop.sh
  Reset:         scripts/reset.sh      (keeps pushed Git history)
  Factory reset: docker compose down -v
EOF

if command -v xdg-open >/dev/null 2>&1; then xdg-open "http://localhost:${IDE_PORT}"; fi
if command -v open >/dev/null 2>&1; then open "http://localhost:${IDE_PORT}"; fi
