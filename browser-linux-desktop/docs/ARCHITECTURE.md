# Architecture — Full Linux OS in the Browser

## The big idea

Projects 1 and 2 put an **editor** in your workflow. This project puts an
entire **operating system** in your browser tab:

```
Your browser
    │  HTTP/WebSocket (KasmVNC protocol)
    ▼
┌─────────────────────────────────────────────┐
│ desktop container (the machine)             │
│                                             │
│   ┌─────────────────────────────────────┐   │
│   │  XFCE desktop session               │   │
│   │   ├─ xfwm4 (window manager)         │   │
│   │   ├─ xfce4-panel, xfdesktop         │   │
│   │   └─ your apps: VS Code, Chromium,  │   │
│   │      terminal, thunar...            │   │
│   └──────────────┬──────────────────────┘   │
│                  │ X11 protocol             │
│                  ▼                          │
│   KasmVNC server (virtual display :1)       │
│                  │ renders pixels           │
│                  ▼                          │
│   built-in web client (HTTP :3000)          │
└─────────────────────────────────────────────┘
```

There is no real monitor. KasmVNC creates a **virtual display**, XFCE and
the apps draw onto it, KasmVNC turns the pixels into an efficient stream,
and its web client in your browser renders them — while your keyboard and
mouse events travel back the same channel. That is the whole trick:
*the GUI is a video stream with input events*.

## Why this base

The image is built on `linuxserver/webtop:debian-xfce` rather than
hand-wiring Xvfb + VNC + noVNC:

- KasmVNC is significantly better than classic VNC + noVNC (modern
  encoding, smoother, handles resizing).
- The linuxserver init system (s6-overlay) gives us
  `/custom-cont-init.d/` hooks — clean place for our setup script.
- `/config` home persistence and PUID/PGID user mapping come free.
- **Debian, not Ubuntu**: Ubuntu's Chromium is a snap (needs snapd, which
  does not work in containers); Debian ships it as a normal package.

## Components

| Container | Role | Key detail |
|-----------|------|-----------|
| `desktop` | The Linux machine (XFCE + apps) | web GUI on internal :3000, published as :8080 |
| `backend` | Company API | uvicorn `--reload` over `/workspace/core-app/backend` |
| `frontend` | Company UI | nginx serves `/srv/workspace/core-app/frontend`, proxies `/api/*` |
| `db` | PostgreSQL 16 | `pgdata` volume |
| `gitserver` | "Company Git" | `git daemon`, seeds `core-app.git` once into `git-data` |
| `repo-init` | Bootstrap | clones repo into `workspace-data`, `chown`s to PUID/PGID |

Volumes: `workspace-data` (repo), `desktop-config` (home dir),
`git-data` (Git server), `pgdata` (database).

Note the separation: **the machine's personality** (`desktop-config`:
VS Code settings, extensions, git identity, desktop files) survives even a
`reset.ps1`, while **the work** (`workspace-data`) is disposable because
Git is the source of truth.

## Boot sequence

```
gitserver (seed bare repo on first boot, serve git://)
   │ healthy
   ▼
repo-init (clone → workspace-data, chown to desktop uid)
   │ completed_successfully
   ▼
desktop ───────────────────────────── backend ──────── frontend
  │ s6 init:                            │ depends: db healthy
  │  1. /custom-cont-init.d/            │
  │     99-desktop-setup.sh             ▼
  │     (git identity, wrappers,        uvicorn --reload
  │      extensions, shortcuts)          watches workspace volume
  │  2. KasmVNC server (display :1)
  │  3. XFCE session starts
  │  4. web client serves :3000
```

## Data lifecycle

| Artifact | Volume | `stop` | `reset` | `reset-all` |
|----------|--------|--------|---------|-------------|
| Uncommitted / unpushed work | `workspace-data` | ✅ | ❌ | ❌ |
| **Pushed commits** | `git-data` | ✅ | ✅ | ❌ |
| Desktop settings, extensions | `desktop-config` | ✅ | ✅ | ❌ |
| Database | `pgdata` | ✅ | ❌ | ❌ |
| Seed content | host folder | ✅ | ✅ | ✅ |

## Gotchas discovered while building this

1. **VS Code thinks it lives in WSL.** Docker Desktop runs containers on a
   WSL2 kernel, and VS Code sniffs `/proc/version`, sees "Microsoft", and
   blocks on a "install VS Code in Windows instead" prompt. Fixed with the
   official escape hatch `DONT_PROMPT_WSL_INSTALL=1` (container env +
   wrapper script).
2. **Electron's sandbox can't create namespaces** under Docker's default
   seccomp profile (`zygote_host_impl_linux.cc FATAL`). Fixed with a
   `/usr/local/bin/code` wrapper adding `--no-sandbox`; same for
   `chromium-browser`. Rationale and trade-off in
   [`SECURITY.md`](SECURITY.md).
3. **Named volumes are pre-populated from image mount points** — an image
   `WORKDIR` deep in the mount path can break a first-time `git clone`.
   `repo-init` defensively clears a non-Git target directory first.
4. **`su abc -c` needs `HOME=/config`**, otherwise git config and VS Code
   extensions land in `/root`.
5. **Chromium needs shared memory**: `shm_size: 1gb` on the desktop service
   (Docker's default 64 MB `/dev/shm` crashes browsers).
6. **nginx caches upstream IPs at startup.** After the backend container was
   recreated (new IP), the frontend 502'd until restarted. Fixed everywhere
   with `resolver 127.0.0.11 valid=5s;` + a variable in `proxy_pass`, which
   forces request-time DNS lookups against Docker's embedded DNS.
7. **Mounted config changes don't restart processes.** Editing a bind-mounted
   `nginx.conf` does nothing until the container (or nginx) is reloaded:
   `docker compose restart frontend`.

## How the three projects compare

| | P1 `local-vscode-devcontainer` | P2 `browser-dev-environment` | P3 `browser-linux-desktop` (this) |
|---|---|---|---|
| Interface | local VS Code | browser: VS Code only | **browser: whole OS** |
| What runs in the box | toolchain | toolchain + IDE | toolchain + IDE + desktop + apps |
| Install arbitrary software | via Dockerfile | via Dockerfile | **live, with sudo, anytime** |
| Image size / boot | small | medium | large / slower first boot |
| Feels like | VS Code | a web IDE | **a remote Linux workstation** |

All three share the same platform pattern: Git server seeds the repo →
one-shot clone into a shared volume → app services hot-reload from it →
reset keeps only what was pushed.
