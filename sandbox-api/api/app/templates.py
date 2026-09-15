"""Sandbox templates.

A template answers two questions:
  spec(name, port)   -> container specs + network spec for a new sandbox
  images()           -> images that must exist in the INNER engine first

The `coreapp` template builds its images INSIDE dind by uploading a tar
build context through the Docker API (docker SDK `build(fileobj=...)`).
No registry, no bind mounts into the dind container - the pattern every
later AgentOS project uses to place images into sandbox engines.
"""
import io
import tarfile

# -- web: single busybox httpd container -------------------------------------
# The page is written at container start (entrypoint heredoc) so the same
# image serves every sandbox with its own name/port baked into the HTML.

# The page is generated at container start so each sandbox serves its own
# identity. To avoid a shell-quoting nightmare, both files are base64-
# encoded by Python and piped through `base64 -d` in the container.
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
<li>stop me, then reload - the platform notices</li>
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
            f"httpd -f -p 80 -h /www")


def web_spec(sid: str, name: str, port: int) -> dict:
    return {
        "network": f"sbx-{sid}",
        "containers": [{
            "name": f"sbx-{sid}-web",
            "image": "busybox:1.36",
            "role": "app",
            "port": port,                      # this container holds the port
            "command": ["sh", "-c", web_entrypoint(name, port, sid)],
            "ports": {"80/tcp": port},         # identity: dind:P -> container:80
            "environment": {},
        }],
    }


# -- blank: just an idle shell container (exec target, no published port) ----

def blank_spec(sid: str, name: str, port: None) -> dict:
    return {
        "network": f"sbx-{sid}",
        "containers": [{
            "name": f"sbx-{sid}-shell",
            "image": "alpine:3.20",
            "role": "app",
            "port": None,
            "command": ["sleep", "infinity"],
            "ports": {},
            "environment": {},
        }],
    }


# -- coreapp: Postgres + FastAPI backend + nginx frontend --------------------
# Backend/frontend images are built inside dind from the tar contexts below.

COREAPP_BACKEND_DOCKERFILE = """\
FROM python:3.12-alpine
WORKDIR /app
RUN pip install --no-cache-dir fastapi==0.115.6 uvicorn==0.32.1 psycopg2-binary==2.9.10
COPY main.py .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
"""

COREAPP_BACKEND_MAIN = '''\
import os
import psycopg2
from fastapi import FastAPI

DB = os.environ["DATABASE_URL"]
app = FastAPI()

def conn():
    return psycopg2.connect(DB)

@app.on_event("startup")
def startup():
    with conn() as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS tasks(
            id serial PRIMARY KEY, title text NOT NULL,
            done boolean NOT NULL DEFAULT false)""")

@app.get("/api/health")
def health():
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute("SELECT 1")
        return {"status": "ok", "db": "up"}
    except Exception as e:
        return {"status": "degraded", "db": "down", "error": str(e)}

@app.get("/api/tasks")
def list_tasks():
    with conn() as c, c.cursor() as cur:
        cur.execute("SELECT id,title,done FROM tasks ORDER BY id")
        return [{"id": i, "title": t, "done": d} for i, t, d in cur.fetchall()]

@app.post("/api/tasks")
def add_task(task: dict):
    with conn() as c, c.cursor() as cur:
        cur.execute("INSERT INTO tasks(title) VALUES (%s) RETURNING id",
                    (task["title"],))
        return {"id": cur.fetchone()[0]}

@app.post("/api/tasks/{task_id}/toggle")
def toggle_task(task_id: int):
    with conn() as c, c.cursor() as cur:
        cur.execute("UPDATE tasks SET done = NOT done WHERE id=%s", (task_id,))
        return {"id": task_id}

@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int):
    with conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM tasks WHERE id=%s", (task_id,))
        return {"id": task_id}
'''

COREAPP_FRONTEND_DOCKERFILE = """\
FROM nginx:1.27-alpine
COPY default.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html
"""

COREAPP_FRONTEND_CONF = """\
server {
    listen 80;
    location / { root /usr/share/nginx/html; index index.html; }
    location /api/ { proxy_pass http://backend:8000; }
}
"""

COREAPP_FRONTEND_HTML = """\
<!doctype html>
<html><head><title>CoreApp - sandboxed task board</title>
<style>
  body{font-family:system-ui;max-width:640px;margin:2rem auto;padding:0 1rem;background:#0f172a;color:#e2e8f0}
  h1{color:#7dd3fc} .card{background:#1e293b;border-radius:12px;padding:1rem 1.25rem;margin:.75rem 0}
  input{flex:1;padding:.5rem;border-radius:8px;border:1px solid #475569;background:#0f172a;color:#e2e8f0}
  button{padding:.5rem .9rem;border-radius:8px;border:0;background:#38bdf8;cursor:pointer}
  li{display:flex;gap:.6rem;align-items:center;padding:.4rem 0;list-style:none}
  ul{padding:0} .done{text-decoration:line-through;opacity:.55}
  #health{font-size:.85rem;color:#86efac} form{display:flex;gap:.5rem}
</style></head><body>
<h1> CoreApp <span style="font-size:.6em;color:#94a3b8">sandboxed task board</span></h1>
<div class="card" id="health">checking stack...</div>
<div class="card"><form id="f"><input id="t" placeholder="New task..." autofocus><button>Add</button></form></div>
<div class="card"><ul id="tasks"></ul></div>
<script>
async function refresh(){
  const h = await (await fetch('/api/health')).json();
  health.textContent = 'backend: '+h.status+' . db: '+h.db;
  const ts = await (await fetch('/api/tasks')).json();
  tasks.innerHTML = ts.map(x=>
    `<li><input type=checkbox ${x.done?'checked':''} onclick="toggle(${x.id})">
     <span class=${x.done?'done':''}>${x.title}</span>
     <button style=margin-left:auto onclick=del(${x.id})>x</button></li>`).join('');
}
f.onsubmit = async e => { e.preventDefault();
  await fetch('/api/tasks',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({title:t.value})}); t.value=''; refresh(); };
async function toggle(id){ await fetch('/api/tasks/'+id+'/toggle',{method:'POST'}); refresh(); }
async function del(id){ await fetch('/api/tasks/'+id,{method:'DELETE'}); refresh(); }
refresh();
</script></body></html>
"""


def _tar(files: dict[str, str]) -> io.BytesIO:
    """Pack {filename: content} into an in-memory tar build context."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for fname, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(fname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf


# Images the API knows how to build on demand inside the inner engine.
BUILDABLE_IMAGES = {
    "sandbox-coreapp-backend:1.2": {
        "Dockerfile": COREAPP_BACKEND_DOCKERFILE,
        "main.py": COREAPP_BACKEND_MAIN,
    },
    "sandbox-coreapp-frontend:1.2": {
        "Dockerfile": COREAPP_FRONTEND_DOCKERFILE,
        "default.conf": COREAPP_FRONTEND_CONF,
        "index.html": COREAPP_FRONTEND_HTML,
    },
}


def coreapp_spec(sid: str, name: str, port: int) -> dict:
    db_url = "postgresql://core:core@db:5432/coredb"
    return {
        "network": f"sbx-{sid}",
        "containers": [
            {
                "name": f"sbx-{sid}-db",
                "image": "postgres:16-alpine",
                "role": "db",
                "port": None,
                "command": None,
                "ports": {},
                "environment": {
                    "POSTGRES_USER": "core",
                    "POSTGRES_PASSWORD": "core",
                    "POSTGRES_DB": "coredb",
                },
                "network_aliases": ["db"],   # backend's DATABASE_URL expects "db"
                "healthcheck": {
                    "test": ["CMD-SHELL", "pg_isready -U core -d coredb"],
                    "interval": 2_000_000_000,   # ns
                    "timeout": 3_000_000_000,
                    "retries": 30,
                },
            },
            {
                "name": f"sbx-{sid}-backend",
                "image": "sandbox-coreapp-backend:1.2",
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
                "image": "sandbox-coreapp-frontend:1.2",
                "role": "frontend",
                "port": port,
                "command": None,
                "ports": {"80/tcp": port},       # identity: dind:P -> :80
                "environment": {},
                "wait_for": {"container": f"sbx-{sid}-backend", "running": True},
            },
        ],
    }


TEMPLATES = {
    "web":     {"needs_port": True,  "spec": web_spec,
                "images": ["busybox:1.36"],
                "health": {"kind": "http"}},
    "blank":   {"needs_port": False, "spec": blank_spec,
                "images": ["alpine:3.20"],
                "health": {"kind": "running"}},
    "coreapp": {"needs_port": True,  "spec": coreapp_spec,
                "images": ["postgres:16-alpine",
                           "sandbox-coreapp-backend:1.2",
                           "sandbox-coreapp-frontend:1.2"],
                "health": {"kind": "http"}},
}


def build_context_for(image: str) -> io.BytesIO | None:
    files = BUILDABLE_IMAGES.get(image)
    return _tar(files) if files else None
