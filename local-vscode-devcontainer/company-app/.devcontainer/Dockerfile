# Dev container image — the Linux "developer machine" VS Code attaches to.
# It only carries *tooling*; the application itself runs in the sibling
# containers defined in docker-compose.yml.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
