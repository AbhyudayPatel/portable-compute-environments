# cluster-in-a-box - T12-lite: distributed workers, three engines deep

> A control plane that schedules sandboxes across **several worker nodes**,
> where each worker is a complete sandbox-api (its own dockerd, agent
> proxy and API) running as **one sandbox of the outer platform**. Three
> Docker engines stacked on one Windows laptop; killing a worker is an
> API call; every placement stays reachable from the browser through all
> three layers by the same port number.

```
Windows browser  http://localhost:9312
   |
Docker Desktop (engine 0)
   '- outer dind (engine 1)                   <- sandbox-api :9000
        '- worker-1 container (privileged)   <- a `worker` sandbox, API published at :9341
             |- dockerd (engine 2)
             |- sbx-agent proxy :7799
             |- sandbox-api :8000
             '- sandbox c-xyz  (busybox httpd) publishing 9312  <- the app you opened
cluster :9430  places / heartbeats / drains / reschedules / fences
```

## Quick start

```powershell
# sandbox-api must publish the cluster port range (default 9300-9349):
cd ..\sandbox-api; docker compose up -d           # (re)creates dind with CLUSTER_PORTS if it changed
cd ..\cluster-in-a-box; powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

```bash
curl -s -X POST localhost:9430/workers -H 'content-type: application/json' -d '{"count":2}'
# first time: the worker image is built INSIDE the outer engine (~3-5 min); workers go STARTING -> ALIVE
curl -s localhost:9430/workers
curl -s -X POST localhost:9430/sandboxes -H 'content-type: application/json' -d '{"name":"demo","template":"web","data_path":"/www"}'
curl -s localhost:9430/sandboxes            # url: http://localhost:93xx
bash scripts/verify.sh                      # 31 assertions, ~10 min on first run
```

## What the control plane does

| Concern | Mechanism |
|---|---|
| Worker lifecycle | `POST /workers` creates `worker` sandboxes on the outer sandbox-api with a published API port (`9340+k`) and an identity-port block (`9300+10k..`); heartbeats promote STARTING -> ALIVE |
| Placement | `least-loaded` (fewest placements), `binpack` (fullest worker with room), `affinity` (label match, then least-loaded); per-worker capacity |
| Liveness | heartbeat every 2 s; 3 misses -> SUSPECT, 5 -> DEAD; DEAD marks its placements UNKNOWN |
| Drain | `POST /workers/{id}/drain`: each placement is re-created on another worker, its `data_path` copied with the agent's tar API, then deleted on the source |
| Failure recovery | `POST /sandboxes/{id}/reschedule` re-places a placement whose worker is SUSPECT/DEAD on a live worker |
| Fencing | a worker returning from DEAD gets a new **epoch**; copies of placements that were re-placed elsewhere are purged on it, placements not moved are re-adopted |
| Observability | `GET /events`, `GET /status`, per-worker remote sandbox view |

## API

| Method | Path | Notes |
|---|---|---|
| POST | `/workers` | `{count, labels}` |
| GET | `/workers[/{id}]` | state, epoch, misses, placements, remote sandboxes |
| POST | `/workers/{id}/drain` | migrate placements away |
| DELETE | `/workers/{id}` | delete the outer sandbox; placements LOST |
| POST | `/sandboxes` | `{name, template, policy, labels, data_path}` |
| GET | `/sandboxes[/{id}]` | worker, remote id, port, url, state, moves |
| POST | `/sandboxes/{id}/reschedule` | only when the worker is not ALIVE |
| DELETE | `/sandboxes/{id}` | |
| GET | `/status` `/events` `/healthz` | |

## Docs

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) - the worker image, three-level port identity, heartbeats, epochs, migration.
- [`docs/EDGE-CASES.md`](docs/EDGE-CASES.md) - T12 cases mapped to verify.sh; what is EXPECTED.
- [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) - files, build story, bugs.
- [`docs/EXAMPLES.md`](docs/EXAMPLES.md) - chaos exercises.
- [`docs/SECURITY.md`](docs/SECURITY.md).
