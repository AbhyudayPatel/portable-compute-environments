#!/bin/sh
# Start the engine-side agent proxy, then the stock dind entrypoint.
# SBX_NODE_TOKEN protects /node/*; compose passes it from .env.
set -e
: "${SBX_NODE_TOKEN:?SBX_NODE_TOKEN must be set}"
mkdir -p /var/lib/sbx
sbx-agent proxy >/var/log/sbx-agent-proxy.log 2>&1 &
echo "[sandbox-dind] sbx-agent proxy started on :7799"
exec dockerd-entrypoint.sh "$@"
