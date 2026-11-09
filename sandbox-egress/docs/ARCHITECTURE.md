# sandbox-egress - Architecture

## Components

| Piece | Where | Role |
|---|---|---|
| `options.egress` | sandbox-api `engine.py` + `egress.py` | at create: CA, proxy container first, env + CA cert into the others, fence after start; at destroy: unfence |
| proxy image | `proxy/` (built inside dind by sandbox-api, tag content-hashed) | mitmproxy 11 + `addon.py`; agent-wrapped like any container |
| policy addon | `proxy/addon.py` | modes, allowlist, cassette record/replay, placeholder secrets, stats; re-reads `/sbx/policy.json` on mtime change |
| egress service | `service/` (:9410) | cassette catalogue, shipping to/from the blob store, passthroughs |
| upstream fixture | `upstream/` (template `upstream`) | deterministic fake internet for tests: `/`, `/headers`, `/auth`, `/secret`, HTTP + self-signed HTTPS |

## Create-time sequence (sandbox-api)

```
POST /sandboxes {options.egress}
  1. openssl: per-sandbox CA (cert + key), CN "sandbox-egress <id>"
  2. network sbx-<id>
  3. container sbx-<id>-egress  (alias egress; also joins peer_networks)
       put_archive /sbx/agent, /sbx/ca/mitmproxy-ca.pem (key+cert), /sbx/policy.json
       start -> record its IP
  4. the template's containers, each with
       put_archive /sbx/agent, /sbx/ca.pem (cert only)
       env HTTP_PROXY/HTTPS_PROXY=http://egress:8080, NO_PROXY=<siblings>,
           SSL_CERT_FILE/REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE/NODE_EXTRA_CA_CERTS/GIT_SSL_CAINFO/PIP_CERT=/sbx/ca.pem
  5. node (via /node/exec): chain SBX_<id> hooked into DOCKER-USER for bridge br-<netid>
  6. readiness as usual (agents + app health)
```

Rollback removes the chain; `destroy_sandbox_resources` always runs the
unfence script (a no-op when there is no chain).

## The fence, line by line

```
iptables -N SBX_<id>
iptables -A SBX_<id> -o br-<net> -j RETURN                       # sandbox-internal traffic
iptables -A SBX_<id> -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN   # replies to inbound (identity port)
iptables -A SBX_<id> -s <proxy-ip> -j RETURN                     # only the proxy may leave
iptables -A SBX_<id> -j DROP
iptables -I DOCKER-USER 1 -i br-<net> -j SBX_<id>
```

`DOCKER-USER` is evaluated before Docker's own forwarding rules, so this
wins over the default masquerade path. Why not an `--internal` network:
it blocks egress perfectly, but Docker also stops honouring published
ports on it (verified during the build), which would break the identity
port contract. The fence keeps both.

What the fence does not stop: name resolution. dockerd's embedded DNS
answers from the node's namespace, so a sandbox can still *resolve* names
(not connect). DNS-tunnel exfiltration is therefore possible in principle;
see SECURITY.md for the mitigation path.

## TLS interception

mitmproxy terminates TLS with a leaf certificate it mints under the
sandbox CA for each host, and opens its own TLS session upstream (verified
against the system bundle; `ssl_insecure=true` in this lab so the
self-signed test upstream works). Inside the sandbox, clients trust the CA
because the environment points them at `/sbx/ca.pem`. verify.sh section 7
confirms the server certificate's issuer is `sandbox-egress <id>`.

## Cassettes

One NDJSON line per exchange:

```json
{"key":"GET http://upstream.test/ <sha256(body)[:16]>","ts":...,
 "request":{"method","url","headers","body_b64"},
 "response":{"status","headers","body_b64"}}
```

Recording happens in the `response` hook *after* the request has been
un-substituted (real secret -> placeholder) and the response scrubbed, so
cassettes never contain real secrets. Replay matches on the key; hop-by-hop
and encoding headers are dropped when re-serving; unknown requests get 599
(strict) or 404.

The egress service ships a cassette to the blob store as a blob (owner
`cassette:<id>`) - identical recordings dedupe to one blob - and can load
any cassette into any sandbox, switching it to replay. A replayed run
makes zero upstream requests (verify 11, 12).

## Placeholder secrets

```json
"secrets": {"GH_TOKEN_PLACEHOLDER": {"host": "api.github.com", "value": "ghp_real"}}
```

- The sandbox only ever sees `GH_TOKEN_PLACEHOLDER` (env, files, cassettes).
- `request` hook: if the destination host matches, the placeholder is
  replaced in headers and body. For any other host it goes out verbatim
  (useless to an attacker who tricks the agent into sending it elsewhere).
- `response` hook: the real value is replaced by the placeholder in headers
  and body, so an upstream that echoes credentials cannot leak them in.

## Why this is a platform primitive

- **Reproducible agent runs** (T07/T08): record once, replay forever, no
  network flakiness, no cost; the cassette is part of the trajectory.
- **Benchmark anti-cheat** (T08): `deny`/`replay` means the agent cannot
  look things up on the live internet.
- **Prompt-injection containment** (T06): exfiltration needs a connection
  that the fence refuses and a secret the sandbox does not have.
- **Fork-PR previews** (T14): secrets never written into the sandbox.
