# sandbox-api - Architecture

## The one-sentence idea

A FastAPI control plane owns a **dedicated inner Docker engine** (DinD) and
exposes sandboxes - labeled groups of containers on per-sandbox inner
networks - through a small REST API with explicit states and idempotent
semantics. A **web console** (served by the API itself at `/`) gives a live
window into all of it.

## The console (the window in)

Served at `http://localhost:9000/` - a dependency-free vanilla-JS SPA in
`api/app/static/` (`index.html`, `style.css`, `app.js`), mounted read-only
from the API container. It polls every 2 s:

| Console element | Backing endpoint |
|---|---|
| header pills (api/dind/counts) | `GET /status` |
| port-pool bar | `GET /status` (`pool.used/free`) |
| sandbox cards | `GET /sandboxes` |
| "what's inside" container table | `GET /sandboxes/{id}/containers` |
| event timeline + live create narration | `GET /sandboxes/{id}/events` |
| browser exec box | `POST /sandboxes/{id}/exec` (with `container` target) |

Nothing is installed or proxied: the same process that owns the sandboxes
serves the UI that watches them. The console is also the teaching surface -
the top strip explains the request path and what create/delete do.

```
+- Windows host ---------------------------------------------------+
|  browser/curl --> localhost:9000 (api)                           |
|  browser      --> localhost:9200..9209 (sandbox apps)            |
|                                                                  |
|  Docker Desktop --> compose network "sandnet"                    |
|    +------------------+      tcp://dind:2375 (NEVER published)   |
|    |  api             | --------------------------+              |
|    |  FastAPI+SQLite  |                           v              |
|    +------------------+        +----------------------------+    |
|                                |  dind (docker:27-dind)     |    |
|                                |  inner dockerd             |    |
|                                |  +-- net sbx-<id>          |    |
|                                |  |    +-- container(s)     |    |
|                                |  |         ports {80:P}    |----+--> dind binds P
|                                |  +-- ...                   |    |    on ALL its ifaces
|                                +----------------------------+    |
+------------------------------------------------------------------+
```

## The identity port mapping (from docker-nested-lab, made dynamic)

The nested lab hard-coded a 9000-9010 DNAT range. Here the pool is
**allocated per sandbox** by the API:

1. Template says a container publishes `{"80/tcp": P}`.
2. Inner dockerd binds port `P` on the dind container's interfaces.
3. Compose publishes the whole pool range `9200-9209:9200-9209` on dind.
4. Docker Desktop forwards `localhost:P` -> dind:`P` -> inner container:80.

So **one port number means the same thing at every layer** - the property
that made the nested lab debuggable, now dynamic. Allocation is
lowest-free, stored in SQLite, and mirrored as a container label
(`sandbox.port`) so the *engine* can re-derive it after a DB wipe.

## The labels contract (what the API is allowed to touch)

Every object the API creates carries:

| Label | Purpose |
|---|---|
| `sandbox.managed=true` | the API never touches anything without it |
| `sandbox.id` | groups containers/network into one sandbox |
| `sandbox.name` | human name (idempotency key) |
| `sandbox.template` | which template created it |
| `sandbox.role` | `app` / `db` / `backend` / `frontend` (exec target choice) |
| `sandbox.port` | on the container holding the published port |

Foreign containers (a user `docker exec`s into dind and runs one by hand)
are invisible to lists, deletes, exec, and reconciliation. This is the
blast-radius contract every later project inherits.

## State machine

```
            create accepted
                 |
                 v
             CREATING --provision ok--> READY --stop--> STOPPED
                 |                      ^  |               |
                 +--failure--------> FAILED +----start-----+
                 |                      |
            (any non-CREATING state)    | delete / TTL
                 v                      v
               DELETED  <---------------+
```

- `CREATING` is the only state that refuses DELETE (409) - teardown of a
  half-built sandbox mid-flight would leave torn resources; the provision
  thread's own rollback handles failures instead.
- `FAILED` sandboxes hold **no port** (freed on failure) and can be
  deleted normally.
- Every transition is appended to the per-sandbox event log with a
  monotonic `seq`.

## Crash recovery: reconciliation on boot

At startup the API lists inner containers labeled `sandbox.managed=true`
and reconciles DB <-> engine (see `engine.reconcile()`):

| Situation | Action |
|---|---|
| engine has sandbox, DB doesn't | **adopt**: insert row, state from engine |
| DB row, engine has nothing | mark `DELETED` (vanished while API down) |
| row says `DELETED`, containers still there | finish the interrupted delete |
| row says `CREATING` | **roll back** the interrupted create, mark `FAILED` |
| states disagree | engine wins |

This makes the DB a cache of *metadata*, never of *existence*. The same
pattern reappears as worker-state repair in the distributed scheduler (T12).

## Background threads

- **provision** (per create): image prep -> containers -> readiness wait ->
  READY/FAILED. Runs off-request so image pulls/builds never block the API.
- **reaper** (every `REAPER_INTERVAL`s): finds rows whose monotonic TTL
  deadline passed and deletes them through the same locked path as manual
  DELETE (racing is safe, both are idempotent).

## Where the inner images come from

- `web`: `busybox:1.36` pulled into dind.
- `blank`: `alpine:3.20` pulled into dind.
- `coreapp`: `postgres:16-alpine` pulled; `sandbox-coreapp-backend:1.0` and
  `sandbox-coreapp-frontend:1.0` are **built inside dind** from tar build
  contexts uploaded through the Docker API (`templates.py` holds the
  Dockerfiles + sources as string constants). No registry, no bind mounts
  into dind, cached in the `dind-data` volume after the first build.

## Data

- `api-data` volume -> SQLite (`sandboxes`, `events`). WAL mode; every write
  under a module lock; short-lived connections (FastAPI threadpool-safe).
- `dind-data` volume -> inner `/var/lib/docker`. `scripts/stop.ps1` keeps
  both; `reset.ps1` wipes both.
