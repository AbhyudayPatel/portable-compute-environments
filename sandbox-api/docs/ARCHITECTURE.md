# sandbox-api v2 - Architecture

## The one-sentence idea

A FastAPI control plane owns a **dedicated inner Docker engine** (dind),
exposes sandboxes (labeled groups of containers on per-sandbox networks)
through a small REST API with explicit states, and reaches **into** every
sandbox through an **agent** that is PID 1 of each container - so runtime
operations (exec, files, stats, fingerprints, GUI) never depend on the
engine's exec facility and would work unchanged behind a microVM.

```
+- Windows host -------------------------------------------------------------+
|  browser/curl --> localhost:9000 (api + console)                            |
|  browser      --> localhost:9200..9209 (sandbox apps, identity ports)       |
|                                                                             |
|  Docker Desktop --> compose network "sandnet"                               |
|    +--------------------+   tcp://dind:2375   (lifecycle, events)           |
|    |  api               | ---------------------------------+               |
|    |  FastAPI + SQLite  |   http://dind:7799  (agent proxy) |               |
|    |  core / informer   | -----------------------------+   |               |
|    +--------------------+                              v   v               |
|                                +------------------------------------------+ |
|                                |  dind (sandbox-dind image)               | |
|                                |  dockerd + runc + runsc(gVisor)          | |
|                                |  sbx-agent proxy :7799                   | |
|                                |  +-- net sbx-<id> -----------------+     | |
|                                |  |  container(s): PID1 = sbx-agent  |     | |
|                                |  |    :7700 agent API, workload     |-----+--> dind binds P
|                                |  |    ports {app:P}                 |     | |
|                                |  +---------------------------------+     | |
|                                +------------------------------------------+ |
+-----------------------------------------------------------------------------+
```

## Components

| Piece | File(s) | Role |
|---|---|---|
| core | `api/app/core.py` | the lifecycle state machine: create/provision/stop/start/delete/reap/reconcile/informer reactions. No HTTP, no threads, no clock. |
| store | `api/app/store.py` | SQLite: sandboxes + events; per-sandbox event seq; global event cursor; dual TTL deadlines |
| engine | `api/app/engine.py` | the only file talking to dockerd: images, networks, containers (agent injection, PID-1 wrapping, runtime choice), readiness, event stream |
| agent client | `api/app/agent.py` | HTTP clients for the sandbox agent (through the proxy) and node operations |
| informer | `api/app/informer.py` | three threads: event watch, periodic resync, TTL reaper |
| templates | `api/app/templates.py` | web / blank / coreapp / desktop; build contexts from `coreapp/` and `desktop/` with content-hashed tags |
| fingerprint | `api/app/fingerprint.py` | DB table hashes, fs/db diff |
| main | `api/app/main.py` | FastAPI routes = thin adapters over core + agent; SSE; console mount |
| clock / bus | `api/app/clock.py`, `bus.py` | injectable clock (virtual in the simulator); in-process pub/sub feeding SSE |
| sbx-agent | `agent/*.go` | the in-sandbox binary (see AGENT.md) |
| sandbox-dind | `dind/` | docker:27-dind + agent proxy + gVisor + daemon.json |

## The agent path (what changed most)

```
v1:  api --docker SDK--> dind:2375 --exec--> container        (one-shot, no kill, no resume)
v2:  api --HTTP--> dind:7799/sbx/<ip>/... --HTTP--> <ip>:7700 (sessions, files, stats, gui)
```

1. At create, the API uploads `/sbx/agent` into every container with
   `put_archive` and rewrites the entrypoint to `["/sbx/agent","init","--"]`
   with the image's original entrypoint+cmd as the workload. The agent
   becomes PID 1, forwards signals, reaps zombies and serves HTTP on :7700.
2. The engine-side **proxy** (same binary, `proxy` mode, started by the
   dind entrypoint) listens on :7799 on the compose network. The dind
   container's network namespace hosts every inner bridge, so the proxy
   can dial any inner container IP directly. `GET /sbx/172.18.0.2/healthz`
   is relayed to `http://172.18.0.2:7700/healthz`.
3. Readiness = every container's agent answers `/healthz` **and** (port
   templates) the app's health path answers through the identity port.
   So `READY` means the whole chain works, not "containers started".
4. Exec, sessions, files, fingerprints, GUI are plain HTTP calls to the
   agent, each authenticated with the sandbox's bearer token.

Why this is the foundation for everything after: a microVM backend (T11)
only has to run the same agent on vsock and the whole runtime API keeps
working; the egress proxy (T05 project) injects its CA through the same
tar; checkpoints (T05) use the proxy's `/node/exec` to mount overlays.

## The informer (watch, do not poll)

```
dockerd /events (filtered: type=container, label sandbox.managed=true)
   |  die / oom / start / destroy
   v
Core.on_container_event(sid, action, container, attrs)
   READY  --die/oom-->  DEGRADED (detail.dead[container] = "exit=137 oom")
   DEGRADED --start (all back)--> READY
   any live --destroy (nothing left in engine)--> DELETED
```

Rules that make it safe:

- The handler takes the **per-sandbox lock** and re-reads the state.
  `stop()` and `delete()` hold that lock while they cause `die` events,
  so when the handler finally runs, the state (`STOPPED`/`DELETED`)
  already explains the event and it is ignored. (The simulator has a test
  that injects the opposite bug and proves the invariant catches it.)
- A **periodic resync** (`RESYNC_INTERVAL`, 30 s) runs `reconcile(periodic=True)`
  to cover anything the event stream missed (dind restart, dropped events).
- Reconcile only rolls back `CREATING` rows that **no live provision
  owns** (`core.in_flight`). After a crash the set is empty, so every
  CREATING row is an orphan; during normal operation an in-flight create is
  never touched. (Both halves of this rule were found by the simulator and
  by verify.sh, see IMPLEMENTATION.md.)

## Isolation: runc or gVisor, per sandbox

`sandbox-dind` installs a pinned gVisor release (`runsc` + its sidecar
binaries) and registers it in `daemon.json`:

```json
{"runtimes": {"runsc": {"path": "/usr/local/bin/runsc",
                        "runtimeArgs": ["--platform=systrap", "--network=sandbox"]}}}
```

`POST /sandboxes {"isolation":"gvisor"}` passes `runtime="runsc"` to every
container create. Inside, `uname -r` reports `4.19.0-gvisor`: the workload
talks to gVisor's userspace kernel, which talks to the host kernel through
a narrow seccomp-filtered interface. Verified on Docker Desktop (WSL2 6.18
kernel, no KVM): networking, published ports, Postgres all work; the
`systrap` platform costs noticeably on syscall-heavy workloads (a
`coreapp` under gVisor takes ~2x longer to become READY).

Templates declare which isolation modes they support; `desktop` is
container-only (KasmVNC and Chromium need more than gVisor offers today).

## The identity port mapping (unchanged)

1. Template publishes `{"80/tcp": P}` (or `3000/tcp` for coreapp/desktop).
2. Inner dockerd binds `P` on the dind container's interfaces.
3. Compose publishes the whole pool `9200-9209:9200-9209` on dind.
4. Docker Desktop forwards `localhost:P` -> dind:P -> inner container.

One number means the same thing at every layer. Allocation is lowest-free
in SQLite and mirrored in the `sandbox.port` label so the engine can
re-derive it after a DB wipe.

## Labels (what the API may touch)

| Label | Purpose |
|---|---|
| `sandbox.managed=true` | the API never touches anything without it |
| `sandbox.id` | groups containers/network into one sandbox |
| `sandbox.name` | human name (idempotency key) |
| `sandbox.template` | which template |
| `sandbox.role` | `app` / `db` / `backend` / `frontend` (exec target choice) |
| `sandbox.port` | on the container holding the published port |
| `sandbox.isolation` | `container` or `gvisor` |

## State machine

```
            create accepted
                 |
                 v
             CREATING --provision ok--> READY <----- start (heal) ---- DEGRADED
                 |                      ^  |                              ^
                 +--failure--> FAILED   |  +--stop--> STOPPED --start--+  | informer: container died
                 |                      |                 |               |
                 |                      +-----------------+               |
                 |    delete / TTL / removed outside the API              |
                 v                      v                                 |
               DELETED  <-----------------------------------------------+ (all containers gone)
```

- `CREATING` refuses DELETE (409): the provision owns the resources and
  rolls them back itself on failure.
- `FAILED` holds **no port** and **no engine resources** (reconcile
  removes leftovers if the engine was down during the rollback).
- `DEGRADED` allows exec on surviving containers and `stop`; `start`
  restarts dead containers and returns to `READY`.
- Every transition is an event with a monotonic per-sandbox `seq`.

## Provisioning as a generator

`Core.provision_steps(sid)` yields after each engine interaction
(image, resources, ready). In production a thread drives it; in the
simulator the scenario interleaves its steps with other operations and
crashes. Between steps the generator re-checks that its row is still
`CREATING` and aborts (destroying what it made) if something rolled it
back or deleted it under it.

## Time

All time reads go through `clock.py`. TTLs are stored twice:
`deadline_mono` (what the reaper compares, immune to wall-clock jumps) and
`deadline_wall` (survives restarts). Boot reconcile re-derives the
monotonic deadline from the wall one, so an API restart neither resets
nor skips a TTL (verify.sh 14 asserts both).

## Data

- `api-data` volume -> SQLite (`sandboxes`, `events`). WAL; every write
  under one lock; short-lived connections.
- `dind-data` volume -> inner `/var/lib/docker` (images, containers).
- `sbx-state` volume -> `/var/lib/sbx` inside dind, ext4: where overlay
  layers and checkpoints live (overlay cannot stack on overlay, so this
  must not be the container's own rootfs).

## Where the inner images come from

- `web`: `busybox:1.36`, `blank`: `alpine:3.20` - pulled into dind.
- `coreapp`: three images built **inside dind** from tar contexts the API
  assembles from `api/coreapp/` (a synced copy of the repo-root `coreapp/`):
  `sandbox-coreapp-db:<hash>` (postgres + init.sql),
  `sandbox-coreapp-backend:<hash>`, `sandbox-coreapp-frontend:<hash>`.
  The 12-hex tag is the sha256 of the build context, so any change to the
  app yields a new tag and a rebuild; unchanged contexts hit the cache.
- `desktop`: `sandbox-desktop:<hash>` built inside dind from
  `api/desktop/` on top of `lscr.io/linuxserver/webtop:debian-xfce`
  (3.9 GB base; first build takes several minutes, later ones seconds).

## Templates added for other projects

| Template | Used by | Notes |
|---|---|---|
| `upstream` | sandbox-egress tests | a fake internet host (`upstream.test`, HTTP + self-signed HTTPS) on its own network; egress proxies join it via `options.egress.peer_networks` |
| `worker` | cluster-in-a-box | a nested sandbox-api node: privileged (`allow_privileged`), `options.extra_ports` publishes its API port and identity block, `options.env` passes its pool |

Generic options every template accepts: `stateful` (overlay volumes,
sandbox-checkpoint), `egress` (proxy + fence, sandbox-egress),
`extra_ports` (role `app` container), `env` (all non-proxy containers),
`layers`/`copies` (fork/restore). All of them are part of the idempotency
spec: the same name with different options is a 409.
