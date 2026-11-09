# AgentOS - The Learning Path

> Read this if you are new to containers, or new to this repo, or both.
> It walks from "what is a container" to "deterministic simulation of a
> control plane", using only things you can run on this laptop and see
> with your own eyes. Every section ends with something to **do**.

## 0. The vocabulary (10 minutes, no typing)

| Word | What it means here | Where you will meet it |
|---|---|---|
| **container** | a process tree with its own filesystem view, network interface and resource limits, started from an *image* | everything |
| **image** | a frozen filesystem (layers) + a default command; built from a `Dockerfile` | `coreapp/`, every `Dockerfile` |
| **engine / dockerd** | the daemon that creates containers from images | Docker Desktop (engine 0), `dind` (engine 1), workers (engine 2) |
| **DinD** | Docker-in-Docker: an engine running *inside* a container, so a program can own a private engine | `sandbox-api` |
| **sandbox** | one or more containers on their own network, created by the platform, with an agent inside each | `sandbox-api` |
| **agent** | a tiny program that is process 1 inside every sandbox container and answers HTTP: run commands, read/write files, report stats | `sandbox-api/agent` |
| **identity port** | port P means the same thing at every layer: inner container -> engine -> laptop `localhost:P` | all app URLs |
| **control plane** | the service that decides what exists and drives the engine; it keeps metadata in SQLite and treats the engine as the truth | `sandbox-api`, `sandbox-scheduler`, `cluster-in-a-box` |
| **reconcile** | compare what the database says with what the engine has, and fix the database | boot and every 30 s |
| **informer** | a live subscription to the engine's events instead of polling | `sandbox-api/informer.py` |
| **overlayfs** | a filesystem that stacks read-only layers under one writable layer; forking = sharing the layers | `sandbox-checkpoint` |
| **CAS** | content-addressed storage: a file's name is its hash | `sandbox-blobs` |
| **egress** | traffic leaving the sandbox; policed by a proxy and an iptables fence | `sandbox-egress` |
| **gVisor** | a userspace kernel between a container and the real kernel | `isolation: gvisor` |
| **DST** | deterministic simulation testing: run the control plane in-process against fakes, driven by a seed | `sandbox-sim` |
| **MCP** | Model Context Protocol: the standard way agents call tools | `sandbox-mcp` |

## 1. A container is a process (20 minutes)

Start `sandbox-api` (`scripts\start.ps1`), open http://localhost:9000,
create a `blank` sandbox, open **details**, and run in the exec box:

```
ps -ef
cat /proc/1/cmdline | tr '\0' ' '
```

Process 1 is `/sbx/agent init --`. There is no init system, no systemd:
the container is just the agent and whatever it starts. Now run
`cat /sys/fs/cgroup/memory.current` - that number is the memory of *this
container only*, read straight from the kernel's accounting. Open the
"Files" panel at `/` and look around: that is the image's filesystem.

**Read next:** `sandbox-api/docs/AGENT.md` sections 2-3 (how the agent
gets in and why PID 1 matters).

## 2. Where the engine lives (20 minutes)

```
docker ps                                   # engine 0: Docker Desktop. You see dind and api.
docker exec sandbox-api-dind-1 docker ps    # engine 1: inside dind. You see your sandbox containers.
```

Two engines, two worlds. The API only ever talks to engine 1. Create a
`web` sandbox and open its URL - then trace the port in
`sandbox-api/docs/ARCHITECTURE.md` ("identity port mapping"): the same
number at every layer is a deliberate design choice, not an accident.

**Do:** `docker exec sandbox-api-dind-1 docker kill <the web container>`
and watch the console: the sandbox turns DEGRADED within a second. That
is the informer. Click **heal**.

## 3. State is the hard part (30 minutes)

Create a `coreapp` sandbox with `options.stateful=true` (console: not yet
a checkbox; use curl from `sandbox-checkpoint/README.md`). Add a task in
the app. Take a checkpoint, add another task, restore. The second task is
gone; the first is back.

Now read `sandbox-checkpoint/docs/ARCHITECTURE.md`: the overlay layers,
why the sandbox is *paused* for the copy (crash consistency), why the
manifest is written last. Then fork the checkpoint twice and change each
fork differently: two timelines from one instant, in seconds.

**Do:** the PITR exercise in `sandbox-checkpoint/docs/EXAMPLES.md` level 4
and explain to yourself why the WAL archive is needed in addition to
checkpoints.

## 4. Why the network is a policy (20 minutes)

Create a coreapp with `options.egress.mode=deny` (see
`sandbox-egress/README.md`). From inside, try to reach anything: 403 from
the proxy. Try to bypass the proxy by IP: the connection hangs and dies -
the iptables fence. Switch to `allow` with a host list, then `record`,
then `replay`: the same request now answers from the cassette with no
network at all.

**Read:** `sandbox-egress/docs/ARCHITECTURE.md` "placeholder secrets" -
the sandbox never sees the real token. Think about what this means for an
agent that gets prompt-injected.

## 5. Trust levels (15 minutes)

Create a `web` sandbox with `isolation: gvisor` and `exec` `uname -a` and
`dmesg`. You are talking to a kernel that is a userspace program. Read
`sandbox-api/docs/SECURITY.md` "isolation levels, honestly" for what this
does and does not buy you.

## 6. Scheduling is a policy too (30 minutes)

Start `sandbox-scheduler` and open http://localhost:9010. Press **herd**:
50 jobs, only 4 run at a time, the queue drains fairly, the "+aged" column
climbs on waiting jobs. Then the preemption exercise in
`sandbox-scheduler/docs/EXAMPLES.md`: a priority-9 job evicts a stateful
service, which later resumes from its checkpoint with its data.

**Read:** `sandbox-scheduler/docs/ARCHITECTURE.md` "the placement tick".
Every rule has a reason; try to find the one you would change.

## 7. Testing without the world (30 minutes)

```
cd sandbox-sim; scripts\run.ps1
```

Hundreds of randomized runs with crashes and outages, in a minute, with
no Docker. Replay one seed with `-Verbose` and read the log like a story.
Then break the core on purpose (`sandbox-sim/docs/EXAMPLES.md` level 2)
and watch the simulator find it in seconds.

**Read:** `sandbox-sim/README.md` "what it found" - the bugs listed there
were real and would have shipped. This is the single most transferable
technique in the repo.

## 8. Agents as users (20 minutes)

Start `sandbox-mcp`, add it to Claude Code (one command in its README),
and ask the assistant to build something in a sandbox. Watch `GET /calls`
and the sandbox-api console at the same time. Every capability you used
by hand in sections 1-6 is now a tool an agent can call.

## 9. Many machines on one machine (30 minutes)

Start `cluster-in-a-box`, create two workers, place sandboxes, kill a
worker, watch the control plane notice and move work. Count the engines
(`cluster-in-a-box/docs/EXAMPLES.md` level 0). This is how the single-node
design scales out: the worker is the single node, unchanged.

## 10. Where to go from here

- Build a template: copy `web` in `sandbox-api/api/app/templates.py`,
  change the image and the health path, add a `stateful` path. The
  simulator, the batteries and the console all pick it up.
- Write a chaos experiment with the fakes in `sandbox-sim` before you
  write it against Docker.
- Read the per-project `IMPLEMENTATION.md` files for the bugs found while
  building; most teach a general lesson (path mangling, logging APIs that
  disappear, root-owned directories, the 20 ms window).
