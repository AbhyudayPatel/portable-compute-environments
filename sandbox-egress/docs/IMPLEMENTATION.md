# sandbox-egress - Implementation Notes

## Files

```
sandbox-egress/
+-- docker-compose.yml        the `egress` service (cassette catalogue); joins sandbox-api_sandnet + sandbox-blobs
+-- .env.example              EGRESS_PORT, SANDBOX_API_URL, BLOBS_URL
+-- proxy/                    the per-sandbox proxy IMAGE, built inside dind by sandbox-api
|   +-- Dockerfile            mitmproxy/mitmproxy:11.0.2 + the policy module, root user
|   +-- egress_policy.py      the addon: modes, allowlist, cassette, placeholder secrets, stats
+-- upstream/                 the test fixture image (template `upstream`): fake internet
|   +-- Dockerfile            python:3.12-alpine + self-signed cert for upstream.test
|   +-- server.py             /, /headers, /auth, /secret, /echo on 80 + 443
+-- service/                  egress control service (cassettes <-> blob store, passthroughs)
+-- scripts/  start/stop/reset.ps1, verify.sh (39 assertions)
+-- docs/     ARCHITECTURE . EDGE-CASES . EXAMPLES . SECURITY . this file
```

Plus, in sandbox-api:

```
api/app/egress.py     CA generation (openssl), env for proxied containers, proxy container spec,
                      fence/unfence iptables scripts, policy JSON
api/app/engine.py     options.egress: proxy container first, CA + policy upload, env injection,
                      peer networks, fence after start, unfence on destroy and rollback
api/app/core.py       pre-builds the proxy image when options.egress is set
api/app/main.py       GET/PUT /sandboxes/{id}/egress, GET/PUT/DELETE .../egress/cassette
api/app/templates.py  buildable images sandbox-egress-proxy and sandbox-upstream; template `upstream`
api/Dockerfile        openssl; proxy/ and upstream/ copied in as build contexts
```

## Build story

1. Feasibility inside dind (recorded in sandbox-api/docs/IMPLEMENTATION.md):
   `--internal` networks block egress but also published ports, so the
   fence is iptables in the node namespace, hooked into `DOCKER-USER`.
2. The addon and the fake upstream were written first, with the test
   contract (what `/auth` and `/secret` prove) before any sandbox-api
   wiring.
3. Wiring: the proxy container is created *first* so its IP is known for
   the fence; the others get the CA cert and proxy environment.
4. The first live run failed on the first create: the proxy image had
   never been built because provisioning only pre-built the template's
   own images. Then a series of silent failures (below) taught more about
   mitmproxy than its docs did.

## Bugs found while building

1. **Proxy image not pre-built.** `provision_steps` ensured only
   `templates.resolve_images(template)`; an egress sandbox failed with
   "No such image: sandbox-egress-proxy". Fix: when `options.egress` is
   set, the proxy image is added to the list.
2. **`ctx.log` is gone in mitmproxy 10+.** The addon used
   `ctx.log.info`; the first `reload()` raised `AttributeError` at import.
   Fix: stdlib `logging`.
3. **`-q` hid everything.** With quiet mode on, a script that fails to
   load leaves no trace in `docker logs`; the proxy simply forwards
   traffic unpoliced. Fix: no `-q`; mitmdump's normal log is the audit
   trail.
4. **A patch that did not apply.** The fix script asserted that the text
   `ctx.` no longer appeared - the comment it had just inserted contained
   it, so the script aborted before writing either file and the rebuild
   shipped the old code. Lesson: assert on the *call* (`ctx.log(`), and
   print what was patched.
5. **Chain removal passed the chain name twice.** `sed 's/^-A //'` left
   `DOCKER-USER -i br-x -j SBX_x`, which the script then prefixed with
   `-D DOCKER-USER`; the rule never deleted and the chain survived the
   sandbox. Fix: strip `-A DOCKER-USER `.
6. **mitmproxy silently skipped the script - because of its mtime.**
   Bisected live inside the proxy container: the same content copied to
   `/tmp/addon2.py` loaded ("Loading script ..."), a file created by hand
   under `/sbx-addon/` loaded, but the file shipped in the image never
   did - no log line, no hooks, every request proxied unpoliced. The
   image is built from the deterministic tar context sandbox-api
   assembles, which stamped every file with **mtime 0** for stable
   hashing; mitmproxy's script loader only (re)loads a script whose mtime
   is newer than its last-load marker, which starts at 0. Fix: contexts
   use a fixed non-zero mtime, and the proxy Dockerfile `touch`es the
   script. A renaming detour (`addon.py` -> `egress_policy.py`) was a red
   herring and is kept only because the new name is clearer.

7. **Stale `Content-Length` after scrubbing.** Replacing a 14-byte secret
   with a 17-byte placeholder through `raw_content` left the original
   `Content-Length`, so clients read a body cut three bytes short
   (`PLACEHOLDER_TOK`). mitmproxy's `content` setter re-encodes and fixes
   the headers; the addon now uses it for every substitution.

## Observations

- TLS interception works for python's `urllib` with `SSL_CERT_FILE`
  pointing at the injected CA; `curl`, `requests`, node and git honour
  the other variables set by `egress.sandbox_env`.
- The fence does not stop DNS (dockerd answers from the node namespace);
  see SECURITY.md.
- Cassette replay is strict by default (599 on unknown requests) so a
  replayed agent run cannot silently depend on live traffic.

## Verified

- [x] `bash scripts/verify.sh` - see the RESULT line in the latest run
      (health, upstream fixture, egress sandbox with fence + env + CA,
      deny, direct-bypass drop, allowlist, TLS interception with the
      sandbox CA as issuer, placeholder secret injection + response scrub +
      wrong-host non-injection, record (3 exchanges, no secrets), ship +
      dedup, replay (recorded body, 599 on unknown, stats), portable
      cassette into a new sandbox, live policy switches, fence removal)
