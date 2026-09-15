"""sandbox-scheduler - the REST control plane (T02).

Endpoints
  GET  /healthz                scheduler + upstream liveness (never hangs)
  GET  /status                 capacity + queue + tenant usage (console)
  POST /jobs                   submit (idempotent via Idempotency-Key header)
  GET  /jobs                   live jobs + recent terminal ones
  GET  /jobs/{id}              one job, with its sandboxes joined live
  POST /jobs/{id}/cancel       cancel from any non-terminal state
  GET  /jobs/{id}/events       ordered per-job event log
  GET  /queue                  the live queue with effective priorities
  GET  /metrics                JSON metrics (wait p50/p95/p99, throughput)
  GET  /metrics/prometheus     Prometheus text exposition
  POST /admin/reset            wipe all jobs (demo/testing only)

The console SPA is served at /. All responses are pure ASCII.
"""
import logging
import re
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import config, scheduler, store, upstream

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("scheduler.api")

app = FastAPI(title="sandbox-scheduler", version="1.0.0")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,20}$")


class JobRequest(BaseModel):
    name: str | None = None
    tenant: str = "default"
    template: str = "blank"
    kind: str = "task"                    # task | service
    cmd: list[str] | None = None          # required for kind=task
    priority: int = Field(default=3, ge=0, le=9)
    count: int = Field(default=1, ge=1, le=8)
    cpu_units: int | None = Field(default=None, ge=1, le=8)
    mem_units: int | None = Field(default=None, ge=1, le=8)
    max_runtime: int | None = Field(default=None, ge=5, le=86400)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if v is not None and not NAME_RE.match(v):
            raise ValueError("name must match ^[a-z0-9][a-z0-9-]{0,30}$")
        return v

    @field_validator("tenant")
    @classmethod
    def _tenant(cls, v):
        if not TENANT_RE.match(v):
            raise ValueError("tenant must match ^[a-z0-9][a-z0-9-]{0,20}$")
        return v

    @field_validator("template")
    @classmethod
    def _template(cls, v):
        if v not in config.TEMPLATE_COST:
            raise ValueError(f"unknown template; valid: "
                             f"{sorted(config.TEMPLATE_COST)}")
        return v

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        if v not in ("task", "service"):
            raise ValueError("kind must be task or service")
        return v


# -- views --------------------------------------------------------------------

def _view(job: dict, sandboxes_by_id: dict | None = None) -> dict:
    eff = (scheduler.effective_priority(job)
           if job["state"] == "QUEUED" else None)
    v = {
        "id": job["id"], "name": job["name"], "tenant": job["tenant"],
        "template": job["template"], "kind": job["kind"],
        "priority": job["priority"], "effective_priority": eff,
        "count": job["count"], "cpu_units": job["cpu_units"],
        "state": job["state"], "max_runtime": job["max_runtime"],
        "wait_seconds": (round(job["admitted_at"] - job["created_at"], 2)
                         if job["admitted_at"] else None),
        "error": job["error"], "result": job["result"],
        "created_at": job["created_at"], "finished_at": job["finished_at"],
        "sandbox_ids": job["sandbox_ids"],
    }
    if sandboxes_by_id is not None:
        v["sandboxes"] = [sandboxes_by_id.get(sid, {"id": sid, "state": "?"})
                          for sid in job["sandbox_ids"]]
    return v


def _sandboxes_by_id() -> dict:
    try:
        return {sb["id"]: sb for sb in upstream.list_sandboxes()}
    except Exception:
        return {}


# -- lifecycle ----------------------------------------------------------------

@app.on_event("startup")
def startup() -> None:
    store.init()
    try:
        scheduler.reconcile()
        log.info("reconcile done")
    except Exception:
        log.warning("reconcile skipped (upstream likely down at boot)")
    scheduler.start_loops()


# -- routes -------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"scheduler": "up", "upstream": "up" if upstream.ping() else "down"}


@app.get("/status")
def status():
    try:
        view = scheduler._engine_view()
        up_down = False
    except upstream.UpstreamDown:
        view = {"total_sandboxes": -1, "mine": -1, "unmanaged": -1,
                "free_ports": -1}
        up_down = True
    res_cpu, res_count = scheduler._reserved_units()
    tenant_used = scheduler._tenant_usage()
    return {
        "scheduler": "up",
        "upstream": "down" if up_down else "up",
        "capacity": {
            "cpu_total": config.CLUSTER_CPU_UNITS,
            "cpu_reserved": res_cpu,
            "slots_total": config.MAX_SCHED_SANDBOXES,
            "slots_used": view["total_sandboxes"],
            "ports_free": view["free_ports"],
        },
        "queue_depth": store.queue_depth(),
        "tenants": tenant_used,
        "max_per_tenant": config.MAX_PER_TENANT,
        "aging": {"interval": config.AGING_INTERVAL,
                  "max_bonus": config.AGING_MAX_BONUS},
    }


@app.post("/jobs", status_code=202)
def submit_job(req: JobRequest,
               idempotency_key: str | None = Header(default=None)):
    # task jobs must carry a command
    if req.kind == "task" and not req.cmd:
        raise HTTPException(422, "kind=task requires cmd")

    # idempotent submission (edge case #5): same key -> same job
    if idempotency_key:
        existing = store.get_by_idem(idempotency_key)
        if existing:
            same = (existing["tenant"] == req.tenant and
                    existing["template"] == req.template and
                    existing["kind"] == req.kind and
                    existing["cmd"] == req.cmd and
                    existing["count"] == req.count)
            if same:
                return JSONResponse(status_code=200, content=_view(existing))
            raise HTTPException(409, "Idempotency-Key reused with a "
                                     "different job body")

    # queue bound (edge case #9)
    if store.queue_depth() >= config.MAX_QUEUE:
        raise HTTPException(429, f"queue full ({config.MAX_QUEUE})",
                            headers={"Retry-After": "10"})

    cpu_def, mem_def, _ = config.TEMPLATE_COST[req.template]
    try:
        job = scheduler.submit(
            name=req.name or f"job-{req.template}",
            tenant=req.tenant, template=req.template, kind=req.kind,
            cmd=req.cmd, priority=req.priority, count=req.count,
            cpu_units=req.cpu_units or cpu_def,
            mem_units=req.mem_units or mem_def,
            max_runtime=req.max_runtime, idem_key=idempotency_key)
    except sqlite3.IntegrityError:
        # two concurrent retries with the same Idempotency-Key raced the
        # insert; the winner's row is the answer (edge case #5).
        existing = store.get_by_idem(idempotency_key) if idempotency_key else None
        if existing:
            return JSONResponse(status_code=200, content=_view(existing))
        raise
    return JSONResponse(status_code=202, content=_view(job))


@app.get("/jobs")
def list_jobs():
    by_id = _sandboxes_by_id()
    return [_view(j, by_id) for j in store.list_jobs()]


@app.get("/jobs/{jid}")
def get_job(jid: str):
    job = store.get(jid)
    if not job:
        raise HTTPException(404, f"job {jid} not found")
    return _view(job, _sandboxes_by_id())


@app.post("/jobs/{jid}/cancel")
def cancel_job(jid: str):
    job = store.get(jid)
    if not job:
        raise HTTPException(404, f"job {jid} not found")
    st = job["state"]
    if st in config.TERMINAL:
        raise HTTPException(409, f"job already terminal ({st})")
    if st == "CANCELLING":
        return _view(job)                      # idempotent (edge case #6)
    if st == "QUEUED":
        store.set_state(jid, "CANCELLED")      # never admitted, no sandbox
        store.add_event(jid, "state", "CANCELLED (while queued)")
        return _view(store.get(jid))
    # ADMITTED/RUNNING: the cancel loop tears sandboxes down with retry
    store.set_state(jid, "CANCELLING")
    store.add_event(jid, "state", "CANCELLING")
    return _view(store.get(jid))


@app.get("/jobs/{jid}/events")
def job_events(jid: str):
    if not store.get(jid):
        raise HTTPException(404, f"job {jid} not found")
    return store.events(jid)


@app.get("/queue")
def queue():
    now = time.monotonic()
    out = []
    for j in store.queued():
        eff = scheduler.effective_priority(j, now)
        out.append({
            "id": j["id"], "name": j["name"], "tenant": j["tenant"],
            "template": j["template"], "kind": j["kind"], "count": j["count"],
            "priority": j["priority"], "age_seconds": round(now - j["queued_mono"], 1),
            "aging_bonus": eff - j["priority"], "effective_priority": eff,
            "cpu_units": j["cpu_units"],
        })
    out.sort(key=lambda j: (-j["effective_priority"], j["age_seconds"]))
    return out


@app.get("/metrics")
def metrics():
    waits = sorted(store.wait_times())

    def pct(p):
        if not waits:
            return None
        k = max(0, min(len(waits) - 1, int(round(p * (len(waits) - 1)))))
        return round(waits[k], 3)

    counts: dict[str, int] = {}
    for j in store.list_jobs(limit_terminal=500):
        counts[j["state"]] = counts.get(j["state"], 0) + 1
    return {
        "upstream_down": scheduler.state["upstream_down"],
        "queue_depth": store.queue_depth(),
        "state_counts": counts,
        "wait_seconds": {"p50": pct(.50), "p95": pct(.95), "p99": pct(.99),
                         "samples": len(waits)},
        "throughput_finished_last_5m": store.finished_since(time.time() - 300),
    }


@app.get("/metrics/prometheus", response_class=PlainTextResponse)
def metrics_prom():
    m = metrics()
    lines = [
        f"scheduler_upstream_down {1 if m['upstream_down'] else 0}",
        f"scheduler_queue_depth {m['queue_depth']}",
    ]
    for st, n in sorted(m["state_counts"].items()):
        lines.append(f'scheduler_jobs_state_total{{state="{st}"}} {n}')
    for q, v in (("0.5", m["wait_seconds"]["p50"]),
                 ("0.95", m["wait_seconds"]["p95"]),
                 ("0.99", m["wait_seconds"]["p99"])):
        if v is not None:
            lines.append(f'scheduler_wait_seconds{{quantile="{q}"}} {v}')
    lines.append(f"scheduler_finished_last_5m "
                 f"{m['throughput_finished_last_5m']}")
    return "\n".join(lines) + "\n"


@app.post("/admin/reset")
def admin_reset():
    """Demo/testing only: wipe the job DB. Orphaned sandboxes are reaped
    by the janitor's name-correlated sweep, so nothing leaks."""
    return store.admin_reset()


# -- console (static SPA), mounted after all API routes -----------------------
STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def console():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
