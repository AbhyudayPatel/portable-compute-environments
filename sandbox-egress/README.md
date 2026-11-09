# sandbox-egress - Network policy, record/replay, zero-secret sandboxes

> Every sandbox can get its own **egress proxy** and an **iptables fence**:
> only the proxy may leave the sandbox network, and the proxy enforces a
> policy - `deny`, `allow` (host allowlist), `record` (allow + cassette),
> `replay` (serve from the cassette, nothing leaves). HTTPS is intercepted
> with a per-sandbox CA injected into every container. Secrets never enter
> the sandbox: it holds placeholders, the proxy swaps in the real value
> only for the right destination and scrubs it out of responses.

```
sandbox network sbx-<id> (bridge br-xxxx)
  +-- backend  HTTP(S)_PROXY=http://egress:8080, SSL_CERT_FILE=/sbx/ca.pem
  +-- db, frontend ...                      \
  +-- egress  (mitmproxy + policy addon) ----+--> internet / peer networks
                                             |
  iptables on the node:  DOCKER-USER -i br-xxxx -j SBX_<id>
     SBX_<id>:  -o br-xxxx RETURN | ESTABLISHED,RELATED RETURN | -s <proxy ip> RETURN | DROP
```

Why both: the proxy is *policy*, the fence is *enforcement*. A process that
ignores `HTTP_PROXY` and dials an IP directly hits the DROP rule (verified).

## Quick start

```powershell
cd ..\sandbox-api;   powershell -ExecutionPolicy Bypass -File scripts\start.ps1
cd ..\sandbox-blobs; powershell -ExecutionPolicy Bypass -File scripts\start.ps1
cd ..\sandbox-egress; powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

```bash
# a sandbox that may only talk to example.com, with a secret it never sees
curl -s -X POST localhost:9000/sandboxes -H 'content-type: application/json' -d '{
  "name":"locked","template":"coreapp",
  "options":{"egress":{"mode":"allow","allow":["example.com","api.github.com"],
             "secrets":{"GH_TOKEN_PLACEHOLDER":{"host":"api.github.com","value":"ghp_real"}}}}}'
# inside: curl -H "Authorization: Bearer GH_TOKEN_PLACEHOLDER" https://api.github.com/user
# the proxy sends ghp_real; the sandbox never holds it.

curl -s localhost:9000/sandboxes/<id>/egress               # policy, stats, fence
curl -s -X PUT localhost:9000/sandboxes/<id>/egress -H 'content-type: application/json' -d '{"mode":"record","allow":["example.com"]}'
curl -s -X POST localhost:9410/sandboxes/<id>/cassettes/ship -d '{"label":"run1"}' -H 'content-type: application/json'
bash scripts/verify.sh      # 39 assertions, ~8 min (first run builds the proxy + upstream images inside dind)
```

## Modes

| Mode | Leaves the sandbox? | Behaviour |
|---|---|---|
| `deny` | nothing | every request 403 `X-Sandbox-Egress: deny` |
| `allow` | allowlisted hosts (globs) | others 403 `blocked` |
| `record` | allowlisted hosts | every exchange appended to `/sbx/cassette.jsonl` (placeholders only) |
| `replay` | nothing | responses from the cassette, matched on method + URL + body hash; unknown -> 599 (`strict_replay`) |

Policy changes are live: `PUT /sandboxes/{id}/egress` rewrites
`/sbx/policy.json` through the proxy container's agent; the addon re-reads
it on the next request.

## API

sandbox-api (enforcement):

| Method | Path | Notes |
|---|---|---|
| POST | `/sandboxes` | `options.egress = {mode, allow[], secrets{}, strict_replay, peer_networks[]}` |
| GET | `/sandboxes/{id}/egress` | `{policy, stats, fence}` |
| PUT | `/sandboxes/{id}/egress` | change policy live |
| GET/PUT/DELETE | `/sandboxes/{id}/egress/cassette` | the recording (NDJSON) |

sandbox-egress (cassettes as artefacts):

| Method | Path | Notes |
|---|---|---|
| POST | `/sandboxes/{id}/cassettes/ship` | `{label}` -> blob store (owner `cassette:<id>`), deduplicated |
| POST | `/sandboxes/{id}/cassettes/{cid}/load` | blob -> sandbox, mode `replay` |
| GET/DELETE | `/cassettes[/{cid}]` | catalogue |
| GET/PUT | `/sandboxes/{id}/egress` | passthrough |

## Docs

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) - the proxy, the CA, the
  fence, cassettes, placeholder secrets, why not `--internal` networks.
- [`docs/EDGE-CASES.md`](docs/EDGE-CASES.md) - cases and verify.sh mapping.
- [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) - files, build story, bugs.
- [`docs/EXAMPLES.md`](docs/EXAMPLES.md) - exercises.
- [`docs/SECURITY.md`](docs/SECURITY.md) - what this protects against and what it does not (DNS).
