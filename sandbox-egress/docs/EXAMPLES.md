# sandbox-egress - Exercises

Prereqs: sandbox-api, sandbox-blobs, sandbox-egress up.
`A=localhost:9000; E=localhost:9410; H='content-type: application/json'`.
Use a `coreapp` sandbox: its backend container has python, which honours
the proxy environment and the injected CA.

## Level 0 - A locked sandbox (5 min)

```bash
S=$(curl -s -X POST $A/sandboxes -H "$H" -d '{"name":"locked","template":"coreapp","options":{"egress":{"mode":"deny"}}}' | python -c "import sys,json; print(json.load(sys.stdin)['id'])")
# wait READY, then:
curl -s $A/sandboxes/$S/egress | python -m json.tool          # policy, stats, the iptables chain
curl -s -X POST $A/sandboxes/$S/exec -H "$H" -d '{"cmd":["python","-c","import urllib.request as u; print(u.urlopen(\"http://example.com\").status)"]}'
```
**Observe:** HTTP 403 from the proxy, and `GET .../egress` shows
`denied: 1`. Look at the fence lines: that is the actual iptables chain on
the engine node.

## Level 1 - Try to cheat (5 min)

Inside the sandbox, bypass the proxy on purpose:
```python
import urllib.request as u
o = u.build_opener(u.ProxyHandler({}))      # no proxy
o.open("http://93.184.215.14/", timeout=4)   # example.com by IP
```
**Observe:** the connection times out - the DROP rule. Nothing but the
proxy may leave.

## Level 2 - Allow, intercept TLS (5 min)

```bash
curl -s -X PUT $A/sandboxes/$S/egress -H "$H" -d '{"mode":"allow","allow":["example.com"]}'
```
Fetch `https://example.com` with python inside: 200, and the response
carries `X-Sandbox-Egress: allow`. Print the server certificate's issuer
(see verify.sh section 7 for the snippet): it is `sandbox-egress <id>`,
the CA the API minted for this sandbox.

## Level 3 - Zero-secret (10 min)

Create the `upstream` fixture sandbox and a coreapp whose policy joins its
network (`peer_networks: ["sbx-<upstream id>"]`), then:

```bash
curl -s -X PUT $A/sandboxes/$S/egress -H "$H" -d '{"mode":"allow","allow":["upstream.test"],"secrets":{"TOKEN_PLACEHOLDER":{"host":"upstream.test","value":"real-token-XYZ"}}}'
```
Inside: request `http://upstream.test/auth` with
`Authorization: Bearer TOKEN_PLACEHOLDER` -> `authorized`. Request
`/secret` -> the body says `TOKEN_PLACEHOLDER`, never the real value.
`env | grep -c real-token` inside -> 0. **Teaches:** the agent in the
sandbox cannot leak what it never had.

## Level 4 - Record, ship, replay (10 min)

Switch to `record`, make three requests, read
`GET $A/sandboxes/$S/egress/cassette` (NDJSON, placeholders only). Ship:
`POST $E/sandboxes/$S/cassettes/ship {"label":"run1"}` -> a blob hash. Ship
again -> the same hash. Switch to `replay`: the same request returns the
recorded body with `X-Sandbox-Egress: replay`; a new URL returns 599.
Create a second sandbox with `mode: deny` and
`POST $E/sandboxes/<id2>/cassettes/<cid>/load`: it answers from the
cassette with no network at all.

## Level 5 - Expert: your own matcher (15 min)

Edit `proxy/egress_policy.py`: make `key()` ignore a `X-Request-Id`
header and a `ts` JSON field in bodies. Rebuild sandbox-api (the proxy
image is content-hashed, so the next egress sandbox uses the new code).
Record an exchange with a changing timestamp, replay it: a hit instead of
a 599. **Teaches:** replay fidelity is a matching policy you own.
