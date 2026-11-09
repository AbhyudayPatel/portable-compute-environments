# sandbox-api v2 - T01: Sandbox API with an in-sandbox agent

> A REST control plane that creates, inspects, drives and destroys
> **sandboxes inside a dedicated Docker-in-Docker engine**. v2 adds the
> pieces a real platform needs and v1 only promised: an **agent inside
> every sandbox** (durable exec sessions, files, stats, fingerprints, GUI),
> an **event-driven core** that watches the engine instead of polling it,
> **gVisor isolation** as a per-sandbox choice, a **computer-use desktop**
> template, and a lifecycle core that is **simulation-tested** thousands of
> times per minute (see `../sandbox-sim`).

```
Windows host --> http://localhost:9000  (FastAPI control plane + console)
                     | docker SDK  tcp://dind:2375   (lifecycle: create/start/stop/rm/events)
                     | HTTP        http://dind:7799  (runtime: exec, files, stats, gui)
                     v
              dind service (sandbox-dind: docker:27-dind + sbx-agent proxy + gVisor)
                     |  one bridge network + N containers per sandbox
                     |  every container: PID 1 = sbx-agent, workload = its child
                     v
   sandbox "web"            sandbox "coreapp"             sandbox "desktop"
   busybox httpd :80        nginx -> FastAPI -> Postgres   XFCE + Chromium (KasmVNC)
        ^                        ^                              ^
   dind publishes 9200..9209 (identity mapping, one per sandbox)
        ^
   Windows browser: http://localhost:920x
```

## Quick start

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

Then open the **console** at http://localhost:9000 - it shows the port
pool, every sandbox with its live state, a live event log (server-sent
events), and per sandbox: what is inside, a one-shot exec box, a
**resumable terminal**, a file browser and a fingerprint button.

Raw API: Swagger at http://localhost:9000/docs.

```powershell
# the full edge-case battery (89 assertions, ~10 min, needs Git Bash):
bash scripts/verify.sh

# the deterministic simulator of the lifecycle core (no docker needed):
cd ..\sandbox-sim; bash scripts\verify.sh
```

## What is new in v2 (and why it matters)

| Capability | v1 | v2 |
|---|---|---|
| Running commands | `docker exec`, no kill on timeout | **sbx-agent** sessions: timeout kills the process group, output resumable with `since=`, pty + resize, stdin, signals |
| Files | none | jailed file API: read/write/list/mkdir/delete + tar up/down |
| State | container status only | merkle **fingerprint** of any path, DB table fingerprints, `GET /diff?a=&b=` |
| Noticing failures | only at API boot | **informer** watches dockerd events: a dead container flips the sandbox to `DEGRADED` within a second; `start` heals; out-of-band removal becomes `DELETED` |
| Isolation | runc only | `isolation: "gvisor"` runs the sandbox under **gVisor** (runsc) - verified on Docker Desktop / WSL2 |
| Desktop | none | `desktop` template: XFCE + Chromium streamed to the browser, **GUI API** (screenshot, click, type, key, windows) |
| TTL | lost on API restart | wall-clock deadline survives restarts |
| CoreApp images | inline source, tag = cache key | built from `coreapp/` with **content-hashed tags** - edit a byte, get a rebuild |
| Events | poll `/events` | `GET /events` and `GET /sandboxes/{id}/events?follow=1` are SSE streams with resumable cursors |
| Testing | bash battery only | bash battery **plus** a deterministic simulator that found 3 real bugs before verify.sh could |

## Templates

| Template | What you get | Port | Isolation |
|---|---|---|---|
| `web` | busybox httpd serving an introspection page + `/health` | yes | container, gvisor |
| `blank` | the agent alone (alpine) - a pure exec target | no | container, gvisor |
| `coreapp` | nginx -> FastAPI -> Postgres task board, images built **inside dind** from `coreapp/` | yes | container, gvisor |
| `desktop` | Debian XFCE desktop in your browser tab + Chromium + xdotool/scrot | yes | container |

## API surface

Lifecycle (unchanged contract from v1, plus `isolation`):

| Method | Path | Notes |
|---|---|---|
| GET | `/healthz` | api, dind, node proxy, informer |
| GET | `/status` | pool, caps, engine facts (runtimes, kernel), informer stats |
| GET | `/templates` | catalogue with resolved image tags |
| POST | `/sandboxes` | `{name, template, ttl_seconds, isolation, options}`; idempotent by name; `options.stateful=true` mounts overlay-backed data paths (see `../sandbox-checkpoint`) |
| GET | `/sandboxes[/{id}]` | list / inspect (`state`, `port`, `url`, `detail.dead`) |
| POST | `/sandboxes/{id}/stop` `/start` | start also heals `DEGRADED` |
| POST | `/sandboxes/{id}/pause` `/unpause` | cgroup freezer: `PAUSED` (checkpoints use it) |
| GET | `/sandboxes/{id}/volumes` | stateful overlay volumes (sizes, mounts) |
| POST | `/sandboxes/{id}/volumes/snapshot` `/restore` | copy uppers to a checkpoint dir / remount over layers |
| GET/PUT/DELETE | `/node/tar` `/node/untar` `/node/dir` `/node/du` | node file ops restricted to `/var/lib/sbx` |
| DELETE | `/sandboxes/{id}` | 204; second delete 404; while CREATING 409 |
| GET | `/sandboxes/{id}/containers` | live, with inner IPs and runtime |
| GET | `/sandboxes/{id}/events` | ordered log; `?follow=1` streams SSE |
| GET | `/events` | global SSE stream, `?cursor=` to resume |

Runtime, through the agent (never `docker exec`):

| Method | Path | Notes |
|---|---|---|
| POST | `/sandboxes/{id}/exec` | `{cmd, timeout, container, env, cwd}` -> exit/stdout/stderr, kills on timeout |
| POST | `/sandboxes/{id}/sessions` | durable session, `tty`, `stdin`, `timeout_seconds` |
| GET | `/sandboxes/{id}/sessions[/{s}]` | list / state (ids are `<container>:<session>`) |
| GET | `/sandboxes/{id}/sessions/{s}/stream` | NDJSON frames, `?since=&follow=1&timeout=` |
| POST | `.../sessions/{s}/stdin` `/signal` `/resize` | `?eof=1`, `{signal}`, `{cols,rows}` |
| DELETE | `.../sessions/{s}` | kill + forget |
| GET/PUT/POST/DELETE | `/sandboxes/{id}/fs...` | `fs?path=` list, `fs/read`, `fs/write`, `fs/mkdir`, `fs`, `fs/tar` (GET/PUT) |
| GET | `/sandboxes/{id}/ps` `/stats` | processes; cgroup stats per container |
| GET | `/sandboxes/{id}/fingerprint` | `?path=&exclude=&detail=1` merkle fingerprint |
| GET | `/sandboxes/{id}/fingerprint/db` | per-table hashes (coreapp) |
| GET | `/diff?a=&b=` | semantic diff: files added/removed/modified, DB rows |
| GET | `/sandboxes/{id}/screenshot` | PNG (desktop) |
| POST | `/sandboxes/{id}/gui` | `{type: click|dblclick|rightclick|move|type|key|scroll|drag, ...}` |
| GET | `/sandboxes/{id}/gui/windows` `/gui/display` | window list, display size |
| GET | `/node/info` | engine node facts (kernel, runsc, overlay support) |

Errors are semantic: `422` validation . `404` unknown/deleted . `409` wrong
state or name conflict . `429` capacity/port pool (with `Retry-After`) .
`503` engine down (with `Retry-After`) . `502` agent unreachable.

## States

```
CREATING --provision ok--> READY <--heal(start)-- DEGRADED
    |                        |  ^                    ^
    +--failure--> FAILED     |  +--start-- STOPPED   | informer: a container died
                             +--stop-----------^     |
                             +--pause--> PAUSED --unpause--> READY
                             +--delete / TTL / removed outside the API --> DELETED
```

`DEGRADED` is new: the informer saw a container of a READY sandbox die
(exit code and OOM flag in `detail.dead`). Exec still works on surviving
containers; `start` restarts the dead ones.

## Docs (read in this order)

1. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) - components, the agent
   path, the informer, isolation, state machine, data.
2. [`docs/AGENT.md`](docs/AGENT.md) - the in-sandbox agent: protocol,
   PID-1 duties, sessions, frames, files, fingerprints, GUI.
3. [`docs/EDGE-CASES.md`](docs/EDGE-CASES.md) - every edge case mapped to
   code and to its verify.sh assertion (89) or simulator test.
4. [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) - file by file, the
   build story, and the bugs found (including the 3 the simulator found).
5. [`docs/EXAMPLES.md`](docs/EXAMPLES.md) - exercises beginner -> expert.
6. [`docs/SECURITY.md`](docs/SECURITY.md) - what is trusted, what changes
   for real deployments.

## Reset

```powershell
scripts\stop.ps1     # keep volumes (inner images, SQLite, overlay state)
scripts\reset.ps1    # wipe volumes (cold start, re-pulls/rebuilds inner images)
```

Operational note: `docker compose up --build` may recreate the `dind`
service when its image changed; the inner containers then come back
stopped (state `STOPPED` after reconcile) - `start` them. To rebuild only
the API without touching sandboxes: `docker compose build api && docker
compose up -d --no-deps api`.
