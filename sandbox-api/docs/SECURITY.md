# sandbox-api v2 - Security Notes

Same local-development posture as v1, with new surfaces that need to be
understood: the agent inside every container, the privileged node
operations on the engine, gVisor as an optional stronger boundary, and a
desktop that runs a browser.

## What this demo trusts

1. **The REST API has no authentication.** Anyone reaching
   `localhost:9000` can create sandboxes, run commands in them, read and
   write their files, drive their GUI. Bind to localhost (compose does
   not restrict this) and treat the machine as single-user.
2. **`dind` runs `--privileged`** and its daemon API (2375) is plaintext.
   Both are reachable only on the compose network. A compromise of the
   `api` container is a compromise of the engine.
3. **The node token** (`SBX_NODE_TOKEN`) authorises `/node/exec`, which is
   root inside the privileged dind container. It is shared via `.env`
   and never leaves the compose network. Change it anyway; it is a
   placeholder by default.
4. **Agent tokens** are per sandbox, random, carried in the container's
   environment (`SBX_AGENT_TOKEN`) and the API's SQLite. They stop one
   sandbox from driving another's agent. They are visible to any process
   inside that sandbox - by design, the sandbox is one trust domain.
5. **The agent runs as root in the container** and the file jail is a
   policy knob (`SBX_AGENT_FS_ROOT`), not a boundary against the sandbox's
   own root.

## Isolation levels, honestly

| Level | What stands between the workload and the host kernel | When to use |
|---|---|---|
| `container` (runc in dind) | namespaces + cgroups + seccomp of the inner engine, inside a privileged outer container | trusted or your own code |
| `gvisor` (runsc in dind) | gVisor's userspace kernel (Sentry) + a narrow syscall surface to the host kernel + the inner engine's namespaces, still inside the privileged outer container | code you did not write: agents, PR previews |
| microVM (T11, not here) | a hardware virtualisation boundary | hostile multi-tenant |

gVisor materially reduces the attack surface (the workload cannot reach
most host syscalls), but the outer dind container is still privileged:
a gVisor escape *plus* a container escape would be needed to reach the
Windows VM. For this lab that is a large step up; for production use
microVMs.

## The desktop template

- Chromium runs with `--no-sandbox`: its *inner* sandbox cannot create
  namespaces under the engine's default seccomp profile. The container is
  the boundary. Do not browse hostile sites with it unless you accept
  "the container is compromised" as the outcome.
- KasmVNC serves without a password by default; it is reachable only via
  the identity port on localhost.
- The GUI API can type anything into anything; it is as powerful as a
  person at the keyboard. Audit: every action is recorded in the sandbox
  event log (`type: gui`).

## Secrets

- Nothing in this project needs a secret except the node token. Do not
  pass secrets to sandboxes through `cmd` (exec commands are logged in
  events) or through template environment (visible in `docker inspect`
  inside dind). The egress project's placeholder-credential scheme is
  the intended path.

## What a real deployment changes (checklist)

- [ ] AuthN/Z on every API route; tenant-scoped sandbox visibility.
- [ ] Bind published ports to `127.0.0.1`; TLS in front of the API.
- [ ] Rotate `SBX_NODE_TOKEN`; keep `/node/*` reachable only from the API.
- [ ] Default `isolation=gvisor` for untrusted workloads; microVMs for
      hostile ones.
- [ ] Per-sandbox resource limits (`mem_limit`, `nano_cpus`, `pids_limit`
      are plumbed through `templates.py` container specs; defaults are
      unlimited today).
- [ ] Egress policy per sandbox (the `sandbox-egress` project).
- [ ] Image allowlist for templates; pinned digests (gVisor already is).
- [ ] Jail the agent's file API per template (`fs_root`) for templates
      that expose files to third parties.

## What we DO verify today

- Management-label contract: the API never touches foreign containers.
- Tar uploads cannot escape their destination (verify 7).
- Timed-out commands are killed, not abandoned (verify 5).
- gVisor sandboxes report the gVisor kernel and still work (verify 10).
- No secrets in `/events` payloads (commands are logged, outputs are not).
