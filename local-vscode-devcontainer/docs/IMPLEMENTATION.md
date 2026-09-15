# Implementation — Project 1: Local VS Code + Dev Containers

This document explains **every platform file**: what it does, why it
exists, and what each line means. For the application code itself
(backend/frontend/db), read [`docs/COREAPP-CODE.md`](../../docs/COREAPP-CODE.md)
first — it is identical across all three projects.

---

## 1. The 30-second mental model

```
VS Code window (Windows UI)
   │  Dev Containers extension speaks the Docker API
   ▼
Docker Desktop ──► builds/starts the compose stack
   │
   ├─ dev        ← VS Code Server injected here; your terminal/tools live here
   ├─ backend    ← sibling container (FastAPI, hot reload from your disk)
   ├─ frontend   ← sibling container (nginx)
   └─ db         ← sibling container (Postgres)
```

Two sentences that explain everything:

1. **VS Code's UI and VS Code's backend are separate processes.** The UI can
   stay on Windows while the backend ("VS Code Server") — which runs the
   terminal, language servers, debugger, extensions — lives inside a Linux
   container.
2. **The environment is declared inside the repo** (`.devcontainer/`), so
   anyone who clones the repo gets the identical machine.

## 2. File map

```
local-vscode-devcontainer/
├── README.md                      ← quickstart
├── docs/
│   ├── ARCHITECTURE.md            ← concepts & comparisons
│   └── IMPLEMENTATION.md          ← this file
├── scripts/
│   ├── start.ps1                  ← prereq check + open VS Code
│   ├── start-app-stack.ps1        ← run only the app (no dev container)
│   └── stop.ps1                   ← stop the app stack
└── company-app/                   ← the repo (a normal git repo)
    ├── .devcontainer/
    │   ├── devcontainer.json      ← THE control file
    │   ├── Dockerfile             ← the dev container image (tooling)
    │   └── docker-compose.yml     ← dev container + app services
    ├── backend/ frontend/ db/     ← the application (see COREAPP-CODE.md)
    └── docker-compose.yml         ← standalone app stack ("repo runs itself")
```

---

## 3. `company-app/.devcontainer/devcontainer.json` — the control file

This JSONC (JSON-with-comments — the official format) file is the only
thing VS Code reads to decide what "Reopen in Container" means.

```jsonc
{
  "name": "CoreApp Dev Container",
  "dockerComposeFile": "docker-compose.yml",
  "service": "dev",
  "workspaceFolder": "/workspace",
```

| Field | Meaning | Why it's needed |
|-------|---------|-----------------|
| `name` | Label shown in VS Code's remote indicator | Pure UX |
| `dockerComposeFile` | Path (relative to this file) of the compose stack to start | Without it VS Code would start a single container; we need the app services (backend/db) alongside |
| `service` | Which compose service VS Code attaches to | In a multi-service compose file VS Code must know which container is "the developer machine" |
| `workspaceFolder` | Directory opened in the VS Code window | Matches the volume mount `/workspace` declared in the compose file — **both must agree** |

```jsonc
  "customizations": {
    "vscode": {
      "extensions": [
        "ms-python.python",
        "ms-azuretools.vscode-docker",
        "redhat.vscode-yaml"
      ],
      "settings": {
        "python.defaultInterpreterPath": "/usr/local/bin/python3",
        "terminal.integrated.defaultProfile.linux": "bash"
      }
    }
  },
```

| Field | Meaning |
|-------|---------|
| `customizations.vscode.extensions` | Extensions installed **into the container's VS Code Server** on first attach — not on your Windows VS Code. The repo decides its own tooling: a Python developer and a Go developer can work on different repos with different extensions, automatically. |
| `customizations.vscode.settings` | Editor settings applied to the remote window. `python.defaultInterpreterPath` points the Python extension at the container's interpreter (there *is* no Windows Python in here). |

```jsonc
  "forwardPorts": [3000, 8000],
```

Shows these ports in VS Code's **Ports** panel. Note the compose file
already *publishes* 3000/8000 on the host — `forwardPorts` is cosmetic
here. (It becomes load-bearing when services don't publish ports and rely
on VS Code's forwarding instead.)

```jsonc
  "postCreateCommand": "pip install -r backend/requirements.txt && git config --global --add safe.directory /workspace",
```

Runs **once, inside the dev container, after it's created**:

1. `pip install -r backend/requirements.txt` — installs the backend's
   dependencies into the dev container too, so you can run/debug the API
   from the VS Code terminal (breakpoints!) instead of only in the backend
   container.
2. `git config --global --add safe.directory /workspace` — Git refuses to
   operate on directories owned by another user ("dubious ownership"). The
   bind-mounted repo is owned by your Windows user as seen through the
   mount; the container user is root. This whitelist entry tells Git the
   mount is trusted.

Other lifecycle hooks exist (`onCreateCommand`, `postStartCommand`,
`postAttachCommand`) — `postCreateCommand` is the right one for
"install things once per container creation".

```jsonc
  "shutdownAction": "stopCompose"
}
```

Closing the VS Code window runs `docker compose stop` on the stack — the
whole environment pauses with the editor. Reopening resumes it.

## 4. `company-app/.devcontainer/Dockerfile` — the dev container image

```dockerfile
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
```

| Line | Why |
|------|-----|
| `FROM python:3.12-slim` | The dev container's main job is Python tooling, so start from the official Python image |
| `apt-get install git curl ca-certificates` | `git` because the developer commits from here; `curl`+certs for debugging APIs/HTTPS. **This is the "reproducible environment" idea in one line**: the tool every developer needs, declared once, identical for everyone |
| `rm -rf /var/lib/apt/lists/*` | Deletes apt's package index → smaller image layer (standard hygiene) |
| `WORKDIR /workspace` | Terminal opens in the repo root |

What's deliberately **not** here: the app source (bind-mounted at runtime)
and the app's runtime (backend/db are sibling containers). This image is
*the developer's toolbox*, nothing more.

## 5. `company-app/.devcontainer/docker-compose.yml` — the stack VS Code starts

When you click "Reopen in Container", VS Code roughly runs
`docker compose -f <this file> -f <its own generated override> up -d`.

```yaml
services:
  dev:
    build:
      context: ..                        # repo root
      dockerfile: .devcontainer/Dockerfile
    volumes:
      - ..:/workspace:cached             # THE workspace mount
    command: sleep infinity
    depends_on:
      - backend
```

| Line | Why |
|------|-----|
| `context: ..` | Build context = repo root, so the Dockerfile path is `.devcontainer/Dockerfile` |
| `..:/workspace:cached` | **The most important line in the project.** Your Windows `company-app` folder appears at `/workspace` inside the dev container. Edits are instant both ways; files live on your disk (survive any container rebuild). `:cached` is a macOS/Windows mount-performance hint |
| `command: sleep infinity` | The dev container does no work by itself — it just needs to *stay alive* so VS Code Server can run in it. VS Code overrides this command anyway |
| `depends_on: backend` | Start the app too when the environment starts |

The app services are the same definitions as the standalone stack (backend
with `../backend:/app` hot-reload mount, frontend with the nginx config and
static mount, db with init.sql + healthcheck + pgdata volume) — paths are
`../…` because this file lives in `.devcontainer/`, one level deeper.

See [`docs/COREAPP-CODE.md`](../../docs/COREAPP-CODE.md) for the nginx
`resolver 127.0.0.11` block and why it's there.

## 6. `company-app/docker-compose.yml` — the standalone app stack

Nearly identical to §5 minus the `dev` service, with `./…` paths and
`name: coreapp`. Its reason to exist: **the repo can run itself** — no VS
Code, no devcontainer spec required:

```bash
cd company-app
docker compose up -d --build     # backend + frontend + db
```

That's what `scripts/start-app-stack.ps1` wraps. It also keeps the honest
separation visible: *development tooling* (dev container) and *application
runtime* (backend/frontend/db) are different concerns.

## 7. `scripts/` — the launchers (PowerShell)

All three scripts share the same skeleton:

```powershell
#requires -Version 5.1
$ErrorActionPreference = 'Stop'
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $ProjectRoot
```

| Line | Why |
|------|-----|
| `#requires -Version 5.1` | Works on the Windows-builtin PowerShell (5.1), not just PowerShell 7 |
| `$ErrorActionPreference = 'Stop'` | Any failing cmdlet aborts the script instead of ploughing on into a broken state |
| `$PSScriptRoot` | The folder the script lives in → the script works no matter where you launch it from |

### `start.ps1`

```powershell
docker info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Docker Desktop is not running...' }
```

- `docker info` is the canonical "is the daemon up?" probe. PowerShell 5.1
  doesn't throw on non-zero exit codes from native commands, so we check
  `$LASTEXITCODE` explicitly.

```powershell
code --install-extension ms-vscode-remote.remote-containers --force | Out-Null
```

- Installing the Dev Containers extension idempotently (`--force` = fine if
  already installed) means a new developer doesn't even need to know which
  extension is required.

```powershell
code (Join-Path $ProjectRoot 'company-app')
```

- Opens the repo folder; the script then prints the one manual step
  (`F1` → *Dev Containers: Reopen in Container*) and the URLs.

### `start-app-stack.ps1`

```powershell
Set-Location (Join-Path $ProjectRoot 'company-app')
docker compose up -d --build
...
Wait-ForUrl 'http://localhost:8000/api/health' 'Backend API'
Wait-ForUrl 'http://localhost:3000/' 'Frontend'
```

- Runs the standalone stack and **gates on real HTTP health** before
  claiming success. `Wait-ForUrl` loops `Invoke-WebRequest` with a deadline
  and throws with a pointer to `docker compose logs` on timeout.

### `stop.ps1`

`docker compose down` for the standalone stack. The devcontainer stack
needs no stop script — `shutdownAction: stopCompose` handles it when you
close VS Code.

---

## 8. The full boot sequence (what "Reopen in Container" really does)

```
1. VS Code parses .devcontainer/devcontainer.json
2. Generates an override compose file (command override, VS Code labels,
   its own mount for the VS Code Server)
3. docker compose ... up -d --build
      ├── build dev image            (Dockerfile §4)
      ├── build backend image        (backend/Dockerfile)
      ├── pull nginx:1.27-alpine, postgres:16-alpine
      ├── start db → healthy (pg_isready)
      ├── start backend → healthy (/api/health)
      ├── start frontend → healthy (wget :3000)
      └── start dev (sleep infinity)
4. Injects VS Code Server into the dev container
5. Reconnects the window: Explorer shows /workspace, terminal = Linux shell
6. Installs the three extensions listed in customizations
7. Runs postCreateCommand (pip install + git safe.directory)
8. You work. Closing the window → docker compose stop (shutdownAction)
```

## 9. Where your code, credentials and state live

| Thing | Location | Why it survives container rebuilds |
|-------|----------|-----------------------------------|
| Source | Windows disk, bind-mounted `/workspace` | Bind mount = same files, not a copy |
| Postgres data | named volume `*_pgdata` | Volumes outlive containers |
| Git credentials | **your Windows SSH agent**, forwarded into the container by the Dev Containers extension | No keys in any image, ever |
| Dev tooling | the dev image | Rebuild = back to the declared state — that's the feature |

## 10. Verified behaviour (what was actually tested here)

- `docker compose config` valid for both compose files ✓
- Standalone app stack: backend healthy (`database: up`), frontend serving,
  `/api/*` proxy round-trip, 3 seeded tasks ✓
- Dev container image builds ✓
- The "Reopen in Container" click itself is the one step that needs the VS
  Code GUI — everything it will do is exactly §8, and every layer it uses
  was built and health-checked during the build of this project.

## 11. Extend it

| You want | Change |
|----------|--------|
| Node.js tooling in the dev container | Add `nodejs npm` (or nodesource) to `.devcontainer/Dockerfile` |
| A Redis service | Add it to both compose files; backend reaches it at `redis:6379` |
| Different app ports | Change the `ports:` mappings (left side = host) in the compose files |
| Real private remote | `git remote add origin <url>` inside `company-app` — your SSH agent is forwarded automatically |
| Pre-built images for faster onboarding | Push the dev/backend images to a registry and swap `build:` for `image:` |
