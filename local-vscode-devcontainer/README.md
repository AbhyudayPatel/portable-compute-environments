# Project 1 — Local VS Code + Dev Containers

The **IBM-style Dev Containers** implementation: VS Code runs on your
Windows laptop, but your entire development environment — compiler/runtime,
terminal, debugger, linters — runs inside a Linux container. The
application services (backend, frontend, Postgres) run as **sibling
containers** on the same Docker network.

```
┌──────────────────────────── YOUR WINDOWS LAPTOP ───────────────────────────┐
│                                                                            │
│   VS Code (the UI you see)                                                 │
│      │  Dev Containers extension                                           │
│      ▼                                                                     │
│   Docker Desktop ──────────────── one Docker daemon ───────────────        │
│      │                                                                     │
│      ├──────────────┬──────────────┬──────────────┬──────────────┐         │
│      ▼              ▼              ▼              ▼              │         │
│  ┌────────┐   ┌──────────┐   ┌───────────┐   ┌──────────┐        │         │
│  │  dev   │   │ backend  │   │ frontend  │   │ postgres │        │         │
│  │ (IDE   │   │ FastAPI  │   │  nginx    │   │          │        │         │
│  │ tools) │   │  :8000   │   │  :3000    │   │  :5432   │        │         │
│  └────┬───┘   └──────────┘   └───────────┘   └──────────┘        │         │
│       │          ▲ your repo bind-mounted from Windows            │         │
│       └──────────┴─ /workspace (dev) and /app (backend) ──────────┘         │
│                                                                            │
│   Browser: http://localhost:3000 (app)   http://localhost:8000 (API)       │
└────────────────────────────────────────────────────────────────────────────┘
```

Key property: **the source code lives on your Windows disk** and is
bind-mounted into the containers. The environment definition itself
(`.devcontainer/`) lives inside the repo, so any developer who clones the
repo gets the identical environment.

## Prerequisites

- Docker Desktop (running)
- VS Code
- VS Code extension: **Dev Containers** (`ms-vscode-remote.remote-containers`)
  — `scripts/start.ps1` installs it automatically.

## Quick start

```powershell
# from this folder
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

Then inside VS Code: `F1` → **Dev Containers: Reopen in Container**.

First build takes a few minutes (pulls base images, installs tooling).
When it finishes:

| What | URL |
|------|-----|
| Company app (frontend) | http://localhost:3000 |
| Backend API | http://localhost:8000/api/health |
| PostgreSQL | `localhost:5432` (user `company` / password `company`) |

Open a terminal in VS Code — it is a **Linux shell inside the dev
container**. Try:

```bash
uname -a          # Linux, not Windows
python --version  # the container's Python, not your Windows one
git status        # your repo, mounted at /workspace
```

## Try the full loop

1. In the VS Code terminal (inside the container): `curl localhost:8000/api/health`
2. Edit `backend/app/main.py` — e.g. add a field to the health response.
3. Save → the backend container hot-reloads → refresh http://localhost:3000.
4. `git add -A && git commit -m "..."` — normal Git, nothing else involved.

## Just run the app (no Dev Containers)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-app-stack.ps1
```

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/start.ps1` | Check prerequisites, install the extension, open VS Code |
| `scripts/start-app-stack.ps1` | Run only backend+frontend+db directly |
| `scripts/stop.ps1` | Stop the standalone app stack |

## Deep dives

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — concepts: how Dev
  Containers work, networking, comparisons
- [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) — **every platform
  file explained line by line**, the boot sequence, and the build story
- [`../docs/COREAPP-CODE.md`](../docs/COREAPP-CODE.md) — the demo
  application's code (backend/frontend/db), file by file
