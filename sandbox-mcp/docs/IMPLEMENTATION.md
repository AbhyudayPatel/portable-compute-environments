# sandbox-mcp - Implementation Notes

## Files

```
sandbox-mcp/
+-- docker-compose.yml     `mcp` service (joins sandbox-api_sandnet) + `client` test image (profile test)
+-- .env.example           MCP_PORT, SANDBOX_API_URL, CHECKPOINT_URL, MCP_TOKENS
+-- server/
|   +-- Dockerfile         python:3.12-slim + mcp SDK + requests + uvicorn
|   +-- app/main.py        FastMCP tools/resources, auth middleware, audit, side routes
+-- client/
|   +-- Dockerfile, mcpcli.py   a real MCP client (official SDK, Streamable HTTP) for verify.sh and humans
+-- scripts/  start/stop/reset.ps1, verify.sh (31 assertions)
+-- docs/     ARCHITECTURE . EDGE-CASES . EXAMPLES . SECURITY . this file
```

## Key implementation points

- `FastMCP("agentos-sandboxes", stateless_http=True, json_response=True)`;
  the ASGI app from `mcp.streamable_http_app()` is mounted under a
  Starlette app that adds `/healthz`, `/tools`, `/calls` and the auth
  middleware. The MCP app's lifespan is passed through so its session
  manager starts.
- Tenant in a `ContextVar` set by the middleware; `own()` enforces the
  name prefix on every sandbox-scoped tool and resource.
- `@tracked` wraps every tool: timing, argument digest, outcome, into
  SQLite; failures become `ToolError` (-> `isError`), never 500s.
- `screenshot` returns `mcp.server.fastmcp.utilities.types.Image`.
- The client image is built only for the `test` profile; verify.sh runs
  it with `docker compose run --rm client ...` so the battery speaks the
  real protocol end to end.

## Bugs found while building

1. **Tool-name shadowing.** A tool named `exec` shadows Python's builtin
   inside the module; harmless here (nothing uses the builtin after the
   definition) but easy to trip on - kept with a note rather than renamed,
   because `exec` is the name agents expect.

## Verified

- [x] `GET /tools` publishes 19 tools; `GET /healthz` sees sandbox-api.
- [x] `bash scripts/verify.sh` - see the RESULT line of the latest run
      (auth, tools through a real session, tenant isolation, resources,
      error semantics, audit trail, checkpoint tools, delete).
