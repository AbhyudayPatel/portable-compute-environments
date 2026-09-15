# 🐳 Nested Docker Lab (Docker-in-Docker on Windows / Docker Desktop)

A **working, verified** nested-container laboratory:

```
Windows host browser
        │  http://localhost:8080
        ▼
Docker Desktop  (WSL2 utility VM, Linux kernel, cgroup v2)
        │  publishes 8080 → outer container
        ▼
┌─ OUTER Linux container  (privileged, docker:27-dind) ──────────────┐
│  eth0 = 172.20.0.2        socat relay  0.0.0.0:8080 ─┐             │
│                                                      │             │
│  └─ INNER Docker daemon (dockerd + containerd)       │             │
│        docker0 = 172.17.0.1      ▲───────────────────┘             │
│        │                          (relays to inner :80)            │
│        └─ INNER container  (busybox httpd)                         │
│             eth0 = 172.17.0.2  :80   ← the web app lives here      │
└────────────────────────────────────────────────────────────────────┘
```

The web app runs in the **innermost** container and is reachable from the Windows
browser at **http://localhost:8080**.

> Status legend used below: **VERIFIED** = executed and observed working in this
> repo's run; **EXPECTED** = reasoned, not executed here.

---

## 1. Quick start (fresh machine)

```bash
# from Windows PowerShell / Git Bash / cmd, anywhere you like:
git clone <this repo>            # or copy the folder
cd docker-nested-lab

docker compose up --build        # builds + starts everything
# … wait for "Nested Docker environment is UP" …

# then open a browser:
#   http://localhost:8080
#   http://localhost:8080/health
```

Verify every layer:

```bash
bash scripts/verify.sh
```

Stop / clean up:

```bash
docker compose down              # stops; inner images PERSIST (named volume)
docker compose down -v           # also deletes the inner /var/lib/docker volume
```

---

## 1b. Use BOTH dockers from your terminal, and expose any inner app

### Two separate engines, both reachable from Windows

```bash
# host / outer engine (Docker Desktop):
docker ps

# INNER engine (the nested dockerd), over TCP:
docker -H tcp://localhost:2376 ps
docker -H tcp://localhost:2376 images
docker -H tcp://localhost:2376 run --rm hello-world
```
`docker ps` and `docker -H tcp://localhost:2376 ps` show **different** containers —
they are two independent daemons.

> Tip: add an alias so you don't type `-H` every time. In Git Bash:
> ```bash
> echo 'alias dock="docker -H tcp://localhost:2376"' >> ~/.bashrc && source ~/.bashrc
> dock ps        # now talks to the INNER engine
> ```

### Run an inner app and open it in Chrome at the SAME port

Ports **9000–9010** are an *identity-mapped* user range: run an app on inner port
`P`, open Chrome at `localhost:P` (same number).

```bash
# easiest — helper script (runs the app inside inner-web):
bash scripts/run-app.sh 9001 demo
#   -> open http://localhost:9001

# or do it by hand against the INNER engine:
docker -H tcp://localhost:2376 exec -d inner-web \
    sh -c 'mkdir -p /apps/x; echo "<h1>hi from :9002</h1>" >/apps/x/index.html; httpd -f -p 9002 -h /apps/x'
#   -> open http://localhost:9002
```

Why this works: the `9000-9010` block is published **1:1** to Windows, and the outer
container DNATs that block straight to `inner-web` on the *same* port. (Verified:
apps on 9000/9001/9003/9005/9010 all reached at their matching `localhost` port.)

> Only ports **9000–9010** are published, so an app on, say, inner `:8000` is NOT
> reachable from Windows. To expose other ports, widen the `ports:` range in
> `docker-compose.yml` (keep it 1:1) and the `APP_PORT_*` vars in `outer/entrypoint.sh`.

---

## 2. Files

```
docker-nested-lab/
├── docker-compose.yml      # orchestration: privileged outer, port publish, volume, healthcheck
├── outer/
│   ├── Dockerfile          # docker:27-dind + socat/curl/debug tools; ships the inner app source
│   └── entrypoint.sh       # PID 1: boots inner dockerd, builds+runs inner app, starts relay
├── inner/
│   ├── Dockerfile          # busybox + httpd (tiny, ~4 MB)
│   ├── index.html          # the visible "stack diagram" page
│   └── style.css
└── scripts/
    └── verify.sh           # layer-by-layer verification
```

---

## 3. What actually happens to a packet

```
Hop 1  Browser → http://localhost:8080
         Windows TCP stack hands the connection to Docker Desktop's
         localhost-forwarding proxy (vpnkit/com.docker on the host side).

Hop 2  → Docker Desktop WSL2 VM
         The publish rule `-p 8080:8080` (from compose `ports:`) NATs/forwards
         the connection into the OUTER container's network namespace.

Hop 3  → OUTER container  eth0 (172.20.0.2)  :8080
         `socat TCP-LISTEN:8080` accepts it.  (verified: `ss -lnt` shows 0.0.0.0:8080)

Hop 4  → INNER bridge  docker0 (172.17.0.1)
         socat opens a new connection to 172.17.0.2:80 across the inner bridge.

Hop 5  → INNER container  eth0 (172.17.0.2)  :80
         busybox `httpd` serves index.html / style.css / /health.
```

### Why an inner `-p 8080:80` is NOT enough

> A container's `-p` publish can only bind on a network **its own daemon** manages.

The **inner** daemon lives inside the **outer** container's network namespace, so
`inner-docker run -p 9090:80` binds `0.0.0.0:9090` **in the outer namespace** —
reachable from the outer container but **not** from Windows.

This was demonstrated live in this repo:

```
inner daemon published: 80/tcp -> 0.0.0.0:9090
  reachable from OUTER?   yes  (curl http://localhost:9090/health works inside outer)
  reachable from WINDOWS? no   (connection fails)        ← the crux
```

The **outer** container is the *only* layer Docker Desktop can publish to the host.
So the outer container runs an explicit **relay** (`socat 8080 → inner:80`). That
makes every hop's address explicit and keeps the path deterministic.

### Address/port at each layer

| Layer | Interface | IP | Port | Who put it there |
|-------|-----------|----|------|------------------|
| Windows | localhost | 127.0.0.1 | 8080 | Docker Desktop proxy |
| WSL2 VM | (NAT) | — | 8080 | `-p 8080:8080` publish |
| OUTER | eth0 | 172.20.0.2 | 8080 | compose `ports:` |
| OUTER | (socat) | 0.0.0.0 | 8080 | entrypoint relay |
| INNER bridge | docker0 | 172.17.0.1 | — | inner dockerd |
| INNER | eth0 | 172.17.0.2 | 80 | `httpd -p 80` |

Additional published ports:

| Windows | Reaches | Purpose |
|---------|---------|---------|
| `localhost:2376` | inner dockerd API (`tcp://127.0.0.1:2375` in outer) | drive the INNER engine from your terminal |
| `localhost:9000-9010` | `inner-web` same port (via outer DNAT) | run a user app on inner port P → Chrome `localhost:P` |

---

## 4. Verification commands (per layer)

**Layer 1 — Windows → Docker Desktop**
```bash
docker version
docker info --format '{{.OSType}}'        # must be "linux"
```

**Layer 2 — Windows → Outer container**
```bash
docker ps
docker exec nested-outer hostname
```

**Layer 3 — Outer → Inner Docker daemon**
```bash
docker exec nested-outer docker version
docker exec nested-outer docker info
```

**Layer 4 — Inner daemon → Inner container**
```bash
docker exec nested-outer docker ps
docker exec nested-outer docker inspect inner-web
```

**Layer 5 — Inner container → HTTP server (from outer)**
```bash
IP=$(docker exec nested-outer docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' inner-web)
docker exec nested-outer curl -fsS http://$IP/health
```

**Layer 6 — Windows → inner app (full path)**
```bash
curl http://localhost:8080/health         # or open in a browser
```

One-shot: `bash scripts/verify.sh`

---

## 5. Observability — inspect each layer

```bash
# Outer container network
docker exec nested-outer ip addr
docker exec nested-outer ip route
docker exec nested-outer ss -lntp

# Inner Docker bridge + networks
docker exec nested-outer docker network ls
docker exec nested-outer ip -brief addr show docker0

# Inner daemon internals
docker exec nested-outer docker info
docker exec nested-outer cat /proc/1/cgroup
docker exec nested-outer mount | grep -Ei 'cgroup|overlay|/var/lib/docker'

# The whole nested process tree (dockerd -> containerd -> shim -> httpd)
docker exec nested-outer ps aux
```

Observed in this repo's run (outer container, ~50 MB RAM, 0.2% CPU):

```
bash  /usr/local/bin/outer-entrypoint.sh        (PID 1)
dockerd  --host=unix:///var/run/docker.sock ...  (the INNER engine)
containerd --config /var/run/docker/containerd/containerd.toml
containerd-shim-runc-v2 ...                      (supervises inner container)
httpd -f -p 80                                    (the web server)
socat TCP-LISTEN:8080 ... TCP:172.17.0.2:80       (the relay)
```

---

## 6. Persistence

The inner engine's `/var/lib/docker` (images, layers, volumes, build cache) is
backed by a **named volume** (`nested-inner-docker-data`) defined in compose.

- **Chosen default: named volume.** Portable, managed by Docker Desktop, survives
  `docker compose down` + `up`, and avoids Windows-path bind-mount permission issues.
- Verified: after `docker compose down && docker compose up -d`, the inner images
  (`nested-lab-inner:1.0`, `busybox:1.36`) were still present — no re-pull.

Alternatives:

| Backing | Pros | Cons | When to use |
|---------|------|------|-------------|
| **named volume** (default) | portable, persistent, clean | opaque location | this lab |
| anonymous volume | zero config | hard to reference/clean | throwaway runs |
| bind mount | inspectable files | Windows ACL/perm friction | debugging inner storage |
| tmpfs | fastest, fully ephemeral | lost on stop, RAM-hungry | pure-ephemeral sandboxes |

To reset the inner engine completely: `docker compose down -v`.

---

## 7. Resource management

- The whole nested stack idles at roughly **50 MB RAM / <1% CPU** for the outer
  container (busybox httpd is ~4 MB). The dominant cost is the inner `dockerd` +
  `containerd`, not the web app.
- Compose sets `deploy.resources.limits: cpus=4, memory=4G` as a guardrail so a
  runaway inner build can't starve the Docker Desktop VM.
- Real ceiling = your **WSL2 VM** allocation (`%USERPROFILE%\.wslconfig`), e.g.:
  ```ini
  [wsl2]
  memory=6GB
  processors=4
  ```
- Inner logging is capped (`--log-opt max-size=5m max-file=2`) so the inner
  container can't fill disk with logs.
- Filesystem layers: each nested layer adds overlay mounts. Keep inner images
  small (busybox/alpine) — don't nest heavy base images.

---

## 8. Linux internals — what the inner daemon does and does NOT control

Docker isolates containers with kernel features. Running Docker *inside* a
container reuses the **same shared host kernel** (here, the WSL2 kernel).

### Namespaces (created per container)
- **PID** — inner containers see only their own processes (their own PID 1).
- **NET** — inner containers get their own interfaces/iptables (the `docker0` bridge, `172.17.0.0/16`).
- **MNT** — inner containers see their own overlay filesystem.
- **IPC** — separate SysV IPC / shared memory.
- **UTS** — separate hostname.

### cgroups (v2 here)
The inner daemon carves out **sub-cgroups** under the outer container's cgroup for
CPU/memory/pids limits. `/proc/1/cgroup` shows `0::/` (cgroup v2 unified).

### Capabilities / seccomp
The **outer** container runs `--privileged`, giving it the capabilities a daemon
needs (mount, net admin, etc.). The **inner** containers are still created with
the *normal* restricted capability set — they are ordinary containers.

### What the inner dockerd controls ✅
- Inner image store (`/var/lib/docker` on the named volume)
- Inner networks (its own `docker0` bridge, iptables inside the outer netns)
- Inner container lifecycle, logs, exec, inner volumes
- Its own Engine API (`/var/run/docker.sock` inside the outer container)

### What the inner dockerd does NOT control ❌
- **The kernel** — it shares the WSL2 kernel with everything. No custom kernel modules/sysctls beyond what's permitted.
- **Host/VM networking** — it can only NAT within the outer namespace; it cannot publish ports to Windows (hence the relay).
- **The outer container's cgroup root** — it operates in a delegated subtree.
- **Hardware / devices** — only what the outer container was granted.

---

## 9. Security

`--privileged` on the **outer** container is (practically) required because a
Docker daemon must create namespaces, cgroups, mounts (overlayfs), veth pairs and
iptables rules — none allowed by the default capability set.

**What it grants:** essentially all Linux capabilities, access to host devices,
and relaxed seccomp/LSM. The nested daemon could, in principle, affect the shared
kernel or escape.

**Risk scope:** this is fine for a **local learning/dev sandbox**. It is **not**
acceptable for multi-tenant or production workloads.

**Production-grade alternatives:**
- Rootless Docker (daemon runs as non-root, user namespaces).
- Sysbox runtime (runs Docker-in-Docker **without** `--privileged`, via deeper user-ns + syscall interception).
- Kubernetes + Kata Containers / gVisor (per-sandbox kernel isolation).
- Separate VMs (strongest isolation, heaviest).

---

## 10. Troubleshooting

| Symptom | Likely cause | Diagnose | Fix |
|---|---|---|---|
| `dockerd exited early` right after launch | stale `/var/run/docker.pid` from prior boot; PID reused in the namespace | `docker exec nested-outer cat /var/log/nested-lab/dockerd.log` | entrypoint now `rm -f`s the pidfile/socket before launch |
| Build aborts with `sed: unrecognized option: u` | GNU `sed -u` not in BusyBox/Alpine sed | look at build step output | removed `-u` from all `sed` in entrypoint |
| Inner API over TCP gives `EOF` / connection reset right after boot | dockerd's deliberate slow-start when binding a non-loopback IP without TLS | `docker exec nested-outer ss -lnt \| grep 2375` | inner daemon binds loopback only; a socat relay on eth0 publishes it — starts instantly |
| socat API relay fails `Address in use` | binding socat `0.0.0.0:2375` overlaps dockerd's `127.0.0.1:2375` (wildcard ⊇ specific) | check outer `ss -lnt` for two 2375 listeners | socat binds the outer **eth0 IP** specifically, not `0.0.0.0` |
| `docker -H ... exec` returns empty but `ps`/`run` work | `exec` uses a hijacked/upgraded HTTP stream; the socat TCP relay doesn't carry its output back cleanly | compare `docker -H ... ps` (works) vs `... exec` (silent) | run exec via the unix socket instead: `docker exec nested-outer docker exec inner-web ...` (the helper script already does this) |
| Inner build can't pull `busybox` | inner bridge has no DNS/NAT | `docker exec nested-outer docker pull busybox:1.36` | ensure compose `dns:` set + `--ip-masq=true`; check outer egress |
| `overlay2` not supported | kernel/fs can't nest overlayfs | inner `docker info --format '{{.Driver}}'` | set `STORAGE_DRIVER=vfs` in compose (slower, more disk) |
| Windows can't reach `:8080` but outer can | relay not up / wrong inner IP | `docker exec nested-outer ss -lnt \| grep 8080` | entrypoint re-resolves inner IP before starting relay |
| Port already in use on Windows | something else on 8080 | `netstat -ano \| findstr :8080` | change `ports:` mapping, e.g. `"8081:8080"` |
| Inner container reachable only from outer | expected — that's the nesting | see §3 demo | reach it from Windows via the relay port |
| Healthcheck flaps on first boot | inner build/pull takes time | `docker inspect -f '{{.State.Health.Status}}' nested-outer` | `start_period: 40s` already set; give it time |
| Docker Desktop not starting | service stopped / WSL issue | `Get-Service com.docker.service` | start Docker Desktop; `wsl --update` if WSL2 broken |
| Wrong mode (Windows containers) | OSType=windows | `docker info --format '{{.OSType}}'` | switch Docker Desktop to Linux containers |

---

## 11. Alternative architectures

The same use case (web app reachable from Windows) can be built three ways:

```
A) DinD (this lab)          B) Socket mount (DooD)          C) Remote Docker
Windows                     Windows                         Windows
 └Docker Desktop            └Docker Desktop                 └Docker Desktop
   └Outer (privileged)        └Outer (NOT privileged)         └Outer (CLI only)
     └INNER dockerd             │ mount /var/run/docker.sock    │ docker -H tcp://host
       └INNER container         ▼                               ▼
                              HOST dockerd                    HOST dockerd
                                └"inner" is a SIBLING           └"inner" is a SIBLING
```

| Property | **DinD (A)** | **Socket mount (B)** | **Remote Docker (C)** |
|---|---|---|---|
| Isolation | **Best** — own daemon/net/storage | None — inner is a host sibling | None — host sibling |
| Security | Contained (but `--privileged`) | **Weakest** — socket = root on host | **Weakest** — remote = root on host |
| Complexity | Medium | **Low** | Medium (TLS/auth) |
| Performance | Good (one extra netns hop) | Best | Good |
| CI/CD usefulness | **High** (isolated builds) | Common but risky | High (shared runners) |
| Networking | Nested (needs relay) | Flat (host bridge) | Flat |
| Persistence | Own `/var/lib/docker` | Host's store | Host's store |
| Production suitability | Only with Sysbox/rootless | No | With TLS+auth |

**Why DinD here:** the task wants *true* nesting and isolation. Socket-mounting
would *appear* nested but the "inner" container would actually be created by the
host daemon as a **sibling** of the outer container on the host bridge — not nested
at all.

To try **B** yourself: run the outer container with
`-v //var/run/docker.sock:/var/run/docker.sock` and install only the Docker CLI
inside; any container it launches will live on the host.

---

## 12. Next steps (progressively harder)

1. **Add a 2nd inner container** (e.g. a small API) and connect them on a custom
   inner network; curl between them from inside the outer container.
2. **Inner docker-compose**: install the compose plugin in the outer image and run
   a multi-service inner stack (web + redis) with one inner `docker compose up`.
3. **Persistence experiment**: write data into an inner *named volume*, `docker
   compose down`, `up`, and prove the data survived.
4. **Rebuild & reload**: change `inner/index.html`, `docker compose up --build`,
   and watch only the inner image rebuild.
5. **Network forensics**: run `tcpdump` (add to image) on the outer `eth0`, the
   inner `docker0`, and inside the inner container; correlate a single request.
6. **Resource limits**: set a memory limit on the inner container, then exceed it
   and observe the cgroup OOM kill.
7. **Rootless DinD**: convert the inner daemon to rootless Docker to drop
   `--privileged` from the *inner* engine.
8. **Sysbox**: run the outer container under the Sysbox runtime to remove
   `--privileged` from the *outer* container.
9. **Per-user sandboxes**: spin up N outer containers on ports 8081..808N, each a
   fully isolated nested environment.
10. **CI worker**: turn the outer container into a CI runner that builds/tests
    images using its private inner daemon, then tears itself down.

---

## 13. Scale-out vision

This lab is the seed for a **sandbox platform**:

```
Windows / Docker Desktop
   └─ Outer sandbox (privileged DinD)  × N  (one per user / job / agent)
        └─ Inner engine
             ├─ frontend   ├─ backend   ├─ database
             ├─ redis      └─ worker    …   (full microservice env, isolated)
```

- **Ephemeral environments / CI workers** → DinD (A) for isolation.
- **Browser-accessible sandboxes** → DinD + a relay per exposed port (as here).
- **High-density, lower isolation** → socket-mount (B) or a shared host daemon (C).

For many concurrent isolated sandboxes, the natural evolution is **Sysbox or
Kata/gVisor** outer runtimes, giving DinD-grade isolation without `--privileged`.
