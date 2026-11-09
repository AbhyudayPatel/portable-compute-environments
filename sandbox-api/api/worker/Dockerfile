# ============================================================================
#  sandbox-worker - a whole sandbox-api NODE as one container image, built
#  INSIDE the outer dind so it can run as a sandbox of the outer platform.
#
#    outer Docker Desktop
#      '- outer dind (level 1 engine)
#           '- worker container (this image, privileged)      <- a sandbox
#                |- dockerd (level 2 engine)   <- the worker's own sandboxes
#                |- sbx-agent proxy :7799
#                '- sandbox-api (uvicorn :8000)
#
#  Everything the stock sandbox-api needs is here: dockerd (docker:27-dind
#  base), python + the app, the agent binary, the CoreApp build context.
#  gVisor is not shipped to keep the image small (isolation=container only
#  inside workers; documented).
# ============================================================================
FROM docker:27-dind

RUN apk add --no-cache python3 py3-pip bash curl coreutils util-linux openssl \
    && pip install --no-cache-dir --break-system-packages \
       fastapi==0.115.6 "uvicorn[standard]==0.32.1" docker==7.1.0 requests==2.32.3 sse-starlette==2.1.3

WORKDIR /app
COPY app/ ./app/
COPY coreapp/ ./coreapp/
COPY bin/sbx-agent /usr/local/bin/sbx-agent
COPY bin/sbx-agent /app/bin/sbx-agent
COPY worker-entrypoint.sh /usr/local/bin/worker-entrypoint.sh
RUN chmod +x /usr/local/bin/worker-entrypoint.sh /usr/local/bin/sbx-agent /app/bin/sbx-agent \
    && mkdir -p /app/desktop /app/egress /app/upstream /data /var/lib/sbx

ENV DOCKER_HOST=unix:///var/run/docker.sock \
    NODE_URL=http://127.0.0.1:7799 \
    SANDBOX_HOST=127.0.0.1 \
    DB_PATH=/data/sandbox-api.db \
    INFORMER_ENABLED=1

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/worker-entrypoint.sh"]
