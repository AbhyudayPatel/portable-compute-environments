"""sandbox-scheduler v2 - the REST control plane (T02, extended).

Endpoints
  GET  /healthz                scheduler + upstream + checkpoint service liveness
  GET  /status                 capacity (cpu, slots, ports, memory budget), queue, tenants
  POST /jobs                   submit (Idempotency-Key); v2 fields: deadline_seconds,
                               stateful, preemptible
  GET  /jobs[/{id}]            list / one (sandboxes joined live)
  POST /jobs/{id}/cancel       from any non-terminal state
  GET  /jobs/{id}/events       ordered per-job event log
  GET  /queue                  live queue (QUEUED + PREEMPTED) with effective priority
  GET  /metrics  /metrics/prometheus
  POST /admin/reset

Threads drive the core: placer / monitor / janitor / cancel ticks, plus
provision and preemption generators run to completion on worker threads.
"""
import logging
import re
import sqlite3
import threading
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import clock, config
from .core import ApiError, Core
from .store import Store
from .upstream import Upstream, UpstreamDown

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("scheduler.api")

app = FastAPI(title="sandbox-scheduler", version="2.0.0")
store = Store(config.DB_PATH)
upstream = Upstream()
core = Core(store, upstream)
_admission = threading.Lock()

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,20}$")


class JobRequest(BaseModel):
    name: str | None = None
    tenant: str = "default"
    template: str = "blank"
    kind: str = "task"
    cmd: list[str] | None = None
    priority: int = Field(default=3, ge=0, le=9)
    count: int = Field(default=1, ge=1, le=8)
    cpu_units: int | None = Field(default=None, ge=1, le=8)
    mem_units: int | None = Field(default=None, ge=1, le=8)
    max_runtime: int | None = Field(default=None, ge=5, le=86400)
    deadline_seconds: int | None = Field(default=None, ge=1, le=86400)
    stateful: bool = False
    preemptible: bool | None = None       # default: True for services, False for tasks

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
            raise ValueError(f"unknown template; valid: {sorted(config.TEMPLATE_COST)}")
        return v

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        if v not in ("task", "service"):
            raise ValueError("kind must be task or service")
        return v


def _http(e: ApiError) -> HTTPException:
    return HTTPException(e.status, detail=e.detail, headers=e.headers or None)


def _view(job: dict, sandboxes_by_id: dict | None = None) -> dict:
    eff = core.effective_priority(job) if job["state"] in ("QUEUED", "PREEMPTED") else None
    v = {
        "id": job["id"], "name": job["name"], "tenant": job["tenant"], "template": job["template"],
        "kind": job["kind"], "priority": job["priority"], "effective_priority": eff,
        "count": job["count"], "cpu_units": job["cpu_units"], "state": job["state"],
        "max_runtime": job["max_runtime"],
        "wait_seconds": (round(job["admitted_at"] - job["created_at"], 2) if job["admitted_at"] else None),
        "error": job["error"], "result": job["result"], "created_at": job["created_at"],
        "finished_at": job["finished_at"], "sandbox_ids": job["sandbox_ids"],
        "deadline_at": job.get("deadline_at"), "deadline_missed": bool(job.get("deadline_missed")),
        "stateful": job.get("stateful"), "preemptible": job.get("preemptible"),
        "checkpoint_id": job.get("checkpoint_id"), "preempt_count": job.get("preempt_count", 0),
        "mem_bytes": job.get("mem_bytes"),
    }
    if sandboxes_by_id is not None:
        v["sandboxes"] = [sandboxes_by_id.get(sid, {"id": sid, "state": "?"}) for sid in job["sandbox_ids"]]
    return v


def _sandboxes_by_id() -> dict:
    try:
        return {sb["id"]: sb for sb in upstream.list_sandboxes()}
    except Exception:
        return {}


# -- drivers ------------------------------------------------------------------

def _run_gen(gen_factory, jid: str, label: str) -> None:
    def run():
        try:
            for _ in gen_factory(jid):
                pass
        except Exception:
            log.exception("%s %s crashed", label, jid)
    threading.Thread(target=run, daemon=True, name=f"{label}-{jid}").start()


def placer_loop():
    while True:
        clock.sleep(config.PLACER_INTERVAL)
        try:
            with _admission:
                res = core.placer_tick()
            for jid in res["admitted"]:
                _run_gen(core.provision_steps, jid, "provision")
            for jid in res["preempting"]:
                _run_gen(core.preempt_steps, jid, "preempt")
        except UpstreamDown as e:
            if not core.state["upstream_down"]:
                log.warning("placer: upstream down: %s", e)
            core.state["upstream_down"] = True
        except Exception:
            log.exception("placer tick failed")


def monitor_loop():
    while True:
        clock.sleep(config.MONITOR_INTERVAL)
        try:
            before = {j["id"] for j in store.by_state("RUNNING")}
            core.monitor_tick()
            for j in store.by_state("RUNNING"):
                if j["id"] not in before and j["kind"] == "task":
                    threading.Thread(target=core.run_task, args=(j["id"],), daemon=True).start()
        except UpstreamDown:
            core.state["upstream_down"] = True
        except Exception:
            log.exception("monitor tick failed")


def janitor_loop():
    while True:
        clock.sleep(config.JANITOR_INTERVAL)
        try:
            core.janitor_tick()
        except UpstreamDown:
            core.state["upstream_down"] = True
        except Exception:
            log.exception("janitor tick failed")


def cancel_loop():
    while True:
        clock.sleep(config.CANCEL_INTERVAL)
        try:
            core.cancel_tick()
        except UpstreamDown:
            core.state["upstream_down"] = True
        except Exception:
            log.exception("cancel tick failed")


@app.on_event("startup")
def startup() -> None:
    store.init()
    try:
        log.info("reconcile: %s", core.reconcile())
    except Exception:
        log.warning("reconcile skipped")
    for fn in (placer_loop, monitor_loop, janitor_loop, cancel_loop):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()


# -- routes -------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"scheduler": "up", "upstream": "up" if upstream.ping() else "down",
            "checkpoint": "up" if upstream.ping_checkpoint() else "down"}


@app.get("/status")
def status():
    try:
        view = core.engine_view()
        up_down = False
    except UpstreamDown:
        view = {"total_sandboxes": -1, "mine": -1, "unmanaged": -1, "free_ports": -1, "memory_budget": None}
        up_down = True
    res_cpu, res_count, tenant_used = core.reserved()
    return {
        "scheduler": "up", "upstream": "down" if up_down else "up",
        "capacity": {"cpu_total": config.CLUSTER_CPU_UNITS, "cpu_reserved": res_cpu,
                     "slots_total": config.MAX_SCHED_SANDBOXES, "slots_used": view["total_sandboxes"],
                     "ports_free": view["free_ports"],
                     "memory_budget": view.get("memory_budget"),
                     "memory_observed": core._observed_memory()},
        "queue_depth": store.queue_depth(), "tenants": tenant_used,
        "max_per_tenant": config.MAX_PER_TENANT,
        "aging": {"interval": config.AGING_INTERVAL, "max_bonus": config.AGING_MAX_BONUS},
        "preemption": {"priority": config.PREEMPT_PRIORITY, "resume_bonus": config.PREEMPT_RESUME_BONUS,
                       **{k: core.state[k] for k in ("preemptions", "resumes", "checkpoint_down")}},
    }


@app.post("/jobs", status_code=202)
def submit_job(req: JobRequest, idempotency_key: str | None = Header(default=None)):
    if req.kind == "task" and not req.cmd:
        raise HTTPException(422, "kind=task requires cmd")
    body = req.model_dump()
    try:
        outcome, job = core.submit(body, idempotency_key)
    except ApiError as e:
        raise _http(e)
    except sqlite3.IntegrityError:
        existing = store.get_by_idem(idempotency_key) if idempotency_key else None
        if existing:
            return JSONResponse(status_code=200, content=_view(existing))
        raise
    return JSONResponse(status_code=200 if outcome == "existing" else 202, content=_view(job))


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
    try:
        return _view(core.cancel(jid))
    except ApiError as e:
        raise _http(e)


@app.get("/jobs/{jid}/events")
def job_events(jid: str):
    if not store.get(jid):
        raise HTTPException(404, f"job {jid} not found")
    return store.events(jid)


@app.get("/queue")
def queue():
    now = clock.now_mono()
    out = []
    for j in store.by_state("QUEUED", "PREEMPTED"):
        eff = core.effective_priority(j, now)
        out.append({
            "id": j["id"], "name": j["name"], "tenant": j["tenant"], "template": j["template"],
            "kind": j["kind"], "count": j["count"], "priority": j["priority"],
            "age_seconds": round(now - j["queued_mono"], 1), "aging_bonus": eff - j["priority"],
            "effective_priority": eff, "cpu_units": j["cpu_units"], "state": j["state"],
            "deadline_in": (round(j["deadline_at"] - clock.now_wall(), 1) if j.get("deadline_at") else None),
            "checkpoint_id": j.get("checkpoint_id"),
        })
    out.sort(key=lambda j: (-j["effective_priority"], j["deadline_in"] if j["deadline_in"] is not None else 1e18, j["age_seconds"]))
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
    c = store.counters()
    return {
        "upstream_down": core.state["upstream_down"], "checkpoint_down": core.state["checkpoint_down"],
        "queue_depth": store.queue_depth(), "state_counts": counts,
        "wait_seconds": {"p50": pct(.50), "p95": pct(.95), "p99": pct(.99), "samples": len(waits)},
        "throughput_finished_last_5m": store.finished_since(clock.now_wall() - 300),
        "preemptions_total": c["preempts"], "resumes_total": core.state["resumes"],
        "deadline_missed_total": c["missed"], "jobs_with_deadline": c["with_deadline"],
        "memory_refusals_total": core.state["memory_refusals"],
    }


@app.get("/metrics/prometheus", response_class=PlainTextResponse)
def metrics_prom():
    m = metrics()
    lines = [f"scheduler_upstream_down {1 if m['upstream_down'] else 0}",
             f"scheduler_queue_depth {m['queue_depth']}",
             f"scheduler_preemptions_total {m['preemptions_total']}",
             f"scheduler_resumes_total {m['resumes_total']}",
             f"scheduler_deadline_missed_total {m['deadline_missed_total']}",
             f"scheduler_memory_refusals_total {m['memory_refusals_total']}"]
    for st, n in sorted(m["state_counts"].items()):
        lines.append(f'scheduler_jobs_state_total{{state="{st}"}} {n}')
    for q, v in (("0.5", m["wait_seconds"]["p50"]), ("0.95", m["wait_seconds"]["p95"]), ("0.99", m["wait_seconds"]["p99"])):
        if v is not None:
            lines.append(f'scheduler_wait_seconds{{quantile="{q}"}} {v}')
    lines.append(f"scheduler_finished_last_5m {m['throughput_finished_last_5m']}")
    return "\n".join(lines) + "\n"


@app.post("/admin/reset")
def admin_reset():
    return store.admin_reset()


STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def console():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
