# sandbox-mcp - Security Notes

- **Tokens are static** (`MCP_TOKENS` in `.env`). Fine for a lab; a real
  deployment issues per-user tokens from an identity provider and
  revokes them.
- **Tenancy by name prefix** stops a token from touching another tenant's
  sandboxes through this server, but sandbox-api itself remains
  unauthenticated on its own port: the MCP server is an authorising
  facade, not a wall. Bind sandbox-api to the compose network only when
  exposing MCP beyond localhost.
- **Capabilities are broad.** `exec`, `write_file` and `gui_action` are
  arbitrary code execution inside a sandbox - by design. The blast radius
  is the sandbox's isolation level (`container` or `gvisor`), its egress
  policy, and whether it is stateful. An agent harness should choose
  `isolation="gvisor"` and `egress_mode="deny"` or an allowlist for
  untrusted tasks.
- **Audit, not just logs.** `/calls` records argument digests, not
  arguments, so prompts and file contents do not end up in the audit
  table; sandbox-api's event log records commands (short form). Treat
  both as sensitive.
- **No rate limits.** A runaway agent can create sandboxes until
  sandbox-api's capacity (429) stops it; per-tenant quotas belong in the
  scheduler layer.
- **Resources** (`sandbox://{id}/events`) are ownership-checked like tools.
