#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  OUTER ENTRYPOINT
#
#  This script runs as PID 1 inside the OUTER (privileged) container.
#  It boots a full, INDEPENDENT Docker engine (Docker-in-Docker) and then
#  builds + runs the innermost web container, and finally exposes that inner
#  container to the outside world via a TCP relay.
#
#  Startup order (deterministic, each step health-gated):
#      start inner dockerd
#        -> wait for the inner daemon to report ServerVersion
#        -> build inner image
#        -> run inner container
#        -> wait for inner HTTP to respond
#        -> start socat relay  8080 -> inner_container:80
#
#  Every step echoes a clear log line and, on failure, dumps diagnostics.
# ═══════════════════════════════════════════════════════════════════════════════
set -Eeuo pipefail
IFS=$'\n\t'

# ── Config ────────────────────────────────────────────────────────────────────
INNER_IMAGE="nested-lab-inner:1.0"
INNER_CONTAINER="inner-web"      # the main web app container
INNER_APP_PORT=80          # port the web server listens on INSIDE the inner container
RELAY_PORT=8080            # OUTER port for the flagship page (published to Windows)
INNER_BUILD_CTX="/opt/inner-app"
LOGDIR="/var/log/nested-lab"
DOCKERD_LOG="${LOGDIR}/dockerd.log"

# ── User-app port range (IDENTITY mapping) ─────────────────────────────────
# Run an app on INNER port P and open Chrome at localhost:P  (same number).
# The outer container DNATs this whole block straight to inner-web, same port,
# and compose publishes the same block 1:1 to Windows. Keep it clear of the
# reserved ports 8080 (main page) and 2375/2376 (inner API).
APP_PORT_BASE=9000         # first user-app port (inner AND Chrome are the same)
APP_PORT_END=9010          # last user-app port
APP_PORT_RANGE="${APP_PORT_BASE}:${APP_PORT_END}"   # iptables --dport range (COLON form)

# ── Pretty logging ────────────────────────────────────────────────────────────
c_reset=$'\033[0m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_red=$'\033[31m'; c_cyn=$'\033[36m'; c_blu=$'\033[34m'
step() { printf '%s[STEP]%s %s\n' "$c_blu" "$c_reset" "$*"; }
ok()   { printf '%s[ OK ]%s %s\n' "$c_grn" "$c_reset" "$*"; }
warn() { printf '%s[WARN]%s %s\n' "$c_ylw" "$c_reset" "$*"; }
err()  { printf '%s[FAIL]%s %s\n' "$c_red" "$c_reset" "$*" >&2; }
info() { printf '%s[INFO]%s %s\n' "$c_cyn" "$c_reset" "$*"; }

mkdir -p "$LOGDIR"

banner() {
  cat <<'EOF'
  ┌─────────────────────────────────────────────────────────────────┐
  │            N E S T E D   D O C K E R   L A B                    │
  │                                                                 │
  │   Windows -> Docker Desktop -> OUTER container                  │
  │                                  -> INNER dockerd               │
  │                                     -> INNER container (web)    │
  └─────────────────────────────────────────────────────────────────┘
EOF
}

# ── Diagnostics dump, used on failure and by `lock health` ────────────────────
dump_diagnostics() {
  err "══════════════════  DIAGNOSTICS  ══════════════════"
  echo "── PID 1 cgroup ──";              cat /proc/1/cgroup            2>&1 || true
  echo "── outer netns: ip -brief addr ──"; ip -brief addr                2>&1 || true
  echo "── outer netns: ip route ──";      ip route                       2>&1 || true
  echo "── listening sockets (outer) ──";  ss -lntp                       2>&1 || true
  echo "── mounts of interest ──";         mount | grep -Ei 'cgroup|overlay|docker' | head -40 || true
  echo "── iptables (nat) ──";             iptables -t nat -L -n -v       2>&1 || true
  echo "── processes ──";                  ps aux | head -30              2>&1 || true
  if [ -f "$DOCKERD_LOG" ]; then
    echo "── dockerd log (tail) ──";         tail -n 60 "$DOCKERD_LOG"      2>&1 || true
  fi
  err "═════════════════════════════════════════════════"
}

trap 'rc=$?; if [ $rc -ne 0 ]; then err "Startup aborted (exit $rc)."; dump_diagnostics; fi; exit $rc' ERR

banner

# ── STEP 0: sanity — this container must be privileged for a nested daemon ────
step "0/5  Verifying outer container privileges"
if ! grep -q 'Cgroup Version' /dev/null 2>/dev/null; then :; fi
if [ ! -e /sys/fs/cgroup/cgroup.controllers ] && [ ! -e /sys/fs/cgroup/memory ]; then
  warn "cgroup v1/v2 mount not visible; dockerd may fail."
fi
# Quick capability probe: a nested daemon needs to create mounts & namespaces.
if ! mount -t tmpfs none /mnt >/dev/null 2>&1; then
  err "Cannot create mounts. Is the outer container running with --privileged ?"
fi
ok "Outer container can create mounts (privileged OK)."
info "Inner Docker daemon data dir: /var/lib/docker (named volume)"

# ── STEP 1: start the INNER Docker daemon ─────────────────────────────────────
step "1/5  Starting INNER dockerd (Docker-in-Docker engine)"

# A previous dockerd (from a prior boot of this container) may have left a stale
# pidfile/socket. Because PID numbers are reused inside the container's PID
# namespace, dockerd would refuse to start with "process with PID N is still
# running". Clean them up unconditionally before launching.
rm -f /var/run/docker.pid /var/run/docker.sock /var/run/docker/containerd/containerd.pid 2>/dev/null || true

# Storage driver: overlay2 inside a container requires the outer container to be
# running on a filesystem that supports nested overlayfs. Docker Desktop's WSL2
# kernel supports it, so overlay2 is safe here. We still leave it overridable.
STORAGE_DRIVER="${STORAGE_DRIVER:-overlay2}"

# Start dockerd in the background. We DO NOT use the image's default entrypoint
# (dockerd-entrypoint.sh) so we keep full control of flags and ordering.
# Expose the inner daemon via a unix socket (used by this script and by
# `docker exec nested-outer docker ...`). For host-terminal access we do NOT bind
# dockerd directly to a non-localhost IP (that triggers a long deliberate
# insecure-bind startup delay); instead we bind to loopback and relay it out with
# socat in STEP 6. dockerd starts instantly this way.
dockerd \
  --host=unix:///var/run/docker.sock \
  --host=tcp://127.0.0.1:2375 \
  --storage-driver="${STORAGE_DRIVER}" \
  --iptables=true \
  --ip-forward=true \
  --ip-masq=true \
  >"$DOCKERD_LOG" 2>&1 &
DOCKERD_PID=$!
ok "dockerd launched (pid $DOCKERD_PID), logging to $DOCKERD_LOG"

# ── STEP 2: wait for the INNER daemon to become healthy ──────────────────────
step "2/5  Waiting for INNER daemon to answer the Engine API"
deadline=$(( $(date +%s) + 60 ))
server_version=""
while [ "$(date +%s)" -lt "$deadline" ]; do
  if server_version=$(docker version --format '{{.Server.Version}}' 2>/dev/null) \
     && [ -n "$server_version" ]; then
    break
  fi
  # surface an early crash instead of waiting the full timeout
  if ! kill -0 "$DOCKERD_PID" 2>/dev/null; then
    err "dockerd exited early."
    exit 1
  fi
  sleep 1
done
if [ -z "$server_version" ]; then
  err "Inner dockerd did not become ready within 60s."
  exit 1
fi
ok "Inner Docker engine UP (Server version ${server_version})."

# ── STEP 3: build the INNER image ────────────────────────────────────────────
step "3/6  Building INNER image '${INNER_IMAGE}' from ${INNER_BUILD_CTX}"
docker build -t "${INNER_IMAGE}" "${INNER_BUILD_CTX}" | sed 's/^/    /'
ok "Inner image built."
docker images --format '    {{.Repository}}:{{.Tag}}  ({{.Size}})' | grep nested-lab || true

# ── STEP 4: run the INNER container and wait for its HTTP ────────────────────
step "4/6  Running INNER container '${INNER_CONTAINER}'"
docker rm -f "${INNER_CONTAINER}" >/dev/null 2>&1 || true
docker run -d --name "${INNER_CONTAINER}" \
  --log-opt max-size=5m --log-opt max-file=2 \
  "${INNER_IMAGE}" >/dev/null
ok "Inner container started."

# Grab its IP on the INNER bridge. This address only exists inside the OUTER
# container's network namespace — it is NOT visible to the host VM or Windows.
INNER_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "${INNER_CONTAINER}")"
ok "Inner container IP on the inner bridge: ${INNER_IP}"

info "Waiting for the inner HTTP server to respond..."
deadline=$(( $(date +%s) + 30 ))
until curl -fsS "http://${INNER_IP}:${INNER_APP_PORT}/health" >/dev/null 2>&1; do
  [ "$(date +%s)" -lt "$deadline" ] || { err "Inner web server did not become healthy."; exit 1; }
  sleep 1
done
ok "Inner web server is healthy at http://${INNER_IP}:${INNER_APP_PORT}"

# ── STEP 5: user-app port range — IDENTITY forward to inner-web ────────────
# Compose publishes ${APP_PORT_RANGE} 1:1 to Windows. Those packets land in THIS
# outer netns on the same ports, so we DNAT them straight to inner-web (same port).
# Result: an app on inner-web:9001 is reached in Chrome at localhost:9001.
step "5/6  Forwarding user-app range ${APP_PORT_RANGE} -> inner-web (${INNER_IP}), identity ports"
iptables -t nat -A PREROUTING -p tcp --dport "${APP_PORT_RANGE}" -j DNAT --to-destination "${INNER_IP}"
iptables -t nat -A OUTPUT      -p tcp --dport "${APP_PORT_RANGE}" -j DNAT --to-destination "${INNER_IP}"
ok "Outer netns DNAT :${APP_PORT_RANGE} -> ${INNER_IP} (identity ports)"

# ── STEP 6: TCP relays in the OUTER namespace ───────────────────────────────
step "6/6  Starting outer TCP relays"

# (a) Main page: host :8080 -> inner web app :80
socat TCP-LISTEN:${RELAY_PORT},fork,reuseaddr,keepalive TCP:${INNER_IP}:${INNER_APP_PORT} &
RELAY_PID=$!

# (b) Inner Engine API: host :2376 -> outer eth0 :2375 -> dockerd 127.0.0.1:2375
#     Lets your Windows terminal drive the INNER engine: docker -H tcp://localhost:2376
#     Bind to the outer container's eth0 IP specifically (NOT 0.0.0.0) so it does
#     not collide with dockerd's own 127.0.0.1:2375 listener (wildcard overlap).
OUTER_ETH0_IP="$(ip -4 -o addr show dev eth0 | awk '{split($4,a,"/");print a[1]}')"
socat TCP-LISTEN:2375,fork,reuseaddr,keepalive,bind=${OUTER_ETH0_IP} TCP:127.0.0.1:2375 &
API_RELAY_PID=$!

sleep 1
for p in "$RELAY_PID" "$API_RELAY_PID"; do
  kill -0 "$p" 2>/dev/null || { err "a socat relay failed to start."; exit 1; }
done
ok "Main relay: 0.0.0.0:${RELAY_PORT} -> ${INNER_IP}:${INNER_APP_PORT}"
ok "API  relay: ${OUTER_ETH0_IP}:2375 -> 127.0.0.1:2375  (inner dockerd, published to Windows as :2376)"

echo
ok "╔══════════════════════════════════════════════════════════════════╗"
ok "║   Nested Docker environment is UP.                               ║"
ok "╚══════════════════════════════════════════════════════════════════╝"
echo
info "Main page:            http://localhost:${RELAY_PORT}"
info "Inner Docker engine:  docker -H tcp://localhost:2376 ps"
info "User apps (identity port): run an app on inner-web port P in ${APP_PORT_BASE}-${APP_PORT_END},"
info "    then open Chrome at the SAME port:  http://localhost:P"
info "    helper:  bash scripts/run-app.sh 9001   # then open http://localhost:9001"
echo
info "Inner daemon containers:"; docker ps --format '    {{.Names}}\t{{.Ports}}'

# ── Supervision loop: keep PID 1 alive and surface child deaths ───────────────
wait -n "$DOCKERD_PID" "$RELAY_PID" 2>/dev/null || true
EXITED=$?
err "A critical child process exited (status $EXITED). Shutting down."
exit $EXITED
