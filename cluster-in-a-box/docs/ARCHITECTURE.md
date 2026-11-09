# cluster-in-a-box - Architecture

## The worker image

`sandbox-api/api/worker/Dockerfile`, built **inside the outer dind** from a
tar context the outer API assembles (`templates.py`, `WORKER_DIR`): the
`docker:27-dind` base, python + the API's dependencies, the API package
itself, the CoreApp build contexts and the agent binary. Its entrypoint
starts `dockerd` (engine 2), waits for it, starts `sbx-agent proxy`, then
`uvicorn app.main:app`. Environment makes the stock API talk to its local
engine: `DOCKER_HOST=unix:///var/run/docker.sock`,
`NODE_URL=http://127.0.0.1:7799`, `SANDBOX_HOST=127.0.0.1`.

The worker is created by the outer API like any sandbox (template
`worker`, `agent_mode: init` - so the outer agent is PID 1 of the worker
and the entrypoint is its child; outer `exec` into a worker works). Two
things are special and gated by the template:

- `privileged: true` (`allow_privileged` on the template): a nested
  dockerd needs it.
- `options.extra_ports`: `{"8000/tcp": 9340+k, "9300+10k/tcp": ..., ...}`
  published by the worker container; the outer dind binds them; the outer
  compose publishes `9300-9349` to Windows.

## Port identity through three engines

```
Windows localhost:9312
  -> Docker Desktop publish 9300-9349 -> outer dind :9312
  -> outer dind publish (worker extra_ports) -> worker container :9312
  -> worker's dockerd publish (its identity pool 9310-9319) -> sandbox container :80
```
The worker's own sandbox-api is configured with `POOL_START/POOL_END` =
its block, so the number is the same at every layer, just like the
single-node design.

## Control plane data

```
workers:    id (w<k>), idx, outer_sid, api_port, pool, labels, state, misses, epoch, last_seen
placements: id, name, template, worker_id, remote_id, port, state, labels, data_path, policy, moves
stale:      (worker_id, remote_id, placement_id)   copies to purge when a worker returns
events:     ts, kind, subject, message
```

## Heartbeats and epochs

`heartbeat_loop` polls each worker's `/healthz` (api + dind up) every
`HEARTBEAT_INTERVAL`. State machine:

```
STARTING --ok--> ALIVE --miss x3--> SUSPECT --miss x5--> DEAD
   ^              ^                   |                   |
   |              '----ok-------------'      ok (epoch+1, fence)
   '----------------------------------------------------'
```

A DEAD worker's placements become `UNKNOWN`. When it answers again, its
epoch increments and `fence()` runs: every `stale` remote recorded for
it (a placement that was re-placed elsewhere while it was dead) is deleted
on the returning worker, and placements that were not moved are
re-adopted (or marked LOST if gone). The epoch is the fencing token: a
reader can tell a worker's "generation" apart; operations against an
older epoch are stale by definition. (A full fencing token check on every
worker call would require the worker to know its epoch - EXPECTED.)

## Placement policies

| Policy | Choice among ALIVE workers with free capacity |
|---|---|
| least-loaded | fewest placements, then lowest index |
| binpack | most placements (fullest) with room, then lowest index |
| affinity | workers whose labels contain all requested labels, then least-loaded; none -> 503 |

## Migration (drain) and rescheduling

`migrate(p, src, dst)`:

1. create `c-<pid>` on `dst`, wait READY;
2. if `data_path` is set and `src` is reachable: `GET src/sandboxes/{rid}/fs/tar?path=` and
   `PUT dst/.../fs/tar?path=` (the agent's tar API; ownership and modes preserved);
3. delete on `src` if reachable, else record a `stale` row;
4. update the placement (worker, remote id, port, `moves+1`).

Drain = migrate every placement of an ALIVE/SUSPECT worker (source
reachable, data copied). Reschedule = migrate from a SUSPECT/DEAD worker
(source unreachable: fresh copy, data is whatever the template provides;
the stale copy is purged when the worker returns).

Stateful placements with a checkpoint-based migration (checkpoint on A,
import on B, fork) are the natural next step: the worker image carries the
full API including volumes and node ops; the control plane would need a
per-worker checkpoint service. Documented as EXPECTED.

## Why nested workers instead of fake ones

A simulated cluster cannot be killed with `docker stop`, cannot leak real
containers, cannot have real port collisions. Here the chaos is real: stop
the worker sandbox and the control plane must notice through missed
heartbeats, its applications really disappear from the browser, and when
it returns the stale copy really is running and really must be purged.
The cost is memory (each worker ~200 MB idle) - three fit beside the
outer stack on a 3.4 GB Docker Desktop VM.
