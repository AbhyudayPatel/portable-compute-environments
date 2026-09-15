#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
#  verify.sh — end-to-end verification of the nested Docker lab.
#
#  Run from WINDOWS (Git Bash / WSL) or any shell with the docker CLI:
#       bash scripts/verify.sh
#
#  It checks each layer independently and tells you exactly which hop failed.
#  Layer numbering matches README.md.
# ════════════════════════════════════════════════════════════════════════════
set -uo pipefail

OUTER_CONTAINER="nested-outer"
INNER_CONTAINER="inner-web"
PORT="${PORT:-8080}"

grn=$'\033[32m'; red=$'\033[31m'; ylw=$'\033[33m'; cyn=$'\033[36m'; rst=$'\033[0m'; bld=$'\033[1m'
pass() { printf '%s ✔ PASS%s  %s\n' "$grn" "$rst" "$*"; }
fail() { printf '%s ✖ FAIL%s  %s\n' "$red" "$rst" "$*"; }
head() { printf '\n%s%s%s\n' "$cyn$bld" "$*" "$rst"; }
note() { printf '   %s%s%s\n' "$ylw" "$*" "$rst"; }

FAILURES=0
chk() { # chk <description> <command...>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then pass "$desc"; else fail "$desc"; FAILURES=$((FAILURES+1)); fi
}

# ── Layer 1: Windows -> Docker Desktop ───────────────────────────────────────
head "Layer 1 · Windows → Docker Desktop daemon"
chk "docker CLI can reach the Docker Desktop daemon" docker version --format '{{.Server.Version}}'
chk "Linux containers mode (OSType=linux)"           bash -c "docker info --format '{{.OSType}}' | grep -q linux"

# ── Layer 2: Windows -> Outer container ──────────────────────────────────────
head "Layer 2 · Windows → OUTER container"
chk "outer container is running"                     docker inspect -f '{{.State.Running}}' "$OUTER_CONTAINER" 
chk "outer container health is 'healthy'"            bash -c "docker inspect -f '{{.State.Health.Status}}' $OUTER_CONTAINER | grep -q healthy || docker inspect -f '{{.State.Health.Status}}' $OUTER_CONTAINER | grep -q starting"

# ── Layer 3: Outer -> Inner Docker daemon ────────────────────────────────────
head "Layer 3 · OUTER container → INNER dockerd"
chk "inner dockerd answers the Engine API"           docker exec "$OUTER_CONTAINER" docker version --format '{{.Server.Version}}'
chk "inner dockerd storage driver reported"          bash -c "docker exec $OUTER_CONTAINER docker info --format '{{.Driver}}' | grep -q ."
chk "inner socket exists (/var/run/docker.sock)"     docker exec "$OUTER_CONTAINER" sh -c 'test -S /var/run/docker.sock'
chk "inner API reachable from host (tcp://localhost:2376)" curl -fsS http://localhost:2376/version

# ── Layer 4: Inner daemon -> Inner container ─────────────────────────────────
head "Layer 4 · INNER dockerd → INNER container"
chk "inner container is running"                     docker exec "$OUTER_CONTAINER" docker inspect -f '{{.State.Running}}' "$INNER_CONTAINER"
INNER_IP=$(docker exec "$OUTER_CONTAINER" docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$INNER_CONTAINER" 2>/dev/null)
note "inner container IP on inner bridge: ${INNER_IP:-<unknown>}"

# ── Layer 5: Inner container -> HTTP server (from inside outer) ──────────────
head "Layer 5 · INNER container → HTTP server (checked from OUTER)"
chk "curl http://<inner>/health from OUTER"          docker exec "$OUTER_CONTAINER" curl -fsS "http://${INNER_IP}/health"
chk "curl http://<inner>/ returns HTML"              bash -c "docker exec $OUTER_CONTAINER curl -fsS http://${INNER_IP}/ | grep -qi 'Nested Docker Lab'"

# ── Layer 6: Windows -> Inner application (full path) ────────────────────────
head "Layer 6 · Windows browser → inner app (full path)"
if curl -fsS "http://localhost:${PORT}/health" >/dev/null 2>&1; then
  pass "http://localhost:${PORT}/health"
  note "payload: $(curl -fsS "http://localhost:${PORT}/health")"
else
  fail "http://localhost:${PORT}/health"
  FAILURES=$((FAILURES+1))
fi
if curl -fsS "http://localhost:${PORT}/" 2>/dev/null | grep -qi 'Nested Docker Lab'; then
  pass "http://localhost:${PORT}/ serves the page"
else
  fail "http://localhost:${PORT}/ serves the page"
  FAILURES=$((FAILURES+1))
fi

# ── Summary ───────────────────────────────────────────────────────────────────
head "Summary"
if [ "$FAILURES" -eq 0 ]; then
  printf '%s%s  ALL LAYERS PASS — open http://localhost:%s in your browser.%s\n' "$grn" "$bld" "$PORT" "$rst"
  exit 0
else
  printf '%s%s  %d check(s) failed. See README.md §Troubleshooting.%s\n' "$red" "$bld" "$FAILURES" "$rst"
  exit 1
fi
