"""Central configuration - everything overridable via environment.

Kept in one module so every tunable is discoverable and documented once.
"""
import os

# Docker daemon of the INNER engine (the dind service on the compose net).
DOCKER_HOST = os.environ.get("DOCKER_HOST", "tcp://dind:2375")

# Client timeout (seconds) for every docker SDK call. Short on purpose:
# if the daemon is paused/unreachable we must fail fast with 503, not hang.
DOCKER_TIMEOUT = int(os.environ.get("DOCKER_TIMEOUT", "8"))

# SQLite metadata store (lives in the api-data volume).
DB_PATH = os.environ.get("DB_PATH", "/data/sandbox-api.db")

# Identity port pool: inner published port P == dind port P == localhost:P.
POOL_START = int(os.environ.get("POOL_START", "9200"))
POOL_END = int(os.environ.get("POOL_END", "9209"))

# Hard cap on existing sandboxes. Derived from the ENGINE (labels), not
# from the DB, so it stays correct even if the DB volume is wiped.
MAX_SANDBOXES = int(os.environ.get("MAX_SANDBOXES", "12"))

# TTL reaper sweep interval (seconds).
REAPER_INTERVAL = float(os.environ.get("REAPER_INTERVAL", "5"))

# Exec defaults / bounds.
EXEC_DEFAULT_TIMEOUT = int(os.environ.get("EXEC_DEFAULT_TIMEOUT", "30"))
EXEC_MAX_TIMEOUT = int(os.environ.get("EXEC_MAX_TIMEOUT", "300"))
EXEC_OUTPUT_LIMIT = 64 * 1024  # 64 KiB per stream, then truncated:true

# Readiness waits (seconds) for sandbox apps after containers start.
READY_TIMEOUT_WEB = int(os.environ.get("READY_TIMEOUT_WEB", "60"))
READY_TIMEOUT_COREAPP = int(os.environ.get("READY_TIMEOUT_COREAPP", "180"))

# Where the API can reach ports published by the inner engine. The inner
# dockerd binds published ports on the dind container's own interfaces, so
# from the API container they are simply dind:<port> on the compose net.
SANDBOX_HOST = os.environ.get("SANDBOX_HOST", "dind")

# Management labels - the API ONLY ever touches objects carrying these.
LABEL_MANAGED = "sandbox.managed"
LABEL_ID = "sandbox.id"
LABEL_NAME = "sandbox.name"
LABEL_TEMPLATE = "sandbox.template"
LABEL_ROLE = "sandbox.role"      # app | db | backend | frontend ...
LABEL_PORT = "sandbox.port"      # set on the container holding the port
