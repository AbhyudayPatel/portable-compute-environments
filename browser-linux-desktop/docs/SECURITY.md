# Security — Linux Desktop Environment

## Current posture (local demo)

| Area | State | Notes |
|------|-------|-------|
| Desktop access | no auth by default; `DESKTOP_PASSWORD` enables HTTP basic auth (user `abc`) | localhost-only demo default |
| Transport | plain HTTP on localhost | add TLS before exposing beyond localhost |
| Git server | anonymous push over `git://`, internal network only | demo only |
| Electron sandbox | **disabled** (`code --no-sandbox`, `chromium --no-sandbox`) | see below |
| Desktop user | `abc`, has **passwordless sudo** inside the container | it's meant to be "your machine" |
| Host ports | bound on `0.0.0.0` | bind to `127.0.0.1` on untrusted networks |

## The `--no-sandbox` trade-off, stated plainly

Electron/Chromium want to create kernel user/PID/network namespaces for
their in-process sandbox. Docker's default seccomp profile blocks that, so
the apps crash (`zygote_host_impl_linux.cc FATAL`). Options:

1. `--no-sandbox` (chosen): the app's *inner* sandbox is off. The blast
   radius of a compromised renderer is **the container**, which is already
   the isolation boundary — disposable and rebuildable.
2. Custom seccomp profile allowing `clone`/`unshare`: keeps the inner
   sandbox, weakens the container's syscall filter.
3. `security_opt: seccomp=unconfined` or `--privileged`: do **not** do this
   for a convenience feature.

Option 1 is the standard practice for Electron in containers. The rule it
implies: **treat anything running inside the desktop as trusted code** —
the same rule you already apply to your laptop.

## Rules that apply even in this demo

1. Never bake credentials into images (no `COPY .ssh`, no tokens in
   Dockerfiles).
2. Secrets enter at runtime: `.env` (gitignored), runtime mounts, agents.
3. The workspace volume holds company code — host access to Docker volumes
   equals access to the code.
4. Sudo inside the container is fine; **root on the host is not** — never
   mount the Docker socket into this desktop unless you explicitly intend
   container escape capability (that's the DinD roadmap, with its own
   hardening doc).

## Hardening checklist (toward production)

- [ ] Set `DESKTOP_PASSWORD` at minimum; better: SSO/OIDC in front of 8080.
- [ ] TLS via reverse proxy or tunnel; never expose the desktop over plain
      HTTP beyond localhost.
- [ ] Replace `git daemon` with authenticated SSH/HTTPS Git hosting.
- [ ] Bind ports to `127.0.0.1`.
- [ ] Resource limits (`mem_limit`, `cpus`) — a browser tab compiling code
      shouldn't be able to starve the laptop.
- [ ] Pin base image digests; scan images.
- [ ] If the desktop will ever run **untrusted** code (AI agents, unknown
      PRs): containers are not enough — move to microVM isolation
      (Firecracker/Kata) and an egress policy. A full desktop with sudo is
      intentionally powerful; that power is the attack surface.
