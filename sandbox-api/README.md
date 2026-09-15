# sandbox-api - T01: Sandbox API

> A REST control plane that creates, inspects, execs into, and destroys
> **sandboxes inside a dedicated Docker-in-Docker engine**. The first
> platform component of AgentOS - everything later (scheduler, quotas,
> gateway, agent runtime) drives sandboxes through this API.

```
Windows host --> http://localhost:9000  (FastAPI control plane)
                     | docker SDK, tcp://dind:2375 (internal net only)
                     v
              dind service (docker:27-dind)
                     |  one bridge network + N containers per sandbox
                     v
   sandbox "web"          sandbox "coreapp"
   busybox httpd :80      nginx -> FastAPI -> Postgres
        ^                      ^
   dind publishes 9200..9209 (identity mapping, one per sandbox)
        ^
   Windows browser: http://localhost:9203
```

## Quick start

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

Then open the **console** - the window into the platform:

###  http://localhost:9000

The console shows you, live:
- a **"How this works"** strip - the 4-layer path every request takes, and
  the exact steps a create/delete performs
- the **port pool** - watch ports get allocated and freed
- **sandbox cards** with live states (CREATING pulses, READY, FAILED...)
- **details >** on any sandbox: *what's inside* (containers, roles, health),
  a **browser exec box** (run `hostname`, `ps aux`, even `psql` in the db
  container), and the **event timeline** streaming what the platform did
- a **create panel** that narrates each provisioning step as it happens

Prefer raw API? Interactive Swagger docs at **http://localhost:9000/docs**.

Then pick your path:

```powershell
# 1. GUIDED TOUR (recommended first) - creates real apps, pauses so you
#    can open them in the browser, shows every feature with live output:
powershell -ExecutionPolicy Bypass -File scripts\demo.ps1

# 2. EDGE-CASE BATTERY - 22 assertions proving every edge case:
bash scripts/verify.sh

# 3. HANDS-ON PRACTICE - exercises beginner -> expert:
#    docs/EXAMPLES.md
```

Or create your first sandbox by hand:

```powershell
curl -X POST http://localhost:9000/sandboxes `
  -H 'content-type: application/json' `
  -d '{"name":"demo","template":"coreapp"}'
# -> 201 {"state":"CREATING",...}; poll GET /sandboxes/<id> until READY,
#   then open the returned "url" - a working task board in your browser.
```

## The applications you can run in a sandbox

| Template | What you get (fully working) | Explore it |
|---|---|---|
| `coreapp` | **3-tier task board** - nginx -> FastAPI -> Postgres, with a real browser UI (add/toggle/delete tasks) | open the URL, use the board; `curl <url>/api/tasks` |
| `web` | busybox httpd serving an **introspection page** that shows the 3-layer request path + a `/health` JSON probe | open the URL; `curl <url>/health` |
| `blank` | idle alpine shell - a pure **exec target** | `POST /sandboxes/{id}/exec {"cmd":["hostname"]}` |

Every sandbox also gives you: ordered **event log** (`/events`),
**stop/start**, **TTL self-destruction**, and **exec with timeout + output
caps** - see [`docs/EXAMPLES.md`](docs/EXAMPLES.md) for exercises on each.

## Templates

| Template | What you get | Port? |
|---|---|---|
| `web` | busybox httpd serving a page stamped with the sandbox name/id | yes |
| `blank` | idle alpine shell - an exec target, nothing published | no |
| `coreapp` | the full three-tier CoreApp (nginx -> FastAPI -> Postgres), images **built inside dind** from API-uploaded tar build contexts | yes |

## API surface

| Method | Path | Notes |
|---|---|---|
| GET | `/healthz` | api + dind liveness; never hangs |
| GET | `/templates` | template catalog |
| POST | `/sandboxes` | create; **idempotent by name** |
| GET | `/sandboxes` | list non-deleted |
| GET | `/sandboxes/{id}` | inspect |
| POST | `/sandboxes/{id}/stop` `/start` | lifecycle |
| POST | `/sandboxes/{id}/exec` | `{cmd:[...], timeout}` -> stdout/stderr/exit, 64 KiB caps, timeout flag |
| GET | `/sandboxes/{id}/events` | ordered per-sandbox event log |
| DELETE | `/sandboxes/{id}` | 204; second delete -> 404 |

Errors are semantic: `400` validation . `404` unknown/deleted . `409` wrong
state or name conflict . `429` capacity/port-pool (with `Retry-After`) .
`503` inner engine down (with `Retry-After`).

## Why it matters (the seeds it plants)

- **Reconciliation loop** -> becomes worker-state repair in the distributed
  scheduler (T12).
- **TTL reaper** -> becomes the scheduler janitor (T02).
- **Per-sandbox event log** -> becomes the trajectory/event store (T07).
- **API-uploaded image builds into a sandbox engine** -> how every later
  project gets workloads into isolated engines without a registry.

## Docs

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) - components, state machine,
  the identity port mapping, labels contract.
- [`docs/EDGE-CASES.md`](docs/EDGE-CASES.md) - all 12 edge cases, each mapped
  to its handling code and its verify.sh test.
- [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) - the files, line by
  line, and the bugs found while building.
- [`docs/SECURITY.md`](docs/SECURITY.md) - what this demo trusts and what a
  real deployment must change.

## Reset

```powershell
scripts\stop.ps1     # keep volumes (inner images + SQLite survive)
scripts\reset.ps1    # wipe volumes (cold start, re-pulls inner images)
```
