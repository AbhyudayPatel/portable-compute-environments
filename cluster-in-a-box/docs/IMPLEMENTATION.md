# cluster-in-a-box - Implementation Notes

## Files

```
cluster-in-a-box/
+-- docker-compose.yml     the `cluster` control plane (joins sandbox-api_sandnet)
+-- .env.example           ports, bases, heartbeat thresholds, worker capacity
+-- control/
|   +-- Dockerfile         python:3.12-slim + fastapi/uvicorn/requests
|   +-- app/main.py        workers, placements, policies, heartbeat + monitor loops, drain, reschedule, fence
+-- scripts/  start/stop/reset.ps1, verify.sh (31 assertions)
+-- docs/     ARCHITECTURE . EDGE-CASES . EXAMPLES . SECURITY . this file
```

In sandbox-api (the worker side):

```
api/worker/Dockerfile           docker:27-dind + python + the API + agent + coreapp context
api/worker/worker-entrypoint.sh dockerd -> agent proxy -> uvicorn
api/app/templates.py            template `worker` (privileged, allow_privileged); WORKER_DIR context assembly
api/app/engine.py               options.extra_ports, options.env, privileged passthrough (gated)
docker-compose.yml              dind publishes CLUSTER_PORTS (9300-9349)
```

## Build story

1. The worker image reuses the stock API unchanged: only environment
   differs (`DOCKER_HOST` unix socket, `NODE_URL` localhost,
   `SANDBOX_HOST` 127.0.0.1). Proof that the single-node design was
   already a "node".
2. `_tar_dir` learned to assemble a context from several directories
   (the API package, the CoreApp context, the agent binary) and to keep
   executable bits, because the worker's entrypoint and the agent must be
   executable inside the image.
3. The control plane is deliberately small (one file): the interesting
   logic is `pick_worker`, `migrate`, `heartbeat_loop` and `fence`.
4. verify.sh stops a worker through the outer API to model death and
   starts it again to model return - real chaos with real containers.

## Bugs found while building

(Recorded after the first full battery run; see the RESULT line there.)

## Verified

- [x] `bash scripts/verify.sh` - see the RESULT line of the latest run
      (nested workers alive with their own dockerd, three-level identity
      ports, least-loaded / binpack / affinity, drain with data migration,
      worker death -> DEAD -> reschedule, return -> epoch + purge,
      teardown).
