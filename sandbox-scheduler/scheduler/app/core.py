"""The scheduler core (v2) - policy and state transitions, no threads, no
HTTP, no clock. main.py drives it with threads; the simulator drives it
in-process against the sandbox-api core.

v2 adds to the v1 policy (capacity + fair queueing + aging + skip-and-fill
+ gang admission):

  preemption     a job with effective priority >= PREEMPT_PRIORITY that
                 does not fit may evict lower-priority RUNNING *service*
                 jobs that are preemptible AND stateful: the victim is
                 checkpointed (sandbox-checkpoint), its sandbox deleted,
                 and the job goes to PREEMPTED - a queued state with a
                 resume bonus. When it is admitted again it is FORKED from
                 its checkpoint instead of created from scratch.
  EDF            jobs may carry deadline_seconds; within a tenant the order
                 is (effective priority desc, earliest deadline, age).
                 Misses are counted.
  memory budget  the monitor samples each running sandbox's memory
                 (cgroup, via the agent) and admission refuses when
                 observed + estimated-for-new would exceed the budget.

Ports: store (SQLite), upstream (sandbox-api + checkpoint clients), clock.

States:
  QUEUED -> ADMITTED -> RUNNING -> SUCCEEDED | FAILED | EXPIRED
  RUNNING -> PREEMPTING -> PREEMPTED -> ADMITTED (resume = fork)
  any non-terminal -> CANCELLING -> CANCELLED
"""
import logging
import uuid

from . import clock, config

log = logging.getLogger("scheduler.core")


class ApiError(Exception):
    def __init__(self, status: int, detail: str, headers: dict | None = None):
        super().__init__(detail)
        self.status, self.detail, self.headers = status, detail, headers or {}


class Core:
    def __init__(self, store, upstream, *, cpu_units=config.CLUSTER_CPU_UNITS,
                 max_sandboxes=config.MAX_SCHED_SANDBOXES, max_queue=config.MAX_QUEUE,
                 max_per_tenant=config.MAX_PER_TENANT, aging_interval=config.AGING_INTERVAL,
                 aging_max_bonus=config.AGING_MAX_BONUS, preempt_priority=config.PREEMPT_PRIORITY,
                 resume_bonus=config.PREEMPT_RESUME_BONUS, memory_budget=config.MEMORY_BUDGET_BYTES,
                 mem_per_unit=config.MEM_RESERVE_PER_UNIT, id_factory=None):
        self.store = store
        self.upstream = upstream
        self.cpu_units = cpu_units
        self.max_sandboxes = max_sandboxes
        self.max_queue = max_queue
        self.max_per_tenant = max_per_tenant
        self.aging_interval = aging_interval
        self.aging_max_bonus = aging_max_bonus
        self.preempt_priority = preempt_priority
        self.resume_bonus = resume_bonus
        self.memory_budget = memory_budget          # None = derive from engine
        self.mem_per_unit = mem_per_unit
        self.new_id = id_factory or (lambda: uuid.uuid4().hex[:10])
        self.state = {"upstream_down": False, "checkpoint_down": False, "preemptions": 0,
                      "resumes": 0, "deadline_missed": 0, "memory_refusals": 0}

    # -- helpers -----------------------------------------------------------------

    def effective_priority(self, job: dict, now_mono: float | None = None) -> int:
        now_mono = clock.now_mono() if now_mono is None else now_mono
        age = max(0.0, now_mono - job["queued_mono"])
        bonus = min(self.aging_max_bonus, int(age // self.aging_interval))
        if job["state"] == "PREEMPTED":
            bonus += self.resume_bonus
        return job["priority"] + bonus

    @staticmethod
    def sandbox_name(job: dict, k: int) -> str:
        return f"sched-{job['id']}-{k}"

    def reserved(self) -> tuple[int, int, dict]:
        cpu = count = 0
        tenant: dict[str, int] = {}
        for j in self.store.active():
            cpu += j["cpu_units"] * j["count"]
            count += j["count"]
            tenant[j["tenant"]] = tenant.get(j["tenant"], 0) + j["count"]
        return cpu, count, tenant

    def engine_view(self) -> dict:
        st = self.upstream.status()
        sandboxes = self.upstream.list_sandboxes()
        mine = sum(1 for sb in sandboxes if sb["name"].startswith("sched-"))
        mem_total = (st.get("engine") or {}).get("memory_bytes")
        budget = self.memory_budget or (int(mem_total * 0.75) if mem_total else None)
        return {"total_sandboxes": len(sandboxes), "mine": mine,
                "unmanaged": len(sandboxes) - mine,
                "free_ports": len(st["pool"]["free"]),
                "memory_budget": budget,
                "sandboxes_by_name": {sb["name"]: sb for sb in sandboxes}}

    def _fits(self, job: dict, view: dict, res_cpu: int, tenant_used: dict,
              observed_mem: int) -> tuple[bool, str]:
        cpu_need = job["cpu_units"] * job["count"]
        free_cpu = self.cpu_units - res_cpu - view["unmanaged"]
        free_slots = self.max_sandboxes - view["total_sandboxes"]
        if tenant_used.get(job["tenant"], 0) + job["count"] > self.max_per_tenant:
            return False, "tenant cap"
        if cpu_need > free_cpu:
            return False, "cpu"
        if job["count"] > free_slots:
            return False, "slots"
        needs_port = config.TEMPLATE_COST[job["template"]][2]
        if needs_port and job["count"] > view["free_ports"]:
            return False, "ports"
        budget = view.get("memory_budget")
        if budget:
            est = observed_mem + self.mem_per_unit * cpu_need
            if est > budget:
                return False, "memory"
        return True, ""

    def _observed_memory(self) -> int:
        return sum((j.get("mem_bytes") or 0) for j in self.store.by_state("RUNNING", "ADMITTED"))

    def select_next(self, view: dict, res_cpu: int, tenant_used: dict) -> tuple[dict | None, dict | None]:
        """Returns (admittable job, blocked head job). The head job is the
        best-ranked job that did NOT fit (preemption candidate)."""
        queued = self.store.by_state("QUEUED", "PREEMPTED")
        if not queued:
            return None, None
        now = clock.now_mono()
        observed_mem = self._observed_memory()
        by_tenant: dict[str, list[dict]] = {}
        for j in queued:
            by_tenant.setdefault(j["tenant"], []).append(j)
        for jobs in by_tenant.values():
            jobs.sort(key=lambda j: (-self.effective_priority(j, now),
                                     j.get("deadline_wall") or float("inf"), j["created_at"]))
        head, head_prio = None, -1
        for tenant in sorted(by_tenant, key=lambda t: tenant_used.get(t, 0)):
            if tenant_used.get(tenant, 0) >= self.max_per_tenant:
                continue
            for job in by_tenant[tenant]:
                fits, why = self._fits(job, view, res_cpu, tenant_used, observed_mem)
                if fits:
                    return job, head
                if why == "memory":
                    self.state["memory_refusals"] += 1
                # preemption candidate = the highest-ranked job (across tenants)
                # that is blocked on CPU, not on its tenant cap / slots / ports
                ep = self.effective_priority(job, now)
                if why == "cpu" and ep > head_prio:
                    head, head_prio = job, ep
        return None, head

    # -- admission ----------------------------------------------------------------

    def admit(self, job: dict) -> None:
        deadline_mono = deadline_wall = None
        if job["max_runtime"]:
            deadline_mono = clock.now_mono() + job["max_runtime"]
            deadline_wall = clock.now_wall() + job["max_runtime"]
        self.store.set_deadline(job["id"], deadline_mono, deadline_wall)
        resumed = job["state"] == "PREEMPTED"
        self.store.set_state(job["id"], "ADMITTED")
        self.store.add_event(job["id"], "state",
                             f"ADMITTED{' (resume from checkpoint ' + str(job.get('checkpoint_id')) + ')' if resumed else ''} "
                             f"(priority {job['priority']}+{self.effective_priority(job) - job['priority']} aged, "
                             f"{job['cpu_units']}cpu x{job['count']})")
        if resumed:
            self.state["resumes"] += 1

    def placer_tick(self) -> dict:
        """One placement round. Returns a summary. Raises UpstreamDown."""
        view = self.engine_view()
        self.state["upstream_down"] = False
        admitted, preempted = [], []
        while True:
            res_cpu, _, tenant_used = self.reserved()
            job, head = self.select_next(view, res_cpu, tenant_used)
            if job:
                self.admit(job)
                admitted.append(job["id"])
                view["total_sandboxes"] += job["count"]
                view["mine"] += job["count"]
                if config.TEMPLATE_COST[job["template"]][2]:
                    view["free_ports"] -= job["count"]
                continue
            if head and self.effective_priority(head) >= self.preempt_priority:
                victims = self.choose_victims(head, res_cpu, view)
                for v in victims:
                    self.store.set_state(v["id"], "PREEMPTING")
                    self.store.add_event(v["id"], "state",
                                         f"PREEMPTING (evicted for job {head['id']} prio "
                                         f"{self.effective_priority(head)})")
                    preempted.append(v["id"])
            break
        return {"admitted": admitted, "preempting": preempted}

    def choose_victims(self, head: dict, res_cpu: int, view: dict) -> list[dict]:
        """Lowest-priority preemptible, stateful, single-sandbox service jobs
        first, until enough cpu units would be freed for `head`."""
        need = head["cpu_units"] * head["count"] - (self.cpu_units - res_cpu - view["unmanaged"])
        if need <= 0:
            return []
        cands = [j for j in self.store.by_state("RUNNING")
                 if j["kind"] == "service" and j.get("preemptible") and j.get("stateful")
                 and j["count"] == 1 and j["priority"] < self.effective_priority(head)
                 and j["id"] != head["id"]]
        cands.sort(key=lambda j: (j["priority"], -j["created_at"]))   # lowest prio, youngest first
        out, freed = [], 0
        for j in cands:
            out.append(j)
            freed += j["cpu_units"]
            if freed >= need:
                return out
        return []                                   # cannot free enough: preempt nobody

    # -- provisioning (generator) ------------------------------------------------------

    def provision_steps(self, jid: str):
        job = self.store.get(jid)
        if not job or job["state"] != "ADMITTED":
            return
        made: list[str] = []
        try:
            for k in range(job["count"]):
                if job.get("checkpoint_id"):
                    sb = self.upstream.fork_checkpoint(job["checkpoint_id"], self.sandbox_name(job, k))
                    self.store.add_event(jid, "create", f"sandbox {sb['name']} forked from checkpoint {job['checkpoint_id']}")
                else:
                    sb = self.upstream.create_sandbox(self.sandbox_name(job, k), job["template"],
                                                      stateful=bool(job.get("stateful")))
                    self.store.add_event(jid, "create", f"sandbox {sb['name']} accepted upstream")
                made.append(sb["id"])
                yield f"created:{k}"
            self.store.set_sandboxes(jid, made)
        except self.upstream.Down as e:
            self.store.add_event(jid, "error", f"upstream down during create: {e}")
            for sid in made:
                self._safe_delete(sid, jid)
            self.store.set_sandboxes(jid, [])
            self.store.set_state(jid, "PREEMPTED" if job.get("checkpoint_id") else "QUEUED")
            self.store.add_event(jid, "state", "requeued after upstream outage")
        except Exception as e:
            self.store.add_event(jid, "error", f"provision failed: {e}")
            for sid in made:
                self._safe_delete(sid, jid)
            self.store.set_sandboxes(jid, [])
            self.store.set_state(jid, "FAILED", str(e)[:400])
            self.store.add_event(jid, "state", "FAILED (provision)")

    def _safe_delete(self, sid: str, jid: str) -> None:
        try:
            self.upstream.delete_sandbox(sid)
        except Exception as e:
            self.store.add_event(jid, "error", f"delete {sid}: {e}")

    # -- preemption (generator) --------------------------------------------------------

    def preempt_steps(self, jid: str):
        job = self.store.get(jid)
        if not job or job["state"] != "PREEMPTING" or not job["sandbox_ids"]:
            return
        sid = job["sandbox_ids"][0]
        try:
            cp = self.upstream.checkpoint_create(sid, label=f"preempt {jid}")
            yield "checkpointed"
            self.store.add_event(jid, "checkpoint", f"checkpoint {cp['id']} taken for preemption")
            self.upstream.delete_sandbox(sid)
            yield "deleted"
            self.store.set_sandboxes(jid, [])
            self.store.set_checkpoint(jid, cp["id"])
            self.store.set_preempted(jid)           # state PREEMPTED, queued_mono reset, count++
            self.store.add_event(jid, "state", f"PREEMPTED (checkpoint {cp['id']}, will resume by fork)")
            self.state["preemptions"] += 1
        except Exception as e:
            # could not checkpoint: the victim keeps running, the head keeps waiting
            self.store.set_state(jid, "RUNNING")
            self.store.add_event(jid, "error", f"preemption aborted: {e}")
            if "Checkpoint" in type(e).__name__:
                self.state["checkpoint_down"] = True

    # -- monitor ---------------------------------------------------------------------

    def monitor_tick(self) -> None:
        sandboxes = self.upstream.list_sandboxes()
        by_name = {sb["name"]: sb for sb in sandboxes}
        for job in self.store.by_state("ADMITTED", "RUNNING"):
            names = [self.sandbox_name(job, k) for k in range(job["count"])]
            found = [by_name[n] for n in names if n in by_name]
            if found and not job["sandbox_ids"]:
                self.store.set_sandboxes(job["id"], [sb["id"] for sb in found])
            if job["state"] == "ADMITTED":
                if not found:
                    continue
                if any(sb["state"] == "FAILED" for sb in found):
                    self.store.set_state(job["id"], "FAILED", "sandbox failed upstream")
                    self.store.add_event(job["id"], "state", "FAILED (sandbox failed)")
                    for sb in found:
                        self._safe_delete(sb["id"], job["id"])
                    continue
                if len(found) == job["count"] and all(sb["state"] == "READY" for sb in found):
                    self.store.set_state(job["id"], "RUNNING")
                    self.store.add_event(job["id"], "state", "RUNNING")
            elif job["state"] == "RUNNING":
                if job["kind"] == "service" and len(found) < job["count"]:
                    self.store.set_state(job["id"], "FAILED", "sandbox vanished while RUNNING")
                    self.store.add_event(job["id"], "error", "sandbox vanished")
                    for sb in found:
                        self._safe_delete(sb["id"], job["id"])
                    continue
                # memory sample (pressure-aware admission)
                total = 0
                for sb in found:
                    try:
                        total += self.upstream.sandbox_memory(sb["id"])
                    except Exception:
                        pass
                if total:
                    self.store.set_mem(job["id"], total)

    # -- task execution ---------------------------------------------------------------

    def run_task(self, jid: str) -> None:
        job = self.store.get(jid)
        if not job or job["state"] != "RUNNING":
            return
        results, ok = [], True
        for sid in job["sandbox_ids"]:
            try:
                r = self.upstream.exec_sandbox(sid, job["cmd"], config.JOB_TASK_TIMEOUT)
                results.append(r)
                if r.get("exit_code") != 0:
                    ok = False
            except Exception as e:
                results.append({"error": str(e)})
                ok = False
        self.store.set_result(jid, {"results": results})
        for sid in job["sandbox_ids"]:
            self._safe_delete(sid, jid)
        self._finish(jid, "SUCCEEDED" if ok else "FAILED",
                     None if ok else "task command failed",
                     "SUCCEEDED" if ok else "FAILED (task exit != 0)")

    def _finish(self, jid: str, state: str, error: str | None, msg: str) -> None:
        job = self.store.get(jid)
        self.store.set_state(jid, state, error)
        self.store.add_event(jid, "state", msg)
        if job and job.get("deadline_at") and clock.now_wall() > job["deadline_at"]:
            self.store.set_deadline_missed(jid)
            self.state["deadline_missed"] += 1
            self.store.add_event(jid, "deadline", "finished after its deadline")

    # -- janitor ----------------------------------------------------------------------

    def janitor_tick(self) -> None:
        now = clock.now_mono()
        for job in self.store.by_state("ADMITTED", "RUNNING"):
            if job["deadline_mono"] and now >= job["deadline_mono"]:
                self.store.add_event(job["id"], "state", "max_runtime exceeded")
                for sid in job["sandbox_ids"]:
                    self._safe_delete(sid, job["id"])
                self._finish(job["id"], "EXPIRED", None, "EXPIRED")
        sandboxes = self.upstream.list_sandboxes()
        live_names = {self.sandbox_name(j, k)
                      for j in self.store.by_state("ADMITTED", "RUNNING", "CANCELLING", "QUEUED",
                                                   "PREEMPTING", "PREEMPTED")
                      for k in range(j["count"])}
        for sb in sandboxes:
            if sb["name"].startswith("sched-") and sb["name"] not in live_names and sb["state"] != "DELETED":
                log.warning("janitor: reaping orphan %s", sb["name"])
                self._safe_delete(sb["id"], "janitor")

    # -- cancel -----------------------------------------------------------------------

    def cancel_tick(self) -> None:
        for job in self.store.by_state("CANCELLING"):
            names = {self.sandbox_name(job, k) for k in range(job["count"])}
            for sb in self.upstream.list_sandboxes():
                if sb["name"] in names and sb["state"] != "DELETED":
                    try:
                        self.upstream.delete_sandbox(sb["id"])
                    except (self.upstream.Conflict, self.upstream.Down):
                        pass
            remaining = [sb for sb in self.upstream.list_sandboxes()
                         if sb["name"] in names and sb["state"] != "DELETED"]
            if not remaining:
                self._finish(job["id"], "CANCELLED", None, "CANCELLED")

    def cancel(self, jid: str) -> dict:
        job = self.store.get(jid)
        if not job:
            raise ApiError(404, f"job {jid} not found")
        st = job["state"]
        if st in config.TERMINAL:
            raise ApiError(409, f"job already terminal ({st})")
        if st == "CANCELLING":
            return job
        if st in ("QUEUED", "PREEMPTED"):
            self.store.set_state(jid, "CANCELLED")
            self.store.add_event(jid, "state", f"CANCELLED (while {st.lower()})")
            return self.store.get(jid)
        self.store.set_state(jid, "CANCELLING")
        self.store.add_event(jid, "state", "CANCELLING")
        return self.store.get(jid)

    # -- submission ---------------------------------------------------------------------

    def submit(self, req: dict, idem_key: str | None) -> tuple[str, dict]:
        if idem_key:
            existing = self.store.get_by_idem(idem_key)
            if existing:
                same = all(existing[k] == req.get(k) for k in ("tenant", "template", "kind", "cmd", "count"))
                if same:
                    return "existing", existing
                raise ApiError(409, "Idempotency-Key reused with a different job body")
        if self.store.queue_depth() >= self.max_queue:
            raise ApiError(429, f"queue full ({self.max_queue})", {"Retry-After": "10"})
        cpu_def, mem_def, _ = config.TEMPLATE_COST[req["template"]]
        jid = self.new_id()
        now_w = clock.now_wall()
        job = {
            "id": jid, "idem_key": idem_key, "name": req.get("name") or f"job-{req['template']}",
            "tenant": req["tenant"], "template": req["template"], "kind": req["kind"],
            "cmd": req.get("cmd"), "priority": req["priority"], "count": req["count"],
            "cpu_units": req.get("cpu_units") or cpu_def, "mem_units": req.get("mem_units") or mem_def,
            "state": "QUEUED", "max_runtime": req.get("max_runtime"),
            "queued_mono": clock.now_mono(),
            "deadline_at": (now_w + req["deadline_seconds"]) if req.get("deadline_seconds") else None,
            "preemptible": bool(req.get("preemptible", req["kind"] == "service")) and req["count"] == 1,
            "stateful": bool(req.get("stateful")),
        }
        self.store.create_job(job)
        self.store.add_event(jid, "state",
                             f"QUEUED (tenant={req['tenant']}, prio={req['priority']}, "
                             f"{job['cpu_units']}cpu x{job['count']}"
                             f"{', deadline ' + str(req['deadline_seconds']) + 's' if req.get('deadline_seconds') else ''}"
                             f"{', stateful+preemptible' if job['stateful'] and job['preemptible'] else ''})")
        return "created", self.store.get(jid)

    # -- crash recovery -----------------------------------------------------------------

    def reconcile(self) -> dict:
        """Re-anchor monotonic clocks; put PREEMPTING jobs back to RUNNING
        (the preemption generator died with the process - the janitor /
        monitor will re-derive truth; a checkpoint may already exist, in
        which case the next preemption just makes a new one)."""
        now_m, now_w = clock.now_mono(), clock.now_wall()
        n = 0
        for job in self.store.by_state("QUEUED", "ADMITTED", "RUNNING", "CANCELLING", "PREEMPTING", "PREEMPTED"):
            age_wall = now_w - job["created_at"]
            self.store.reset_queued_mono(job["id"], now_m - age_wall)
            if job["deadline_wall"]:
                self.store.set_deadline(job["id"], now_m + max(0, job["deadline_wall"] - now_w), job["deadline_wall"])
            if job["state"] == "PREEMPTING":
                self.store.set_state(job["id"], "RUNNING")
                self.store.add_event(job["id"], "reconcile", "preemption interrupted by restart; back to RUNNING")
            elif job["state"] == "ADMITTED":
                # the provision thread died with the process: nobody will
                # create (or finish creating) this job's sandboxes. Re-queue
                # it; sandbox-api's idempotent create adopts anything that
                # already exists under the same name. (Found by the
                # simulator: v1 left such jobs ADMITTED forever.)
                self.store.set_sandboxes(job["id"], [])
                self.store.set_state(job["id"], "PREEMPTED" if job.get("checkpoint_id") else "QUEUED")
                self.store.add_event(job["id"], "reconcile", "provisioning interrupted by restart; re-queued")
            self.store.add_event(job["id"], "reconcile", f"re-anchored after scheduler restart (state kept: {job['state']})")
            n += 1
        return {"reanchored": n}
