# CoreApp — the demo application, line by line

This document explains **the application code itself** — the same code that
lives in all three projects:

- `local-vscode-devcontainer/company-app/`
- `browser-dev-environment/seed/company-app/`
- `browser-linux-desktop/seed/company-app/`

Read this once; it applies everywhere. Each project's own
`docs/IMPLEMENTATION.md` then only explains its *platform* files (the stuff
that runs the app), not the app again.

## What CoreApp is

A deliberately tiny but **real** three-tier app — an internal task board:

```
Browser  ──►  nginx (frontend)  ──►  FastAPI (backend)  ──►  PostgreSQL (db)
              static files           /api/* JSON             tasks table
```

It exists so that every environment can prove the full developer loop:

> edit code → service hot-reloads → browser shows the change → commit → push

Tiny enough to read in 10 minutes; real enough to touch a database, a
reverse proxy, port publishing, health checks and CORS.

---

## `backend/requirements.txt`

```
fastapi==0.115.6
uvicorn[standard]==0.32.1
psycopg2-binary==2.9.10
```

| Line | What | Why it's needed |
|------|------|-----------------|
| `fastapi` | The web framework | Defines the REST API (`/api/tasks`, `/api/health`) with automatic validation via type hints |
| `uvicorn[standard]` | The ASGI server | Actually runs the Python app and speaks HTTP. `[standard]` pulls in `uvloop` + `watchfiles` — the latter powers `--reload` (hot reload on file save) |
| `psycopg2-binary` | PostgreSQL driver | The backend's way to talk to the `db` container. `-binary` ships precompiled wheels, so no C compiler is needed in the slim image |

**Why pin versions?** Reproducibility is the entire point of these
environments — the same repo must build the same environment on any laptop,
including six months from now.

---

## `backend/app/db.py` — database access

```python
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://company:company@localhost:5432/companydb",
)
```

The connection string comes from the **environment**, never hard-coded.
Every compose file in these projects injects
`DATABASE_URL=postgresql://company:company@db:5432/companydb` — note the
hostname `db`, the compose **service name** (containers resolve each other
by service name over the Docker network). The fallback `localhost:5432`
lets a developer run the API directly on a host, outside Docker.

```python
def get_connection(retries: int = 10, delay: float = 1.0):
    last_error = None
    for _ in range(retries):
        try:
            return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        except Exception as exc:
            last_error = exc
            time.sleep(delay)
    raise last_error
```

| Piece | Why |
|-------|-----|
| Retry loop | Containers start in dependency order but Postgres still needs a few seconds to initialise. Retrying means the API doesn't crash-loop on boot. (Compose healthchecks make this mostly redundant — it's defence in depth.) |
| `RealDictCursor` | Rows come back as dicts (`{"id": 1, "title": ...}`) instead of tuples → FastAPI can JSON-serialise them directly |
| New connection per call | Demo-simple. A production service would use a connection pool |

---

## `backend/app/main.py` — the API

### App setup

```python
app = FastAPI(title="CoreApp API", version="1.0.0")
```

Creates the ASGI application object that uvicorn serves
(`uvicorn app.main:app` = "the `app` object in the `app/main.py` module").

### CORS middleware

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)
```

**Need:** browsers block cross-origin requests by default. The frontend
(normally on `:3000`) calling the API directly on `:8000` is cross-origin.
In practice all three projects **proxy `/api/*` through nginx**, making the
call same-origin, so CORS never fires — but this middleware keeps direct
browser→`:8000` access working too, which is handy while debugging.

### Request model

```python
class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=280)
```

A Pydantic model: FastAPI validates incoming JSON automatically. POSTing
`{"title": ""}` returns `422 Unprocessable Entity` without any code of ours
running — validation as a declaration.

### `GET /api/health`

```python
@app.get("/api/health")
def health() -> dict:
    database = "up"
    try:
        conn = get_connection(retries=1, delay=0)
        conn.close()
    except Exception:
        database = "down"
    return {"status": "ok", "service": "coreapp-api", "database": database}
```

Three jobs:

1. **Compose healthcheck target** — the backend container's healthcheck
   (`python -c "urllib.request.urlopen('http://127.0.0.1:8000/api/health')"`)
   hits exactly this route; a 200 marks the container `healthy`.
2. **Launcher script gate** — `start.ps1` polls this URL before declaring
   the environment ready.
3. **Frontend status pills** — the UI shows `API: up` / `DB: up` from this
   response.

Note `retries=1, delay=0`: a health check must answer *now*, not after ten
seconds of retrying.

### `GET /api/tasks`

```python
@app.get("/api/tasks")
def list_tasks() -> list:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, title, done, created_at FROM tasks ORDER BY id;")
            return cur.fetchall()
    finally:
        conn.close()
```

The pattern every route follows: open → query in a cursor context manager →
`finally: close`. `fetchall()` returns a list of dicts (thanks to
`RealDictCursor`), which FastAPI serialises to JSON, including converting
the `created_at` timestamp to ISO-8601 strings.

### `POST /api/tasks`

```python
@app.post("/api/tasks", status_code=201)
def create_task(payload: TaskCreate) -> dict:
    ...
    cur.execute(
        "INSERT INTO tasks (title) VALUES (%s) RETURNING id, title, done, created_at;",
        (payload.title,),
    )
    task = cur.fetchone()
    conn.commit()
    return task
```

| Detail | Why |
|--------|-----|
| `status_code=201` | Proper REST: created, not generic 200 |
| `%s` parameter binding | **Never** f-string values into SQL — `%s` lets psycopg2 escape values, preventing SQL injection |
| `RETURNING ...` | Postgres returns the inserted row (with generated `id` + `created_at`) in the same round trip, so the API responds with the complete object |
| `conn.commit()` | psycopg2 opens a transaction implicitly; without commit the insert rolls back on close |

### `PATCH /api/tasks/{task_id}/toggle`

```python
cur.execute(
    "UPDATE tasks SET done = NOT done WHERE id = %s RETURNING ...;",
    (task_id,),
)
...
if task is None:
    raise HTTPException(status_code=404, detail="Task not found")
```

`{task_id}` in the decorator path becomes the `task_id: int` function
argument — FastAPI parses and validates it (a non-integer id gets a 422).
`done = NOT done` flips the boolean in SQL. No row updated → `fetchone()`
is `None` → proper 404 via FastAPI's exception type.

### `DELETE /api/tasks/{task_id}`

Same pattern: `DELETE ... RETURNING id`, 404 if nothing deleted,
`status_code=204` (success, no body) — which is why the frontend checks
`if (response.status === 204) return null;` before parsing JSON.

---

## `backend/Dockerfile`

```dockerfile
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
```

| Line | Why |
|------|-----|
| `FROM python:3.12-slim` | Debian-slim CPython: small (~120 MB), glibc (so `psycopg2-binary` wheels work), official |
| `ENV PYTHONUNBUFFERED=1` | Python flushes stdout immediately → `docker compose logs` shows logs in real time instead of after a buffer fills |
| `WORKDIR /app` | All relative paths below are inside `/app` |
| `COPY requirements.txt` **then** `RUN pip install` **then** `COPY app` | **Layer caching order.** Docker caches each layer. `requirements.txt` rarely changes → the expensive `pip install` layer stays cached; editing app code only rebuilds the cheap final `COPY` |
| `--no-cache-dir` | Don't store the pip download cache in the image → smaller image |
| `--host 0.0.0.0` | **Critical.** uvicorn's default `127.0.0.1` would only accept connections from *inside the container*; `0.0.0.0` listens on all interfaces so Docker port publishing and other containers can reach it |
| `--reload` | Watch source files and restart the server on change. In the dev stacks a bind mount/volume overlays `/app` (or the app dir), so saving a file in the IDE reloads the API within ~1 s |

---

## `frontend/nginx.conf`

```nginx
server {
    listen 3000;
    server_name _;
    root /usr/share/nginx/html;      # (in the platform stacks: /srv/workspace/core-app/frontend)
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        resolver 127.0.0.11 valid=5s;
        set $backend_upstream http://backend:8000;
        proxy_pass $backend_upstream;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

| Block | Why |
|-------|-----|
| `listen 3000` | The container serves on 3000; compose publishes `3000:3000` to the host |
| `try_files $uri $uri/ /index.html;` | SPA fallback: real files are served directly; unknown paths get `index.html` so client-side routing survives refreshes |
| `location /api/` | The reverse-proxy trick: the browser only ever talks to **one origin** (`localhost:3000`); nginx forwards API calls to the backend container. No CORS, no backend port in the JavaScript |
| `resolver 127.0.0.11 valid=5s;` + `set $backend_upstream ...` + `proxy_pass $backend_upstream;` | **The 502 fix.** Plain `proxy_pass http://backend:8000;` resolves the hostname **once at nginx startup**; recreate the backend container (new IP) and nginx 502s until restarted. Using a variable forces **request-time** DNS lookups against Docker's embedded DNS (`127.0.0.11`), with a 5-second cache TTL |
| `proxy_set_header ...` | Preserves the original Host and client IP for the backend's logs |

## `frontend/index.html` / `app.js` / `styles.css`

Plain static SPA — no build step, so nginx can serve it as-is.

- **`index.html`** — the shell: status pills (`#api-status`, `#db-status`),
  the add-task form, the `<ul id="task-list">`, and a footer documenting
  the request path. Loads `styles.css` and `app.js`.
- **`app.js`** — all behaviour:
  - `api(path, options)` — one `fetch` wrapper for `/api/*`; adds the JSON
    header, throws on non-2xx, and special-cases `204` (empty body).
  - `refreshHealth()` — GET `/api/health`, paints the pills green/red;
    re-runs every 10 s via `setInterval`.
  - `renderTask(task)` — builds an `<li>` with a checkbox (PATCH toggle),
    title, and delete button (DELETE).
  - `loadTasks()` — GET `/api/tasks`, re-renders the list.
  - form submit → POST `/api/tasks`, clear input, reload list.
- **`styles.css`** — a small dark theme using CSS custom properties
  (`--bg`, `--accent`, …). Nothing load-bearing; safe to restyle as an
  exercise.

Because the frontend calls **same-origin** `/api/*`, this JavaScript works
unchanged whether the stack runs on your laptop, in a container, or behind
a future gateway — the proxy decides where the API lives.

## `db/init.sql`

```sql
CREATE TABLE IF NOT EXISTS tasks (...);
INSERT INTO tasks (title, done) VALUES (...) ON CONFLICT DO NOTHING;
```

Mounted into Postgres at `/docker-entrypoint-initdb.d/01-init.sql`, which
the official Postgres image executes **once, only when the data volume is
empty** (first boot of `pgdata`). Creates the `tasks` table and seeds three
rows. `IF NOT EXISTS` / `ON CONFLICT DO NOTHING` make it idempotent so
re-running it by hand never fails.

> Re-seeding after data exists requires wiping the volume
> (`reset.ps1` / `reset-all.ps1` do exactly that).

## Repo-level `docker-compose.yml` — "the repo runs itself"

A standalone stack (backend + frontend + db, `name: coreapp`) living inside
the application repo. Its jobs:

1. Lets anyone run the app with one `docker compose up` — no platform, no
   IDE, no VS Code needed (used by Project 1's `start-app-stack.ps1`).
2. Documents the app's runtime topology **in the repo itself**.
3. In Project 2/3 it is *informational* (the platform orchestrates the app
   from the workspace volume instead) — and becomes functional the day the
   environment gets its own inner Docker daemon (DinD roadmap).

## `.gitattributes` / `.gitignore`

- `.gitattributes`: `* text=auto eol=lf` — normalise line endings to LF on
  commit. Shell scripts with CRLF explode inside Linux containers
  (`/bin/sh^M: bad interpreter`); this makes the repo safe to clone from
  Windows.
- `.gitignore`: `__pycache__/`, `.env`, `node_modules/`, `.vscode/` — keeps
  caches, secrets and editor state out of Git.

---

## Where to go next

| Project | Its implementation doc |
|---------|------------------------|
| Local VS Code + Dev Containers | `local-vscode-devcontainer/docs/IMPLEMENTATION.md` |
| Browser VS Code (code-server) | `browser-dev-environment/docs/IMPLEMENTATION.md` |
| Full Linux OS in the browser | `browser-linux-desktop/docs/IMPLEMENTATION.md` |
