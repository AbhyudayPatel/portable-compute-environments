"""Central configuration - everything overridable via environment."""
import os

# Upstream: the T01 sandbox-api that actually owns the docker engine.
SANDBOX_API_URL = os.environ.get("SANDBOX_API_URL", "http://host.docker.internal:9000").rstrip("/")
# T05 checkpoint service (needed for preemption of stateful service jobs).
CHECKPOINT_URL = os.environ.get("CHECKPOINT_URL", "http://host.docker.internal:9040").rstrip("/")
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "8"))

DB_PATH = os.environ.get("DB_PATH", "/data/scheduler.db")

# Cluster model: abstract budgets the scheduler may spend.
CLUSTER_CPU_UNITS = int(os.environ.get("CLUSTER_CPU_UNITS", "4"))
MAX_SCHED_SANDBOXES = int(os.environ.get("MAX_SCHED_SANDBOXES", "8"))
MAX_QUEUE = int(os.environ.get("MAX_QUEUE", "50"))
MAX_PER_TENANT = int(os.environ.get("MAX_PER_TENANT", "3"))

# Aging: +1 effective priority per AGING_INTERVAL queued, capped.
AGING_INTERVAL = float(os.environ.get("AGING_INTERVAL", "5"))
AGING_MAX_BONUS = int(os.environ.get("AGING_MAX_BONUS", "6"))

# Preemption: a queued job with effective priority >= PREEMPT_PRIORITY may
# evict lower-priority preemptible+stateful service jobs (checkpoint, delete,
# resume later by fork). A preempted job gets PREEMPT_RESUME_BONUS.
PREEMPT_PRIORITY = int(os.environ.get("PREEMPT_PRIORITY", "8"))
PREEMPT_RESUME_BONUS = int(os.environ.get("PREEMPT_RESUME_BONUS", "3"))

# Memory budget (bytes) for admission; empty = 75% of the engine's RAM as
# reported by sandbox-api /status. New jobs are estimated at
# MEM_RESERVE_PER_UNIT per cpu unit until the monitor measures them.
_mb = os.environ.get("MEMORY_BUDGET_BYTES", "")
MEMORY_BUDGET_BYTES = int(_mb) if _mb else None
MEM_RESERVE_PER_UNIT = int(os.environ.get("MEM_RESERVE_PER_UNIT", str(256 << 20)))

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

TERMINAL = ("SUCCEEDED", "FAILED", "EXPIRED", "CANCELLED")
ACTIVE = ("ADMITTED", "RUNNING", "CANCELLING", "PREEMPTING")   # hold cluster reservations
