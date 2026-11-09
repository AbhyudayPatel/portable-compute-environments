# sandbox-mcp - Edge Cases

**VERIFIED** = `scripts/verify.sh` (31 assertions driven by a real MCP
client in a container); **EXPECTED** = reasoned.

| # | Edge case | Handling | Test |
|---|---|---|---|
| 1 | No / bad token | middleware answers 401 before any JSON-RPC | 2 |
| 2 | Tenant A touches tenant B's sandbox | `own()` compares the name prefix; tool error `not yours`, nothing executed | 4 |
| 3 | Listing leaks | `list_sandboxes` filters by prefix | 4 |
| 4 | Resource access across tenants | `own()` in the resource handler too | 5 |
| 5 | Upstream errors (404, 409, 503) | wrapped as `ToolError` -> MCP `isError: true` with the detail; the session stays usable | 6 |
| 6 | Unexpected exceptions in a tool | caught by `@tracked`, recorded with `ok=0`, surfaced as a tool error | 6, 7 |
| 7 | Long operations (`wait_ready`, `exec`) | bounded by `timeout_seconds`; HTTP timeouts sized above them | 3 |
| 8 | Binary results (screenshot) | returned as an MCP image content block, not text | EXPECTED in verify (desktop image is heavy); exercised manually |
| 9 | Audit without leaking content | only a sha256 digest of the arguments is stored | 7 |
| 10 | Checkpoint service absent | `ckpt()` raises a tool error "not configured" | EXPECTED (configured in compose) |
| 11 | Name collisions between tenants | prefixing makes `alice-demo` and `bob-demo` distinct sandboxes | by construction |
| 12 | Idempotent create via MCP | same name + spec -> sandbox-api returns the existing one; the tool returns it | 3 (first create) |
| 13 | Stateless transport | `stateless_http=True`: no session affinity, any request self-contained | by construction |

## Known limits

- Streaming tool output (live session frames) is not exposed; `exec`
  returns when the command ends. An MCP progress notification channel is
  the natural extension.
- Tokens are static and in `.env`.
