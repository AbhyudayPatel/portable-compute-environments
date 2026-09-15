# Networking — Browser Development Environment

## Port map (host = your laptop)

| Host port | Container:port | Service | Purpose |
|-----------|----------------|---------|---------|
| 8080 | ide:8080 | code-server | Browser IDE |
| 3000 | frontend:3000 | nginx | Company app UI |
| 8000 | backend:8000 | uvicorn | Company API |
| 5432 | db:5432 | postgres | DB access from host tools |
| — | gitserver:9418 | git daemon | internal only (not published) |

All host ports are configurable in `.env`.

## The three networking perspectives

This is the part that confuses everyone. There are three different answers
to "how do I reach the backend?":

### 1. From your laptop (Windows)

```
Browser/curl ──► http://localhost:8000/api/health
```

`localhost:8000` works because compose **publishes** `8000:8000` — Docker
Desktop forwards the host port into the container.

### 2. From another container on `devnet`

```
frontend nginx ──► http://backend:8000   (service name as hostname)
```

`localhost` inside a container means *that container*. Containers reach
each other by **service name** over the compose network. That is exactly
what the frontend's nginx config does with `proxy_pass http://backend:8000`.

### 3. From your browser, through the frontend (the recommended way)

```
Browser ──► http://localhost:3000/api/health
                │
                ▼
        frontend nginx ── proxy ──► backend:8000
```

The JavaScript calls **same-origin** `/api/*`; nginx forwards it. No CORS,
no backend port knowledge in the frontend code, one origin to secure later.

## Request path: adding a task

```
Windows browser
   │  POST http://localhost:3000/api/tasks
   ▼
Docker Desktop port publish :3000
   ▼
frontend container (nginx :3000)
   │  location /api/ → proxy_pass http://backend:8000
   ▼  (devnet)
backend container (uvicorn :8000)
   │  INSERT INTO tasks ...
   ▼  (devnet, hostname "db")
db container (postgres :5432)
```

## Adding a new service (recipe)

1. Add it to `docker-compose.yml` on the `devnet` network.
2. Give it a healthcheck if anything should wait for it.
3. Decide how it is reached:
   - **container-to-container**: nothing to do — use its service name.
   - **from your laptop**: publish a port (`"HOST:CONTAINER"`), pick a free
     host port, document it.
   - **through the frontend proxy**: add a `location` block in
     `frontend/nginx.conf`.

## Gotcha: nginx caches upstream IPs (502 after backend restart)

Classic Docker footgun, hit and fixed during this build:

```
proxy_pass http://backend:8000;      # hostname resolved ONCE at nginx startup
```

nginx resolves the upstream hostname at startup and caches the IP forever.
Recreate the backend container (new IP) and every proxied request becomes
502 until nginx restarts. The fix — force request-time resolution against
Docker's embedded DNS (127.0.0.11):

```
location /api/ {
    resolver 127.0.0.11 valid=5s;
    set $backend_upstream http://backend:8000;
    proxy_pass $backend_upstream;      # variable => re-resolved per request
    ...
}
```

All frontend nginx configs in this repo use this pattern. If you ever see a
502 right after a backend redeploy while `curl localhost:8000` works fine,
this is why.

## Where this goes next

### Reverse-proxy gateway (one port, many services)

Instead of publishing every service, publish one nginx gateway:

```
localhost:80
   ├── /ide  → ide:8080
   ├── /api  → backend:8000
   └── /     → frontend:3000
```

(code-server needs websocket headers — `Upgrade`/`Connection` — proxied
correctly; that is the one detail to get right.)

### Port gateway (dynamic environments)

If you later run *multiple* environments (one per developer/PR), static
ports don't scale. A small gateway allocates `localhost:51001 → env A
frontend`, `51002 → env A backend`, etc.

### Docker-in-Docker networking

If the environment eventually gets its own inner Docker daemon (so
developers can run the repo's own `docker-compose.yml` *inside* the IDE
container), an inner container's published port only exists on the inner
daemon's network. Reaching it from Windows requires forwarding through the
outer layer — or, better, the gateway pattern above so only the outer
environment needs published ports. That complexity is the reason DinD is a
roadmap item and not part of the current stack.
