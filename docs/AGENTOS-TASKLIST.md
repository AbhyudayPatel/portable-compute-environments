# AgentOS - Master Tasklist

> **North star:** an AI Agent Execution Sandbox platform  - 
> `TASK API -> ENVIRONMENT IR -> SCHEDULER -> Workers (microVMs) -> Sandboxes
> (app/DB/browser/agent) -> Observability -> Event Store -> Agent Dataset ->
> Train/Evaluate`.

## How this tasklist works

- **One task = one standalone top-level project folder.** No shared
  multi-implementation folders. Each project has its own
  `docker-compose.yml`, `scripts/` (start/stop/verify), `.env` port config,
  `README.md`, and `docs/` (ARCHITECTURE / IMPLEMENTATION / EDGE-CASES).
- **Every project runs on its own default ports** (see the port map) so
  several can be up at once; all ports overridable via `.env`.
- **Status legend:** `[ ]` planned . `[~]` scaffolded . `[x]` **VERIFIED**
  (executed + observed). Anything reasoned but not executed is marked
  **EXPECTED** in that project's docs.
- Each task lists **Edge cases** (must be handled in code, with a test or
  documented behavior) and **Advanced applications** (the "hard mode"
  features that make it portfolio-grade).

## Already built (phases 3-5 groundwork)

| Folder | Phase | Status |
|---|---|---|
| `docker-nested-lab/` | 3 DinD, 4 nested networking | VERIFIED |
| `browser-dev-environment/` | 5 Compose sandbox, 9 browser IDE | VERIFIED |
| `browser-linux-desktop/` | 5 Compose sandbox, 9 browser desktop | VERIFIED |
| `local-vscode-devcontainer/` | 5 Compose sandbox (local IDE) | VERIFIED |

## Port map (defaults, all `.env`-overridable)

| Project | API/UI | Sandbox app range |
|---|---|---|
| sandbox-api | 9000 | 9200-9209 |
| sandbox-scheduler | 9010 | 9210-9219 |
| sandbox-quotas | 9020 | 9220-9229 |
| sandbox-gateway | 9030 | 9230-9239 |
| sandbox-checkpoint | 9040 | 9240-9249 |
| agent-runtime | 9050 | 9250-9259 |
| trajectory-recorder | 9060 | 9260-9269 |
| agent-bench | 9070 | 9270-9279 |
| chaos-lab | 9080 | 9280-9289 |
| gpu-sandbox | 9090 | 9290-9299 |
| microvm-lab | 9100 | 9300-9309 |
| distributed-scheduler | 9110 | 9310-9319 |
| env-compiler | 9120 | 9320-9329 |
| github-to-live | 9130 | 9330-9339 |
| self-healing | 9140 | 9340-9349 |
| agentos | 9150 | 9350-9359 |

---

## T01 - `sandbox-api/` - Sandbox API (Phase 6)  [x]

**Goal.** A REST control plane that creates, lists, inspects, execs into, and
destroys **sandboxes inside a dedicated DinD engine** - the first real
"platform" component. Everything later (scheduler, agents, chaos) calls this
API.

**Build.**
- `dind` service: `docker:27-dind`, TLS off on the internal network only
  (2375 never published to the host), named volume over `/var/lib/docker`.
- `api` service: FastAPI + docker SDK (`DOCKER_HOST=tcp://dind:2375`),
  SQLite metadata store in a volume, reconciliation on boot.
- Sandbox = a group of containers on a per-sandbox inner network, labeled
  `sandbox.id/name/template/managed`. Templates: `web` (busybox httpd),
  `coreapp` (Postgres + FastAPI backend + nginx frontend - images built
  **inside** dind by uploading a tar build context through the Docker API,
  cached after first build), `blank`.
- Port allocator: pool 9200-9209, identity-mapped (inner port P -> dind
  publishes P -> compose publishes P -> `localhost:P`). Lowest-free, stored in
  SQLite, freed on delete.
- Endpoints: `POST/GET/DELETE /sandboxes`, `GET /sandboxes/{id}`,
  `POST /sandboxes/{id}/exec`, `GET /sandboxes/{id}/events`,
  `POST /sandboxes/{id}/stop|start`, `GET /healthz`, `GET /templates`.
- Background **TTL reaper** (per-sandbox `ttl_seconds`, default off).
- Sandbox state machine: `CREATING -> READY -> FAILED | STOPPED -> READY ->
  DELETED`, with an event log per sandbox.

**Edge cases (all handled + tested).**
1. **Idempotent create** - same `name` + same spec returns 200 with the
   existing sandbox; same name + different spec -> 409.
2. **Port exhaustion** - pool full -> 429 with `Retry-After`, no half-created
   containers (rollback on any failure after allocation).
3. **Partial creation failure** - if container N of a template fails, all
   containers/network already created for that sandbox are torn down and the
   port freed; sandbox row marked `FAILED` with the error in its event log.
4. **API crash / restart recovery** - on boot the API lists inner containers
   labeled `sandbox.managed=true` and reconciles DB <-> engine (adopt orphans
   into DB, purge DB rows whose containers vanished).
5. **Foreign-container safety** - the API only ever touches containers with
   its own management label; a container a user created manually in dind is
   invisible to deletes and reconciliation.
6. **Exec edge cases** - exec into STOPPED/DELETED sandbox -> 409; exec
   timeout (default 30s, bounded) returns `timed_out: true` with partial
   output; output truncated at 64 KiB with `truncated: true`; non-zero exit
   codes returned, not raised as 500.
7. **Delete idempotency** - deleting a missing sandbox -> 404 (not 500);
   deleting twice in a row -> second is 404; delete while CREATING -> refused
   with 409 (no torn half-state).
8. **DinD unavailable** - dockerd down -> endpoints return 503 with
   `Retry-After`, never hang; `/healthz` reports `dind: down`.
9. **TTL race** - TTL reaper and a manual DELETE racing: both paths are
   idempotent (scoped lock per sandbox id); a sandbox whose TTL fires while
   a long exec runs is stopped, not hard-killed mid-write.
10. **Capacity cap** - `MAX_SANDBOXES` (default 8) -> 429; counter is derived
    from the engine, not just the DB (survives DB wipes).
11. **Name validation** - DNS-safe names only (`^[a-z0-9][a-z0-9-]{1,40}$`),
    400 otherwise; template must exist -> 400 with the valid list.
12. **Event log integrity** - every state transition appended with a
    monotonic per-sandbox sequence number; `GET .../events` returns them in
    order even after API restart.

**Advanced applications.**
- **Image build inside DinD via API-uploaded tar context** (no registry, no
  bind mounts into dind) with build caching - the pattern every later
  project uses to get CoreApp images into sandbox engines.
- **TTL reaper** = the seed of the scheduler's janitor (T02).
- **Reconciliation loop** = the seed of the distributed scheduler's
  worker-state repair (T12).
- **Per-sandbox event log** = the seed of the trajectory/event store (T07).
- Idempotency + state machine = the contract every later control plane
  reuses verbatim.

**Verify.** `scripts/verify.sh` - full battery: create->poll READY->curl the
sandbox's web page through `localhost:<allocated port>`->exec->idempotency->
409 conflict->exhaust pool->429->TTL reap->delete idempotency->503 path.
Status: **VERIFIED**.

---

## T02 - `sandbox-scheduler/` - Concurrent Sandbox Scheduler (Phase 7)  [x]

**Goal.** A queue + placement engine on top of the T01 API: submit N sandbox
requests at once; the scheduler admits, queues, places, and reaps them under
capacity constraints.

**Build.**
- `scheduler` service (FastAPI + worker coroutines) wrapping a sandbox-api
  client; job model: `QUEUED -> ADMITTED -> RUNNING -> SUCCEEDED/FAILED/
  EXPIRED/CANCELLED`.
- Admission control: capacity = f(running sandboxes, reserved CPU/mem
  units); weighted fair queueing across **tenants** (header `X-Tenant`).
- Priority levels with **aging** (queued jobs gain priority over time to
  prevent starvation).
- Janitor: TTLs, max-runtime kills, orphan reaping (sandboxes in the engine
  with no job row).
- Endpoints: `POST /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel`,
  `GET /queue`, `GET /metrics` (wait-time p50/p99, throughput, admit rate).

**Edge cases.**
1. Thundering herd - 50 simultaneous POSTs: exactly `capacity` admitted,
   rest fairly queued (no oversubscribe race - single admission lock).
2. Priority inversion - low-priority long job blocks high-priority: aging
   guarantees bounded wait; test asserts max wait of aged job.
3. Starvation - continuous high-priority stream must still let an aged
   low-priority job in within `AGING_THRESHOLD`.
4. Crash mid-admission - scheduler dies after sandbox created but before DB
   commit: on boot, reconcile (adopt sandbox to job or destroy).
5. Duplicate submission - idempotency-key header; retries return the same
   job id.
6. Cancel races - cancel a job that is being admitted *right now*; cancel a
   RUNNING job (sandbox destroyed); cancel a terminal job -> 409.
7. Downstream outage - sandbox-api 503: scheduler backs off with jitter,
   jobs stay `QUEUED`, no hot loop; `GET /metrics` shows `upstream_down`.
8. Clock skew / TTL - TTL uses monotonic deadline captured at admission,
   not wall clock comparisons at reap time.
9. Queue bound - `MAX_QUEUE` exceeded -> 429 `Retry-After` (never unbounded
   memory growth).
10. Head-of-line blocking - a job needing 4 CPU units must not block jobs
    needing 1 when 2 are free (skip-and-fill placement).

**Advanced applications.**
- Weighted fair queueing per tenant + per-tenant caps (noisy-tenant
  protection).
- Gang admission option (`all_or_nothing: true`) for multi-sandbox jobs  - 
  the seed of multi-node agent environments.
- Placement constraints scaffold (`labels`, `requires_gpu: false`) so T11/
  T12 slot in without a rewrite.
- Prometheus `/metrics` + a Grafana-ready dashboard JSON.

**Verify.** `scripts/verify.sh` - herd test, aging test, cancel race loop
(100 iterations, assert no leaked sandboxes), crash-recovery (kill -9
scheduler mid-test, restart, assert zero orphans).

---

## T03 - `sandbox-quotas/` - Resource Isolation & Quotas (Phase 8)  [ ]

**Goal.** Hard per-sandbox CPU/memory/disk/PID/network quotas enforced with
cgroups + Docker limits, with a "quota violator" lab that proves enforcement.

**Build.**
- Quota profiles (`tiny/small/medium`: cpu, mem, pids, disk, egress Mbps)
  applied at sandbox creation through the T01 API (extended with
  `quota_profile`).
- `violator` image: scripts that fork-bomb, malloc-balloon, disk-fill,
  and flood-UDP on demand.
- Enforcement surfacing: sandbox events show `OOM_KILLED`,
  `PIDS_EXCEEDED`, `DISK_QUOTA_HIT` as first-class states.
- Disk quota via per-sandbox sized loopback volume mounted into the
  sandbox container (XFS pquota where available; document the
  Docker-Desktop/WSL2 caveat honestly).

**Edge cases.**
1. OOM kill must kill the **violator container only**, never dockerd or a
   sibling sandbox (cgroup scope verification test).
2. Fork bomb - `pids.limit` hit -> sandbox marked degraded, API stays up,
   neighbors unaffected (latency assertion on a neighbor's exec).
3. Disk fill - writes past quota fail with ENOSPC **inside** the sandbox;
   host `df` unaffected; sandbox teardown frees the loopback device
   (leak check in verify).
4. CPU throttling accounting - a 0.5-CPU sandbox under load must show
   throttled periods in `cpu.stat`; neighbor keeps >=90% of its share.
5. Memory `memory.high` vs `memory.max` - throttle vs kill semantics both
   demonstrated and asserted.
6. Quota change on a RUNNING sandbox - `POST /sandboxes/{id}/quota` does a
   live cgroup update; rejected values (mem < 16 MiB) -> 400.
7. Network egress cap - `tc` token-bucket on the sandbox veth; cap survives
   sandbox container restart (re-apply hook) - **EXPECTED** on WSL2 if tc
   unavailable: degrade to "unenforced" state, never silent-fail.
8. PID exhaustion vs exec - when pids.max is hit, the API's own exec must
   still work (exec pre-created helper or elevated reservation) or fail
   with a clean 409, never hang.
9. Swap disabled / swap double-count edge (mem+swap limit semantics).
10. Quota profile deletion while in use -> 409 with the using sandboxes.

**Advanced applications.**
- PSI (Pressure Stall Information) per sandbox exported to `/metrics`.
- "Noisy neighbor" detector: flags sandboxes whose throttle ratio > X%.
- Cost model: quota units -> \$-per-hour estimate printed in API responses
  (seed of billing in T16).

**Verify.** Each violation script run in its own sandbox; assertions on
sandbox state, neighbor health, host health, and clean teardown.

---

## T04 - `sandbox-gateway/` - Browser + Terminal Gateway (Phase 9)  [ ]

**Goal.** One public URL per sandbox: reverse proxy + in-browser terminal +
file upload/download - the "window into the sandbox".

**Build.**
- `gateway` service: dynamic nginx (OpenResty/lua or nginx + conf generated
  from the T01 API watch stream) mapping `localhost:9030/s/<id>/` -> sandbox
  app port.
- Web terminal: `ttyd` sidecar injected per sandbox, proxied at
  `/s/<id>/term/`, xterm.js frontend page served by the gateway.
- `GET /s/<id>/files?path=...` / `PUT` - file API via exec-based tar
  streaming.
- Per-sandbox bearer token minted at creation; gateway enforces it
  (cookie + header), tokens revocable.

**Edge cases.**
1. WebSocket through the proxy - upgrade headers, 60s idle-timeout bumped,
   ping/pong keepalive so a terminal isn't killed mid-session.
2. Apps with absolute paths - sandbox web apps that emit `/style.css` break
   under a path prefix: sub_filter rewrite + documented contract "sandbox
   apps must be path-relative"; demo broken app -> fixed app.
3. Sandbox restart mid-session - terminal WS dies cleanly, UI auto-reconnects
   with backoff, proxy returns 502-with-Retry-After not a hung connection.
4. Token theft surface - token never in URL query (logs!); cookie
   `HttpOnly; SameSite=Strict`; per-sandbox path-scoped.
5. Path traversal - `GET /files?path=../../etc/shadow` -> 400; tar-stream
   escapes rejected server-side.
6. Two sandboxes, same cookie name - cookie path scoping test.
7. Sandbox deleted while proxied - open WS closed with 4404 code; HTTP ->
   404 page that lists live sandboxes.
8. Large file download - 500 MiB streamed (no buffering) while gateway
   memory stays flat (assert via /metrics).
9. Concurrent terminals - 5 WS sessions to one sandbox, each an independent
   shell (no cross-talk test).
10. Gateway restart - route table rebuilt from the API (not from local
    cache) before accepting traffic (readiness gate).

**Advanced applications.**
- Shareable read-only live links (`/s/<id>/view?token=...`) - the seed of
  "human watches the agent work" in T06/T16.
- Session recording toggle: terminal I/O captured as an asciinema-style
  event stream into the T07 event store.
- Idle-sandbox auto-sleep hint header (`X-Sandbox-Idle`) consumed later by
  T05 checkpointing.

**Verify.** Scripted WS client + HTTP battery incl. traversal, reconnect,
big-file, and teardown cases.

---

## T05 - `sandbox-checkpoint/` - Stateful Sandboxes: Checkpoint & Rollback  [ ]

**Goal.** Freeze a sandbox's full state (filesystem **and** DB) and roll it
back - "Git for execution state". The single most valuable AgentOS feature.

**Build.**
- Checkpoint = `docker commit` of sandbox containers + tar snapshots of
  named volumes + `pg_dump` for CoreApp DB + a manifest JSON (checkpoint id,
  parent id, timestamp, sandbox spec, sizes). Stored content-addressed in a
  `checkpoint-store` volume.
- Lineage: checkpoints form a tree; `POST /sandboxes/{id}/checkpoints`,
  `POST /checkpoints/{cid}/restore`, `POST /checkpoints/{cid}/fork`
  (new sandbox from checkpoint), `DELETE` with GC of unreferenced layers.
- Quiesce protocol: app-aware hook (`POST /api/freeze` on CoreApp ->
  Postgres `CHECKPOINT` + `fsync`) -> `docker pause` -> snapshot -> unpause.

**Edge cases.**
1. Checkpoint during active writes - quiesce ordering test: restore must
   show the DB transactionally consistent (no partial rows).
2. Restore onto different port - state must be port-agnostic (no absolute
   URLs baked into DB content); test asserts restored app works on a new
   allocated port.
3. Checkpoint of a crashed container - commit works on stopped containers;
   manifest records `was_running` so restore re-starts or not accordingly.
4. Corrupt/incomplete snapshot (API killed mid-tar) - manifest written
   last, fsynced; store scan on boot marks dangling checkpoint dirs
   `CORRUPT` and excludes them from restore.
5. Restore version skew - image tag in manifest missing in engine -> 409
   with remediation, never a silent re-pull of `latest`.
6. Fork while parent mutating - fork takes the checkpoint, not live state
   (document + test the snapshot isolation).
7. Storage growth - dedup by content hash; GC must never delete a layer
   referenced by any manifest (reference counting, tested with concurrent
   delete+fork).
8. pg_dump vs volume-tar divergence - DB restored from dump, files from
   tar; test where a file and a row written together both reappear.
9. Rollback while a client is connected through T04 gateway - connections
   drained, then restore; clients get 409-Retry page.
10. Restore-failure rollback - if restored sandbox fails health check,
    auto-restore the pre-restore checkpoint (checkpoint-of-last-known-good).

**Advanced applications.**
- **Branch-per-experiment**: agent tries fix A on fork 1, fix B on fork 2  - 
  the core of parallel agent search (feeds T08).
- Time-travel debugging: checkpoint every agent step (hooks into T07).
- Incremental checkpoints (rsync-style volume diffs) with size/latency
  tradeoff report.
- Checkpoint TTL + LRU eviction under a store-size budget.

**Verify.** Write data -> checkpoint -> mutate/destroy -> restore -> byte-exact
assertions; fork isolation; corruption recovery; concurrent GC race.

---

## T06 - `agent-runtime/` - Agent Execution (Phase 10)  [ ]

**Goal.** Run an LLM-style agent **inside** a sandbox against CoreApp: a
tool-using loop (exec/read/write/git) driven by a pluggable provider  - 
including a deterministic **mock provider** so everything is testable
offline.

**Build.**
- `runtime` service: submits jobs `{task, repo, tools[], budget}`; spawns a
  sandbox via T01, injects an **agent-sidecar** that exposes tool RPC
  (exec/read/write/list/git) over a local socket.
- Provider interface: `mock` (scripted response sequences - deterministic,
  seeded), `openai-compatible` (env-configured endpoint; marked EXPECTED
  unless a key is present).
- Budgets: max steps, max tokens, wall-clock deadline, cost cap; every
  breach -> graceful `BUDGET_EXCEEDED` terminal state with partial trace.
- Human-in-the-loop gate: tool calls matching a danger list
  (`rm -rf /`, `git push --force`, ...) require `POST /jobs/{id}/approve`.

**Edge cases.**
1. Infinite loop - step budget enforced; loop detector (identical tool call
   3x) -> early stop with `LOOP_DETECTED`.
2. Malformed tool calls - bad JSON / unknown tool -> corrective feedback
   message fed back to provider, counted against budget; after 5 malformed
   -> `FAILED`.
3. Agent kills its own sidecar (`kill 1`) - supervisor restarts sidecar,
   job marked degraded, event logged; after 3 restarts -> FAILED.
4. Agent fills disk with logs - sidecar log capped + rotated (ties to T03
   quotas); breach -> truncated marker in trace, not data loss.
5. Provider 429/500 - exponential backoff + jitter, retry budget distinct
   from step budget; provider hard-down -> `PROVIDER_UNAVAILABLE`, job
   resumable.
6. Path jail escape - `read ../../etc/shadow`, symlink inside workspace ->
   outside: canonical-path enforcement, denied calls logged as security
   events.
7. Concurrent agents on one repo - isolated git worktrees + merge step with
   conflict reporting (seed of multi-agent collaboration).
8. Non-determinism - same task + mock provider + seed -> byte-identical
   trace (replay-compat for T07).
9. Timeout mid-tool-call - exec timeout returns partial output to the
   agent as an observation (agent sees reality, not a crash).
10. Budget accounting accuracy - token/cost accounting within 5% of
    provider-reported usage; wall-clock uses monotonic clock.

**Advanced applications.**
- Tool schema hot-reload per task (file-based tool registry) - seed of
  agent tool ecosystems.
- Structured "agent memory" volume that survives sandbox restarts
  (ties to T05 checkpoints).
- Cost dashboard: per-task \$ and token histograms (mock-priced).

**Verify.** Mock-provider task suite: loop-break, jail-escape, approval-gate,
sidecar-kill, determinism (two runs, diff traces -> empty).

---

## T07 - `trajectory-recorder/` - Event Store, Trajectories & Replay (Phase 11)  [ ]

**Goal.** Every agent/sandbox/API action becomes an ordered, hash-chained
event in an append-only store - powering replay, time-travel, debugging, and
dataset export.

**Build.**
- `event-store` service: Postgres (JSONB) + append API, per-stream sequence
  numbers, hash chain (`prev_hash` -> tamper-evidence), monotonic+wall dual
  timestamps.
- Recorders: T01 sandbox lifecycle events, T06 tool calls (args + redacted
  results), T04 terminal I/O frames, container state transitions.
- **Replay engine**: given a trace, re-execute it in a fresh sandbox with
  recorded provider responses (mock-from-trace); supports step-through
  (`GET /replay/{id}/step?n=`) and divergence reporting.
- Dataset export: `GET /datasets/export?format=jsonl|parquet` with redaction
  report.

**Edge cases.**
1. Out-of-order delivery - concurrent writers: per-stream sequence enforced
   server-side; gaps rejected with 409 + expected seq (client resends).
2. Crash mid-append - WAL + fsync policy; on boot, tail-verify hash chain,
   truncate at first bad link, log `CHAIN_REPAIRED`.
3. Huge outputs - >64 KiB payloads chunked into blob table, event holds
   pointer + hash; retrieval reassembles and re-verifies.
4. Secret leakage - allowlist + regex scanner redaction **before** write;
   test with planted fake keys; re-redaction creates a new redaction event
   (immutable history).
5. Clock skew - events ordered by (stream, seq), never by wall clock; skew
   detector metric exported.
6. Replay non-determinism - replay asserts recorded container states match;
   divergence (e.g., timestamp-dependent app) reported with field-level diff.
7. Retention vs tamper-evidence - GDPR-style delete = crypto-shredding
   (per-stream data key destroyed), chain intact, payload tombstoned.
8. Backpressure - recorder faster than store: bounded queues, drop policy
   `oldest-debug-first` (never drop security/budget events), drop counter
   metric.
9. Duplicate delivery - idempotency key per event; retries deduped.
10. Export consistency - export is a snapshot at seq N; concurrent appends
    don't leak into the file (manifest records high-water mark).

**Advanced applications.**
- Trace diffing (`/diff?a=..&b=..`) - compare two agent runs field by field.
- "Time-travel UI": scrub a timeline, see sandbox state + terminal frame +
  tool call at that instant (consumes T05 checkpoints + T04 recordings).
- Training-ready JSONL schema (OpenAI fine-tune / HF datasets compatible).

**Verify.** Hash-chain tamper test, reorder storm test, crash-recovery
(kill -9 mid-write), redaction test, replay determinism on T06 mock traces.

---

## T08 - `agent-bench/` - Agent Evaluation Benchmark (Phase 12)  [ ]

**Goal.** A rigorous benchmark harness: task suite -> K runs x M agents ->
scores with statistics - the "SWE-bench of this platform".

**Build.**
- Task spec YAML: repo setup, task prompt, grading command(s), rubric
  weights (tests-pass 60%, diff-quality 20%, time 10%, cost 10%).
- Runner: drives T06 via T02 scheduler, one fresh sandbox per trial, seeded
  mock provider + optional real provider; results DB + HTML report.
- Scoring: tests pass/fail via isolated grader container (agent output
  mounted read-only), diff metrics, budget consumption, normalized scores.
- Stats: multi-seed runs, bootstrap confidence intervals, flake detection
  (same code, different verdict -> quarantined).

**Edge cases.**
1. Reward hacking - agent edits the tests: test files hashed before the run
   and restored/hashed after; mismatch -> score 0 + `TAMPERED`.
2. Flaky graders - grading run 3x on identical output; non-unanimous ->
   `FLAKY`, excluded from stats, reported separately.
3. Environment leakage - trial N must never see trial N-1 state: fresh
   sandbox + fresh DB per trial (assert via canary row).
4. Network cheating - offline mode with pre-cached deps; unexpected egress
   attempt logged and scored as violation.
5. Partial credit - rubric engine, never binary-only; timeouts ->
   partial score, not crash.
6. Machine variance - CPU-second accounting (cgroup cpu.stat), not wall
   time, for the time score.
7. Non-deterministic agents - >=5 seeds per task; report mean +/- CI, never a
   single run.
8. Grader escape - grader runs with no network, read-only mount, its own
   quota (agent output can't fork-bomb the platform).
9. Benchmark DoS - task suite with a pathological task (100 GB output):
   output caps + per-trial kill.
10. Version skew - task spec, agent version, provider, and image digests
    all recorded in results for reproducibility.

**Advanced applications.**
- CI regression gate: `bench run --gate baseline.json` fails the pipeline
  if mean score drops beyond CI overlap.
- Leaderboard with significance testing (paired bootstrap).
- Failure taxonomy auto-classifier (loop / jail-attempt / budget-blowout /
  wrong-fix) from T07 traces.

**Verify.** Known-good agent (scripted fix) scores ~1.0; known-bad agent
scores ~0; tamper test scores 0 with `TAMPERED`; flake injection detected.

---

## T09 - `chaos-lab/` - Failure Injection & Chaos Engineering (Phase 13)  [ ]

**Goal.** Controlled failure injection against running sandboxes - network,
disk, CPU, clock, DNS, DB - with blast-radius guardrails and resilience
scoring for agents.

**Build.**
- `chaosd` service with experiment API: YAML spec
  (`steady-state hypothesis -> method -> rollback`), injectors: tc/netem
  latency+loss, iptables partition, SIGSTOP freeze, disk fill, DB connection
  kill, clock skew (faketime sidecar), DNS blackhole.
- Guardrails: label double-check, dry-run mode, dead-man auto-rollback
  (chaosd dies -> all injections reverted), max blast radius = N sandboxes.
- Resilience score: does the sandbox/agent detect, degrade, recover?
  (uses T07 events + T08 scoring).

**Edge cases.**
1. Wrong-target injection - target resolved by sandbox id **and** label
   fingerprint; dry-run prints the exact tc/iptables commands.
2. Idempotent rollback - applying rollback twice is a no-op; rollback of a
   never-applied injection -> 404.
3. Chaos during checkpoint (T05) - mutually exclusive locks; experiment
   waits or 409s, never snapshots a corrupted-by-design state silently.
4. Partition heal -> split brain - CoreApp DB partitioned from backend, then
   healed: hypothesis asserts no divergent writes (single-writer test).
5. Disk-fill on shared volume - injector targets the sandbox's loopback
   disk only (T03), host fs never touched; assertion in verify.
6. Freezing PID 1 vs sidecar - both supported; freeze of dockerd's child
   must not wedge the engine (verified).
7. Clock skew + TLS - faketime past cert validity: app behavior documented,
   hypothesis "TLS fails closed" asserted.
8. chaosd crash mid-experiment - watchdog reverts all netem/iptables rules
   (dead-man switch via a reaper sidecar).
9. Stacked experiments - two experiments on one sandbox: allowed only if
   disjoint injector types; conflict -> 409.
10. False-positive healthchecks - chaos latency under healthcheck timeout
    must not trigger platform auto-restart loops (backoff test).

**Advanced applications.**
- Chaos + benchmark combo: "resilience benchmark" - agent must keep CoreApp
  SLO while experiments run.
- Game-day runner: randomized experiment schedule + post-mortem report
  generated from T07 events.
- Fault library versioning (injector digests recorded in results).

**Verify.** Each injector: apply -> assert effect (measurable) -> rollback ->
assert baseline; dead-man test (kill -9 chaosd); guardrail tests.

---

## T10 - `gpu-sandbox/` - GPU Sandboxes (Phase 14)  [ ]

**Goal.** Sandboxes that request and share GPU slices, with an agent
fine-tuning workload demo. (Windows/Docker-Desktop: design + CPU fallback
VERIFIED; real-GPU paths EXPECTED on a Linux worker, scripts provided.)

**Build.**
- `gpu-worker` profile: nvidia-container-toolkit install script (Linux),
  `requires_gpu` placement flag through T02, CDI/`--gpus` plumbing in T01.
- Sharing modes: exclusive, time-slice, MPS; per-sandbox GPU-mem cap via
  MPS or vGPU where available.
- Demo: tiny LoRA fine-tune inside a sandbox with checkpointing to T05
  store; `nvidia-smi` telemetry into T07 events.

**Edge cases.**
1. GPU OOM containment - sandbox CUDA OOM must not corrupt neighbors
   (MPS mem cap test).
2. Driver/toolkit mismatch - preflight check reports `GPU_UNAVAILABLE`,
   scheduler marks node non-GPU, jobs queue (not fail).
3. Exclusive-lock fairness - GPU exclusive jobs can't starve forever
   (aging from T02).
4. Multi-GPU placement - NVLink-topology-aware spreading vs packing
   strategy flag.
5. CUDA context leak on kill - after sandbox destroy, `nvidia-smi` shows 0
   procs (leak reaper otherwise).
6. Host info leakage - sandbox sees only its GPU slice, not host processes.
7. ECC error / XID - telemetry maps XID to sandbox, auto-drains node.
8. CPU-only fallback - `requires_gpu: false` variant of the demo runs
   everywhere (CI-safe).
9. Mixed CPU/GPU herd - scheduler packing keeps GPU jobs off CPU-only
   workers.
10. Cold-start latency - model-load time measured; warm-pool option keeps
    one sandbox pre-loaded per model.

**Advanced applications.**
- GPU benchmark track in T08 (train-to-target-loss with time/$ score).
- Trajectory dataset for ML-interp pipelines (T07 export).
- Spot-instance cost model vs on-prem comparison report.

**Verify.** CPU-fallback demo VERIFIED; GPU assertions scripted + EXPECTED
with a Linux runbook.

---

## T11 - `microvm-lab/` - microVM Isolation (Phase 15)  [ ]

**Goal.** Replace containers-as-isolation with Firecracker microVMs behind
the same sandbox API - the production-grade isolation boundary.

**Build.**
- Linux worker runbook + scripts (Firecracker + jailer + TAP networking +
  vsock agent); rootfs builder (CoreApp rootfs from Dockerfile via
  `docker export`).
- `EnvRunner` interface in T01 with two backends: `docker` (VERIFIED on
  this machine) and `firecracker` (EXPECTED here, VERIFIED on Linux CI).
- Snapshot load/save (<1s resume target) mapped onto the T05 checkpoint
  API.

**Edge cases.**
1. TAP setup idempotency - re-running net setup never duplicates rules.
2. IPAM collision across microVMs - allocator + arping probe before assign.
3. Kernel/rootfs mismatch - manifest pairs them; mismatch -> 409.
4. Snapshot-restore networking - stale ARP/conntrack: gratuitous ARP on
   resume; test asserts connectivity within 2s.
5. Jailer seccomp - escape attempt from inside microVM hits jailer, logged.
6. Clock drift on resume - chrony/clocksource note; drift metric.
7. Balloon edge - balloon deflate under host pressure, guest OOM behavior
   asserted.
8. Host key rotation - vsock re-handshake, no silent MITM window.
9. Partial boot failure - kernel panic captured to serial log, surfaced in
   sandbox events.
10. Density limits - N microVMs vs memory overhead report (measured).

**Advanced applications.**
- Warm snapshot pool: pre-booted microVM fleet, restore-to-ready latency
  histogram.
- A/B study: docker vs firecracker - cold start, exec latency, memory
  overhead, escape surface (written report).
- Copy-on-write rootfs (devmapper thin) for instant forks (with T05).

**Verify.** Docker backend VERIFIED here; Firecracker suite scripted +
EXPECTED with Linux runbook (nested-virt check script included).

---

## T12 - `distributed-scheduler/` - Distributed Workers (Phase 16)  [ ]

**Goal.** Multiple sandbox workers (machines/VMs) under one control plane:
placement, heartbeats, leases, drain, and cross-worker migration.

**Build.**
- `control-plane` (Postgres-backed) + `worker-agent` (runs beside each
  sandbox-api/dind) registering with capabilities (cpu/mem/gpu/labels).
- Placement policies: least-loaded, binpack, label-affinity; pluggable.
- Leases + fencing tokens on every worker operation; leader election for
  the scheduler via Postgres advisory lock.
- Drain mode + **migration**: T05 checkpoint on worker A -> transfer ->
  restore on worker B.

**Edge cases.**
1. Split brain - two schedulers think they're leader: fencing token check
   on every worker call; stale-leader writes rejected (tested with a
   paused-leader partition).
2. Worker flapping - heartbeat hysteresis (miss 3 -> suspect, 5 -> dead),
   no placement thrash.
3. Heartbeat delay != death - long GC pause simulation: jobs not
   rescheduled twice (idempotent re-placement: adopt-or-recreate).
4. Migration losing in-flight exec - exec sessions drained or explicitly
   failed with `MIGRATING`; never silently duplicated.
5. Clock skew across workers - all deadlines in monotonic-relative terms
   exchanged at RPC time.
6. Partial migration - restore fails on B: automatic rollback to A
   (checkpoint untouched until success ack).
7. Control-plane DB down - workers keep running current sandboxes,
   refuse new placements, degrade mode metric.
8. Version skew - worker/agent API version negotiation; incompatible ->
   worker quarantined.
9. Ghost sandboxes - worker dies and restarts: boot reconciliation between
   engine labels and control-plane rows (adopt/destroy, like T01 but
   cluster-wide).
10. Network partition of a worker - sandboxes keep running locally;
    control plane marks `UNKNOWN`, never double-runs the same sandbox id.

**Advanced applications.**
- Cluster simulator mode: N fake workers, replay a schedule trace, compare
  policies (utilization, migration count, SLO misses).
- Global quota enforcement across workers (tenant budgets from T03).
- Locality-aware placement for checkpoint storage (migrate to the worker
  holding the checkpoint).

**Verify.** Two workers as two compose stacks on this machine; kill-worker
failover, split-brain injection, full migration round-trip with CoreApp
data intact.

---

## T13 - `env-compiler/` - AI-Generated Environments (Environment IR)  [ ]

**Goal.** The "environment compiler": a canonical **IR** describing a
sandbox (services, resources, security, ports, healthchecks, seeds, chaos
hooks) that compiles to compose/Dockerfile/Firecracker manifests - plus an
LLM-assisted generator that infers an IR from a repo.

**Build.**
- IR JSON Schema (versioned, e.g. `ir/v1`) + validator with semantic
  checks (port conflicts, dependency cycles, impossible resource asks).
- Backends: `ir -> docker-compose`, `ir -> sandbox-api calls` (VERIFIED);
  `ir -> k8s`/`ir -> firecracker` stubs (EXPECTED).
- Repo analyzer: detects stack (package.json/requirements.txt/Dockerfile)
  -> draft IR; LLM (mock-provider capable) fills gaps; strict-schema retry
  loop with error feedback.
- `envc compile`, `envc diff`, `envc validate`, `envc heal` (reconcile
  drift between IR and live sandbox).

**Edge cases.**
1. Impossible IR - requests > host capacity -> compile error with a concrete
   suggestion ("reduce db.mem to <= 3.2 GiB").
2. Cyclic depends_on - cycle detected, reported with the cycle path.
3. Port conflicts - static analysis across services incl. ranges.
4. Secrets in IR - schema forbids inline secrets (reference indirection
   only); validator rejects, linter warns.
5. LLM hallucination - unknown service/image names: strict-schema +
   registry-existence check + bounded retry (3) with validator errors fed
   back; failure -> deterministic template fallback, never silent garbage.
6. Non-deterministic generation - canonicalization (sorted keys, normalized
   defaults) so same repo -> byte-identical IR.
7. IR version migration - `ir/v1 -> v2` upgrader with lossy-field report.
8. Unknown package/distro - analyzer confidence score; below threshold ->
   human-review gate.
9. Heal loops - drift that can't converge in 3 passes -> `UNHEALABLE` with
   the residual diff.
10. Compose feature gaps - IR features unsupported by a backend -> explicit
    `UNSUPPORTED(backend, feature)` compile error, not silent drop.

**Advanced applications.**
- Environment diffing + cost estimate per IR (quota units -> $, from T03).
- "Compile once, run anywhere": same IR to docker here, firecracker on
  Linux, k8s later - with a compatibility matrix.
- Chaos hooks compiled into T09 experiment stubs attached to the sandbox.

**Verify.** Golden-file tests (repo -> expected IR -> expected compose),
validator battery (each bad-IR case), mock-LLM determinism, heal drift
injection.

---

## T14 - `github-to-live/` - GitHub -> Live App Platform  [ ]

**Goal.** Push to deploy: webhook -> build in an isolated sandbox -> health
check -> public preview URL. Per-PR preview environments with teardown.

**Build.**
- `receiver`: GitHub webhook endpoint (HMAC verification), job queue to
  T02, builds via T13 IR, serves previews via T04 gateway at
  `/pr/<n>/`, comments back via API (mock GitHub server included for
  offline tests).
- Build pipeline: clone (incl. submodules/LFS flags) -> IR infer -> build ->
  smoke test -> publish -> TTL.

**Edge cases.**
1. Push storms - force-push x 10 in a second: per-ref debounce + cancel
   superseded builds (only newest survives).
2. Webhook replay/forgery - HMAC + timestamp tolerance window + nonce
   cache; replay -> 403.
3. Fork PRs - read-only token, secrets never injected, egress restricted
   (no crypto-miner CI).
4. Monorepo - partial deploy (path filters); unaffected services not
   redeployed.
5. Private deps - deploy-key scoping per repo; key material never written
   to the preview sandbox (build-only, shredded).
6. URL collision - preview slug = repo+PR hash; collision -> suffix; two
   repos same PR number never clash.
7. Zombie previews - PR closed/merged -> teardown; webhook missed -> sweeper
   reconciles with repo state hourly.
8. Build resource exhaustion - build sandbox quotas (T03); a OOMing build
   fails the check-run with logs, platform unaffected.
9. Flaky smoke tests - retry policy (2) then fail; flakiness reported in
   the PR comment.
10. Secret leakage in logs - build logs redacted (T07 redactor) before the
    PR comment.

**Advanced applications.**
- Build-cache sharing across previews (registry-backed layer cache).
- Cost attribution per PR ("this preview cost $0.04").
- Preview with seeded demo data variants (`?seed=demo1`).

**Verify.** Mock-GitHub end-to-end: push event -> green check-run -> preview
serving CoreApp -> close event -> preview gone; storm + replay tests.

---

## T15 - `self-healing/` - Self-Healing Software  [ ]

**Goal.** The platform detects CoreApp failures, diagnoses from the event
store, proposes a fix with the agent, verifies it in a sandbox, and rolls it
out - closing the loop from software -> agents -> better software.

**Build.**
- `supervisor`: SLO monitors on a live CoreApp, incident detector ->
  **incident timeline** auto-built from T07 events.
- Remediation loop: diagnose (trace summary) -> agent proposes patch ->
  verify in a forked sandbox (T05 + T08 gate) -> staged rollout with
  auto-rollback -> post-incident report.

**Edge cases.**
1. Oscillation - fix A->B->A flapping: remediation history hash; repeat
   diagnosis -> escalate to human instead of loop #4.
2. Fix makes it worse - canary SLO check fails -> auto-rollback within one
   check interval; incident report includes the bad patch.
3. Non-rollbackable changes - DB migrations forced expand-contract:
   rollback of a contract phase is blocked with explanation.
4. Alert storms - dedup by fingerprint, suppression windows; 100 identical
   alerts -> 1 incident.
5. Healing during chaos game-day - incidents caused by a declared T09
   experiment are labeled `EXPECTED_DISRUPTION`, not remediated.
6. Diagnosis with missing data - trace gaps (recorder down) -> remediation
   refused ("insufficient evidence"), never guess-patch.
7. Concurrent incidents - remediation serialized per service; global
   remediation budget per hour.
8. Verify-environment divergence - fork behaves differently from prod:
   canary still authoritative; fork result advisory only (documented).
9. Rollback of a rollback - checkpoint lineage ensures a clean target
   always exists (T05 last-known-good).
10. Human veto window - configurable delay before rollout with one-click
    cancel.

**Advanced applications.**
- Playbook learning: successful remediations become reusable playbooks
  (pattern -> patch template).
- Regression-test-on-heal: every accepted patch adds a failing->passing
  test to CoreApp's suite.
- SLO budget-driven healing aggressiveness (error-budget policy).

**Verify.** Inject a real bug (chaos-assisted) -> full loop observed:
detection, timeline, patch, canary, rollout, report; oscillation and
bad-patch rollback scenarios.

---

## T16 - `agentos/` - AI-Native Compute Platform (Phase 17)  [ ]

**Goal.** The capstone: one control plane unifying T01-T15 - tasks in,
environments compiled, agents scheduled into microVM sandboxes, everything
observed, trajectories exported as training data.

**Build.**
- Unified API gateway + console UI: task submission, live sandbox board,
  agent observatory (terminal stream + event timeline + replay scrubber),
  benchmark leaderboards, chaos game-day panel, dataset export.
- Control-plane composition: scheduler (T12) + IR compiler (T13) +
  checkpoint store (T05) + event store (T07) as one deployable stack
  (compose profile per component for gradual adoption).
- Dataset pipeline: filter traces -> quality gate (T08 scores) -> export ->
  "train/evaluate" hook document.

**Edge cases.** (integration-level, on top of each component's)
1. Component-down degradation matrix - event store down -> platform runs
   degraded, observatory shows gaps honestly (no fake data).
2. Cross-component idempotency - task submitted twice during a gateway
   retry -> one sandbox tree (end-to-end idempotency key propagation).
3. Upgrade migration - schema migrations for event store + scheduler DB
   with rollback scripts; mixed-version components rejected cleanly.
4. Backpressure end-to-end - 500-task burst: queue metrics visible, UI
   stays responsive, nothing OOMs.
5. Multi-tenant isolation - tenant A can never see B's sandboxes, traces,
   previews (authorization test matrix).
6. Billing/cost accounting correctness - per-tenant usage sums match
   cgroup + time accounting within 5%.
7. Disaster recovery - full control-plane backup/restore drill:
   checkpoint store + event store + DBs restored to a fresh stack.
8. UI consistency under races - sandbox disappears mid-click: every view
   handles 404/410 gracefully.
9. Time-travel across components - scrubber seeks: sandbox state (T05),
   terminal frame (T04), event (T07) all align within 100 ms.
10. Kill-the-platform - power-loss simulation (`kill -9` everything):
    restart -> reconciliation converges to a truthful state with zero
    ghosts, zero double-runs.

**Advanced applications.**
- One-command demo: `start.ps1` -> full AgentOS with a seeded agent
  benchmark + chaos game-day running against CoreApp, all observable.
- "Agent dataset flywheel" report: traces -> dataset -> mock-fine-tune ->
  benchmark delta (the closing of the document's final loop).
- Plugin SDK: custom tools/injectors/scorers registered via manifest.

**Verify.** End-to-end demo script + the DR drill + tenant isolation matrix.

---

## Dependency graph

```
T01 sandbox-api --+- T02 scheduler --+- T04 gateway -- T06 agent-runtime
                  |                  +- T03 quotas
                  |                  +- T05 checkpoint
T06 + T04 + T05 --+- T07 trajectory-recorder -- T08 agent-bench
T01..T08 -- T09 chaos-lab
T02 ------- T10 gpu-sandbox
T01 ------- T11 microvm-lab -- T12 distributed-scheduler
T01..T03 -- T13 env-compiler -- T14 github-to-live
T05..T09 -- T15 self-healing
ALL ------ T16 agentos
```
