# AgentOS - a sandbox platform for AI agents, built and verified on one Windows laptop

> Everything here runs on Docker Desktop. It started as "portable
> containerized dev environments" (three ways to put a Linux workstation
> in a browser) and grew into **AgentOS**: a control plane that creates
> isolated sandboxes, reaches inside them through an agent, checkpoints
> and forks their state, polices their network, schedules them with
> preemption, exposes all of it to AI agents over MCP, and scales out to
> nested worker nodes - with a deterministic simulator that found real
> bugs before any live test did.

```
             +------------------- control planes --------------------+
             |  sandbox-scheduler :9010  (queues, fairness, EDF,      |
             |      preemption via checkpoints, memory budget)        |
             |  cluster-in-a-box :9430  (nested workers, placement,   |
             |      heartbeats, drain, fencing)                       |
             |  sandbox-mcp :9420  (tools for AI agents, tenants,     |
             |      audit)                                            |
             +--------------------+---------------------------------+
                                  v
   sandbox-checkpoint :9040 -> sandbox-api :9000 <- sandbox-egress :9410
   (snapshot/fork/restore/PITR)   |   (proxy + fence + cassettes + zero-secret)
                                  |  docker SDK + agent proxy
   sandbox-blobs :9400            v
   (content-addressed store)   dind (private engine, runc or gVisor)
                                  |  one network + N containers per sandbox,
                                  |  sbx-agent as PID 1 in each
                                  v
                    web . blank . coreapp . desktop . upstream . worker
                                  ^
   sandbox-sim (no docker): the api core + the scheduler core under a
   virtual clock and a fake engine, thousands of seeded runs per minute
```

## Start here

New to all of this? Read [`docs/LEARNING-PATH.md`](docs/LEARNING-PATH.md)
- it goes from "a container is a process" to "simulate your control
plane" with something to do at every step.

Want the platform up in one go (Windows PowerShell, Docker Desktop running):

```powershell
cd sandbox-api;        powershell -ExecutionPolicy Bypass -File scripts\start.ps1   # console http://localhost:9000
cd ..\sandbox-blobs;   powershell -ExecutionPolicy Bypass -File scripts\start.ps1
cd ..\sandbox-checkpoint; powershell -ExecutionPolicy Bypass -File scripts\start.ps1
cd ..\sandbox-egress;  powershell -ExecutionPolicy Bypass -File scripts\start.ps1
cd ..\sandbox-scheduler;  powershell -ExecutionPolicy Bypass -File scripts\start.ps1   # console http://localhost:9010
cd ..\sandbox-mcp;     powershell -ExecutionPolicy Bypass -File scripts\start.ps1
cd ..\cluster-in-a-box; powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

Every project has `scripts/verify.sh` (run from Git Bash): a battery of
assertions against the live stack. The simulator runs without Docker.

## The projects

| Folder | What it is | Verified by |
|---|---|---|
| [`sandbox-api/`](sandbox-api/) | the sandbox control plane: DinD engine, **in-sandbox agent** (sessions, files, stats, fingerprints, GUI), **informer** (live DEGRADED/DELETED), **gVisor** isolation, **desktop** template, stateful overlay volumes, pause, node ops, egress wiring | `verify.sh` 101 assertions |
| [`sandbox-sim/`](sandbox-sim/) | **deterministic simulation testing** of the api core and the scheduler core: virtual clock, fake engine, seeded faults, invariants, shrinking, mutation tests | 76 pytest + sched tests; 600-seed and 150-seed fuzz runs |
| [`sandbox-blobs/`](sandbox-blobs/) | **content-addressed blob store**: streaming, refs, pins, GC with a grace window | 44 |
| [`sandbox-checkpoint/`](sandbox-checkpoint/) | **checkpoint / fork / restore / point-in-time** for stateful sandboxes (overlayfs layers, Postgres WAL archive, manifests, lineage, blob shipping) | 51 |
| [`sandbox-egress/`](sandbox-egress/) | per-sandbox **egress policy**: proxy + iptables fence, allowlists, TLS interception, **record/replay cassettes**, **zero-secret placeholders** | 48 |
| [`sandbox-scheduler/`](sandbox-scheduler/) | queue + placement: fairness, aging, **EDF**, **preemption by checkpoint and fork**, **memory budget**; simulable core | v1 battery + v2 sections |
| [`sandbox-mcp/`](sandbox-mcp/) | the platform as an **MCP server**: 19 tools, tenant tokens, audit; a real MCP client drives the battery | 31 |
| [`cluster-in-a-box/`](cluster-in-a-box/) | **distributed workers three engines deep**: placement policies, heartbeats, drain with data migration, failure + reschedule, fencing | 31 |
| [`coreapp/`](coreapp/) | the canonical demo app (FastAPI + Postgres + nginx); every copy in the repo is synced from here (`scripts/sync-coreapp.*`) | drift check |
| `browser-dev-environment/`, `browser-linux-desktop/`, `local-vscode-devcontainer/`, `docker-nested-lab/` | the original dev-environment projects and the DinD lab the platform grew from | their own docs |

## What changed in this generation (the twelve improvements)

1. **In-sandbox agent** (Go, PID 1): durable exec sessions that survive
   client and API restarts, timeouts that kill, pty, files, stats,
   fingerprints, GUI - no `docker exec` anywhere.
2. **Deterministic simulation** of both control planes; four real bugs in
   sandbox-api and two in the scheduler were found by it first.
3. **Watch, don't poll**: an informer on the engine's event stream plus
   server-sent event streams for clients.
4. **Computer-use sandboxes**: a desktop template with screenshot and
   click/type/key actions.
5. **Egress policy** with record/replay and zero-secret placeholders.
6. **State fingerprints and semantic diffs** (filesystem merkle trees,
   database table hashes).
7. **Copy-on-write forks and PITR** for stateful sandboxes.
8. **gVisor** as a per-sandbox isolation choice, verified on WSL2.
9. **MCP** facade with tenants and audit.
10. **Content-addressed blob store** shared by checkpoints and cassettes.
11. **Cluster-in-a-box**: nested worker nodes with a real control plane.
12. **Scheduler preemption, EDF and memory-aware admission**, simulable.

The design rationale for each lives in its project's `docs/ARCHITECTURE.md`;
the bugs found while building live in `docs/IMPLEMENTATION.md`; every
claim marked VERIFIED has a line in a `verify.sh` or a simulator test.

## Port map

| Project | Port(s) |
|---|---|
| sandbox-api | 9000, sandbox apps 9200-9209, worker ports 9300-9349 |
| sandbox-scheduler | 9010 |
| sandbox-checkpoint | 9040 |
| sandbox-blobs | 9400 |
| sandbox-egress | 9410 |
| sandbox-mcp | 9420 |
| cluster-in-a-box | 9430 |

## Environment notes (Windows, Docker Desktop)

- Docker Desktop's VM on this machine has 3.4 GB RAM; the whole platform
  fits, the desktop template and more than two nested workers need the
  rest. Give the VM more in `%USERPROFILE%\.wslconfig` if you can.
- Git Bash rewrites absolute Linux paths given to `docker exec`; the
  verify scripts set `MSYS_NO_PATHCONV=1` where it matters.
- `docker compose up --build` can recreate the `dind` service; inner
  sandboxes then come back `STOPPED`. The scripts rebuild only the API
  when that is all that changed.

## The roadmap, updated

[`docs/AGENTOS-TASKLIST.md`](docs/AGENTOS-TASKLIST.md) keeps the original
sixteen-task plan with its status. T01 (sandbox-api), T02 (scheduler),
T05 (checkpoint) are VERIFIED; T04 (gateway/terminal), T06 (agent
runtime), T07 (event store), T12 (distributed) are substantially seeded by
the agent, MCP, blobs and cluster projects respectively.
