# -----------------------------------------------------------------------------
# sandbox-api control plane image (v2).
#
#   stage agentbuild  compiles the in-sandbox agent (Go, static) from the
#                     `agent` build context that compose passes in
#                     (additional_contexts: agent: ./agent)
#   final             python:3.12-slim + fastapi/uvicorn/docker SDK/requests,
#                     the app, the agent binary (injected into every sandbox
#                     container at create time), the canonical CoreApp build
#                     contexts and the desktop image context.
# -----------------------------------------------------------------------------
FROM golang:1.23-alpine AS agentbuild
WORKDIR /src
COPY --from=agent . .
RUN go mod tidy && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -trimpath -ldflags="-s -w" -o /out/sbx-agent .

FROM python:3.12-slim

# openssl: per-sandbox CA generation for egress policies
RUN apt-get update && apt-get install -y --no-install-recommends openssl     && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY coreapp/ ./coreapp/
COPY desktop/ ./desktop/
# egress proxy + upstream fixture build contexts (built INSIDE dind on demand)
COPY --from=egress . ./egress/
COPY --from=upstream . ./upstream/
COPY --from=agentbuild /out/sbx-agent /app/bin/sbx-agent

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
