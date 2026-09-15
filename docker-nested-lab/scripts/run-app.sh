#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
#  run-app.sh — launch a tiny web app on an INNER port, reachable in Chrome
#               at the SAME port number.
#
#  Usage (from Windows Git Bash / WSL):
#       bash scripts/run-app.sh <port> [name]
#
#  Example:
#       bash scripts/run-app.sh 9001 demo
#       # then open  http://localhost:9001
#
#  The app runs as a process inside the inner-web container (the DNAT target
#  for the 9000-9010 user range), so identity port mapping applies.
#  Valid ports: 9000-9010.
# ════════════════════════════════════════════════════════════════════════════
set -euo pipefail

INNER="docker -H tcp://localhost:2376"
TARGET_CONTAINER="inner-web"
MIN=9000; MAX=9010

PORT="${1:-}"
NAME="${2:-app-$PORT}"

if [[ -z "$PORT" || ! "$PORT" =~ ^[0-9]+$ ]]; then
  echo "usage: bash scripts/run-app.sh <port ${MIN}-${MAX}> [name]" >&2
  exit 1
fi
if (( PORT < MIN || PORT > MAX )); then
  echo "error: port must be in ${MIN}-${MAX} (the published user range)" >&2
  exit 1
fi

echo ">>> starting app '$NAME' on INNER port $PORT (inside $TARGET_CONTAINER)"
$INNER exec -d "$TARGET_CONTAINER" sh -c "
  mkdir -p /apps/$NAME
  printf '<!DOCTYPE html><html><head><meta charset=utf-8><title>%s</title>
    <style>body{font-family:system-ui;display:grid;place-items:center;height:100vh;margin:0;background:linear-gradient(135deg,#1e293b,#0f172a);color:#e2e8f0}
    .c{text-align:center;padding:2rem 3rem;border:1px solid #334155;border-radius:16px;background:#1e293b}
    h1{margin:0 0 .4rem}code{background:#0ea5e9;padding:.1rem .5rem;border-radius:6px;color:#082f49;font-weight:700}</style>
    </head><body><div class=c><h1>🚀 %s</h1><p>served from the INNER container <code>%s</code> on port <code>%s</code></p>
    <p style=\"opacity:.7;font-size:.85rem\">reached via Windows localhost:%s &mdash; same port, through the nested relay</p></div></body></html>' \
      \"$NAME on :$PORT\" \"$NAME on :$PORT\" \"$TARGET_CONTAINER\" \"$PORT\" \"$PORT\" > /apps/$NAME/index.html
  httpd -f -p $PORT -h /apps/$NAME
"

# give it a moment, then confirm it is listening
sleep 2
if $INNER exec "$TARGET_CONTAINER" sh -c "netstat -lnt 2>/dev/null | grep -q ':$PORT '" 2>/dev/null; then
  echo ">>> listening on inner port $PORT"
else
  echo ">>> (could not confirm listener via TCP exec; continuing anyway)"
fi

echo
echo "✔ open in Chrome:   http://localhost:$PORT"
echo "  (inner port $PORT  ==  Chrome port $PORT)"
