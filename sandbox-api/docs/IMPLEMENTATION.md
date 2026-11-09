# sandbox-api v2 - Implementation Notes

The files, what each does, the build story, and - the most useful part -
the bugs found while building, including three the simulator found that no
hand-written test had.

## Files

```
sandbox-api/
+-- docker-compose.yml        dind (custom image) + api; identity port range; sbx-state volume
+-- .env.example              API_PORT, POOL_*, MAX_SANDBOXES, intervals, DEFAULT_ISOLATION, SBX_NODE_TOKEN
+-- agent/                    the in-sandbox agent (Go)  -> docs/AGENT.md
|   +-- main.go init.go reaper.go sessions.go server.go fs.go fingerprint.go stats.go gui.go proxy.go
|   +-- Dockerfile            standalone build (scratch image with the binary)
+-- dind/
|   +-- Dockerfile            docker:27-dind + agent (build stage) + gVisor (pinned, sha512-verified)
|   +-- daemon.json           registers the runsc runtime
|   +-- entrypoint.sh         starts `sbx-agent proxy`, then the stock dockerd entrypoint
+-- api/
|   +-- Dockerfile            agent build stage + python:3.12-slim + app + coreapp/ + desktop/
|   +-- requirements.txt      fastapi, uvicorn, docker, requests, sse-starlette
|   +-- coreapp/              synced copy of ../coreapp (scripts/sync-coreapp.*) - build contexts
|   +-- desktop/              Dockerfile + custom-cont-init.d for the desktop image (built inside dind)
|   +-- app/
|       +-- config.py         every tunable, env-driven
|       +-- clock.py          the injectable clock
|       +-- bus.py            in-process pub/sub for SSE
|       +-- store.py          SQLite (class-based), events with global cursor, dual deadlines
|       +-- engine.py         dockerd: images, agent injection + PID-1 wrap, runtime, readiness, events
|       +-- agent.py          AgentClient (via proxy) + NodeClient
|       +-- core.py           the lifecycle state machine (no HTTP/threads/clock)
|       +-- informer.py       watch / resync / reaper threads
|       +-- templates.py      web, blank, coreapp, desktop; content-hashed image tags
|       +-- fingerprint.py    DB fingerprints + diffs
|       +-- main.py           routes
|       +-- static/           console v2 (index.html, app.js, style.css)
+-- scripts/  start/stop/reset.ps1, sync-coreapp.ps1/.sh, verify.sh (89 assertions), demo.ps1
+-- docs/     ARCHITECTURE . AGENT . EDGE-CASES . EXAMPLES . SECURITY . this file
```

## Build story (in the order it happened)

1. **The agent first.** Written as a Go module, compiled in a `golang:1.23-alpine`
   stage, smoke-tested as PID 1 in a throwaway alpine image before any
   platform code changed: exec with exit codes, timeout killing the
   process group (no leftover `sleep`), pty session with resize and
   resume by `since`, non-tty stdin + SIGINT, ring-buffer gap reporting,
   file API round trips, tar escape neutralisation, fingerprint change
   on a one-byte edit, cgroup stats, `docker stop` exiting 143 in 0.5 s.
2. **The engine image.** gVisor's download layout had changed: `latest/`
   only carries tarballs and the tarball now ships a `gvisor-bin/`
   directory of sidecars (`gvisor_sentry`, ...) that `runsc` expects next
   to itself. Extracting only the two classic binaries produced
   `sidecar "gvisor_sentry" not usable`. Fix: pin a dated release, verify
   its sha512, extract the whole layout. Then a standalone dind test
   proved: `uname -r` = `4.19.0-gvisor`, networking and published ports
   work under runsc on WSL2, Postgres starts under runsc.
3. **Feasibility probes inside dind** (kept as findings for later
   projects): `--internal` networks are reachable from the dind namespace
   but their published ports are NOT reachable from the host (so egress
   policy must use iptables, not internal networks); overlayfs mounts work
   on the ext4 volume but not on the container's own overlay rootfs (hence
   the `sbx-state` volume); `cp --reflink` is unsupported on ext4 (forks
   copy the upper layer instead).
4. **The Python rewrite**: class-based store, injectable clock, core
   extracted from the routes, informer, templates from files, content-
   hashed tags, canonical `coreapp/` at the repo root.
5. **verify.sh v2** (89 assertions) and the simulator (`../sandbox-sim`)
   were written together; the simulator found bugs 6-8 below before the
   bash battery ever ran green.

## Bugs found while building (all fixed, all covered)

1. **MSYS path mangling in tests.** Git Bash rewrites `/app/...` arguments
   of `docker exec` into `C:/Program Files/Git/app/...`. verify.sh avoids
   absolute Linux paths in host-side commands and pipes JSON through
   python's stdin instead of temp files.
2. **gVisor sidecar layout** (see build story 2).
3. **Desktop init hook silently skipped.** linuxserver's init logs
   `90-sandbox-desktop.sh: is not an executable file` and moves on; a
   build context uploaded from Windows carries no exec bit. Fix:
   `RUN chmod +x /custom-cont-init.d/*` in the desktop Dockerfile. Symptom
   had been "chromium-browser: No such file".
4. **Health probe raced the backend.** `READY` used to mean "nginx answers
   `/`"; the first `/api/health` call could still hit a starting uvicorn.
   Templates now declare their real health path (`/api/health`,
   `/health`) and readiness probes that.
5. **`docker compose up --build api` recreated dind.** When the dind image
   id changes, compose recreates the service and every inner container
   comes back stopped. Scripts use `build api` + `up -d --no-deps api`.
6. **(simulator) CREATING rows orphaned forever.** If the API boots while
   dind is down, the boot reconcile is skipped and periodic resyncs
   deliberately ignore CREATING rows (a provision thread might own them).
   Rows left CREATING by a crash were therefore never cleaned up. Found as
   "engine/DB divergence at quiescence" in 5 of 30 seeds. Fix:
   `boot_pending` - the first resync that reaches the engine runs in boot
   mode.
7. **(simulator) Boot-mode reconcile rolled back a live create.** After
   fix 6, a resync in boot mode could run while a *legitimate* provision
   was in flight and roll it back; the generator then kept creating
   containers for a row that was now FAILED/DELETED ("DELETED row still
   has engine resources", seed 20). Fix: `core.in_flight` ownership set +
   the generator re-checks its row between steps.
8. **(live) The 20 ms window.** verify.sh section 13 then saw a create
   rolled back 20 ms after insertion: `in_flight` was registered when the
   provision *thread* started, not when the row was inserted, and a
   periodic resync hit the gap. Fix: register in `create()` itself. The
   simulator had not modelled the gap (its generator is created in the
   same step); the live battery did - the two test styles are
   complementary.
9. **(simulator) FAILED rows keeping resources.** With the engine down
   during a rollback, a FAILED row could keep containers; reconcile
   skipped FAILED rows. Now it removes their leftovers.
10. **Simulator non-determinism.** Sandbox ids came from `uuid4` and list
    order tied on virtual timestamps, so replays diverged. Ids and tokens
    became injectable factories (`Core(id_factory=...)`).
11. **Simulator harness ordering.** A TTL invariant that *reaps* while a
    later invariant still used the pre-reap snapshot produced a false
    "divergence"; mutating checks now run last.
12. **Fake engine fidelity.** `stop` did not emit `die` events, so a
    mutation test for the stop/informer race stopped catching the bug;
    the fake now emits what dockerd emits.

## Observations worth keeping

- gVisor (`systrap`, no KVM) on WSL2: a `web` sandbox becomes READY in
  about the same time as runc; `coreapp` takes roughly twice as long (apt-
  free images, but Postgres init is syscall-heavy).
- Docker Desktop's VM here has 3.4 GB RAM; `desktop` sandboxes use about
  500 MB each at idle plus Chromium. The base image is 3.9 GB in the inner
  engine's volume.
- A content-hashed tag doubles as a build-cache key: unchanged `coreapp/`
  never rebuilds; any edit rebuilds exactly the changed image.

## Character encoding decision

Unchanged from v1: all files are pure ASCII (box drawings use `+ - |`,
arrows are `->`). Windows tools and charset-less HTTP responses mangle
anything else.

## Verified end to end

- [x] `bash scripts/verify.sh` - **89/89** across 17 sections (health,
      templates, web via agent, idempotency/validation, exec semantics,
      durable sessions, files, fingerprints + diff, coreapp build +
      roles + db fingerprint + stop/start, gVisor, DEGRADED + heal,
      out-of-band removal, pool exhaustion, TTL across restart, delete
      idempotency, restart reconcile, SSE)
- [x] `../sandbox-sim`: **76/76** pytest (60 seeds + 12 scenario tests +
      4 mutation tests) and **500 seeds x 300 steps, 0 failures**
- [x] desktop template: build inside dind, KasmVNC through the identity
      port, GUI API drives Chromium to a page and screenshots it
- [x] console v2: SSE live log, resumable terminal, files, fingerprint
