# Implementation — Project 3: Full Linux OS in the Browser

This document explains **every file unique to this project**, line by line,
plus the build story with every bug we hit. The platform services
(gitserver, repo-init, backend, frontend, db) are *identical in design* to
Project 2 — read
[`browser-dev-environment/docs/IMPLEMENTATION.md`](../../browser-dev-environment/docs/IMPLEMENTATION.md)
for their line-by-line treatment, and
[`docs/COREAPP-CODE.md`](../../docs/COREAPP-CODE.md) for the app itself.
This file focuses on what's new: **the desktop**.

---

## 1. The 30-second mental model

```
Your browser ──► http://localhost:8080 ──► KasmVNC web client
                                              │ pixels out, keyboard/mouse in
                                              ▼
desktop container:  KasmVNC server (virtual display :1)
                        │ X11
                        ▼
                    XFCE session: xfwm4, panel, and YOUR APPS
                        ├─ VS Code (real desktop app)
                        ├─ Chromium
                        ├─ xfce4-terminal, thunar, mousepad
                        └─ git, python3, sudo apt …
```

**How a GUI fits in a browser:** there is no physical monitor. KasmVNC
creates a *virtual* X display (`:1`). XFCE and the apps draw onto it like
any X display. KasmVNC encodes that framebuffer into a web-friendly stream
and serves a web client over HTTP; the client renders pixels and sends back
input events. The desktop is, quite literally, **a video you can type
into**.

The company repo still lives in the shared `workspace-data` volume (cloned
by `repo-init`, pushed to `gitserver`), mounted into the desktop at
`/config/workspace` — and the backend/frontend containers still hot-reload
from that same volume. The platform pattern didn't change; only the *thing
you sit in front of* grew from "web IDE" to "whole machine".

## 2. File map (desktop-specific)

```
browser-linux-desktop/
├── docker-compose.yml              ← same platform + the `desktop` service
├── .env.example                    ← adds DESKTOP_PORT/TITLE/PASSWORD, PUID/PGID, TZ
├── desktop/
│   ├── Dockerfile                  ← webtop base + developer toolbox
│   └── custom-cont-init.d/
│       └── 99-desktop-setup.sh     ← per-start personalisation hook
├── infra/ backend/ frontend/ db/   ← same as Project 2 (see its docs)
├── seed/company-app/               ← the "company repo" initial content
├── scripts/                        ← start/stop/reset/reset-all (.ps1 + .sh)
└── docs/                           ← ARCHITECTURE, SECURITY, this file
```

---

## 3. `desktop/Dockerfile`, line by line

```dockerfile
FROM linuxserver/webtop:debian-xfce
```

**The single most load-bearing line.** `webtop` images are pre-built
"Linux desktop in a browser" bases maintained by linuxserver.io. Choosing
it gives us, for free:

- **KasmVNC** server + web client (much smoother than DIY TigerVNC+noVNC:
  better encoders, dynamic resizing, baked-in web UI on container port
  3000);
- the **s6-overlay** init system with the `/custom-cont-init.d/` hook
  directory — scripts placed there run as root on every container start,
  before the desktop session comes up (that's where §4 lives);
- a conventional unprivileged user **`abc`** (default uid/gid 911,
  configurable via `PUID`/`PGID`), whose home is `/config` — a path
  designed to be a persistent volume;
- optional HTTP basic auth via a `PASSWORD` env var.

**Why Debian and not Ubuntu:** Ubuntu ships Chromium only as a *snap*, and
snapd does not run in normal containers. Debian ships it as a plain `.deb`.
That one fact decided the base variant (`debian-xfce`).

```dockerfile
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git curl ca-certificates gnupg \
       python3 python3-pip python3-venv \
       nano htop unzip jq \
       xfce4-terminal mousepad thunar \
       chromium \
```

The developer toolbox. Notes:

- `xfce4-terminal / mousepad / thunar` are listed explicitly even though
  webtop ships an XFCE desktop — pinning them in *our* Dockerfile means the
  toolbox doesn't silently depend on the base image's app choices.
- `gnupg` + `ca-certificates` are required by the Microsoft repo dance
  below (verify the key, trust HTTPS).
- `--no-install-recommends` keeps hundreds of MB of "recommended" extras
  out of the image.

```dockerfile
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
       | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/code stable main" \
       > /etc/apt/sources.list.d/vscode.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends code \
    && rm -rf /var/lib/apt/lists/*
```

Installing **the real VS Code** (the desktop app, not code-server):

1. Download Microsoft's signing key → dearmor into a keyring file.
2. Register the apt repo, pinned to that keyring (`signed-by`).
3. `apt-get update && apt-get install code`.
4. `rm -rf /var/lib/apt/lists/*` — layer hygiene.

Because the install happens through apt, VS Code's many GUI library
dependencies (libX11, libgtk, libsecret…) resolve automatically — this is
why we use the apt repo instead of copying a `.deb` and hoping.

```dockerfile
COPY custom-cont-init.d/ /custom-cont-init.d/
```

Drops §4's script into the linuxserver hook directory **inside the image**.
(Copying beats mounting here: Windows bind mounts don't reliably preserve
the executable bit. s6 also doesn't require `+x` — it runs scripts via the
shell — but in-image placement removes the whole class of problem.)

---

## 4. `desktop/custom-cont-init.d/99-desktop-setup.sh`, line by line

Runs **at every container start, as root, before the desktop session**.

```bash
ABC_HOME=/config
GIT_USER_NAME="${GIT_USER_NAME:-Company Developer}"
GIT_USER_EMAIL="${GIT_USER_EMAIL:-dev@company.local}"
```

- `/config` is user `abc`'s home in webtop images. Every personalisation
  targets that home, so it lands on the persistent `desktop-config` volume
  and survives `reset.ps1`.

### Git identity

```bash
su abc -c "HOME=$ABC_HOME git config --global user.name  '$GIT_USER_NAME'"  || true
su abc -c "HOME=$ABC_HOME git config --global user.email '$GIT_USER_EMAIL'" || true
su abc -c "HOME=$ABC_HOME git config --global init.defaultBranch main"      || true
su abc -c "HOME=$ABC_HOME git config --global credential.helper cache"      || true
```

| Detail | Why |
|--------|-----|
| `su abc -c "…"` | Run **as the desktop user**, not root — the config must land in abc's home, and files must be abc-owned |
| `HOME=$ABC_HOME` inside the command | **Gotcha #4 from the build:** `su` (without `-`) keeps the *caller's* environment, so `HOME` would stay `/root` and `git config --global` would write `/root/.gitconfig` — invisible to the desktop user. Forcing `HOME=/config` fixes it |
| `\|\| true` | Personalisation must never block the desktop from booting |
| `credential.helper cache` | If a real remote is added later, a token entered once is remembered (in memory) instead of re-asked every push |

### The `code` wrapper (the two Electron-in-container fixes)

```bash
cat > /usr/local/bin/code <<'EOF'
#!/bin/bash
export DONT_PROMPT_WSL_INSTALL=1
exec /usr/bin/code --no-sandbox "$@"
EOF
chmod +x /usr/local/bin/code
```

Two bugs are defeated by these three lines:

1. **`DONT_PROMPT_WSL_INSTALL=1`** — VS Code's launcher script sniffs
   `/proc/version`, sees the Docker Desktop **WSL2 kernel string**
   ("microsoft-standard-WSL2"), concludes "I'm inside WSL", and stops on an
   interactive *"install VS Code in Windows instead"* prompt. This
   Microsoft-provided escape hatch suppresses it. (Also set as a container
   env var in the compose file, so even `/usr/bin/code` invoked directly
   behaves.)
2. **`--no-sandbox`** — Electron's Chromium sandbox creates kernel
   namespaces; Docker's default seccomp profile forbids that, and VS Code
   dies with
   `FATAL:content/browser/zygote_host/zygote_host_impl_linux.cc … Invalid argument`.
   `--no-sandbox` disables the app's *inner* sandbox; the **container**
   remains the isolation boundary (full trade-off discussion in
   [`SECURITY.md`](SECURITY.md)).

`/usr/local/bin` precedes `/usr/bin` in `PATH`, so typing `code` in any
terminal — and the desktop shortcut below — gets the wrapper, while the
pristine binary stays untouched.

### Extension auto-install

```bash
su abc -c "HOME=$ABC_HOME DONT_PROMPT_WSL_INSTALL=1 code --install-extension ms-python.python" >/dev/null 2>&1 || true
```

Pre-installs the Python extension for abc at every boot (idempotent —
VS Code skips what exists). "Best effort" (`|| true`, output discarded): if
the laptop is offline, the desktop must still boot. In practice this pulled
`ms-python.python`, `ms-python.vscode-pylance`, `ms-python.debugpy` and
`ms-python.vscode-python-envs` during our verification.

### The Chromium wrapper

```bash
cat > /usr/local/bin/chromium-browser <<'EOF'
#!/bin/bash
exec /usr/bin/chromium --no-sandbox --disable-dev-shm-usage "$@"
EOF
chmod +x /usr/local/bin/chromium-browser
```

- `--no-sandbox`: same seccomp story as VS Code.
- `--disable-dev-shm-usage`: Chromium expects a big `/dev/shm`; containers
  default to 64 MB. We both raise `shm_size` in compose *and* pass this
  flag — belt and suspenders against tab crashes.

### Desktop welcome files

```bash
mkdir -p "$ABC_HOME/Desktop"
cat > "$ABC_HOME/Desktop/README.txt" <<'EOF'
…(welcome text: where the repo is, which URLs work from inside vs outside)…
EOF

cat > "$ABC_HOME/Desktop/code-coreapp.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=VS Code - CoreApp
Exec=/usr/local/bin/code /config/workspace/core-app
Icon=code
Terminal=false
Categories=Development;
EOF
chmod +x "$ABC_HOME/Desktop/code-coreapp.desktop"
chown -R abc:abc "$ABC_HOME/Desktop"
```

- `README.txt` — the in-desktop cheat sheet (notably: **inside** the
  desktop the app is `http://frontend:3000`, because `localhost` there is
  the desktop container; from Windows it's `http://localhost:3000`).
- `code-coreapp.desktop` — a freedesktop shortcut: double-click → the
  wrapper launches VS Code **with the repo folder open**. `Exec` points at
  the wrapper's absolute path so it doesn't depend on the session's PATH.
- `chown -R abc:abc` — files created by this root-run hook would otherwise
  be root-owned on abc's desktop.

---

## 5. `docker-compose.yml` — only the differences vs Project 2

Everything about `gitserver`, `repo-init`, `backend`, `frontend`, `db` is
the Project 2 pattern (same images' design, same healthchecks, same
depends_on graph). The differences:

```yaml
name: linux-desktop-env
```

Separate compose project → separate containers/volumes from Project 2
(`linux-desktop-env_workspace-data`, …). The reset scripts target these
names.

```yaml
  repo-init:
    ...
        chown -R ${PUID:-911}:${PGID:-911} /workspace
```

The desktop user is `abc` (uid **911** by linuxserver convention, not 1000
like code-server's `coder`), so the workspace is chowned to
`${PUID:-911}` — and it follows your `.env` if you remap the user.

### The `desktop` service

```yaml
  desktop:
    build: { context: ./desktop }
    depends_on:
      repo-init: { condition: service_completed_successfully }
    environment:
      PUID: ${PUID:-911}
      PGID: ${PGID:-911}
      TZ: ${TZ:-Etc/UTC}
      TITLE: ${DESKTOP_TITLE:-Company Linux Desktop}
      PASSWORD: ${DESKTOP_PASSWORD:-}
      GIT_USER_NAME: ${GIT_USER_NAME:-Company Developer}
      GIT_USER_EMAIL: ${GIT_USER_EMAIL:-dev@company.local}
      DONT_PROMPT_WSL_INSTALL: "1"
    volumes:
      - desktop-config:/config
      - workspace-data:/config/workspace
    ports:
      - "${DESKTOP_PORT:-8080}:3000"
    shm_size: "1gb"
    healthcheck:
      test: ["CMD", "curl", "-s", "http://127.0.0.1:3000/"]
      interval: 10s
      timeout: 5s
      retries: 30
    networks: [devnet]
```

| Field | Why |
|-------|-----|
| `PUID`/`PGID` | linuxserver images remap `abc` to these ids at boot; 911 is their default |
| `TITLE` | Browser tab title of the desktop session |
| `PASSWORD` (default empty) | webtop's optional HTTP basic auth (username `abc`). Empty = no auth on localhost — friendly demo default; set it for anything more |
| `DONT_PROMPT_WSL_INSTALL: "1"` | Container-wide fix for VS Code's WSL detection (belt; the wrapper is the suspenders) |
| `desktop-config:/config` | **The machine's personality** — dotfiles, VS Code settings/extensions, desktop files — persisted separately so `reset.ps1` (which wipes the workspace) keeps your machine configured |
| `workspace-data:/config/workspace` | The company repo at `/config/workspace/core-app`. **Nested volume mounts** (`/config` + `/config/workspace`) are legal: Docker mounts both, deepest last |
| `"${DESKTOP_PORT:-8080}:3000"` | webtop serves its web UI on container port **3000**; we publish it as 8080 so the laptop-facing URL matches the other projects. (Container-internal port collisions don't exist — each container has its own network namespace — so frontend also using container-port 3000 is fine) |
| `shm_size: "1gb"` | Chromium/Electron share memory via `/dev/shm`; Docker's 64 MB default crashes browsers |
| healthcheck `curl -s http://127.0.0.1:3000/` | Any HTTP response (even a 401 when a password is set) proves the web server is up — hence no `-f` flag |
| `retries: 30` | The first boot does real work (init scripts, extension install); give it time |

## 6. `.env.example` additions

`DESKTOP_PORT`, `DESKTOP_TITLE`, `DESKTOP_PASSWORD`, `PUID`, `PGID`, `TZ` —
all with working defaults; plus the same app/db variables as Project 2.

## 7. `scripts/`

Same lifecycle as Project 2, with three deltas:

- `start.ps1` waits on `http://localhost:8080/` (the desktop) instead of
  code-server's `/healthz`, and its banner describes the desktop.
- `reset.ps1` removes only `linux-desktop-env_workspace-data` and
  `_pgdata` — **`desktop-config` survives**, so after a reset your VS Code
  extensions, git identity and desktop shortcuts are still there while the
  repo re-clones from the Git server.
- `reset-all.ps1` (`docker compose down -v`) is the only path that also
  wipes `desktop-config` and `git-data`.

---

## 8. The build story — five real bugs

| # | Symptom | Root cause | Fix |
|---|---------|-----------|-----|
| 1 | `code --version` hangs on *"install VS Code in Windows… [y/N]"* | Docker Desktop's WSL2 kernel string in `/proc/version` makes VS Code think it's in WSL | `DONT_PROMPT_WSL_INSTALL=1` (env + wrapper) |
| 2 | VS Code window never appears; log shows `zygote_host_impl_linux.cc FATAL … Invalid argument` | Electron's sandbox needs kernel namespaces; Docker's default seccomp denies them | `code` wrapper adding `--no-sandbox` |
| 3 | Python extension missing after first boot | The init-time `code --install-extension` had hit bug #1's prompt | Fixed by the same env var; verified extensions now auto-install |
| 4 | `git config` seemed to do nothing for abc | `su abc -c` kept `HOME=/root`; config landed in `/root/.gitconfig` | `HOME=/config` forced inside every `su -c` |
| 5 | 502 from `:3000/api/*` after a backend redeploy | nginx caches upstream IPs at startup (platform-wide bug) | `resolver 127.0.0.11` + variable `proxy_pass` in **all** nginx.conf copies; see Project 2's NETWORKING.md |

Plus one environment quirk: running `git -C /config/...` from a Windows
**git-bash** shell mangles the path (MSYS turns `/config` into
`C:\Program Files\Git\config`) — the container is fine; prefix commands
with `MSYS_NO_PATHCONV=1` when exec'ing from git-bash.

## 9. Verified behaviour (actually executed)

- Desktop web client on `:8080` → 200; XFCE session + `xfwm4` running on
  the virtual display ✓
- VS Code 1.137 launches **as a desktop app on `:1`** and stays up ✓
- `ms-python.python`, `pylance`, `debugpy` auto-installed ✓
- Chromium 152 renders pages (headless DOM test) ✓
- `xfce4-terminal`, `thunar`, `mousepad`, `python3 3.13`, `git 2.47` all
  present ✓
- Git loop as abc: edit → commit → push → gitserver updated ✓
- **Reset proof:** workspace + DB wiped → restart → pushed commit back in
  the fresh clone; desktop extensions still installed ✓
- `scripts/start.ps1` run end-to-end: health gates pass, banner prints,
  browser opens ✓

## 10. Extend it

| You want | Change |
|----------|--------|
| More apps (Postman, DBeaver…) | Add to the `apt-get install` list in `desktop/Dockerfile` |
| Bigger desktop | `docker compose exec desktop …` → KasmVNC resizes with the browser window automatically; raise `shm_size` if you add heavy apps |
| Auth on the desktop | Set `DESKTOP_PASSWORD` in `.env` (username `abc`) |
| Persistent extra volume | Add another named volume and mount under `/config/…` |
| Autostart an app at login | Drop a `.desktop` file into `/config/.config/autostart/` from the init script |
| Docker inside the desktop | The DinD roadmap — see the root README and `docker-nested-lab/`; note `abc` is already in the image's `docker` group, awaiting a deliberate decision |
