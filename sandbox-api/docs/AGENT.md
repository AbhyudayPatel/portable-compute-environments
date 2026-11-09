# sbx-agent - the in-sandbox runtime agent

> One small static binary that lives inside every sandbox container and
> gives the control plane a proper control channel: durable exec sessions,
> files, resource stats, state fingerprints, and (on desktops) a GUI API.
> This document is the complete protocol reference plus the reasoning
> behind each design decision. Read it top to bottom once; after that it
> works as a lookup table.

```
                  +------------------------ dind container -----------------------+
 api  --HTTP-->   |  sbx-agent proxy :7799                                         |
 (python)         |     /sbx/<inner-ip>/<path>  ------>  http://<inner-ip>:7700/  |
                  |     /node/exec, /node/info  (privileged, X-Node-Token)         |
                  |                                                                |
                  |   +-- sandbox network sbx-<id> ------------------------------+ |
                  |   |  container A            container B                     | |
                  |   |  PID 1 = sbx-agent init  PID 1 = sbx-agent init          | |
                  |   |     |- workload (httpd)     |- workload (postgres)        | |
                  |   |     '- sessions...          '- sessions...                | |
                  |   |  :7700 agent API           :7700 agent API              | |
                  |   +-------------------------------------------------------+ |
                  +----------------------------------------------------------------+
```

## 1. Why an agent at all (the problem with `docker exec`)

v1 of the platform ran commands with `docker exec` through the engine API.
That works for a demo and fails for a platform:

| Need | `docker exec` | sbx-agent |
|---|---|---|
| Kill a command that timed out | cannot (exec has no kill; the process ran on) | process group gets TERM then KILL |
| Resume output after the client (or the API) restarts | impossible, the stream is gone | `?since=<seq>` replays from a ring buffer |
| Interactive shell with a terminal | awkward raw-socket multiplexing | `tty:true` sessions with resize |
| Files in / out | `put_archive` on the *engine* API | tar + raw file endpoints, jailed |
| What is the sandbox's state? | nothing | merkle fingerprint of any path |
| Resource usage | engine stats endpoint, coarse | cgroup v2 files incl. PSI |
| Future microVM backend | there is no docker exec in a VM | same HTTP surface over vsock |

Everything the control plane does to a sandbox *at runtime* therefore goes
through the agent. The engine API is only used for lifecycle (create,
start, stop, remove, events).

## 2. How it gets into a container

1. The API builds a tar in memory containing `sbx/agent` (0755) and any
   extra files an extension wants next to it (for example an egress CA).
2. After `containers.create()` and before `start()`, the tar is uploaded
   with `put_archive("/")`. Every image therefore gets `/sbx/agent` without
   rebuilding it. The binary is ~6 MB, fully static (Go, `CGO_ENABLED=0`),
   so it runs on alpine, busybox, debian, distroless alike.
3. The container's command is rewritten:

   ```
   entrypoint = ["/sbx/agent", "init", "--"]
   command    = <image Entrypoint> + (<spec command> or <image Cmd>)
   ```

   The agent becomes PID 1 and starts the workload as its child. This is
   the `init` mode. Nothing about the workload changes: same argv, same
   environment, same stdio.
4. Images whose own init must stay PID 1 (s6-overlay desktops) use the
   `exec` mode instead: the API starts the container unchanged and runs
   `docker exec -d /sbx/agent serve` once. The API's `start` re-launches it
   after a stop.

Which mode a container uses is declared per template (`agent_mode`).

## 3. Being PID 1 correctly

A PID 1 has duties the workload must never notice:

- **Signal forwarding.** `docker stop` sends SIGTERM to PID 1. The agent
  forwards TERM, INT, HUP, QUIT, USR1, USR2 and WINCH to the workload. When
  the workload exits, the agent exits with the same code, so the container
  behaves exactly like one without the agent (verified: `docker stop` of a
  `sleep infinity` workload exits 143 in 0.5 s).
- **Zombie reaping.** Orphans re-parent to PID 1. One goroutine owns
  `wait4(-1, WNOHANG)` and runs on every SIGCHLD. Go's `exec.Cmd.Wait`
  would race it for the same statuses (one of them gets ECHILD), so the
  agent never calls `Wait`; it registers interest in a pid and the reaper
  hands the exit code over a channel. If a process exits before anyone
  registers, its status is parked in an "unclaimed" map (`reaper.go`).
- **Session cleanup.** When the workload exits, every session's process
  group is killed before the agent exits.

## 4. The HTTP API (agent modes)

Base URL from the control plane's point of view:
`http://dind:7799/sbx/<inner-ip>/`. Every request needs
`Authorization: Bearer <token>` where the token is generated per sandbox at
create time (`SBX_AGENT_TOKEN` in the container's environment). An empty
token disables auth (never the case in the platform).

### 4.1 Health

`GET /healthz` -> `{ok, version, pid, pid1, uptime_seconds, fs_root, sessions}`

### 4.2 Sessions (the core feature)

A **session** is a process started by the agent plus a ring buffer of
everything it wrote. Sessions outlive HTTP connections; the only way a
session disappears is `DELETE`, the TTL after exit (`SBX_AGENT_SESSION_TTL`,
default 3600 s) or the agent's own shutdown.

| Method | Path | Body / query | Reply |
|---|---|---|---|
| POST | `/sessions` | `{cmd:[...], env:{}, cwd, tty, cols, rows, timeout_seconds, buffer_bytes, stdin}` | 201 session |
| GET | `/sessions` | | `{sessions:[...]}` |
| GET | `/sessions/{id}` | | session |
| GET | `/sessions/{id}/stream` | `?since=N&follow=1&timeout=S` | NDJSON frames |
| POST | `/sessions/{id}/stdin` | raw bytes, `?eof=1` closes | `{written}` |
| POST | `/sessions/{id}/signal` | `{signal:"TERM"|"KILL"|"INT"|...|"9"}` | `{signalled}` |
| POST | `/sessions/{id}/resize` | `{cols, rows}` | `{cols, rows}` |
| DELETE | `/sessions/{id}` | | 204 (kills if running) |
| POST | `/exec` | session body + `max_bytes` | one-shot result |

A session object:

```json
{"id":"e9ab1352b0e0","cmd":["sh"],"tty":true,"pid":32,"state":"running",
 "exit_code":null,"timed_out":false,"started_at":"...","last_seq":18,
 "dropped_frames":0,"bytes_total":108,"timeout_seconds":0}
```

**Frames.** Every chunk the process writes becomes one frame with a
monotonically increasing `seq` (starting at 1):

```
{"seq":1,"s":"o","d":"aGVsbG8K"}      stdout, data base64
{"seq":2,"s":"e","d":"ZXJyCg=="}      stderr (non-tty only; a pty has one stream)
{"seq":3,"s":"x","code":0}            exit frame, always the last
```

**Streaming rules** (`/stream`):

- `since=N` returns frames with `seq > N`. A fresh client uses `since=0`.
- `follow=1` keeps the connection open until the exit frame was sent or
  `timeout` seconds (default 30) pass; the client simply calls again with
  the last `seq` it saw. `follow=0` returns what is buffered and closes.
- If the ring buffer had to evict frames older than `since`, the first
  line is `{"gap":true,"oldest":<seq>,"since":N}` so the client KNOWS it
  missed data instead of silently getting a hole.
- Per-session buffer: `buffer_bytes` (default `SBX_AGENT_BUFFER` = 1 MiB).
  `dropped_frames` on the session counts evictions.

**Timeouts.** `timeout_seconds > 0` arms a timer: on expiry the agent
sends SIGTERM to the process group, waits 5 s, then SIGKILL, and marks the
session `timed_out:true`. The exit frame still arrives (code 143 or 137).

**Process groups.** Non-tty sessions start with `Setpgid`; tty sessions
become session leaders (`Setsid`). Signals go to `-pid`, so `sh -c "a | b"`
dies as a whole. This is what makes "kill on timeout" trustworthy.

**One-shot exec** (`POST /exec`) is sugar: start a session, collect frames
until exit, reply with `{exit_code, timed_out, truncated, stdout, stderr,
bytes_total, session_id}`. `truncated` refers only to this reply
(`max_bytes` per stream, default 64 KiB); the session keeps the full output
up to its ring size, retrievable by `session_id`.

### 4.3 Files

All paths are absolute and jailed under `SBX_AGENT_FS_ROOT` (default `/`).
The jail resolves symlinks of the deepest existing ancestor before
deciding, so `/workspace/link -> /etc` cannot escape a `/workspace` root.

| Method | Path | Notes |
|---|---|---|
| GET | `/fs/list?path=` | `{path, entries:[{name,type,size,mode,mtime,link}]}` |
| GET | `/fs/stat?path=` | one entry |
| GET | `/fs/read?path=&max=` | raw bytes; 413 if larger than `max` (default 1 MiB) |
| PUT | `/fs/file?path=&mode=0644` | raw body -> file, parents created |
| POST | `/fs/mkdir?path=` | `mkdir -p` |
| DELETE | `/fs?path=` | `rm -rf` (refuses `/`) |
| GET | `/fs/get?path=` | tar stream of a file or a whole tree |
| PUT | `/fs/put?path=` | tar stream extracted UNDER path; entries are cleaned so `../../etc/x` lands at `<path>/etc/x` (verified) |

### 4.4 Introspection

- `GET /ps` - `/proc` scan: pid, ppid, state, rss_bytes, cmd.
- `GET /stats` - cgroup v2 of the container (`cpu.stat`, `cpu.max`,
  `memory.current/max/high/peak/stat/events`, `pids.current/max`,
  `io.stat`, and the PSI pressure files), `loadavg`, disk usage, uptime.
  cgroup v1 fallback reads the classic files.
- `GET /fingerprint?path=&exclude=a,b&detail=1` - the merkle fingerprint
  (section 5).

### 4.5 GUI (desktop template only)

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/gui/display` | | `{width,height,display}` via `xdotool getdisplaygeometry` |
| GET/POST | `/gui/screenshot` | | PNG (`scrot`, else ImageMagick `import`, else `xwd`) |
| POST | `/gui/action` | `{type, x, y, x2, y2, button, text, keys, count, direction}` | types: `move click dblclick rightclick type key scroll drag` |
| GET | `/gui/windows` | | visible windows with geometry, active flag |

`key` uses xdotool syntax (`ctrl+l`, `Return`, `alt+F4 Tab`). All commands
run with `DISPLAY=$SBX_DISPLAY` (default `:1`) and a 15 s cap. Verified
flow: launch Chromium, `ctrl+l`, type a URL, `Return`, screenshot shows the
page, window title changes.

## 5. Fingerprints

```
file    hash = sha256(content)
symlink hash = sha256("link:" + target)
dir     hash = sha256( for each child sorted by name:
                        name NUL type NUL mode NUL childhash LF )
```

Two trees with the same root hash have identical content, layout and
permissions. mtimes are ignored on purpose: they change on every checkout
or copy and say nothing about state. `exclude` takes comma-separated names
(`node_modules`, `.git` - matched against any path component) or path
prefixes (`build/tmp`). With `detail=1` every entry is listed so the
control plane can diff two sandboxes file by file (`GET /diff` in the
API).

## 6. Proxy mode (inside dind)

```
sbx-agent proxy      SBX_NODE_ADDR=0.0.0.0:7799  SBX_NODE_TOKEN=<secret>
```

- `/sbx/<ip>/<rest>` - reverse proxy to `http://<ip>:7700/<rest>` with
  streaming flush (`FlushInterval: -1`) so follow streams are not
  buffered. The caller's `Authorization` header passes through untouched.
  The dind container hosts every inner bridge, so it can dial any inner IP
  directly; the control plane needs exactly one engine-side port for all
  control traffic, and never a published port per sandbox.
- `POST /node/exec {cmd, stdin, timeout_seconds}` - run a command in the
  dind namespace (root). This is how checkpoints mount overlays and how
  egress policy writes iptables rules. Protected by `X-Node-Token`.
- `GET /node/info` - kernel, cgroup version, presence of `runsc`,
  `iptables`, overlay/btrfs/xfs support, disk usage of `/var/lib/docker`.

## 7. Configuration

| Variable | Mode | Default | Meaning |
|---|---|---|---|
| `SBX_AGENT_ADDR` | init/serve | `0.0.0.0:7700` | listen address |
| `SBX_AGENT_TOKEN` | init/serve | empty | bearer token (platform always sets one) |
| `SBX_AGENT_BUFFER` | init/serve | 1048576 | ring buffer bytes per session |
| `SBX_AGENT_SESSION_TTL` | init/serve | 3600 | seconds to keep exited sessions |
| `SBX_AGENT_MAX_SESSIONS` | init/serve | 64 | cap on live + retained sessions |
| `SBX_AGENT_FS_ROOT` | init/serve | `/` | jail for `/fs/*` and `/fingerprint` |
| `SBX_DISPLAY` | init/serve | `:1` | X display for `/gui/*` |
| `SBX_XAUTHORITY` | init/serve | unset | `XAUTHORITY` for X clients if the server needs it |
| `SBX_NODE_ADDR` | proxy | `0.0.0.0:7799` | listen address |
| `SBX_NODE_TOKEN` | proxy | required | token for `/node/*` |

## 8. Security model

- The agent runs as root inside the container (it must spawn anything and
  read any file); its blast radius is the container, which is already the
  isolation unit. Under `isolation=gvisor` it runs inside the gVisor
  kernel like everything else.
- Only reachable from the inner bridge and the dind namespace. From the
  Windows host nothing can reach :7700 (not published). The proxy :7799 is
  reachable only on the compose network.
- Per-sandbox bearer token: a compromised sandbox cannot drive another
  sandbox's agents (different tokens), only its own siblings (same trust
  domain anyway).
- The file jail is a policy knob for templates, not a security boundary
  against root inside the container.
- `/node/*` is root on the engine host (the privileged dind container):
  the token must be treated as a secret; it never leaves the compose
  network.

## 9. Source map

```
sandbox-api/agent/
  main.go         modes + usage
  init.go         PID 1 supervision, signal forwarding, serve mode
  reaper.go       the single wait4() owner
  sessions.go     ring buffer, sessions, pty, timeouts, process groups
  server.go       routes, auth, JSON helpers, /stream, /exec
  fs.go           jailed file API, tar in/out
  fingerprint.go  merkle walk
  stats.go        cgroup v2/v1, /proc, PSI
  gui.go          xdotool / scrot wrappers
  proxy.go        engine-side relay + node operations
  Dockerfile      static build (also embedded as a stage in api/ and dind/)
```

Build locally without the platform:

```
cd sandbox-api/agent
docker build -t sbx-agent:local .
# smoke test as PID 1:
docker run -d --name t -p 17700:7700 -e SBX_AGENT_TOKEN=t0k \
  --entrypoint /sbx-agent sbx-agent:local init -- sleep infinity
curl -H 'Authorization: Bearer t0k' localhost:17700/healthz
```
(The scratch image has no shell; use an alpine-based test image as in
docs/IMPLEMENTATION.md for richer experiments.)
