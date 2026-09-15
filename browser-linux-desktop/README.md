# Project 3 — Full Linux OS in the Browser

The most complete implementation: **a whole Debian XFCE desktop streamed
into your browser**. Not just an IDE — an actual Linux machine with a
window manager, VS Code, Chromium, terminal, file manager, text editor,
git and python, running entirely inside Docker.

```
┌──────────────────────────── YOUR WINDOWS LAPTOP ───────────────────────────┐
│                                                                            │
│   Browser ──► http://localhost:8080 ──► KasmVNC web client                 │
│                                         (streams the desktop, sends        │
│                                          keyboard/mouse back)              │
│                                                                            │
│   Docker Desktop                                                           │
│      ▼  linux-desktop-env (docker compose)                                 │
│  ┌──────────────────────────────────────────────────────────────────┐      │
│  │                     Linux environment (devnet)                    │      │
│  │                                                                  │      │
│  │   desktop ──────────── the machine you work in                   │      │
│  │   ┌────────────────────────────────────────────────────────┐     │      │
│  │   │ Debian 13 + XFCE 4.20                                   │     │      │
│  │   │  🖥  XFCE desktop (KasmVNC → browser)                  │     │      │
│  │   │  💻 xfce4-terminal      📁 thunar (file manager)        │     │      │
│  │   │  📝 mousepad            🌐 chromium                     │     │      │
│  │   │  🛠  VS Code 1.137      🐍 python 3.13   ⎇ git          │     │      │
│  │   │                                                          │     │      │
│  │   │  home: /config (persisted)                               │     │      │
│  │   │  repo:  /config/workspace/core-app ◄── workspace volume  │     │      │
│  │   └────────────────────────────────────────────────────────┘     │      │
│  │                                                                  │      │
│  │   backend (FastAPI :8000, hot reload from the SAME volume)       │      │
│  │   frontend (nginx :3000, serves the app + proxies /api)          │      │
│  │   db (PostgreSQL :5432)                                          │      │
│  │   gitserver (git://gitserver/core-app.git) — "company Git"       │      │
│  │   repo-init (one-shot clone into the workspace volume)           │      │
│  └──────────────────────────────────────────────────────────────────┘      │
│                                                                            │
│   Browser ──► http://localhost:3000 (app)   :8000 (API)                    │
└────────────────────────────────────────────────────────────────────────────┘
```

## Quick start (Windows)

```powershell
cd browser-linux-desktop
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

First build is large (~2 GB desktop base image + packages) — several
minutes. Subsequent starts take seconds.

On Linux/macOS: `bash scripts/start.sh`.

## What you get

| URL | What it is |
|-----|-----------|
| http://localhost:8080 | **The Linux desktop** (full OS in the browser) |
| http://localhost:3000 | Company app frontend |
| http://localhost:8000 | Backend API (`/api/health`, `/api/tasks`) |
| `localhost:5432` | PostgreSQL (`company` / `company`) |

On the desktop you'll find a **README.txt** and a **VS Code – CoreApp**
shortcut. The repo lives at `/config/workspace/core-app`.

## Do anything — it's a real Linux machine

Inside the desktop terminal:

```bash
whoami                 # abc — a normal user with sudo
sudo apt install gimp  # install anything
python3 --version      # 3.13
git --version
htop                   # watch YOUR machine's processes
```

And the company loop:

```bash
cd /config/workspace/core-app
# edit in VS Code / mousepad / terminal — backend hot-reloads
git add -A && git commit -m "..." && git push
```

Open Chromium **inside the desktop** and visit `http://frontend:3000` to
see the app from within the machine itself (from Windows it's
`http://localhost:3000`).

## Verified working

- [x] Debian 13 XFCE desktop streams to the browser (KasmVNC, port 8080)
- [x] VS Code 1.137 launches as a real desktop app; Python + Pylance
      extensions auto-installed
- [x] Chromium renders pages (tested headless)
- [x] Backend/frontend/db healthy; edits hot-reload from the workspace
- [x] Git commit + push as the desktop user
- [x] **Reset test:** wipe workspace + DB, keep Git → pushed commit returns
      in the fresh clone; desktop settings/extensions survive

## Scripts

| Script | What it does |
|--------|--------------|
| `scripts/start.ps1` / `start.sh` | Build, start, health-check, open browser |
| `scripts/stop.ps1` / `stop.sh` | Stop everything (all state preserved) |
| `scripts/reset.ps1` / `reset.sh` | Wipe workspace + DB; keep Git **and** desktop settings |
| `scripts/reset-all.ps1` | Factory reset — re-seeds Git from `seed/company-app` |

## Configuration (`.env`)

| Variable | Default | Purpose |
|----------|---------|---------|
| `DESKTOP_PORT` | `8080` | Host port for the desktop |
| `DESKTOP_PASSWORD` | *(empty = no auth)* | HTTP basic auth (user `abc`) |
| `DESKTOP_TITLE` | `Company Linux Desktop` | Browser tab title |
| `PUID` / `PGID` | `911` | Desktop user id mapping |
| `GIT_USER_NAME` / `GIT_USER_EMAIL` | `Company Developer` / `dev@company.local` | Commit identity |
| `FRONTEND_PORT` / `BACKEND_PORT` / `DB_PORT` | `3000` / `8000` / `5432` | App ports on your laptop |

> **Port note:** all three projects share ports 3000/8000/5432/8080. Run one
> at a time, or change them here.

## Docs

- [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) — **start here: every
  desktop file explained line by line**, and the five build bugs
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how a desktop streams to
  a browser, component table, boot sequence, the WSL/namespace gotchas
- [`docs/SECURITY.md`](docs/SECURITY.md) — `--no-sandbox` trade-off, auth,
  hardening checklist
- [`../docs/COREAPP-CODE.md`](../docs/COREAPP-CODE.md) — the demo app's code,
  file by file
- [`../browser-dev-environment/docs/IMPLEMENTATION.md`](../browser-dev-environment/docs/IMPLEMENTATION.md)
  — the shared platform services (gitserver, repo-init, backend, frontend,
  db) line by line

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| First start takes long | The desktop base image is ~2 GB; later starts are fast |
| "port is already allocated" | Stop the other project stacks, or change ports in `.env` |
| VS Code won't launch from a terminal | Use `code` (the wrapper adds `--no-sandbox`); never run it as root |
| Chromium won't launch | Use `chromium-browser` (wrapper) or `chromium --no-sandbox` |
| Desktop feels laggy | Browsers render KasmVNC best with hardware acceleration on; also try Chrome/Edge fullscreen (F11) |
| 502 from `:3000/api/*` right after a backend redeploy | Already fixed via nginx `resolver`; if it ever recurs: `docker compose restart frontend` |
| Weird container state after interrupted start | `docker compose down && docker compose up -d` |
