# sandbox-egress - Edge Cases

**VERIFIED** = asserted by `scripts/verify.sh` (39 assertions, 14
sections); **EXPECTED** = reasoned, not executed.

| # | Edge case | Handling | Test |
|---|---|---|---|
| 1 | Process ignores `HTTP_PROXY` and dials an IP | per-sandbox iptables chain drops everything leaving the bridge except from the proxy IP | 5 |
| 2 | Non-HTTP egress (raw TCP/UDP) | same chain: dropped (the proxy only speaks HTTP) | 5 (covers any protocol by construction) |
| 3 | Host not on the allowlist | 403 `X-Sandbox-Egress: blocked` before any upstream connection | 6 |
| 4 | Glob allowlists | `fnmatch` on the host (`*.test`) | 13 |
| 5 | TLS clients and the sandbox CA | CA cert injected at `/sbx/ca.pem` + env for curl/python/requests/node/git/pip; leaf certs minted per host | 7 (issuer is `sandbox-egress <id>`) |
| 6 | Certificate pinning / clients that ignore the env | interception fails for them; they would need a bypass that the fence then blocks | EXPECTED (documented) |
| 7 | Placeholder sent to the wrong host | only injected when the destination matches the secret's `host`; elsewhere the placeholder goes out verbatim | 8 |
| 8 | Upstream echoes the secret back | response bodies and headers scrubbed: real value -> placeholder | 8 |
| 9 | Secrets in recordings | request un-substituted and response scrubbed before the cassette line is written | 9 |
| 10 | Replay of an unrecorded request | 599 (strict) so a replayed run cannot quietly depend on live traffic; `strict_replay=false` gives 404 | 11 |
| 11 | Replay must not touch the network | the mode synthesizes responses in the `request` hook; verify compares the recorded body (counter frozen) | 11, 12 |
| 12 | Cassette portability | shipped as a blob (dedup by content), loaded into another sandbox, same answers | 10, 12 |
| 13 | Policy change at runtime | `/sbx/policy.json` rewritten through the agent; addon reloads on mtime | 13 |
| 14 | Sandbox deleted | chain unhooked from `DOCKER-USER`, flushed, deleted; proxy container removed with the sandbox | 14 |
| 15 | Create fails midway | rollback also runs the unfence script | EXPECTED (code path) |
| 16 | Proxy image missing in the engine | pre-built like template images when `options.egress` is set | 3 (first create builds it) |
| 17 | Intra-sandbox traffic | `-o br-x RETURN` keeps containers of the same sandbox talking; `NO_PROXY` lists sibling aliases | 4 (app health through nginx -> backend) |
| 18 | Replies to inbound connections (identity port) | `ESTABLISHED,RELATED RETURN` precedes DROP | 4 (health via the published port) |
| 19 | DNS exfiltration | not blocked (dockerd resolves from the node namespace) | EXPECTED - documented in SECURITY.md with the mitigation |
| 20 | Policy with no allow list in `allow` mode | everything 403 (equivalent to deny) | by construction |

## Known limits

- One proxy per sandbox (a container ~150 MB RSS); fine for a lab, a
  shared proxy with per-sandbox policies would scale better.
- Cassette matching is exact on method + URL + body hash; requests with
  timestamps or nonces in the body will miss on replay (599). Normalising
  matchers are an extension point in `EgressPolicy.key`.
