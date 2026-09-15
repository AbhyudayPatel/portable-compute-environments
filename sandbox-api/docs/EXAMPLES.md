# sandbox-api - Practice Exercises

Hands-on exercises to build real understanding, ordered beginner ->
expert. Each says what to do, what to **observe**, and what it *teaches*.
Run the stack first: `scripts/start.ps1`. API is at `http://localhost:9000`.

Two ways to interact, both equivalent:

- **The console** - http://localhost:9000 - a live dashboard (recommended
  for beginners; nothing to install, no commands to remember)
- **The raw API** - http://localhost:9000/docs (Swagger UI) or `curl`

---

## Level 0 - Learn the console first (5 min)

Open **http://localhost:9000**.

### 0.1 Read the "How this works" strip (top of the page)
It shows the 4-layer path every request takes:
`you -> API -> dind (private docker engine) -> sandbox (own network + containers)`
and the exact steps a CREATE and a DELETE perform. **Observe:** this is the
mental model for everything below.

### 0.2 Watch the port pool
The bar near the top shows ports 9200-9209. **Observe:** when you create a
sandbox with a port, a cell lights up; when you delete it, the cell frees.
That cell number IS the localhost port the app is reachable on.

### 0.3 Create from the UI
Pick a template card (each card draws its containers), type a name, click
create. **Observe:** the creation log under the form narrates each step live
(port allocated -> image built -> containers started -> READY). That log IS
the sandbox's event log, streaming.

### 0.4 Open "details >" on a sandbox
Three panels:
1. **What's inside** - the live containers (name, role, image, status,
   health) from the engine. A `coreapp` shows 3 rows: frontend, backend, db.
2. **Run a command inside** - pick a container, type `hostname` or
   `ps aux`, run. Try the `...-db` container with
   `psql -U core -d coredb -c 'select * from tasks'` after adding tasks in
   the app's browser UI - you are reading the sandbox's database directly.
3. **Event timeline** - everything the platform did to this sandbox, in
   order, with timestamps.

### 0.5 Delete from the UI
Click delete, confirm. **Observe:** the toast says what was removed
(containers + network) and which port was freed; the card vanishes; the
event log survives (query it via `GET /sandboxes/{id}/events`).

---

## Level 1 - Create & explore (5 min)

### 1.1 Your first sandbox
```powershell
curl -X POST http://localhost:9000/sandboxes -H 'content-type: application/json' `
  -d '{"name":"my-first","template":"web"}'
# poll until READY:
curl http://localhost:9000/sandboxes   # find the id + url
```
**Observe:** response is `201` with `state: CREATING`. A few seconds later
it flips to `READY`. Open the `url` - the page shows the 3-layer path your
request travelled.
**Teaches:** sandboxes are async - create returns immediately, provisioning
happens in the background.

### 1.2 The full app
```powershell
curl -X POST http://localhost:9000/sandboxes -H 'content-type: application/json' `
  -d '{"name":"my-app","template":"coreapp"}'
```
**Observe:** takes ~60 s the first time (two images are *built inside the
inner engine* - watch `GET /sandboxes/{id}/events` to see `build` events),
seconds on later creates (cached). Open the URL -> working task board.
Add/toggle/delete tasks, then `curl http://localhost:<port>/api/tasks`.
**Teaches:** a sandbox can be a whole multi-container application, not just
one container.

### 1.3 Exec into it
```powershell
curl -X POST http://localhost:9000/sandboxes/<id>/exec `
  -H 'content-type: application/json' -d '{"cmd":["hostname"]}'
```
**Observe:** the hostname is the inner container name (`sbx-<id>-...`).
Try `["sh","-c","ip addr"]`, `["cat","/etc/os-release"]`.
**Teaches:** you executed through two Docker engines from one HTTP call.

---

## Level 2 - Lifecycle & state machine (10 min)

### 2.1 Stop/start
```powershell
curl -X POST http://localhost:9000/sandboxes/<id>/stop
curl http://localhost:<port>/          # <- connection refused now
curl -X POST http://localhost:9000/sandboxes/<id>/start
```
**Observe:** while STOPPED, `exec` returns **409** (try it). The URL comes
back after `start`.
**Teaches:** state machine - operations are rejected from the wrong state,
never silently no-op'd.

### 2.2 Exec from the wrong state
`POST /exec` on a STOPPED sandbox -> `409 {"detail":"exec requires READY..."}`.
**Teaches:** every transition is guarded.

### 2.3 Delete is idempotent
```powershell
curl -X DELETE http://localhost:9000/sandboxes/<id>   # 204
curl -X DELETE http://localhost:9000/sandboxes/<id>   # 404
```
**Observe:** first is 204, second is 404 - never 500. Then check
`GET /sandboxes/<id>/events` - **the audit trail survives deletion**.
**Teaches:** safe retries for clients; auditability.

### 2.4 Recreate the same name
Delete `my-first`, then re-create `my-first` with the same template.
**Observe:** works fine (deleted names are tombstoned, not leaked). But
creating `my-app` twice *without deleting* returns the existing sandbox
(`200`), and with a *different* template -> `409`.
**Teaches:** idempotent create = the platform's contract with retrying
clients.

---

## Level 3 - Self-managing platform (10 min)

### 3.1 TTL self-destruction
```powershell
curl -X POST http://localhost:9000/sandboxes -H 'content-type: application/json' `
  -d '{"name":"short-lived","template":"web","ttl_seconds":30}'
```
**Observe:** watch `GET /sandboxes` - ~30 s after it becomes READY the
reaper deletes it (`state: DELETED`, event `TTL expired`).
**Teaches:** resource reclamation; the seed of the T02 scheduler janitor.

### 3.2 Port-pool exhaustion
Create **ten** `web` sandboxes (pool is 9200-9209), then an eleventh.
**Observe:** #11 gets `429` + `Retry-After`, shows as `FAILED` with
`port: null` - no half-created containers (`docker compose -p sandbox-api
exec dind docker ps -a` to confirm nothing leaked). Delete one, retry  - 
the freed port is reused (lowest-free).
**Teaches:** admission control + rollback on resource exhaustion.

### 3.3 The event log as a flight recorder
Create, stop, start, exec, delete a sandbox, then `GET /sandboxes/<id>/events`.
**Observe:** every action in order with monotonic `seq` - including
`reconcile` events if you restarted the API mid-life.
**Teaches:** the seed of the T07 trajectory/event store.

---

## Level 4 - Failure & recovery (10 min)

### 4.1 API crash recovery (reconciliation)
```powershell
curl -X POST http://localhost:9000/sandboxes ... {"name":"survivor","template":"web"}
docker compose -p sandbox-api restart api
curl http://localhost:9000/sandboxes     # survivor is still there, READY
```
**Observe:** after the restart the API re-adopts the sandbox from engine
labels. `GET .../events` shows `adopted from engine` / state-sync entries.
**Teaches:** the DB is a *metadata cache*, never the source of truth for
existence - the pattern behind T12 worker repair.

### 4.2 Engine down -> 503, never a hang
```powershell
docker pause sandbox-api-dind-1
curl http://localhost:9000/healthz                       # {"dind":"down"}
curl -X POST http://localhost:9000/sandboxes ...         # 503 + Retry-After
docker unpause sandbox-api-dind-1
```
**Teaches:** fail-fast with a semantic status instead of hanging sockets.

### 4.3 Exec timeout & output caps
```powershell
# times out at 2s, flagged, API stays responsive:
'{"cmd":["sleep","30"],"timeout":2}'            -> "timed_out": true
# 200KB of output gets cut at 64 KiB:
'{"cmd":["sh","-c","head -c 200000 /dev/zero | tr \\0 x"]}' -> "truncated": true
```
**Teaches:** bounded resources per call - a runaway command can never
exhaust the API's memory or a client's patience.

### 4.4 Foreign-container safety
```powershell
docker compose -p sandbox-api exec dind docker run -d --name rogue nginx:alpine
curl http://localhost:9000/sandboxes     # rogue is NOT listed
docker compose -p sandbox-api restart api   # reconcile leaves it alone
```
**Teaches:** the label contract - the platform only manages what it created.

---

## Level 5 - Break it thoughtfully (free play)

- Create a coreapp, stop its **db container by hand**
  (`docker compose -p sandbox-api exec dind docker stop sbx-<id>-db`),
  reload the task board -> 502. `/api/health` reports `db: down`. Restart
  it and watch recovery.
- Fill the pool, then write a loop that deletes one and immediately
  creates one - watch ports get reused.
- Set `MAX_SANDBOXES=3` in `.env`, restart, and find the 429 path for
  capacity (distinct from the port-pool 429).
- Read the inner engine directly:
  `docker compose -p sandbox-api exec dind docker ps -a` - correlate every
  container with a `sandbox.id` label.

---

## What each exercise maps to

| Exercise | AgentOS concept it seeds |
|---|---|
| 1.2, 2.x | sandbox lifecycle API (all later tasks) |
| 3.1 | scheduler janitor (T02) |
| 3.2 | admission control / quotas (T02, T03) |
| 3.3 | event store / trajectories (T07) |
| 4.1 | distributed reconciliation (T12) |
| 4.2 | graceful degradation (T16 matrix) |
| 4.3 | bounded agent tool calls (T06) |
| 4.4 | blast-radius / tenant isolation (T16) |
