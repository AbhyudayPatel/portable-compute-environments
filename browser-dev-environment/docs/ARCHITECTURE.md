# Architecture — Browser Development Environment

## Design principles

> **The container is the computer. Git is the source of truth. The browser
> is the interface. Docker is the execution layer.**

Everything in this project follows from those four sentences:

- The developer's *machine* is a Linux container, not Windows.
- Source code lives in a Docker volume whose lifecycle is tied to Git, not
  to the Windows filesystem.
- The UI is code-server over HTTP — no local IDE install required.
- Any container may be destroyed at any time; only volumes survive, and the
  Git volume is the one that actually matters.

## Components

| Container | Image | Role | Stateful? |
|-----------|-------|------|-----------|
| `ide` | custom: `codercom/code-server` + git/python | The developer's Linux workstation + browser VS Code | mounts `workspace-data` |
| `backend` | custom: `python:3.12-slim` + deps only | Runs the company API with hot reload **from the workspace volume** | mounts `workspace-data` |
| `frontend` | `nginx:1.27-alpine` | Serves the SPA from the workspace volume; proxies `/api/*` | mounts `workspace-data` (ro) |
| `db` | `postgres:16-alpine` | Application database | owns `pgdata` |
| `gitserver` | custom: `alpine` + git | Simulated company Git server (`git daemon`), seeds `core-app.git` | owns `git-data` |
| `repo-init` | same image as gitserver | One-shot bootstrap: clone repo → workspace volume, fix ownership | exits |

Volumes: `workspace-data` (the checked-out repo), `git-data` (the Git
server's repositories), `pgdata` (the database).

## Boot sequence

```
                        docker compose up
                               │
                ┌──────────────┴───────────────┐
                ▼                              ▼
           gitserver                        db
           │ seeds bare repo                │ init.sql
           │ on first boot                  │
                ▼                              │
        healthy (ls-remote OK)                 ▼
                │                        healthy (pg_isready)
                ▼                              │
           repo-init ──────────────────────────┤
           clone core-app.git                  │
           into workspace-data                 │
           chown 1000:1000                     │
                │                              │
     completed_successfully                    │
                ▼                              ▼
        ┌───────────────┐   depends_on   ┌──────────┐
        │  ide          │◄───────────────┤ backend  │
        │  code-server  │                │ frontend │
        └───────────────┘                └──────────┘
```

Ordering is enforced with `depends_on` conditions:

- `gitserver` must be **healthy** before `repo-init` runs (healthcheck =
  `git ls-remote` against the daemon).
- `repo-init` must reach `service_completed_successfully` before `ide`,
  `backend` and `frontend` start — guaranteeing the repo exists in the
  volume.
- `backend` additionally waits for `db` to be healthy.

## The layered model

```
Layer 4  APPLICATION RUNTIME   backend · frontend · db
Layer 3  WEB IDE               code-server (browser = interface)
Layer 2  WORKSPACE             workspace-data volume ← cloned from Git
Layer 1  ENVIRONMENT           Linux userspace, git, python, tooling
```

The key structural decision: **the application runs from the same volume
the developer edits**. There is no build/copy step between "save in the
IDE" and "running service":

- backend: `uvicorn --reload --reload-dir /workspace/core-app/backend`
- frontend: nginx `root /srv/workspace/core-app/frontend`

So the loop is: *edit → save → refresh*. Commits and pushes are normal Git
against `git://gitserver/core-app.git`.

## Data lifecycle

| Artifact | Where it lives | Survives `stop` | Survives `reset` | Survives `reset-all` |
|----------|---------------|-----------------|------------------|----------------------|
| Uncommitted edits | `workspace-data` | ✅ | ❌ | ❌ |
| Committed, **unpushed** work | `workspace-data` | ✅ | ❌ | ❌ |
| **Pushed** commits | `git-data` | ✅ | ✅ | ❌ |
| Database rows | `pgdata` | ✅ | ❌ | ❌ |
| Seed content | `seed/company-app` on host | ✅ | ✅ | ✅ |

This table *is* the employer requirement made concrete: work is durable
exactly when it has been pushed to the company Git — the same rule as a
real laptop, except here the "laptop" is disposable.

## Gotchas discovered during the build (worth knowing)

1. **Named-volume pre-population.** When Docker creates a named volume, it
   copies the image contents of the mount point into it. The backend
   image's `WORKDIR /workspace/core-app/backend` therefore pre-created
   empty directories in fresh volumes and broke `git clone`. Fixes: the
   backend image uses `WORKDIR /workspace` only, and `repo-init` removes a
   non-Git `core-app` directory before cloning.
2. **`git daemon` is a separate Alpine package** (`git-daemon`).
3. **Stale containers after an interrupted first `up`** can linger without
   network attachments; `docker compose down && up` resets that state.

## What this deliberately is NOT (yet)

- **No Docker-in-Docker.** The app services are *siblings* of the IDE on
  one daemon. DinD becomes relevant when the environment itself must run
  arbitrary `docker build/run/compose` for developers — see the roadmap in
  the root README. The repo carries its own `docker-compose.yml` as a
  placeholder for that day.
- **No production auth.** The IDE is password-protected but HTTP-only; the
  git server allows anonymous push. See [`SECURITY.md`](SECURITY.md).
- **No reverse-proxy gateway.** Services are exposed on separate host
  ports. The natural evolution is one nginx gateway (`/ide`, `/app`,
  `/api`) and, later, a port-gateway for dynamically allocated
  environments — see [`NETWORKING.md`](NETWORKING.md).
