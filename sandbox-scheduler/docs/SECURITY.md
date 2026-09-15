# sandbox-scheduler - Security Notes

Same local-demo trust posture as the rest of the repo. What is trusted, and
what must change before shared use.

## What this demo trusts

1. **No auth on the scheduler API.** Anyone reaching `localhost:9010` can
   submit/cancel jobs, which create real containers downstream. Local
   laptop only.
2. **`/admin/reset` is open.** It wipes the job database on a POST.
   Deliberate (demos/tests) - remove or auth-gate for any shared use.
3. **The tenant header is self-declared.** `tenant: "alice"` is a fairness
   label, not an identity. Nothing stops a client from lying about it;
   multi-tenant fairness guarantees only hold under honest labeling.
   Real multi-tenancy needs auth that binds tenant to identity.
4. **Task commands are arbitrary code execution** inside sandboxes -
   by design (that is the product). The blast radius is the sandbox,
   which today is a container in a privileged DinD engine (see
   sandbox-api/docs/SECURITY.md). T03 adds hard quotas; T11 adds real
   isolation.
5. **Upstream is plaintext HTTP** to `host.docker.internal:9000`. Fine on
   one laptop; across machines it would need TLS + a shared secret.

## What IS enforced today (and tested)

- Queue bound (429 past MAX_QUEUE) - memory exhaustion via submit spam
  is bounded.
- Per-tenant sandbox caps - one tenant cannot own the cluster (given
  honest labels; see #3).
- Input validation: DNS-safe names/tenants, bounded priorities/counts/
  runtimes - nothing unbounded enters the system.
- Idempotency-Key cannot be reused with a different body (409).
- Cancel of terminal jobs is rejected (409) - no replaying history.
- The scheduler only ever manages sandboxes it created (name correlation
  `sched-*`); foreign sandboxes in the engine are never touched (except
  being counted as unmanaged capacity - they cost budget, never data).

## What a real deployment changes (checklist)

- [ ] AuthN on all routes; tenant derived from identity, not headers.
- [ ] Remove or protect `/admin/reset`.
- [ ] Per-tenant quotas in real units (CPU-seconds, GB-hours) - T03/T16.
- [ ] Secrets for task jobs via indirection (never in `cmd` - cmd is
      logged in events! Documented limitation).
- [ ] Audit log with caller identity (T07 event store).
- [ ] TLS to upstream; schedulers on multiple machines elect a leader
      (T12) before any of this scales out.
