# CoreApp (simulated company repository)

This folder plays the role of **the company's private repository**. It is a
normal Git repo — the development environment does not add any extra layer
on top of Git.

It contains:

```
company-app/
├── .devcontainer/            ← the development environment definition (as code)
│   ├── devcontainer.json     ← tells VS Code how to build/attach the dev container
│   ├── Dockerfile            ← the dev container image (developer tooling)
│   └── docker-compose.yml    ← dev container + the app services as siblings
├── backend/                  ← FastAPI service (Python), hot-reloaded in Docker
├── frontend/                 ← static SPA served by nginx, proxies /api/*
├── db/init.sql               ← Postgres bootstrap
└── docker-compose.yml        ← standalone app stack ("the repo runs itself")
```

## The application

A tiny internal task board:

- `GET  /api/health` — service + database status
- `GET  /api/tasks` — list tasks
- `POST /api/tasks` — create `{ "title": "..." }`
- `PATCH /api/tasks/{id}/toggle` — flip done state
- `DELETE /api/tasks/{id}` — remove

## Two ways to work on it

| Flow | How | Where your terminal runs |
|------|-----|--------------------------|
| **Dev Container** (primary) | Open this folder in VS Code → `F1` → *Dev Containers: Reopen in Container* | inside the Linux dev container |
| **Standalone app stack** | `docker compose up -d --build` in this folder | on your host |

## Connecting a real private remote

This repo is local-only by default. To use it against a real private Git
server (GitHub/GitLab/etc.):

```bash
git remote add origin git@github.com:your-org/coreapp.git   # or your URL
git push -u origin main
```

Your Git credentials live on **your machine** (SSH agent / credential
manager), not inside any container image. VS Code's Dev Containers
extension automatically forwards your SSH agent into the dev container, so
`git push` from the container uses your host keys.
