"""The scheduler core. Four background loops around one admission lock:

  placer   - every PLACER_INTERVAL: while capacity remains, pick the next
             job (fair + aged + skip-and-fill) and admit it.
  monitor  - tracks ADMITTED jobs: sandbox READY -> RUNNING; task jobs get
             their command executed, result captured, sandbox released.
  janitor  - max-runtime expiry (monotonic deadlines), orphan reaping.
  cancel   - finishes CANCELLING jobs: retrying sandbox deletes until gone.

Admission invariants (all under a single lock -> no oversubscribe race):
  free_cpu   = CLUSTER_CPU_UNITS - reserved units - 1 per unmanaged sandbox
  free_slots = MAX_SCHED_SANDBOXES - total sandboxes in the engine
  free_ports = upstream pool free (only port-needing templates consume)
A job admits only if ALL of: fits free_cpu, fits free_slots, fits free_ports
(when the template needs one), and its tenant is under MAX_PER_TENANT.
"""
import logging
import threading
import time
import uuid

from . import config, store, upstream

log = logging.getLogger("scheduler.core")

# Single admission lock: every admission decision is serialized, so a
# thundering herd can never oversubscribe (edge case #1).
_admission = threading.Lock()

# Visible in /metrics - honest degradation signal (edge case #7).
state = {"upstream_down": False}


# -- helpers ------------------------------------------------------------------

def effective_priority(job: dict, now_mono: float | None = None) -> int:
    """base priority + aging bonus, capped (edge cases #2/#3)."""
    now_mono = time.monotonic() if now_mono is None else now_mono
    age = max(0.0, now_mono - job["queued_mono"])
    bonus = min(config.AGING_MAX_BONUS, int(age // config.AGING_INTERVAL))
    return job["priority"] + bonus


def sandbox_name(job: dict, k: int) -> str:
    """Name-correlated: scheduler sandboxes are always sched-<jobid>-<k>,
    so reconciliation and the orphan sweeper can always map them back."""
    return f"sched-{job['id']}-{k}"


def _reserved_units() -> tuple[int, int]:
    """(cpu, count) reserved by scheduler-owned active jobs (DB view)."""
    cpu = count = 0
    for j in store.active():
        cpu += j["cpu_units"] * j["count"]
        count += j["count"]
    return cpu, count


def _tenant_usage() -> dict[str, int]:
    usage: dict[str, int] = {}
    for j in store.active():
        usage[j["tenant"]] = usage.get(j["tenant"], 0) + j["count"]
    return usage


def _engine_view() -> dict:
    """Engine truth via upstream /status + /sandboxes. Raises UpstreamDown."""
    st = upstream.status()
    sandboxes = upstream.list_sandboxes()
    mine = sum(1 for sb in sandboxes if sb["name"].startswith("sched-"))
    return {
        "total_sandboxes": len(sandboxes),
        "mine": mine,
        "unmanaged": len(sandboxes) - mine,
        "free_ports": len(st["pool"]["free"]),
        "sandboxes_by_name": {sb["name"]: sb for sb in sandboxes},
    }


def _fits(job: dict, view: dict, res_cpu: int, res_count: int,
          tenant_used: dict[str, int]) -> bool:
    cpu_need = job["cpu_units"] * job["count"]
    # unmanaged engine sandboxes consume 1 abstract unit each (documented
    # approximation; they have no unit annotation to read).
    free_cpu = (config.CLUSTER_CPU_UNITS - res_cpu - view["unmanaged"])
    free_slots = config.MAX_SCHED_SANDBOXES - view["total_sandboxes"]
    if tenant_used.get(job["tenant"], 0) + job["count"] > config.MAX_PER_TENANT:
        return False
    if cpu_need > free_cpu or job["count"] > free_slots:
        return False
    needs_port = config.TEMPLATE_COST[job["template"]][2]
    if needs_port and job["count"] > view["free_ports"]:
        return False
    return True


def _select_next(view: dict, res_cpu: int, res_count: int) -> dict | None:
    """Weighted-fair across tenants, skip-and-fill within a tenant.

    Tenant order = least sandboxes currently held first (equal weights).
    Within the tenant, jobs ordered by (effective_priority desc, age asc);
    a job that doesn't fit is SKIPPED, not blocking smaller ones
    (head-of-line fix, edge case #10).
    """
    queued = store.queued()
    if not queued:
        return None
    now = time.monotonic()
    tenant_used = _tenant_usage()

    by_tenant: dict[str, list[dict]] = {}
    for j in queued:
        by_tenant.setdefault(j["tenant"], []).append(j)
    for jobs in by_tenant.values():
        jobs.sort(key=lambda j: (-effective_priority(j, now), j["created_at"]))

    # least-served tenant first (equal weights -> plain fair queueing)
    for tenant in sorted(by_tenant, key=lambda t: tenant_used.get(t, 0)):
        if tenant_used.get(tenant, 0) >= config.MAX_PER_TENANT:
            continue
        for job in by_tenant[tenant]:
            if _fits(job, view, res_cpu, res_count, tenant_used):
                return job
        # nothing of this tenant fits right now; try next tenant
    return None


# -- admission + provisioning -------------------------------------------------

def _admit(job: dict) -> None:
    """DB-side admission. The caller holds _admission; capacity was checked."""
    deadline_mono = deadline_wall = None
    if job["max_runtime"]:
        deadline_mono = time.monotonic() + job["max_runtime"]
        deadline_wall = time.time() + job["max_runtime"]
    store.set_deadline(job["id"], deadline_mono, deadline_wall)
    store.set_state(job["id"], "ADMITTED")
    store.add_event(job["id"], "state",
                    f"ADMITTED (priority {job['priority']}"
                    f"+{effective_priority(job) - job['priority']} aged, "
                    f"{job['cpu_units']}cpu x{job['count']})")
    threading.Thread(target=_provision, args=(job["id"],), daemon=True).start()


def _provision(jid: str) -> None:
    """Create the job's sandboxes via upstream. Gang-aware rollback."""
    job = store.get(jid)
    if not job or job["state"] != "ADMITTED":
        return
    made: list[str] = []
    try:
        for k in range(job["count"]):
            sb = upstream.create_sandbox(sandbox_name(job, k), job["template"])
            made.append(sb["id"])
            store.add_event(jid, "create",
                            f"sandbox {sb['name']} accepted upstream")
        store.set_sandboxes(jid, made)
    except upstream.UpstreamDown as e:
        # transient: put it back so the placer retries later (no hot loop;
        # the placer's own backoff applies)
        log.warning("provision upstream down for %s: %s", jid, e)
        store.add_event(jid, "error", f"upstream down during create: {e}")
        for sid in made:
            _safe_delete(sid, jid)
        store.set_sandboxes(jid, [])
        store.set_state(jid, "QUEUED")           # requeue, deadline re-set on admit
        store.add_event(jid, "state", "QUEUED (requeued after upstream outage)")
    except Exception as e:
        # e.g. upstream 429 -> gang-safe rollback (edge case: partial gang)
        store.add_event(jid, "error", f"provision failed: {e}")
        for sid in made:
            _safe_delete(sid, jid)
        store.set_sandboxes(jid, [])
        store.set_state(jid, "FAILED", str(e)[:400])
        store.add_event(jid, "state", "FAILED (provision)")


def _safe_delete(sid: str, jid: str) -> None:
    try:
        upstream.delete_sandbox(sid)
    except Exception as e:
        store.add_event(jid, "error", f"delete {sid}: {e}")


# -- the four loops -----------------------------------------------------------

def placer_loop() -> None:
    while True:
        time.sleep(config.PLACER_INTERVAL)
        try:
            with _admission:
                view = _engine_view()
                state["upstream_down"] = False
                while True:
                    res_cpu, res_count = _reserved_units()
                    job = _select_next(view, res_cpu, res_count)
                    if not job:
                        break
                    _admit(job)
                    # reflect the reservation in this cycle's view so the
                    # loop's capacity math stays exact without re-querying
                    view["total_sandboxes"] += job["count"]
                    view["mine"] += job["count"]
                    needs_port = config.TEMPLATE_COST[job["template"]][2]
                    if needs_port:
                        view["free_ports"] -= job["count"]
        except upstream.UpstreamDown as e:
            if not state["upstream_down"]:
                log.warning("placer: upstream down: %s", e)
            state["upstream_down"] = True
        except Exception:
            log.exception("placer tick failed")


def monitor_loop() -> None:
    while True:
        time.sleep(config.MONITOR_INTERVAL)
        try:
            for job in store.by_state("ADMITTED", "RUNNING"):
                _monitor_job(job)
        except upstream.UpstreamDown:
            state["upstream_down"] = True
        except Exception:
            log.exception("monitor tick failed")


def _monitor_job(job: dict) -> None:
    names = [sandbox_name(job, k) for k in range(job["count"])]
    sandboxes = upstream.list_sandboxes()
    found = [sb for sb in sandboxes if sb["name"] in names]
    # keep the job's sandbox_ids truthful (creation may have raced monitor)
    if found and not job["sandbox_ids"]:
        store.set_sandboxes(job["id"], [sb["id"] for sb in found])

    if job["state"] == "ADMITTED":
        if not found:
            return  # create thread hasn't posted yet
        if any(sb["state"] == "FAILED" for sb in found):
            store.set_state(job["id"], "FAILED", "sandbox failed upstream")
            store.add_event(job["id"], "state", "FAILED (sandbox failed)")
            for sb in found:
                _safe_delete(sb["id"], job["id"])
            return
        if len(found) == job["count"] and all(sb["state"] == "READY"
                                              for sb in found):
            store.set_state(job["id"], "RUNNING")
            store.add_event(job["id"], "state", "RUNNING")
            if job["kind"] == "task":
                threading.Thread(target=_run_task, args=(job["id"],),
                                 daemon=True).start()
    elif job["state"] == "RUNNING":
        if job["kind"] == "service" and len(found) < job["count"]:
            store.set_state(job["id"], "FAILED",
                            "sandbox vanished while RUNNING")
            store.add_event(job["id"], "error", "sandbox vanished")
            for sb in found:
                _safe_delete(sb["id"], job["id"])


def _run_task(jid: str) -> None:
    """Task-kind job: execute cmd in the sandbox, capture, release."""
    job = store.get(jid)
    if not job:
        return
    sids = job["sandbox_ids"]
    results = []
    ok = True
    for sid in sids:
        try:
            r = upstream.exec_sandbox(sid, job["cmd"], config.JOB_TASK_TIMEOUT)
            results.append(r)
            if r.get("exit_code") != 0:
                ok = False
        except Exception as e:
            results.append({"error": str(e)})
            ok = False
    store.set_result(jid, {"results": results})
    for sid in sids:
        _safe_delete(sid, jid)
    if ok:
        store.set_state(jid, "SUCCEEDED")
        store.add_event(jid, "state", "SUCCEEDED")
    else:
        store.set_state(jid, "FAILED", "task command failed")
        store.add_event(jid, "state", "FAILED (task exit != 0)")


def janitor_loop() -> None:
    while True:
        time.sleep(config.JANITOR_INTERVAL)
        try:
            # expiry on monotonic deadlines (edge case #8)
            now = time.monotonic()
            for job in store.by_state("ADMITTED", "RUNNING"):
                if job["deadline_mono"] and now >= job["deadline_mono"]:
                    store.add_event(job["id"], "state",
                                    "max_runtime exceeded")
                    for sid in job["sandbox_ids"]:
                        _safe_delete(sid, job["id"])
                    store.set_state(job["id"], "EXPIRED")
            # orphan sweep: sched-* sandboxes with no live job
            sandboxes = upstream.list_sandboxes()
            live_names = {sandbox_name(j, k)
                          for j in store.by_state("ADMITTED", "RUNNING",
                                                  "CANCELLING", "QUEUED")
                          for k in range(j["count"])}
            for sb in sandboxes:
                if sb["name"].startswith("sched-") and \
                        sb["name"] not in live_names and \
                        sb["state"] != "DELETED":
                    log.warning("janitor: reaping orphan %s", sb["name"])
                    _safe_delete(sb["id"], "janitor")
        except upstream.UpstreamDown:
            state["upstream_down"] = True
        except Exception:
            log.exception("janitor tick failed")


def cancel_loop() -> None:
    """Finish CANCELLING jobs: delete with retry until the engine says gone
    (upstream returns 409 while the sandbox is still CREATING)."""
    while True:
        time.sleep(config.CANCEL_INTERVAL)
        try:
            for job in store.by_state("CANCELLING"):
                names = {sandbox_name(job, k) for k in range(job["count"])}
                for sb in upstream.list_sandboxes():
                    if sb["name"] in names and sb["state"] != "DELETED":
                        try:
                            upstream.delete_sandbox(sb["id"])
                        except (upstream.UpstreamConflict,
                                upstream.UpstreamDown):
                            pass           # still CREATING / down; next tick
                # re-check: gone for real -> terminal CANCELLED
                remaining = [sb for sb in upstream.list_sandboxes()
                             if sb["name"] in names and sb["state"] != "DELETED"]
                if not remaining:
                    store.set_state(job["id"], "CANCELLED")
                    store.add_event(job["id"], "state", "CANCELLED")
        except upstream.UpstreamDown:
            state["upstream_down"] = True
        except Exception:
            log.exception("cancel tick failed")


# -- crash recovery (edge case #4) --------------------------------------------

def reconcile() -> None:
    """On boot: jobs whose state assumed in-flight work get re-anchored.

    Self-stabilizing design: ADMITTED/RUNNING jobs are re-derived from the
    engine by the monitor loop; CANCELLING by the cancel loop; QUEUED just
    resume. Here we only re-anchor the clocks (monotonic is per-process).
    """
    now_m, now_w = time.monotonic(), time.time()
    for job in store.by_state("QUEUED", "ADMITTED", "RUNNING", "CANCELLING"):
        age_wall = now_w - job["created_at"]
        store.reset_queued_mono(job["id"], now_m - age_wall)
        if job["deadline_wall"]:
            remaining = job["deadline_wall"] - now_w
            store.set_deadline(job["id"], now_m + max(0, remaining),
                               job["deadline_wall"])
        store.add_event(job["id"], "reconcile",
                        f"re-anchored after scheduler restart (state kept: "
                        f"{job['state']})")


def start_loops() -> None:
    for fn in (placer_loop, monitor_loop, janitor_loop, cancel_loop):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()


# -- submission (called by the API layer) -------------------------------------

def submit(name: str, tenant: str, template: str, kind: str,
           cmd: list[str] | None, priority: int, count: int,
           cpu_units: int, mem_units: int, max_runtime: int | None,
           idem_key: str | None) -> dict:
    jid = uuid.uuid4().hex[:10]
    job = {
        "id": jid, "idem_key": idem_key, "name": name, "tenant": tenant,
        "template": template, "kind": kind, "cmd": cmd, "priority": priority,
        "count": count, "cpu_units": cpu_units, "mem_units": mem_units,
        "state": "QUEUED", "max_runtime": max_runtime,
        "queued_mono": time.monotonic(),
    }
    store.create_job(job)
    store.add_event(jid, "state",
                    f"QUEUED (tenant={tenant}, prio={priority}, "
                    f"{cpu_units}cpu x{count})")
    return store.get(jid)
