# sandbox-mcp - The platform as a Model Context Protocol server

> Any MCP-capable agent - Claude Code, IDE agents, custom harnesses - can
> create sandboxes, run commands, read and write files, fingerprint and
> diff state, drive a desktop, set egress policy and checkpoint/fork with
> **standard tool calls**. Tokens map to tenants; every call is recorded.
> The bespoke "agent sidecar RPC" of the original roadmap is replaced by
> the protocol every agent already speaks.

```
 Claude Code / any MCP client
     |  Streamable HTTP  POST /mcp   Authorization: Bearer <token>
     v
 sandbox-mcp :9420  (FastMCP, stateless)  --REST-->  sandbox-api :9000
     |  token -> tenant; names prefixed <tenant>-     sandbox-checkpoint :9040
     '-- SQLite audit: tenant, tool, sandbox, args digest, ms, ok
```

## Quick start

```powershell
cd ..\sandbox-api; powershell -ExecutionPolicy Bypass -File scripts\start.ps1
cd ..\sandbox-mcp; powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

Point Claude Code at it:

```
claude mcp add --transport http agentos http://localhost:9420/mcp --header "Authorization: Bearer dev-token"
```

and ask it to "create a web sandbox called demo, wait for it, run
`hostname` inside, write a file and fingerprint /www".

Or use the bundled real client:

```bash
docker compose run --rm client --token alice-token tools
docker compose run --rm client --token alice-token call create_sandbox '{"name":"demo","template":"web"}'
docker compose run --rm client --token alice-token call exec '{"sandbox_id":"<id>","cmd":["hostname"]}'
bash scripts/verify.sh      # 31 assertions driven by the MCP client
```

## Tools

| Tool | What it does |
|---|---|
| `list_templates` | catalogue (images, isolation modes, stateful paths) |
| `create_sandbox(name, template, isolation, stateful, egress_mode, egress_allow, ttl_seconds)` | idempotent create, name prefixed with the tenant |
| `wait_ready(sandbox_id, timeout_seconds)` | block until READY/FAILED |
| `list_sandboxes` / `get_sandbox` / `delete_sandbox` | tenant-scoped |
| `exec(sandbox_id, cmd, timeout_seconds, container)` | killed on timeout; exit code + streams |
| `read_file` / `write_file` / `list_dir` | through the in-sandbox agent |
| `fingerprint(sandbox_id, path)` / `diff(sandbox_a, sandbox_b)` | merkle root; semantic diff |
| `events(sandbox_id)` | the platform's event log for the sandbox |
| `screenshot(sandbox_id)` -> image / `gui_action(...)` | computer-use (desktop template) |
| `set_egress(sandbox_id, mode, allow)` | live network policy |
| `checkpoint(sandbox_id, label)` / `fork(checkpoint_id, name)` / `restore(checkpoint_id)` | via sandbox-checkpoint |

Resource: `sandbox://{id}/events` (NDJSON).

## Tenancy and audit

`MCP_TOKENS="dev-token:dev,alice-token:alice,bob-token:bob"`. A token
selects a tenant; `create_sandbox("demo")` makes `alice-demo`; every other
tool checks the sandbox name starts with `alice-` and returns an MCP tool
error otherwise (not a transport error - agents see it as a normal failed
call). `GET /calls` lists every call with tenant, tool, sandbox, args
digest, duration and outcome.

## Docs

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) - transport, auth, tool
  design rules, audit.
- [`docs/EDGE-CASES.md`](docs/EDGE-CASES.md) - cases mapped to verify.sh.
- [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) - files and bugs found.
- [`docs/EXAMPLES.md`](docs/EXAMPLES.md) - driving the platform from Claude Code.
- [`docs/SECURITY.md`](docs/SECURITY.md).
