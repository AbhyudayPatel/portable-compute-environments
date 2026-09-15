# sandbox-api - Security Notes

This is a **local development demo**, in the same trust posture as the rest
of the repo. Here is exactly what is trusted and what must change before
any shared/hosted use.

## What this demo trusts

1. **The inner engine API (2375) is plaintext and unauthenticated.** It is
   reachable ONLY on the internal compose network - compose never publishes
   it to the host. The blast radius if the `api` container is compromised:
   full control of the inner engine (which is itself a privileged
   container - see next).
2. **`dind` runs `--privileged`.** That is root-equivalent on the host.
   This is a lab; the honest escape hatches are T11 (microVM isolation)
   and rootless/sysbox runtimes. Do not expose sandbox workloads you don't
   trust at the "I wouldn't run this as root" level.
3. **No auth on the REST API.** Anyone who can reach `localhost:9000` can
   create/destroy sandboxes. Fine on a single-developer laptop; not fine
   on a shared host (bind to localhost, or put an auth gateway in front  - 
   that is T04's bearer-token design).
4. **Exec is arbitrary command execution** inside the sandbox. That is the
   point of the product, but it means the API is a remote-code-execution
   service by design - treat it accordingly.

## What a real deployment changes (checklist)

- [ ] AuthN/Z on every route (API gateway or middleware); per-tenant
      sandbox scoping.
- [ ] Bind published ports to `127.0.0.1`, not `0.0.0.0`.
- [ ] TLS on 2375 if the API and engine ever live on different machines
      (or better: workers dial OUT to the control plane - T12's model).
- [ ] Replace privileged DinD with microVMs (T11) or rootless+sysbox.
- [ ] Resource quotas by default (T03), network egress policy per sandbox.
- [ ] Secrets via indirection only (T13's IR forbids inline secrets);
      never in labels/events - the event log is plaintext today.
- [ ] Image allowlist for templates.
- [ ] Audit log of API callers (T07 event store with caller identity).

## What we DO verify today

- The management-label contract: the API cannot touch foreign containers
  even by accident (EDGE-CASES.md #5).
- Path-naive inputs rejected at validation (#11), so container names stay
  DNS-safe and injection-free through the Docker API (no shell is ever
  involved in create/delete - all SDK calls).
- No secrets appear in `/events` payloads: events carry state transitions
  and short action summaries, never env vars or request bodies.
