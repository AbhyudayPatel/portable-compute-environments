# sandbox-mcp - Exercises

## Level 0 - Talk MCP with the bundled client (3 min)

```bash
cd sandbox-mcp
docker compose run --rm client --token dev-token tools
docker compose run --rm client --token dev-token call list_templates '{}'
```
**Observe:** a real MCP session (initialize, tools/list, tools/call) over
Streamable HTTP. Try without `--token`: a 401 before any JSON-RPC.

## Level 1 - A full loop (5 min)

```bash
docker compose run --rm client --token dev-token call create_sandbox '{"name":"demo","template":"web"}'
docker compose run --rm client --token dev-token call wait_ready '{"sandbox_id":"<id>"}'
docker compose run --rm client --token dev-token call exec '{"sandbox_id":"<id>","cmd":["sh","-c","echo hi; hostname"]}'
docker compose run --rm client --token dev-token call write_file '{"sandbox_id":"<id>","path":"/www/x.txt","content":"from mcp"}'
docker compose run --rm client --token dev-token call fingerprint '{"sandbox_id":"<id>","path":"/www"}'
curl -s localhost:9420/calls | python -m json.tool | head -40
```
**Teaches:** every call landed in the audit table with your tenant.

## Level 2 - Claude Code as the agent (10 min)

```
claude mcp add --transport http agentos http://localhost:9420/mcp --header "Authorization: Bearer dev-token"
```
Then, in a Claude Code session: *"Using the agentos tools, create a coreapp
sandbox called shop, wait for it, add a task through its API with curl from
inside the backend container, then fingerprint the database."* Watch the
sandbox-api console at http://localhost:9000 while it works; watch
`GET /calls` fill up.

## Level 3 - Tenants (5 min)

Create a sandbox with `alice-token`, then try `get_sandbox` with
`bob-token`: a tool error ("not yours"), not a transport error. List with
bob: empty. **Teaches:** scoping by token without changing sandbox-api.

## Level 4 - Safer sandboxes for untrusted tasks (5 min)

```bash
docker compose run --rm client --token dev-token call create_sandbox \
  '{"name":"locked","template":"coreapp","isolation":"gvisor","egress_mode":"deny","stateful":true}'
```
Inside, `exec` a `python -c "import urllib.request; urllib.request.urlopen('http://example.com')"`:
403 from the proxy. `set_egress` to `allow` with `["example.com"]` and
retry. Then `checkpoint`, break something with `exec`, `restore`.

## Level 5 - Expert: computer use (10 min)

Create a `desktop` sandbox, `wait_ready` (first time pulls a 3.9 GB base
inside the engine), then `screenshot` - the client prints the image size;
a multimodal agent sees the picture. `gui_action` with `type: "key",
keys: "super"` opens the menu; screenshot again.
