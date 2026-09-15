# Architecture — Local VS Code + Dev Containers

This project is the **IBM "Create portable dev environments with Dev
Containers"** model, built out concretely. Its defining property:

> VS Code (the UI) stays on Windows. Everything that *runs* — the
> toolchain, the terminal, the app services — runs in Linux containers.

## What happens when you click "Reopen in Container"

```
VS Code reads company-app/.devcontainer/devcontainer.json
        │
        ▼
dockerComposeFile: docker-compose.yml     ← which compose stack
service: dev                              ← which service is "the IDE machine"
workspaceFolder: /workspace               ← where the repo appears inside it
        │
        ▼
VS Code generates an override compose file, then runs roughly:
        docker compose up -d --build
        │
        ├── builds dev image (.devcontainer/Dockerfile)
        ├── starts dev + backend + frontend + db     (siblings, one network)
        ├── mounts the repo (your Windows folder) at /workspace in dev
        ├── injects VS Code Server into dev and connects the window to it
        ├── installs the extensions listed in customizations
        └── runs postCreateCommand
```

From then on:

- the **integrated terminal** is a shell in the `dev` container (`uname -a`
  → Linux);
- the **file explorer** shows `/workspace`, which *is* your Windows
  `company-app` folder, bind-mounted — edits are instant on both sides and
  survive container rebuilds;
- **backend/frontend/db** are sibling containers started by the same
  compose stack.

## Field-by-field: devcontainer.json

| Field | Value here | Meaning |
|-------|-----------|---------|
| `dockerComposeFile` | `docker-compose.yml` | VS Code starts this whole stack, not just one container |
| `service` | `dev` | The container VS Code Server runs in |
| `workspaceFolder` | `/workspace` | Folder opened in the VS Code window |
| `customizations.vscode.extensions` | python, docker, yaml | Installed *inside* the container's VS Code Server, per-repo |
| `customizations.vscode.settings` | interpreter, default shell | Repo-owned editor settings |
| `forwardPorts` | 3000, 8000 | Surfaced in the Ports panel (compose already publishes them) |
| `postCreateCommand` | pip install + git safe.directory | One-time container personalization |
| `shutdownAction` | `stopCompose` | Closing VS Code stops the whole stack |

## Why the app services are siblings, not children (no DinD)

```
                 Docker Desktop (one daemon)
                       │
        ┌──────────────┼──────────────┬──────────┐
        ▼              ▼              ▼          ▼
       dev          backend        frontend      db
     (IDE/tools)    :8000           :3000       :5432
        │              ▲
        └── repo mounted at /workspace (dev) and /app (backend) ──┐
                    (both mounts point at your Windows folder)     │
```

The `dev` container does **not** need its own Docker daemon. The backend is
not "Docker inside the dev container" — it is a peer. That is why:

- networking is trivial (`backend:8000`, `db:5432` by service name);
- ports are published once (`8000:8000`, `3000:3000`) and work from Windows;
- there is no nested-networking problem to solve.

Docker-in-Docker would only appear if the *developer environment itself*
needed to build/run containers as part of the product (e.g. the repo's own
`docker-compose.yml` executed from inside the IDE container against an
inner daemon). That is a roadmap scenario, not a requirement here.

## Networking cheat sheet

| Caller | Target | Address |
|--------|--------|---------|
| Your browser | frontend | `http://localhost:3000` |
| Your browser / curl | backend directly | `http://localhost:8000` |
| Browser via frontend proxy | backend | `http://localhost:3000/api/...` |
| backend container | postgres | `db:5432` |
| frontend container | backend | `http://backend:8000` |
| Your DB client on Windows | postgres | `localhost:5432` |

## Persistence

| Data | Where | Survives rebuild |
|------|-------|------------------|
| Source code | your Windows folder (bind mount) | ✅ always |
| Postgres data | named volume `*_pgdata` | ✅ |
| Dev container tooling | image | rebuilt = back to declared state (that's the point) |

## Git

The repo is a plain Git repo on your disk. Add your private remote and push
normally; VS Code's Dev Containers extension **forwards your host SSH
agent** into the container, so `git push` from the integrated terminal uses
your Windows keys without copying them anywhere.

## Comparison with the browser approach (Project 2)

| | Project 1 (this) | Project 2 (`browser-dev-environment`) |
|---|---|---|
| IDE | Local VS Code | code-server in a container |
| Interface | VS Code window | Browser tab (`:8080`) |
| Source lives | Windows disk (bind mount) | Docker volume (clone from Git server) |
| Git auth | your host SSH agent, forwarded | container identity / demo server |
| Prerequisites | VS Code + extension + Docker | Docker only |
| "Open a web link and work" | ❌ | ✅ |
| Best for | developers with VS Code already | controlled, zero-install, disposable workstations |
