# Project 2 — Browser-Based Container Development Environment

This is the implementation that matches the employer requirement: **run one
script on a Windows laptop, get a web link, and work in a VS Code
environment that lives entirely inside a controlled Linux container** —
including the company repo, which you edit, run, commit and push without
the source ever touching the Windows filesystem.

```
┌──────────────────────────── YOUR WINDOWS LAPTOP ───────────────────────────┐
│                                                                            │
│   Browser ──► http://localhost:8080 ──────► VS Code in the browser         │
│                                                                            │
│   Docker Desktop                                                           │
│      │                                                                     │
│      ▼  company-dev-env (docker compose)                                   │
│  ┌──────────────────────────────────────────────────────────────────┐      │
│  │                     Linux environment (devnet)                    │      │
│  │                                                                  │      │
│  │   ide (code-server :8080) ◄── you work HERE                      │      │
│  │      │  /home/coder/workspace/core-app  (workspace-data volume)  │      │
│  │      │                                                           │      │
│  │   backend (FastAPI :8000, hot reload from the same volume)       │      │
│  │   frontend (nginx :3000, serves app + proxies /api → backend)    │      │
│  │   db (PostgreSQL :5432)                                          │      │
│  │                                                                  │      │
│  │   gitserver (git://gitserver/core-app.git) ◄── "company Git"     │      │
│  │   repo-init (one-shot: clones the repo into the workspace)       │      │
│  └──────────────────────────────────────────────────────────────────┘      │
│                                                                            │
│   Browser ──► http://localhost:3000 (app)   :8000 (API)                    │
└────────────────────────────────────────────────────────────────────────────┘
```

## Quick start (Windows)

```powershell
cd browser-dev-environment
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

The script checks Docker, builds the images, starts everything, waits for
health checks, and opens the IDE. **First run takes a few minutes** (image
pulls/builds).

On Linux/macOS: `bash scripts/start.sh`.

## What you get

| URL | What it is |
|-----|-----------|
| http://localhost:8080 | **Browser IDE (VS Code)** — password `dev123` |
| http://localhost:3000 | Company app frontend |
| http://localhost:8000 | Backend API (`/api/health`, `/api/tasks`) |
| `localhost:5432` | PostgreSQL (`company` / `company`) |

Inside the IDE, the terminal is a real Linux shell. The company repo is at
`/home/coder/workspace/core-app`, already cloned and ready.

## The daily workflow

1. Open http://localhost:8080, enter the password.
2. Edit `backend/app/main.py` — the backend container hot-reloads from your
   working copy; refresh http://localhost:3000 to see it.
3. Commit and push **from the IDE terminal**:
   ```bash
   cd /home/coder/workspace/core-app
   git add -A && git commit -m "My change" && git push
   ```

## The demo that matters: disposability

```powershell
scripts\reset.ps1        # destroys containers + workspace + database, KEEPS Git
scripts\start.ps1        # fresh environment boots...
```

...and your pushed commits are back in the clone. Anything not pushed is
gone. **Git is the source of truth; the container is disposable.** This was
tested end to end: commit pushed from the IDE → full reset → commit
returned in the fresh clone.

## Scripts

| Script | What it does |
|--------|--------------|
| `scripts/start.ps1` / `start.sh` | Build, start, health-check, open browser |
| `scripts/stop.ps1` / `stop.sh` | Stop everything (state preserved) |
| `scripts/reset.ps1` / `reset.sh` | Wipe workspace + DB, **keep Git history** |
| `scripts/reset-all.ps1` | Factory reset — re-seeds Git from `seed/company-app` |

## Configuration

Copy `.env.example` → `.env` (the start script does this automatically):

| Variable | Default | Purpose |
|----------|---------|---------|
| `IDE_PASSWORD` | `dev123` | Browser IDE password |
| `IDE_PORT` | `8080` | IDE host port |
| `GIT_USER_NAME` / `GIT_USER_EMAIL` | `Company Developer` / `dev@company.local` | Commit identity inside the container |
| `FRONTEND_PORT` / `BACKEND_PORT` / `DB_PORT` | `3000` / `8000` / `5432` | App ports on your laptop |
| `POSTGRES_PASSWORD` | `company` | DB password |

> **Note:** Project 1 (`local-vscode-devcontainer`) uses the same app ports
> (3000/8000/5432). Run one stack at a time, or change ports in `.env`.

## How it works — the boot sequence

```
gitserver  (healthy: repo seeded + git daemon listening)
   │
   ▼
repo-init  (one-shot: clones git://gitserver/core-app.git
   │        into the workspace-data volume, chowns to the IDE user)
   ▼
┌──────────┬───────────┬──────────┐
ide        backend     frontend   db
(healthy)  (healthy)   (healthy)  (healthy)
```

## Docs

- [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) — **start here: every
  platform file explained line by line**, boot sequence, the three build bugs
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — components, layers, decisions
- [`docs/NETWORKING.md`](docs/NETWORKING.md) — ports, the three networking perspectives, how to add a service
- [`docs/GIT-WORKFLOW.md`](docs/GIT-WORKFLOW.md) — the clone/push lifecycle, replacing the demo Git server with a real one
- [`docs/SECURITY.md`](docs/SECURITY.md) — what is demo-grade here and how to harden it
- [`../docs/COREAPP-CODE.md`](../docs/COREAPP-CODE.md) — the demo app's code,
  file by file

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "port is already allocated" | Another stack (e.g. Project 1) holds the port — stop it, or change ports in `.env` |
| IDE asks for a password you didn't set | It's `dev123` unless you changed `IDE_PASSWORD` in `.env` |
| Weird state after an interrupted first start | `docker compose down && docker compose up -d` (stale containers from a failed first run can linger) |
| Backend says `database: down` right after boot | Give Postgres ~20 s to finish initialising; refresh |
| Change a base image / dependency | `docker compose up -d --build` |
| PowerShell refuses to run scripts | `powershell -ExecutionPolicy Bypass -File scripts\start.ps1` |
