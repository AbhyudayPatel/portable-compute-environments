# Security — Browser Development Environment

## Current posture (local demo)

| Area | Current state | Acceptable for | Not acceptable for |
|------|---------------|----------------|--------------------|
| IDE access | password (`dev123`) over plain HTTP on localhost | single-developer local use | any shared/remote machine |
| Git server | anonymous push over `git://`, internal network only | local demo | anything real |
| Database | default credentials, port published to host | local demo | shared machines |
| Host ports | 3000/8000/5432/8080 bound on `0.0.0.0` | local | laptops on untrusted networks (bind to 127.0.0.1 instead) |
| Source code | never touches the Windows filesystem | good baseline | — |

## Rules that apply even in this demo

1. **Never bake credentials into images.** No `COPY .ssh`, no tokens in
   Dockerfiles. Images are pushed around; layers leak.
2. **Secrets enter at runtime** — environment variables from `.env`
   (gitignored), mounted files, or an agent.
3. **The workspace volume is the sensitive artifact.** It holds the
   checked-out company code. Treat host access to Docker volumes as access
   to the code.
4. **Be deliberate about bind-mounting host folders.** Mounting
   `C:\Users\you\company` into a container gives the container that folder.
   This project deliberately avoids host source mounts for the browser
   flow — code lives in a Docker volume cloned from Git.

## Hardening checklist (production / multi-user)

- [ ] Put the IDE behind HTTPS (reverse proxy with TLS, or a tunnel).
- [ ] Replace static IDE password with SSO/OIDC (code-server supports
      external auth via a proxy such as oauth2-proxy).
- [ ] Replace `git daemon` with SSH/HTTPS Git hosting with real auth
      (or point `repo-init` at the corporate Git server).
- [ ] Bind host ports to `127.0.0.1` unless remote access is intended.
- [ ] Rotate the Postgres password; don't publish 5432 unless needed.
- [ ] Add container resource limits (`mem_limit`, `cpus`) so one
      environment can't starve the laptop.
- [ ] Scan images; pin base image digests.
- [ ] Audit extensions available in the IDE.
- [ ] If the environment ever runs untrusted code (AI agents, PR previews),
      move from containers to stronger isolation (DinD with care, or
      microVMs such as Firecracker/Kata) and treat the environment as a
      sandbox with an egress policy.

## The employer conversation to have

- **Where does code live at rest?** (Here: a Docker volume on the employee
  laptop + the Git server. Acceptable? Or must it be a remote environment?)
- **Who may push?** (Here: whoever is inside the environment. Real answer
  should be: the authenticated employee.)
- **What may leave the environment?** (Here: nothing enforced. Real
  deployments add egress rules/DLP.)
