# sandbox-egress - Security Notes

## What it protects against (verified)

- A process inside the sandbox dialling the outside directly, ignoring
  the proxy: dropped by the per-sandbox iptables chain (verify 5).
- A process talking to a non-allowlisted host through the proxy: 403
  (verify 6).
- A secret leaving the sandbox: the sandbox never holds it; the proxy
  injects it only toward the configured host and scrubs it out of
  responses (verify 8).
- Hidden network dependence during a replayed run: the cassette is the
  only source; unknown requests fail loudly with 599 (verify 11).
- Secrets in recordings: cassettes hold placeholders only (verify 9).

## What it does not protect against (documented)

- **DNS as a channel.** Names are resolved by dockerd's embedded DNS from
  the node namespace; the sandbox can encode data in queries. Mitigation:
  give egress-policed sandboxes a `dns` pointing at the proxy container
  (which answers only allowlisted names) or a null resolver - a
  configuration change, not implemented here.
- **The proxy container itself.** It is inside the sandbox network with
  full egress; it is a trusted component. A compromise of the proxy (a
  mitmproxy bug reachable from the sandbox side) defeats the policy.
  Mitigation: run it under gVisor as well (templates can declare the
  proxy's isolation), keep mitmproxy pinned and updated.
- **The CA key** lives in the proxy container (`/sbx/ca/mitmproxy-ca.pem`).
  It is per sandbox and only trusted inside that sandbox, so leaking it
  lets an attacker impersonate hosts *to that sandbox only*.
- **Certificate pinning** in a client will break interception (expected;
  such clients need an explicit bypass or a `NO_PROXY` entry, which the
  fence would then block).
- **`ssl_insecure=true`** in this lab means the proxy does not verify
  upstream certificates (needed for the self-signed test upstream).
  Production: `false`, with the proxy trusting the system bundle.
- **No authentication** on the policy endpoints (local lab): anyone who
  can reach sandbox-api can relax a policy.

## Operational notes

- Policy changes are logged in the sandbox's event stream (`type: egress`).
- `GET /sandboxes/{id}/egress` shows the live iptables chain so an
  operator can audit the fence without shelling into the node.
- Deleting the sandbox removes the chain; a leftover chain (`iptables -S
  DOCKER-USER | grep SBX_`) after deletion would indicate a bug.
