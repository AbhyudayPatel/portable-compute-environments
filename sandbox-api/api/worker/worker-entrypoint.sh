#!/bin/bash
# worker: start the inner engine, the agent proxy, then sandbox-api.
# POOL_START/POOL_END/MAX_SANDBOXES/SBX_NODE_TOKEN come from the
# cluster control plane through the template's environment.
set -e
: "${SBX_NODE_TOKEN:=worker-node-token}"
export SBX_NODE_TOKEN
rm -f /var/run/docker.pid /var/run/docker.sock 2>/dev/null || true
mkdir -p /var/lib/sbx /data

echo "[worker] starting inner dockerd"
dockerd --host=unix:///var/run/docker.sock --storage-driver=overlay2 \
        --iptables=true --ip-forward=true --ip-masq=true >/var/log/dockerd.log 2>&1 &
for i in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 1; done
docker info >/dev/null 2>&1 || { echo "[worker] dockerd failed"; tail -20 /var/log/dockerd.log; exit 1; }
echo "[worker] inner dockerd up"

sbx-agent proxy >/var/log/sbx-agent-proxy.log 2>&1 &
echo "[worker] agent proxy up on :7799"

cd /app
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
