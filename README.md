# Portable Containerized Development Environments

Two complete, tested implementations of the same employer requirement:

> *A script runs on a developer's Windows laptop, creates a controlled
> Linux Docker environment, and the developer edits the company's private
> repo and pushes from inside that environment — with the application
> itself running in Docker.*

## Design principles

> **The container is the computer. Git is the source of truth.
> The browser/IDE is the interface. Docker is the execution layer.**

Both projects run the same demo company application — **CoreApp**, an
internal task board (FastAPI + PostgreSQL + nginx-served frontend) — so the
difference between them is purely *where the IDE runs and where the code
lives*.

## The three implementations

| | `local-vscode-devcontainer/` | `browser-dev-environment/` | `browser-linux-desktop/` |
|---|---|---|---|
| Model | IBM Dev Containers | Browser IDE | **Full Linux OS in browser** |
| Interface | VS Code on Windows | VS Code in the browser (code-server) | **Debian XFCE desktop (KasmVNC)** |
| You work at | `code company-app` → *Reopen in Container* | http://localhost:8080 | http://localhost:8080 |
| Apps available | repo toolchain | toolchain + web IDE | **VS Code, Chromium, terminal, file manager, sudo apt install anything** |
| Source code lives | your Windows disk, bind-mounted | a Docker volume, cloned from a Git server | a Docker volume, cloned from a Git server |
| Company Git | your real remote (SSH agent forwarded) | simulated `gitserver` container (replaceable) | simulated `gitserver` container (replaceable) |
| App services | sibling containers | sibling containers | sibling containers |
| Docker-in-Docker | not needed | not needed (roadmap) | not needed (roadmap) |
| Prereqs | Docker Desktop + VS Code + extension | **Docker Desktop only** | **Docker Desktop only** |
| Launcher | `scripts/start.ps1` | `scripts/start.ps1` | `scripts/start.ps1` |

### Project 1 at a glance

```
VS Code (Windows) ── Dev Containers ext ──► Docker Desktop
                                               ├── dev container  (terminal/tools, repo at /workspace)
                                               ├── backend :8000  ┐
                                               ├── frontend :3000 ├─ siblings on one network
                                               └── db :5432       ┘
```

### Project 2 at a glance

```
Browser :8080 ──► code-server inside Linux container ──► workspace volume
                                                            (cloned from gitserver)
Docker Desktop ──► ├── ide :8080        ← you work here
                   ├── backend :8000    ← hot-reloads FROM the workspace volume
                   ├── frontend :3000   ← serves FROM the workspace volume
                   ├── db :5432
                   └── gitserver        ← "company Git", survives resets
```

## Quick start

```powershell
# Project 3 — a full Linux OS in your browser:
cd browser-linux-desktop
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
# → opens http://localhost:8080 (Debian XFCE desktop: VS Code, Chromium, terminal, files)

# Project 2 — browser VS Code only (employer requirement, minimal):
cd browser-dev-environment
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
# → opens http://localhost:8080 (password: dev123)

# Project 1 — the local VS Code experience:
cd local-vscode-devcontainer
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
# → then in VS Code: F1 → "Dev Containers: Reopen in Container"
```

All three use ports 3000/8000/5432/8080 — **run one at a time** or adjust
`.env`. 

## Verified end to end

- [x] All containers healthy via compose healthchecks
- [x] Frontend → nginx proxy → backend → Postgres round-trip
- [x] Backend hot reload on edits made inside the environment
- [x] Edit → commit → `git push` from inside the Linux environment
- [x] **Disposability proof:** destroy workspace+DB+containers, keep Git →
      restart → pushed commits return in the fresh clone
- [x] Project 1 dev container image builds; compose configs valid
- [x] Project 3: XFCE desktop streams to browser; VS Code (real app, with
      `--no-sandbox` wrapper), Chromium, terminal, thunar all verified;
      Python/Pylance extensions auto-install; reset keeps pushed history

## Documentation (read in this order)

1. [`docs/COREAPP-CODE.md`](docs/COREAPP-CODE.md) — the demo application
   itself (FastAPI backend, nginx frontend, Postgres schema), file by file,
   line by line. Shared by all three projects.
2. Per-project implementation docs — **every platform file explained line
   by line, with the build story and the bugs found**:
   - [`local-vscode-devcontainer/docs/IMPLEMENTATION.md`](local-vscode-devcontainer/docs/IMPLEMENTATION.md)
   - [`browser-dev-environment/docs/IMPLEMENTATION.md`](browser-dev-environment/docs/IMPLEMENTATION.md)
   - [`browser-linux-desktop/docs/IMPLEMENTATION.md`](browser-linux-desktop/docs/IMPLEMENTATION.md)
3. Each project's `docs/ARCHITECTURE.md` for concepts and decisions, plus
   NETWORKING / GIT-WORKFLOW / SECURITY deep-dives where they exist.

## The one honest limitation

*"Push without version changes"* — Git cannot transfer changes without
creating commits. What both environments guarantee is that the platform
adds **no extra versioning layer**: a push is a plain `git push`. If
automatic commits are wanted, they can be scripted, but they are still
commits. See `browser-dev-environment/docs/GIT-WORKFLOW.md`.

## Roadmap (in order)

1. **Reverse-proxy gateway** — one URL with `/ide`, `/app`, `/api`.
2. **Real company Git** — swap `gitserver` for GitHub/GitLab with
   agent-forwarded or token auth (guide in Project 2's docs).
3. **SSO/TLS** on the IDE; bind ports to localhost.
4. **Port gateway + multiple environments** (per developer / per PR).
5. **Docker-in-Docker** — give the environment its own inner daemon so the
   repo's *own* `docker-compose.yml` runs inside it (see
   `docker-nested-lab/` for the DinD groundwork, and the networking doc for
   the port-forwarding consequences).
6. **Remote workers / control plane** — the same architecture, with the
   containers scheduled on servers instead of laptops.

## Repository layout

```
did/
├── README.md                        ← you are here
├── docker-nested-lab/               ← earlier DinD experiments (roadmap item 5)
├── local-vscode-devcontainer/       ← Project 1: local VS Code + Dev Containers
│   └── company-app/                 ← the demo repo, .devcontainer/ included
├── browser-dev-environment/         ← Project 2: browser VS Code (code-server)
│   ├── docker-compose.yml           ← the platform
│   ├── ide/  infra/  backend/  frontend/  db/
│   ├── seed/company-app/            ← initial content of the "company repo"
│   └── scripts/                     ← start / stop / reset / reset-all
└── browser-linux-desktop/           ← Project 3: full Linux OS in the browser
    ├── desktop/                     ← webtop base + VS Code, Chromium, XFCE apps
    │   └── custom-cont-init.d/      ← desktop setup hooks (git identity, wrappers)
    ├── infra/  backend/  frontend/  db/   (same platform pattern as Project 2)
    ├── seed/company-app/
    └── scripts/
```
