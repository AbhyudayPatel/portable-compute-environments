#!/bin/bash
# ============================================================================
#  Desktop personalisation — runs at every container start (as root, before
#  the desktop session). Applies the git identity from the environment,
#  installs the VS Code Python extension for the desktop user, drops a
#  welcome note + VS Code shortcut on the desktop, and installs a Chromium
#  wrapper that works inside containers.
# ============================================================================

ABC_HOME=/config
GIT_USER_NAME="${GIT_USER_NAME:-Company Developer}"
GIT_USER_EMAIL="${GIT_USER_EMAIL:-dev@company.local}"

# --- Git identity for the desktop user -------------------------------------
su abc -c "HOME=$ABC_HOME git config --global user.name  '$GIT_USER_NAME'"  || true
su abc -c "HOME=$ABC_HOME git config --global user.email '$GIT_USER_EMAIL'" || true
su abc -c "HOME=$ABC_HOME git config --global init.defaultBranch main"      || true
su abc -c "HOME=$ABC_HOME git config --global credential.helper cache"      || true

# --- VS Code container wrapper ----------------------------------------------
# 1. VS Code detects the Docker Desktop WSL2 kernel and shows a WSL prompt;
#    DONT_PROMPT_WSL_INSTALL=1 suppresses that.
# 2. Electron's Chromium sandbox cannot create kernel namespaces under
#    Docker's default seccomp profile, so it needs --no-sandbox. The
#    container itself is the isolation boundary; see docs/SECURITY.md.
# /usr/local/bin precedes /usr/bin in PATH, so `code` picks up the wrapper.
cat > /usr/local/bin/code <<'EOF'
#!/bin/bash
export DONT_PROMPT_WSL_INSTALL=1
exec /usr/bin/code --no-sandbox "$@"
EOF
chmod +x /usr/local/bin/code

# --- VS Code Python extension (best effort; needs network) ------------------
su abc -c "HOME=$ABC_HOME DONT_PROMPT_WSL_INSTALL=1 code --install-extension ms-python.python" >/dev/null 2>&1 || true

# --- Chromium container wrapper ---------------------------------------------
# Chromium's kernel sandbox and /dev/shm assumptions don't hold inside a
# container; this wrapper is what the desktop shortcut/menu entry should use.
cat > /usr/local/bin/chromium-browser <<'EOF'
#!/bin/bash
exec /usr/bin/chromium --no-sandbox --disable-dev-shm-usage "$@"
EOF
chmod +x /usr/local/bin/chromium-browser

# --- Desktop welcome note + VS Code shortcut --------------------------------
mkdir -p "$ABC_HOME/Desktop"

cat > "$ABC_HOME/Desktop/README.txt" <<'EOF'
============================================================
 WELCOME TO YOUR COMPANY LINUX DESKTOP
============================================================

This is a full Debian XFCE desktop running INSIDE Docker,
streamed to your browser. Your Windows drive is not touched.

Your company repository:
    /config/workspace/core-app

Open it in VS Code:
    double-click "VS Code - CoreApp" on this desktop

Run the company app FROM INSIDE this desktop:
    chromium-browser http://frontend:3000     (company app UI)
    chromium-browser http://backend:8000/api/health

From your Windows browser the same app is at:
    http://localhost:3000                     (company app UI)
    http://localhost:8000/api/health          (API)

Edit -> save -> refresh: backend and frontend both run
hot-reloaded straight from your working copy.

Git (terminal inside this desktop):
    cd /config/workspace/core-app
    git add -A && git commit -m "..." && git push

Pushed commits survive a full environment reset.
============================================================
EOF

cat > "$ABC_HOME/Desktop/code-coreapp.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=VS Code - CoreApp
Comment=Open the company repository in VS Code
Exec=/usr/local/bin/code /config/workspace/core-app
Icon=code
Terminal=false
Categories=Development;
EOF
chmod +x "$ABC_HOME/Desktop/code-coreapp.desktop"

chown -R abc:abc "$ABC_HOME/Desktop"
