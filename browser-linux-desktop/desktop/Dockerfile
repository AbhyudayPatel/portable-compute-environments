# ============================================================================
#  Company Linux Desktop — a complete Linux OS in your browser.
#
#  Base: linuxserver/webtop (Debian + XFCE + KasmVNC). The desktop streams
#  to any browser over HTTP on container port 3000 — no VNC client,
#  no X server, no local install needed.
#
#  We add the developer toolbox:
#    - git, python3, curl, htop, nano, unzip
#    - VS Code (real desktop app, from Microsoft's apt repo)
#    - Chromium browser (view the company app from inside the desktop)
#    - xfce4-terminal / thunar / mousepad (terminal, files, editor)
# ============================================================================
FROM linuxserver/webtop:debian-xfce

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git curl ca-certificates gnupg \
       python3 python3-pip python3-venv \
       nano htop unzip jq \
       xfce4-terminal mousepad thunar \
       chromium \
    # --- VS Code from Microsoft's official apt repository ---
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
       | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/code stable main" \
       > /etc/apt/sources.list.d/vscode.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends code \
    && rm -rf /var/lib/apt/lists/*

# linuxserver init hooks: scripts here run once per container start, as root,
# before the desktop session comes up.
COPY custom-cont-init.d/ /custom-cont-init.d/
