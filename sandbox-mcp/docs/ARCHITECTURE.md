# sandbox-mcp - Architecture

## Transport

Streamable HTTP (the current MCP standard transport): the client POSTs
JSON-RPC to `/mcp`; responses come back as JSON (`json_response=True`) or
SSE for streams. The server is **stateless** (`stateless_http=True`): no
session affinity, any replica can answer, which matches the platform's
REST backends. The official python SDK's `FastMCP` provides the protocol;
tools are plain typed python functions whose signatures become the tool
schemas agents see.

## Auth and tenancy

A Starlette middleware checks `Authorization: Bearer <token>` on `/mcp`
and maps the token to a tenant (`MCP_TOKENS`). The tenant is stored in a
`ContextVar`, so every tool sees `current_tenant` without threading it
through arguments. Scoping is by name: sandboxes created here are
`<tenant>-<name>`, and `own(sandbox_id)` refuses anything with a different
prefix. It is simple and visible (the sandbox-api console shows who owns
what), and it needs no change to sandbox-api. A real deployment would
move ownership into sandbox-api rows and an authorising gateway.

## Tool design rules

1. **Thin.** Each tool is one or two REST calls; no logic lives here that
   sandbox-api does not already enforce.
2. **Typed.** Signatures carry types and defaults; agents get accurate
   schemas for free.
3. **Errors are tool errors.** Upstream failures and ownership violations
   raise `ToolError`, which FastMCP turns into `isError: true` results.
   The agent sees a normal failed call it can reason about, never a
   broken transport.
4. **Images are images.** `screenshot` returns an MCP `Image` content
   block, so a multimodal agent can look at the desktop directly.
5. **Everything is recorded.** The `@tracked` decorator writes a row per
   call: tenant, tool, sandbox id, sha256 of the arguments, duration,
   ok/error. `GET /calls` exposes it. Combined with sandbox-api's own
   event log this is the raw material for trajectories (T07).

## What the catalogue covers

| Area | Tools | Backed by |
|---|---|---|
| lifecycle | list_templates, create_sandbox, wait_ready, list/get/delete_sandbox | sandbox-api |
| runtime | exec, read_file, write_file, list_dir, events | sandbox-api -> agent |
| state | fingerprint, diff | sandbox-api -> agent |
| GUI | screenshot, gui_action | sandbox-api -> agent (desktop) |
| network | set_egress | sandbox-api egress |
| state in time | checkpoint, fork, restore | sandbox-checkpoint |

`create_sandbox` exposes `stateful`, `egress_mode/allow` and `isolation`
so an agent can ask for a gVisor-isolated, egress-denied, checkpointable
sandbox in one call.

## Side endpoints (plain HTTP)

`/healthz`, `/tools` (names, for curl) and `/calls` (audit) are regular
Starlette routes next to the MCP mount; they need no token.

## The bundled client

`client/mcpcli.py` is a 60-line real MCP client (official SDK, Streamable
HTTP) used by verify.sh so the battery exercises the protocol end to end -
initialize, list tools, call tools, read resources - rather than poking
JSON-RPC by hand.
