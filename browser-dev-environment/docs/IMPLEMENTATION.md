# Implementation — Project 2: Browser VS Code (code-server platform)

This document explains **every platform file**: what it does, why it
exists, and what each line means — plus the full build story, including the
three real bugs that were found and fixed. For the application code itself,
read [`docs/COREAPP-CODE.md`](../../docs/COREAPP-CODE.md) first.

---

## 1. The 30-second mental model

```
Your browser
   │ http://localhost:8080
   ▼
code-server container (Linux) ──► VS Code UI + real Linux terminal
   │ mounts workspace-data volume
   ▼
/home/coder/workspace/core-app  ← cloned from the gitserver container
   ▲                                    ▲
   │ backend & frontend containers run the app FROM THE SAME VOLUME
   │
git push → gitserver (git-data volume) → survives any reset
```

The design contract, from which every file follows:

> **The container is the computer. Git is the source of truth. The browser
> is the interface. Docker is the execution layer.**

Concretely:

- The company source code **never touches the Windows filesystem** — it
  lives in a Docker *named volume* cloned from a Git server.
- The IDE itself runs **inside** the container; the browser is a thin
  client. (Project 1 is the opposite split: IDE on Windows, tools in a
  container.)
- The application containers run **from the same volume you edit**, so
  "save file → service reloads" has no build step in between.

## 2. File map

```
browser-dev-environment/
├── docker-compose.yml             ← THE platform definition (6 services)
├── .env.example                   ← every tunable (ports, password, git identity)
├── ide/
│   ├── Dockerfile                 ← code-server + developer tooling
│   └── entrypoint.sh              ← git identity, then start code-server
├── infra/
│   ├── git.Dockerfile             ← alpine + git + git-daemon (2 services use it)
│   └── gitserver-entrypoint.sh    ← seed bare repo, serve git://
├── backend/Dockerfile.runtime     ← deps-only image; source comes from the volume
├── frontend/nginx.conf            ← static files from the volume + /api proxy
├── db/init.sql                    ← Postgres bootstrap
├── seed/company-app/              ← the "company repo" initial content
├── scripts/                       ← start/stop/reset/reset-all (.ps1 + .sh)
└── docs/                          ← ARCHITECTURE, NETWORKING, GIT-WORKFLOW,
                                     SECURITY, and this file
```

## 3. Boot order, enforced by `depends_on` conditions

```
gitserver   ── healthy when: git ls-remote git://127.0.0.1/core-app.git works
   │
   ▼ (condition: service_healthy)
repo-init   ── clones the repo into workspace-data, chowns, exits 0
   │
   ▼ (condition: service_completed_successfully)
┌──────────┬───────────────┬────────────────┐
ide        backend         frontend         (db runs in parallel,
(healthy   (also waits for (healthy)          backend waits for its
 via       db healthy)                        own healthcheck)
 /healthz)
```

If any condition fails, dependent services never start — you get a clear
error instead of a half-broken environment.

---

## 4. `docker-compose.yml` — service by service

### Header

```yaml
name: company-dev-env
```

Fixes the compose **project name**. Everything compose creates (containers,
volumes, the network) gets prefixed with it:
`company-dev-env-ide-1`, `company-dev-env_workspace-data`, …
The reset scripts rely on these exact volume names.

### `gitserver` — the simulated company Git server

```yaml
  gitserver:
    build:
      context: ./infra
      dockerfile: git.Dockerfile
    image: company-dev-git-tools
    entrypoint: ["/usr/local/bin/gitserver-entrypoint.sh"]
    volumes:
      - git-data:/srv/git
      - ./seed/company-app:/seed:ro
    healthcheck:
      test: ["CMD", "git", "ls-remote", "git://127.0.0.1/core-app.git"]
      interval: 5s
      timeout: 5s
      retries: 12
    networks: [devnet]
```

| Field | Meaning | Why |
|-------|---------|-----|
| `image: company-dev-git-tools` | Name the built image explicitly | `repo-init` reuses the **same image** (below) — one build, two jobs |
| `entrypoint: [...]` | Run our script instead of the image default | Seeding must happen before serving |
| `git-data:/srv/git` | The bare repo lives in a named volume | **Pushed commits survive `reset.ps1`** — the whole disposability demo hinges on this |
| `./seed/company-app:/seed:ro` | Mount the seed content read-only | The repo's initial content is versioned with the platform on the host; `:ro` makes accidental mutation impossible |
| healthcheck `git ls-remote git://127.0.0.1/...` | "Healthy" = *the daemon actually serves the repo* | A plain TCP check could pass before the seed finishes; this one proves end-to-end readiness, which `repo-init` depends on |
| No `ports:` | The git server is **not published to the host** | Reachable only inside `devnet` — least exposure |

### `repo-init` — the one-shot bootstrap

```yaml
  repo-init:
    image: company-dev-git-tools
    depends_on:
      gitserver:
        condition: service_healthy
    volumes:
      - workspace-data:/workspace
    command:
      - sh
      - -c
      - |
        set -e
        if [ ! -d /workspace/core-app/.git ]; then
          echo "[repo-init] Cloning git://gitserver/core-app.git ..."
          rm -rf /workspace/core-app
          git clone git://gitserver/core-app.git /workspace/core-app
        else
          echo "[repo-init] Repository already present, skipping clone."
        fi
        chown -R 1000:1000 /workspace
        echo "[repo-init] Workspace ready."
```

Line by line:

| Line | Why |
|------|-----|
| `set -e` | Any failure aborts with non-zero exit → compose reports `service "repo-init" didn't complete successfully` instead of silently continuing into a broken workspace |
| `if [ ! -d /workspace/core-app/.git ]` | **Idempotency.** A real clone has `.git`; if present, this is a restart, not a first boot → skip cloning and *keep the developer's uncommitted work* |
| `rm -rf /workspace/core-app` (before clone) | **Bug fix #2.** A brand-new named volume is pre-populated with the image contents of whatever path it's mounted at. Another image's deep `WORKDIR` once created empty `core-app/backend/` dirs in the fresh volume, and `git clone` refuses a non-empty target. Clearing a *non-Git* directory is safe — real work always has `.git` |
| `git clone git://gitserver/core-app.git` | The clone happens **inside Docker**, from the internal Git service — source lands in the volume without ever existing on Windows |
| `chown -R 1000:1000 /workspace` | The IDE container's user is `coder` (uid 1000); without this the browser IDE couldn't save files |
| `condition: service_completed_successfully` (consumers) | ide/backend/frontend wait until this container **exited with 0** — guaranteeing the repo exists before anything tries to use it |

Why a separate service instead of cloning in the IDE's entrypoint?
**Ordering.** Three services need the repo; a one-shot job with a
`service_completed_successfully` dependency is the clean compose primitive
for "prepare shared state, then start everyone".

### `ide` — code-server

```yaml
  ide:
    build: { context: ./ide }
    depends_on:
      repo-init: { condition: service_completed_successfully }
    environment:
      PASSWORD: ${IDE_PASSWORD:-dev123}
      GIT_USER_NAME: ${GIT_USER_NAME:-Company Developer}
      GIT_USER_EMAIL: ${GIT_USER_EMAIL:-dev@company.local}
    volumes:
      - workspace-data:/home/coder/workspace
    ports:
      - "${IDE_PORT:-8080}:8080"
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8080/healthz"]
      ...
```

| Field | Why |
|-------|-----|
| `PASSWORD: ${IDE_PASSWORD:-dev123}` | code-server reads this env var when `--auth password` is used. `${VAR:-default}` = value from `.env`, else the default |
| `workspace-data:/home/coder/workspace` | The repo appears inside the IDE at `~/workspace/core-app`. code-server opens exactly this path (see entrypoint) |
| `"${IDE_PORT:-8080}:8080"` | host port `IDE_PORT` → container port 8080. Your browser never sees the container's port, only the host's |
| healthcheck `/healthz` | code-server ships a real health endpoint — perfect for the launcher script's readiness gate |

### `backend` — runs *from the volume*

```yaml
  backend:
    build: { context: ./backend, dockerfile: Dockerfile.runtime }
    depends_on:
      repo-init: { condition: service_completed_successfully }
      db:        { condition: service_healthy }
    environment:
      DATABASE_URL: postgresql://company:${POSTGRES_PASSWORD:-company}@db:5432/companydb
    volumes:
      - workspace-data:/workspace
    ports: [ "${BACKEND_PORT:-8000}:8000" ]
```

The crucial idea: this service has **no `COPY` of the source anywhere in
its build** — the image carries only dependencies (`Dockerfile.runtime`,
§6). At runtime it mounts the same `workspace-data` volume the developer
edits and starts uvicorn with `--reload` pointed into it. Consequence:

> Edit `backend/app/main.py` in the browser IDE → save → uvicorn restarts
> → refresh the app. The loop has zero build steps.

Also note `DATABASE_URL` uses hostname `db` — the compose service name
(containers resolve each other by service name over `devnet`).

### `frontend` — nginx from the volume

```yaml
  frontend:
    image: nginx:1.27-alpine
    volumes:
      - ./frontend/nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - workspace-data:/srv/workspace:ro
```

- Serves static files straight from the workspace
  (`root /srv/workspace/core-app/frontend;`) → saving `index.html` in the
  IDE and refreshing is the whole "frontend deploy".
- The nginx config (§7) also reverse-proxies `/api/*` to the backend, so
  the browser only ever talks to one origin.
- `:ro` on the workspace: the web server should never be able to mutate
  source code.

### `db` — Postgres

```yaml
  db:
    image: postgres:16-alpine
    environment: { POSTGRES_USER: company, POSTGRES_PASSWORD: ..., POSTGRES_DB: companydb }
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./db/init.sql:/docker-entrypoint-initdb.d/01-init.sql:ro
    healthcheck: { test: ["CMD-SHELL", "pg_isready -U company -d companydb"], ... }
    ports: [ "${DB_PORT:-5432}:5432" ]
```

- The official image executes anything in `/docker-entrypoint-initdb.d/`
  **once, only when the data directory is empty** — that's how the schema +
  seed rows appear on first boot.
- `pg_isready` is Postgres' own health probe; the backend's
  `service_healthy` dependency on it removes boot races.
- `pgdata` is a named volume → data survives `stop.ps1`, is destroyed by
  `reset.ps1`.

### Volumes & network

```yaml
volumes:
  workspace-data:   # the checked-out company repo
  git-data:         # the git server's repos
  pgdata:           # the database
networks:
  devnet:
```

One isolated bridge network for all six services; nothing is reachable from
the host except through published `ports:`.

---

## 5. `ide/Dockerfile` + `ide/entrypoint.sh`

```dockerfile
FROM codercom/code-server:4.89.1
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
       git curl ca-certificates python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /usr/local/bin/dev-entrypoint.sh
RUN chmod +x /usr/local/bin/dev-entrypoint.sh
USER coder
WORKDIR /home/coder
ENTRYPOINT ["/usr/local/bin/dev-entrypoint.sh"]
```

| Line | Why |
|------|-----|
| `FROM codercom/code-server:4.89.1` | code-server = VS Code's open-source core served over HTTP. **Pinned tag** → reproducible builds |
| `USER root` … `apt-get install …` | Add the developer tooling (git, python) the base image lacks |
| `COPY … /usr/local/bin/` + `chmod +x` | Install the entrypoint as an executable **in the image**, not as a mount — Windows bind mounts don't reliably carry the executable bit, which would kill the container with `permission denied` |
| `USER coder` | Drop privileges — the IDE runs as the unprivileged uid-1000 user (the one `repo-init` chowns the workspace to) |
| `ENTRYPOINT ["/usr/local/bin/dev-entrypoint.sh"]` | JSON (exec) form → code-server becomes PID 1's child properly and receives SIGTERM on `docker stop` |

`entrypoint.sh`:

```sh
#!/bin/sh
set -e
git config --global user.name  "${GIT_USER_NAME:-Company Developer}"
git config --global user.email "${GIT_USER_EMAIL:-dev@company.local}"
git config --global init.defaultBranch main
git config --global credential.helper cache

exec code-server --bind-addr 0.0.0.0:8080 --auth password /home/coder/workspace
```

| Line | Why |
|------|-----|
| `git config --global …` | The committer identity comes from `.env` — the developer never configures git by hand; commits inside the container carry a proper identity |
| `credential.helper cache` | If a developer authenticates to a *real* remote (e.g. pastes a token), it's cached in memory instead of demanded every push |
| `--bind-addr 0.0.0.0:8080` | Listen on all interfaces — Docker port publishing can't reach a process bound to container-localhost |
| `--auth password` | Login page; the password is the `PASSWORD` env var |
| `/home/coder/workspace` | The folder code-server opens on launch — exactly where the volume mounts the repo |
| `exec` | Replaces the shell → code-server is the container's main process and gets signals directly |

## 6. `backend/Dockerfile.runtime` — the deps-only image

```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi==0.115.6 "uvicorn[standard]==0.32.1" psycopg2-binary==2.9.10
EXPOSE 8000
WORKDIR /workspace
CMD ["uvicorn", "--app-dir", "/workspace/core-app/backend", "app.main:app",
     "--host", "0.0.0.0", "--port", "8000",
     "--reload", "--reload-dir", "/workspace/core-app/backend"]
```

| Decision | Why |
|----------|-----|
| No `COPY` of source | The source doesn't exist at build time — it's cloned into the volume at runtime. This is the inversion vs Project 1's `backend/Dockerfile` (which does `COPY app`) |
| Dependencies baked in | `pip install` needs the requirements *content*; baking the exact same pinned list keeps the image self-sufficient |
| `WORKDIR /workspace` (shallow) | **Bug fix #2's other half.** A deep `WORKDIR /workspace/core-app/backend` creates those directories **in the image**, and Docker pre-populates fresh named volumes with image mount-point contents — breaking `repo-init`'s clone. Shallow WORKDIR + explicit `--app-dir`/`--reload-dir` flags avoids creating the deep path |
| `--reload-dir /workspace/core-app/backend` | uvicorn's file watcher follows the mounted volume — inotify events work because named volumes are native Linux filesystems inside the Docker Desktop VM |

## 7. `frontend/nginx.conf`

Same proxy pattern explained in
[`COREAPP-CODE.md`](../../docs/COREAPP-CODE.md) — with two
platform-specific notes:

- `root /srv/workspace/core-app/frontend;` — serves from the workspace
  volume (mounted at `/srv/workspace`).
- The `resolver 127.0.0.11 valid=5s;` + variable-`proxy_pass` block is
  **bug fix #3** (nginx cached the backend's IP at startup; after a backend
  recreate every proxied call 502'd until nginx restarted). See
  [`NETWORKING.md`](NETWORKING.md) for the full explanation.

## 8. `infra/git.Dockerfile` + `infra/gitserver-entrypoint.sh`

```dockerfile
FROM alpine:3.20
RUN apk add --no-cache git git-daemon
COPY gitserver-entrypoint.sh /usr/local/bin/gitserver-entrypoint.sh
RUN chmod +x /usr/local/bin/gitserver-entrypoint.sh
```

- Alpine → ~10 MB base. `git-daemon` is a **separate Alpine package**
  (bug fix #1 — without it, `git daemon` fails with
  `'daemon' is not a git command`).

The entrypoint script:

```sh
REPO=/srv/git/core-app.git
if [ ! -d "$REPO" ]; then
  git init --bare --initial-branch=main "$REPO"
  TMP=$(mktemp -d)
  git clone "$REPO" "$TMP/repo" 2>/dev/null || true
  cp -r /seed/. "$TMP/repo/"
  cd "$TMP/repo"
  git config user.name "Platform Bot"
  git config user.email "platform@company.local"
  git checkout -b main 2>/dev/null || git switch -c main
  git add -A
  git commit -m "Initial import of CoreApp"
  git push origin main
  ...
fi
exec git daemon --reuseaddr --verbose --export-all --enable=receive-pack \
  --base-path=/srv/git --listen=0.0.0.0 --port=9418 /srv/git
```

| Step | Why |
|------|-----|
| `if [ ! -d "$REPO" ]` | Seeding happens **only on first boot** of the `git-data` volume. Every later start keeps pushed history — this is what makes `reset.ps1` survivable |
| `git init --bare --initial-branch=main` | A *bare* repo = a Git server-side repo with no working tree; the thing you push to |
| clone → `cp /seed/.` → commit → `push origin main` | The seed files (mounted from `./seed/company-app`) become the repo's first commit, authored by "Platform Bot" |
| `git checkout -b main … || git switch -c main` | Cloning an empty repo leaves an unborn HEAD; create `main` explicitly (the `|| switch` covers older git versions) |
| `--enable=receive-pack` | **Allows push over `git://`.** Without it the daemon is read-only. This is anonymous push — fine on a private compose network, unacceptable elsewhere (see SECURITY.md) |
| `--export-all --base-path=/srv/git` | Serve every repo under `/srv/git`; the URL path maps onto the filesystem: `git://gitserver/core-app.git` → `/srv/git/core-app.git` |
| `--listen=0.0.0.0 --port=9418` | Accept connections from other containers (the default would be localhost-only) |
| `exec` | Daemon as main process → clean signal handling |

## 9. `.env.example`

Every tunable in one file; `start.ps1` copies it to `.env` on first run.
Compose reads `.env` automatically; every reference in the compose file
uses `${VAR:-default}` so the stack works even with no `.env` at all.

## 10. `scripts/` — the lifecycle

| Script | Mechanics worth knowing |
|--------|------------------------|
| `start.ps1` / `start.sh` | docker probe → copy `.env` → `compose up -d --build` → poll `/api/health`, `:3000/`, `:8080/healthz` with deadlines → print the URL table → open the browser. The PowerShell version parses `.env` itself for the banner, stripping inline comments (`^IDE_PASSWORD=([^#]+)`) |
| `stop.ps1` / `stop.sh` | `compose down` — containers and network removed, **all volumes kept** |
| `reset.ps1` / `reset.sh` | `compose down`, then surgically `docker volume rm` only `…_workspace-data` and `…_pgdata`. `git-data` survives → next start re-clones and **pushed commits come back** |
| `reset-all.ps1` | Confirmation prompt, then `compose down -v` — wipes *all* volumes including git-data → next start re-seeds from `seed/company-app` (factory reset) |

---

## 11. The build story — three real bugs

These were found by actually running the stack, not anticipated:

### Bug 1 — `git: 'daemon' is not a git command`
Alpine splits the daemon into its own package. Fix: `apk add git git-daemon`.
Lesson: a healthcheck that proves *behaviour* (`git ls-remote`) caught the
readiness lie immediately.

### Bug 2 — clone fails: `/workspace/core-app already exists and is not empty`
Docker initialises a **fresh named volume with the image contents of the
mount point**. The backend image's deep `WORKDIR` had created
`workspace/core-app/backend/` in the image; the new volume inherited those
empty dirs, and `git clone` (correctly) refuses a non-empty target.
Fix: (a) backend image uses shallow `WORKDIR /workspace` + `--app-dir`;
(b) `repo-init` `rm -rf`s a non-Git target before cloning (safe: real work
always contains `.git`).

### Bug 3 — 502s after backend recreation
nginx resolves upstream hostnames **once at startup**. Recreate backend →
new IP → nginx kept talking to the old one. Fix: `resolver 127.0.0.11
valid=5s;` + `set $backend_upstream …; proxy_pass $backend_upstream;` —
variables force request-time DNS lookups against Docker's embedded DNS.
Applied to every nginx.conf in the repo.

## 12. Verified behaviour (actually executed during the build)

- All 6 services `healthy` via their healthchecks ✓
- Browser → `:3000` → nginx → `/api/*` → backend → Postgres ✓
  (list/create/toggle/delete all exercised)
- `code-server` `/healthz` 200; login page on `:8080` ✓
- Inside the IDE container as `coder`: edit → commit → `git push` →
  `gitserver` log confirms `main` updated ✓
- **Disposability proof:** `reset` (workspace+db destroyed, git kept) →
  restart → fresh clone contains the pushed commit ✓
- `scripts/start.ps1` run end-to-end: builds, waits, prints the banner,
  opens the browser ✓

## 13. Extend it

| You want | Change |
|----------|--------|
| Real company Git | Point `repo-init`'s clone URL at it, delete `gitserver`, add auth (see GIT-WORKFLOW.md) |
| More app services (Redis, worker) | Add services on `devnet`; backend reaches them by service name |
| One URL for everything | Add an nginx gateway service publishing one port with `/ide`, `/api`, `/` routes (see NETWORKING.md) |
| VS Code extensions pre-installed | Add `code --install-extension <id>` lines to `ide/entrypoint.sh` before `exec` |
| Inner Docker daemon (DinD) | Give the IDE container its own daemon or mount the host socket — with the networking consequences in NETWORKING.md |
