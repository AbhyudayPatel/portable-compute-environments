# CoreApp (simulated company repository)

This repository plays the role of **the company's private repo** in the
browser development environment. The platform clones it into your workspace
on first boot; you edit, commit and push it entirely from inside the
browser IDE.

```
core-app/
├── backend/              ← FastAPI service (Python), hot-reloaded by the platform
├── frontend/             ← static SPA served by nginx, proxies /api/*
├── db/init.sql           ← Postgres bootstrap (copy; platform keeps its own)
└── docker-compose.yml    ← reference stack: "the repo knows how to run itself"
```

## The application

A tiny internal task board:

- `GET  /api/health` — service + database status
- `GET  /api/tasks` — list tasks
- `POST /api/tasks` — create `{ "title": "..." }`
- `PATCH /api/tasks/{id}/toggle` — flip done state
- `DELETE /api/tasks/{id}` — remove

## How it runs in the browser environment

You do **not** need to start anything yourself. The platform's compose
stack already runs:

| Service | Runs from | Port |
|---------|-----------|------|
| backend | uvicorn `--reload` over this working copy | `localhost:8000` |
| frontend | nginx serving `frontend/` from this working copy | `localhost:3000` |
| db | PostgreSQL 16 | `localhost:5432` |

Edit any file here → save → the change is live (backend hot-reloads;
frontend changes on refresh).

## The Git loop

```bash
git status
git add -A
git commit -m "Describe the change"
git push        # pushes to git://gitserver/core-app.git
```

Your pushed commits are the durable artifact. Destroy the whole environment
(`scripts/reset.ps1`), start it again, and your pushed work returns —
because **Git is the source of truth and containers are disposable**.

## The reference stack

`docker-compose.yml` in this repo lets the app stand alone (backend +
frontend + db). In the current platform it is informational — the platform
orchestrates these services itself. It becomes functional the day the
environment gets its own Docker daemon (Docker-in-Docker); see the
platform docs for that roadmap.
