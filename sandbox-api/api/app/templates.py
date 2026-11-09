"""Sandbox templates.

A template answers:
  spec(sid, name, port)  -> network + container specs for a new sandbox
  images                 -> images that must exist in the INNER engine first
                            (pulled, or BUILT inside dind from a tar context)

v2 changes:
  * CoreApp is no longer inline source: the build contexts come from the
    `coreapp/` directory that is a synced copy of the repo-root canonical
    app (scripts/sync-coreapp.ps1). Image tags carry a content hash, so
    changing one byte of the app yields a new tag and a rebuild - the
    "stale image behind an old tag" bug can no longer happen.
  * every container is wrapped by the in-sandbox agent (agent_mode init)
    unless the image needs its own PID 1 (agent_mode exec, e.g. s6 desktops).
  * templates declare their default exec target, fs jail, fingerprint path
    and which isolation modes they support.
"""
import hashlib
import io
import os
import tarfile
from pathlib import Path

COREAPP_DIR = Path(os.environ.get("COREAPP_DIR", "/app/coreapp"))
DESKTOP_DIR = Path(os.environ.get("DESKTOP_DIR", "/app/desktop"))
EGRESS_DIR = Path(os.environ.get("EGRESS_DIR", "/app/egress"))
UPSTREAM_DIR = Path(os.environ.get("UPSTREAM_DIR", "/app/upstream"))
WORKER_DIR = Path(os.environ.get("WORKER_DIR", "/app/worker"))


# -- build contexts -----------------------------------------------------------

def _tar_dir(root: Path, extra: dict[str, str] | None = None) -> tuple[io.BytesIO, str]:
    """Pack a directory (plus generated files) into an in-memory tar build
    context. Returns (tar, content-hash). Deterministic: sorted names, fixed
    mtimes, so the same files always hash the same. Executable bits are
    kept for files whose name ends in .sh or sits under bin/."""
    buf = io.BytesIO()
    h = hashlib.sha256()
    files: dict[str, bytes] = {}
    if root.exists():
        for p in sorted(root.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                files[p.relative_to(root).as_posix()] = p.read_bytes()
    # the worker context is assembled from several places
    if root == WORKER_DIR:
        app_dir = Path(__file__).parent
        for p in sorted(app_dir.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                files["app/" + p.relative_to(app_dir).as_posix()] = p.read_bytes()
        for p in sorted(COREAPP_DIR.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                files["coreapp/" + p.relative_to(COREAPP_DIR).as_posix()] = p.read_bytes()
        agent = Path(os.environ.get("AGENT_BINARY", "/app/bin/sbx-agent"))
        if agent.exists():
            files["bin/sbx-agent"] = agent.read_bytes()
    for name, content in (extra or {}).items():
        files[name] = content.encode()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in sorted(files):
            data = files[name]
            h.update(name.encode() + b"\0" + data + b"\0")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            # fixed, NON-ZERO mtime: deterministic hashing, but tools that treat
            # "mtime <= 0" as "nothing to load" (mitmproxy's script loader does)
            # still see a real timestamp
            info.mtime = 1_000_000_000
            info.mode = 0o755 if (name.endswith(".sh") or name.startswith("bin/")) else 0o644
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf, h.hexdigest()[:12]


def _context_hash(root: Path, extra: dict[str, str] | None = None) -> str:
    return _tar_dir(root, extra)[1]


COREAPP_FRONTEND_DOCKERFILE = """\
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html app.js styles.css /usr/share/nginx/html/
"""

COREAPP_DB_DOCKERFILE = """\
FROM postgres:16-alpine
COPY init.sql /docker-entrypoint-initdb.d/01-init.sql
"""

# image name -> (directory, generated files). Tags are computed lazily from
# the context hash so the catalogue stays in sync with the files on disk.
BUILDABLE = {
    "sandbox-coreapp-backend": (COREAPP_DIR / "backend", {}),
    "sandbox-coreapp-frontend": (COREAPP_DIR / "frontend", {"Dockerfile": COREAPP_FRONTEND_DOCKERFILE}),
    "sandbox-coreapp-db": (COREAPP_DIR / "db", {"Dockerfile": COREAPP_DB_DOCKERFILE}),
    "sandbox-desktop": (DESKTOP_DIR, {}),
    "sandbox-egress-proxy": (EGRESS_DIR, {}),
    "sandbox-upstream": (UPSTREAM_DIR, {}),
    "sandbox-worker": (WORKER_DIR, {}),
}

_tag_cache: dict[str, str] = {}


def image_tag(short: str) -> str:
    """'sandbox-coreapp-backend' -> 'sandbox-coreapp-backend:<ctx-hash>'."""
    if short not in _tag_cache:
        root, extra = BUILDABLE[short]
        _tag_cache[short] = f"{short}:{_context_hash(root, extra)}"
    return _tag_cache[short]


def build_context_for(image: str) -> io.BytesIO | None:
    """Tar build context for a buildable image tag, else None (= pull it)."""
    short = image.split(":")[0]
    if short not in BUILDABLE:
        return None
    root, extra = BUILDABLE[short]
    return _tar_dir(root, extra)[0]


# -- web: single busybox httpd container -------------------------------------
# The page is generated at container start so each sandbox serves its own
# identity. Both files are base64-encoded by Python and decoded in the
# container: zero shell quoting, any content.

WEB_PAGE = """<!doctype html><html><head><title>{name}</title><style>
body{{font-family:system-ui;max-width:680px;margin:2rem auto;padding:0 1rem;background:#0f172a;color:#e2e8f0}}
h1{{color:#7dd3fc}}.card{{background:#1e293b;border-radius:12px;padding:1rem 1.25rem;margin:.75rem 0}}
code{{color:#fbbf24}}
</style></head><body>
<h1>&#9729; sandbox <code>{name}</code></h1>
<div class=card><b>You reached this page through 3 layers:</b><ol>
<li>Windows browser &rarr; <code>localhost:{port}</code></li>
<li>Docker Desktop &rarr; dind inner engine port <code>{port}</code></li>
<li>inner container <code>sbx-{sid}-web</code> :80</li></ol></div>
<div class=card><b>Try me:</b><ul>
<li><code>curl localhost:{port}/health</code> - machine-readable probe</li>
<li><code>POST /sandboxes/{sid}/exec</code> with <code>["hostname"]</code> - run commands inside me</li>
<li><code>POST /sandboxes/{sid}/sessions</code> - a durable shell you can resume</li>
</ul></div>
<div class=card>template <code>web</code> . id <code>{sid}</code> . port <code>{port}</code></div>
</body></html>"""

WEB_HEALTH = '{{"status":"ok","sandbox":"{name}","port":{port}}}'


def _b64(s: str) -> str:
    import base64
    return base64.b64encode(s.encode()).decode()


def web_entrypoint(name: str, port: int, sid: str) -> str:
    page = WEB_PAGE.format(name=name, port=port, sid=sid)
    health = WEB_HEALTH.format(name=name, port=port)
    return (f"mkdir -p /www && "
            f"echo {_b64(page)} | base64 -d > /www/index.html && "
            f"echo {_b64(health)} | base64 -d > /www/health && "
            f"exec httpd -f -p 80 -h /www")


def web_spec(sid: str, name: str, port: int) -> dict:
    return {
        "network": f"sbx-{sid}",
        "containers": [{
            "name": f"sbx-{sid}-web",
            "image": "busybox:1.36",
            "role": "app",
            "port": port,
            "command": ["sh", "-c", web_entrypoint(name, port, sid)],
            "ports": {"80/tcp": port},
            "environment": {},
        }],
    }


# -- blank: the agent IS the container (pure exec target, no port) -----------

def blank_spec(sid: str, name: str, port: None) -> dict:
    return {
        "network": f"sbx-{sid}",
        "containers": [{
            "name": f"sbx-{sid}-shell",
            "image": "alpine:3.20",
            "role": "app",
            "port": None,
            "command": [],                 # no workload: agent serves alone
            "ports": {},
            "environment": {},
        }],
    }


# -- coreapp: Postgres + FastAPI backend + nginx frontend, from coreapp/ ------

def coreapp_spec(sid: str, name: str, port: int) -> dict:
    db_url = "postgresql://company:company@db:5432/companydb"
    return {
        "network": f"sbx-{sid}",
        "containers": [
            {
                "name": f"sbx-{sid}-db",
                "image": image_tag("sandbox-coreapp-db"),
                "role": "db",
                "port": None,
                "command": None,
                "ports": {},
                "environment": {
                    "POSTGRES_USER": "company",
                    "POSTGRES_PASSWORD": "company",
                    "POSTGRES_DB": "companydb",
                },
                "network_aliases": ["db"],
                "healthcheck": {
                    "test": ["CMD-SHELL", "pg_isready -U company -d companydb"],
                    "interval": 2_000_000_000,
                    "timeout": 3_000_000_000,
                    "retries": 30,
                },
            },
            {
                "name": f"sbx-{sid}-backend",
                "image": image_tag("sandbox-coreapp-backend"),
                "role": "backend",
                "port": None,
                "command": None,
                "ports": {},
                "environment": {"DATABASE_URL": db_url},
                "network_aliases": ["backend"],
                "wait_for": {"container": f"sbx-{sid}-db", "healthy": True},
            },
            {
                "name": f"sbx-{sid}-frontend",
                "image": image_tag("sandbox-coreapp-frontend"),
                "role": "frontend",
                "port": port,
                "command": None,
                "ports": {"3000/tcp": port},
                "environment": {},
                "network_aliases": ["frontend"],
                "wait_for": {"container": f"sbx-{sid}-backend", "running": True},
            },
        ],
    }


# -- desktop: full XFCE desktop (KasmVNC) + Chromium + xdotool/scrot ---------
# A computer-use sandbox: the identity port serves the desktop in a browser
# tab; the agent (exec mode - s6 must stay PID 1) exposes /gui/*.

def desktop_spec(sid: str, name: str, port: int) -> dict:
    return {
        "network": f"sbx-{sid}",
        "containers": [{
            "name": f"sbx-{sid}-desktop",
            "image": image_tag("sandbox-desktop"),
            "role": "app",
            "port": port,
            "command": None,
            "ports": {"3000/tcp": port},
            "environment": {
                "PUID": "911", "PGID": "911", "TZ": "Etc/UTC",
                "TITLE": f"sandbox {name}",
                "DONT_PROMPT_WSL_INSTALL": "1",
            },
            "agent_mode": "exec",
            "agent_env": {"SBX_DISPLAY": ":1", "SBX_XAUTHORITY": "/config/.Xauthority"},
            "shm_size": "1g",
        }],
    }


# -- upstream: a deterministic "internet" for egress tests ---------------------
# One container, alias upstream.test, HTTP :80 + HTTPS :443 (self-signed).
# Egress proxies of other sandboxes join this sandbox's network through
# options.egress.peer_networks; the sandboxes themselves cannot reach it.

def upstream_spec(sid: str, name: str, port: None) -> dict:
    return {
        "network": f"sbx-{sid}",
        "containers": [{
            "name": f"sbx-{sid}-upstream",
            "image": image_tag("sandbox-upstream"),
            "role": "app",
            "port": None,
            "command": None,
            "ports": {},
            "environment": {"REAL_TOKEN": "real-token-XYZ"},
            "network_aliases": ["upstream.test"],
        }],
    }


# -- worker: a nested sandbox-api node (cluster-in-a-box) ---------------------
# One privileged container running dockerd + agent proxy + sandbox-api. The
# cluster control plane passes its API port and its identity-port block
# through options.extra_ports and options.env.

def worker_spec(sid: str, name: str, port: None) -> dict:
    return {
        "network": f"sbx-{sid}",
        "containers": [{
            "name": f"sbx-{sid}-worker",
            "image": image_tag("sandbox-worker"),
            "role": "app",
            "port": None,
            "command": None,
            "ports": {},
            "environment": {"DOCKER_TLS_CERTDIR": "", "MAX_SANDBOXES": "6"},
            "privileged": True,
            "network_aliases": ["worker"],
        }],
    }


TEMPLATES = {
    "worker": {
        "description": "a nested sandbox-api node: dockerd + agent proxy + API in one privileged container",
        "needs_port": False, "spec": worker_spec,
        "images": ["sandbox-worker"],
        "health": {"kind": "agent"}, "ready_timeout_key": "coreapp",
        "default_exec_role": "app", "fs_root": "/", "fingerprint_path": "/data",
        "isolation": ["container"],
        "allow_privileged": True,
    },
    "upstream": {
        "description": "test fixture: a fake internet host (upstream.test) for egress policies",
        "needs_port": False, "spec": upstream_spec,
        "images": ["sandbox-upstream"],
        "health": {"kind": "agent"}, "ready_timeout_key": "default",
        "default_exec_role": "app", "fs_root": "/", "fingerprint_path": "/",
        "isolation": ["container"],
    },
    "web": {
        "description": "busybox httpd serving an introspection page",
        "needs_port": True, "spec": web_spec,
        "images": ["busybox:1.36"],
        "health": {"kind": "http", "path": "/health"}, "ready_timeout_key": "web",
        "default_exec_role": "app", "fs_root": "/", "fingerprint_path": "/www",
        "isolation": ["container", "gvisor"],
        "stateful": {"app": {"path": "/www"}},
    },
    "blank": {
        "description": "alpine shell, pure exec target, no port",
        "needs_port": False, "spec": blank_spec,
        "images": ["alpine:3.20"],
        "health": {"kind": "agent"}, "ready_timeout_key": "default",
        "default_exec_role": "app", "fs_root": "/", "fingerprint_path": "/workspace",
        "isolation": ["container", "gvisor"],
        "stateful": {"app": {"path": "/workspace"}},
    },
    "coreapp": {
        "description": "3-tier task board: nginx -> FastAPI -> Postgres (from coreapp/)",
        "needs_port": True, "spec": coreapp_spec,
        "images": ["sandbox-coreapp-db", "sandbox-coreapp-backend", "sandbox-coreapp-frontend"],
        "health": {"kind": "http", "path": "/api/health"}, "ready_timeout_key": "coreapp",
        "default_exec_role": "backend", "fs_root": "/", "fingerprint_path": "/app",
        "db": {"role": "db", "user": "company", "database": "companydb"},
        "isolation": ["container", "gvisor"],
        # stateful: overlay-backed data paths (checkpoint/fork/restore) and
        # a WAL archive so the database can be replayed to any LSN
        "stateful": {
            "db": {"path": "/var/lib/postgresql/data",
                   "binds": {"wal": "/wal"},
                   "chown": {"wal": "70:70"},      # postgres uid in the alpine image
                   "command": ["postgres", "-c", "wal_level=replica", "-c", "archive_mode=on",
                               "-c", "archive_timeout=15",
                               "-c", "archive_command=test ! -f /wal/%f && cp %p /wal/%f"]},
            "backend": {"path": "/workspace"},
        },
        "quiesce": {"role": "db", "cmd": ["psql", "-U", "company", "-d", "companydb", "-c", "CHECKPOINT"]},
    },
    "desktop": {
        "description": "XFCE desktop in the browser (KasmVNC) + Chromium + GUI action API",
        "needs_port": True, "spec": desktop_spec,
        "images": ["sandbox-desktop"],
        "health": {"kind": "http", "path": "/"}, "ready_timeout_key": "desktop",
        "default_exec_role": "app", "fs_root": "/", "fingerprint_path": "/config/workspace",
        "isolation": ["container"],
        "gui": True,
        "stateful": {"app": {"path": "/config/workspace"}},
    },
}


def resolve_images(template: str) -> list[str]:
    """Image refs for a template with buildable short names expanded to
    their content-hashed tags."""
    out = []
    for img in TEMPLATES[template]["images"]:
        out.append(image_tag(img) if img in BUILDABLE else img)
    return out


def catalog() -> dict:
    return {
        name: {
            "description": t["description"],
            "needs_port": t["needs_port"],
            "images": resolve_images(name),
            "isolation": t["isolation"],
            "default_exec_role": t["default_exec_role"],
            "gui": bool(t.get("gui")),
            "stateful_paths": {r: v["path"] for r, v in (t.get("stateful") or {}).items()},
            "quiesce": t.get("quiesce"),
        }
        for name, t in TEMPLATES.items()
    }
