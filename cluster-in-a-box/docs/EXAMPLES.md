# cluster-in-a-box - Exercises

Prereqs: sandbox-api (publishing 9300-9349) and this control plane up.
`C=localhost:9430; A=localhost:9000; H='content-type: application/json'`.

## Level 0 - See the three engines (5 min)

```bash
curl -s -X POST $C/workers -H "$H" -d '{"count":1}'          # STARTING, then ALIVE (first time builds the image)
curl -s $C/workers | python -m json.tool
```
Then look at the same worker from the outer platform:
```bash
OS=$(curl -s $C/workers/w0 | python -c "import sys,json; print(json.load(sys.stdin)['outer_sid'])")
curl -s -X POST $A/sandboxes/$OS/exec -H "$H" -d '{"cmd":["docker","ps"]}'          # engine 2, inside the sandbox
docker exec sandbox-api-dind-1 docker ps                                               # engine 1
docker ps                                                                              # engine 0
```
**Teaches:** a worker is just a sandbox that happens to contain a Docker
engine; the outer agent still gives you exec/files/stats on it.

## Level 1 - Place and reach (5 min)

```bash
curl -s -X POST $C/sandboxes -H "$H" -d '{"name":"hello","template":"web","data_path":"/www"}'
curl -s $C/sandboxes          # url http://localhost:93xx
```
Open the URL. Count the hops in ARCHITECTURE.md. Try `curl localhost:9340/sandboxes`
(the worker's own API) and find the same sandbox under its remote id.

## Level 2 - Policies (10 min)

Create a second worker. Place six `blank` sandboxes with `least-loaded`
and watch them alternate; then six with `binpack` and watch them pile
onto one worker until it is full (capacity 6). Create a worker with
`labels: {"zone":"b"}` and place with `policy: affinity`.

## Level 3 - Drain (10 min)

Write a file into a placement through its worker's API
(`PUT localhost:934k/sandboxes/<rid>/fs/write?path=/www/x.txt`), then
`POST $C/workers/w0/drain`. The placement reappears on another worker, with
the file. `GET $C/events` tells the story. **Teaches:** the agent's tar API
is enough to migrate stateless workloads' data between nodes.

## Level 4 - Kill a worker (10 min)

```bash
curl -s -X POST $A/sandboxes/$OS/stop            # the worker vanishes
watch -n2 'curl -s localhost:9430/workers | python -c "import sys,json; print([(w[\"id\"],w[\"state\"],w[\"misses\"]) for w in json.load(sys.stdin)])"'
```
ALIVE -> SUSPECT (3 misses) -> DEAD (5). Its placements become UNKNOWN.
`POST $C/sandboxes/<id>/reschedule` re-creates one elsewhere. Then
`POST $A/sandboxes/$OS/start`: the worker returns with epoch 2 and the
stale copy is purged (`GET $C/events`, kind `fence`).

## Level 5 - Expert: break the control plane (15 min)

Stop the cluster container while workers run (`docker compose stop`),
place nothing, start it again: workers are still ALIVE, placements still
READY (the SQLite state survived; heartbeats resume). Now delete a
placement directly on a worker's API behind the control plane's back:
the monitor marks it LOST within a few seconds. Finally reduce
`WORKER_MAX_SANDBOXES` to 1 in `.env`, restart, and watch `binpack`
refuse (503) once every worker is full.
