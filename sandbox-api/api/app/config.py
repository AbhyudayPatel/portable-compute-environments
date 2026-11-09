"""Central configuration - everything overridable via environment.

Kept in one module so every tunable is discoverable and documented once.
"""
import os

# -- inner engine -------------------------------------------------------------
# Docker daemon of the INNER engine (the dind service on the compose net).
DOCKER_HOST = os.environ.get("DOCKER_HOST", "tcp://dind:2375")

# Client timeout (seconds) for every docker SDK call. Short on purpose:
# if the daemon is paused/unreachable we must fail fast with 503, not hang.
DOCKER_TIMEOUT = int(os.environ.get("DOCKER_TIMEOUT", "8"))

# -- engine-side agent proxy (runs inside the dind container) -----------------
# The ONE door to every sandbox agent: http://dind:7799/sbx/<inner-ip>/...
NODE_URL = os.environ.get("NODE_URL", "http://dind:7799").rstrip("/")
# Token for privileged /node/* operations (mounts, iptables). Shared with
# the dind service through compose/.env.
NODE_TOKEN = os.environ.get("SBX_NODE_TOKEN", "change-me-node-token")
# Per-call timeout when talking to agents through the proxy.
AGENT_TIMEOUT = int(os.environ.get("AGENT_TIMEOUT", "10"))

# -- metadata store -----------------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", "/data/sandbox-api.db")

# -- identity port pool -------------------------------------------------------
# inner published port P == dind port P == localhost:P
POOL_START = int(os.environ.get("POOL_START", "9200"))
POOL_END = int(os.environ.get("POOL_END", "9209"))

# Hard cap on existing sandboxes. Derived from the ENGINE (labels), not
# from the DB, so it stays correct even if the DB volume is wiped.
MAX_SANDBOXES = int(os.environ.get("MAX_SANDBOXES", "12"))

# -- background loops ---------------------------------------------------------
REAPER_INTERVAL = float(os.environ.get("REAPER_INTERVAL", "5"))
# Full engine<->DB resync cadence (the informer handles live events; this
# catches anything the event stream missed, e.g. during a dind restart).
RESYNC_INTERVAL = float(os.environ.get("RESYNC_INTERVAL", "30"))
INFORMER_ENABLED = os.environ.get("INFORMER_ENABLED", "1") == "1"

# -- exec defaults / bounds ---------------------------------------------------
EXEC_DEFAULT_TIMEOUT = int(os.environ.get("EXEC_DEFAULT_TIMEOUT", "30"))
EXEC_MAX_TIMEOUT = int(os.environ.get("EXEC_MAX_TIMEOUT", "300"))
EXEC_OUTPUT_LIMIT = 64 * 1024  # 64 KiB per stream in the one-shot reply

# -- readiness waits (seconds) ------------------------------------------------
READY_TIMEOUT_WEB = int(os.environ.get("READY_TIMEOUT_WEB", "60"))
READY_TIMEOUT_COREAPP = int(os.environ.get("READY_TIMEOUT_COREAPP", "240"))
READY_TIMEOUT_DESKTOP = int(os.environ.get("READY_TIMEOUT_DESKTOP", "600"))
READY_TIMEOUT_DEFAULT = int(os.environ.get("READY_TIMEOUT_DEFAULT", "90"))

# Where the API can reach ports published by the inner engine. The inner
# dockerd binds published ports on the dind container's own interfaces, so
# from the API container they are simply dind:<port> on the compose net.
SANDBOX_HOST = os.environ.get("SANDBOX_HOST", "dind")

# -- isolation ----------------------------------------------------------------
# "container" = runc (default). "gvisor" = runsc if the engine has it.
DEFAULT_ISOLATION = os.environ.get("DEFAULT_ISOLATION", "container")

# -- agent injection ----------------------------------------------------------
AGENT_BINARY = os.environ.get("AGENT_BINARY", "/app/bin/sbx-agent")
AGENT_PORT = 7700

# -- management labels (the API ONLY ever touches objects carrying these) ----
LABEL_MANAGED = "sandbox.managed"
LABEL_ID = "sandbox.id"
LABEL_NAME = "sandbox.name"
LABEL_TEMPLATE = "sandbox.template"
LABEL_ROLE = "sandbox.role"      # app | db | backend | frontend ...
LABEL_PORT = "sandbox.port"      # set on the container holding the port
LABEL_ISOLATION = "sandbox.isolation"

# Sandbox states. DEGRADED is new in v2: a container of a READY sandbox
# died (noticed live by the informer) - exec still works on survivors.
STATES = ("CREATING", "READY", "DEGRADED", "PAUSED", "STOPPED", "FAILED", "DELETED")
