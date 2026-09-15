# Git Workflow — Browser Development Environment

## The lifecycle

```
                 first boot
                     │
                     ▼
  gitserver: git init --bare core-app.git
             seed from ./seed/company-app
             commit "Initial import of CoreApp"
                     │
                     ▼
  repo-init:  git clone git://gitserver/core-app.git
              → workspace-data volume
                     │
                     ▼
  developer:  edit → git add/commit → git push
  (in IDE)          ──────────────────►  gitserver (git-data volume)
                     │
                     ▼
  reset.ps1:  destroy workspace + db + containers
              start.ps1 → repo-init clones again
              → pushed commits are back
```

## Day-to-day commands (inside the IDE terminal)

```bash
cd /home/coder/workspace/core-app

git status
git diff
git add -A
git commit -m "Describe the change"
git push            # → git://gitserver/core-app.git

git log --oneline   # your history
git pull            # pick up anything pushed from elsewhere
```

Commit identity comes from `.env` (`GIT_USER_NAME`, `GIT_USER_EMAIL`) and
is applied by the IDE container's entrypoint — no manual setup.

## "Push without version changes" — the honest answer

If the requirement means *"edit and push normally, without the platform
rewriting history or adding its own versioning layer"* — that is exactly
what this does. The platform adds **nothing** on top of Git; a push is a
plain `git push` of plain commits.

If it literally means *"push file changes without creating commits"* — Git
cannot do that; a push transfers commits. What you *can* do is auto-commit
(a save-hook or a cron inside the container that commits+pushs), which
still creates versions, just automatic ones. Decide with the employer which
semantic they want.

## Replacing the demo Git server with the real company Git

The `gitserver` container exists so this project is fully self-contained.
Against a real server (GitHub/GitLab/Bitbucket/on-prem), change **one
thing**: where `repo-init` clones from.

1. Point the clone URL at the real repo:

   ```yaml
   # docker-compose.yml → repo-init command
   git clone https://github.com/your-org/core-app.git /workspace/core-app
   # or  git@github.com:your-org/core-app.git
   ```

2. Delete the `gitserver` service (and the `repo-init` dependency on it).

3. Solve authentication — **without baking keys into images**:

   | Method | How | Notes |
   |--------|-----|-------|
   | Personal access token | developer pastes token on first pull; `credential.helper cache` (already enabled) keeps it in memory | simplest |
   | SSH deploy key mounted at runtime | mount a key read-only into the IDE container via `.env`-controlled path | never COPY it into the image |
   | SSH agent forwarding | forward the host agent into the container | what VS Code Dev Containers does automatically (Project 1) |

4. Keep the seed folder for demos/tests, or delete it.

## Security note on the demo server

`git daemon --enable=receive-pack` allows **anonymous, unauthenticated
push** over an unencrypted protocol. That is acceptable for a local demo on
a private Docker network and unacceptable everywhere else. Real deployments
use SSH or HTTPS with auth. See [`SECURITY.md`](SECURITY.md).
