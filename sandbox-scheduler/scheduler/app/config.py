"""Central configuration - everything overridable via environment."""
import os

# Upstream: the T01 sandbox-api that actually owns the docker engine.
SANDBOX_API_URL = os.environ.get("SANDBOX_API_URL",
                                 "http://host.docker.internal:9000").rstrip("/")
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "8"))

DB_PATH = os.environ.get("DB_PATH", "/data/scheduler.db")

# Cluster model: abstract budgets the scheduler may spend. cpu_units are
# abstract (1 unit ~ "a sandbox's fair share"); T03 turns these into real
# cgroup limits.
CLUSTER_CPU_UNITS = int(os.environ.get("CLUSTER_CPU_UNITS", "4"))
MAX_SCHED_SANDBOXES = int(os.environ.get("MAX_SCHED_SANDBOXES", "8"))

# Queue bounds - the queue may NEVER grow without limit (edge case #9).
MAX_QUEUE = int(os.environ.get("MAX_QUEUE", "50"))

# Noisy-neighbor cap: max concurrent sandboxes one tenant may hold.
MAX_PER_TENANT = int(os.environ.get("MAX_PER_TENANT", "3"))

# Aging: queued job gains +1 effective priority per AGING_INTERVAL seconds,
# capped at +AGING_MAX_BONUS (starvation-proofing, edge cases #2/#3).
AGING_INTERVAL = float(os.environ.get("AGING_INTERVAL", "5"))
AGING_MAX_BONUS = int(os.environ.get("AGING_MAX_BONUS", "6"))

# Loop cadences (seconds).
PLACER_INTERVAL = float(os.environ.get("PLACER_INTERVAL", "1"))
MONITOR_INTERVAL = float(os.environ.get("MONITOR_INTERVAL", "2"))
JANITOR_INTERVAL = float(os.environ.get("JANITOR_INTERVAL", "3"))
CANCEL_INTERVAL = float(os.environ.get("CANCEL_INTERVAL", "1"))

JOB_TASK_TIMEOUT = int(os.environ.get("JOB_TASK_TIMEOUT", "60"))

# Per-template default cost {template: (cpu_units, mem_units, needs_port)}.
TEMPLATE_COST = {
    "web":     (1, 1, True),
    "blank":   (1, 1, False),
    "coreapp": (2, 2, True),
}

# Job states:
#   QUEUED -> ADMITTED -> RUNNING -> SUCCEEDED | FAILED | EXPIRED
#   QUEUED|ADMITTED|RUNNING -> CANCELLING -> CANCELLED
TERMINAL = ("SUCCEEDED", "FAILED", "EXPIRED", "CANCELLED")
ACTIVE = ("ADMITTED", "RUNNING", "CANCELLING")   # hold cluster reservations
