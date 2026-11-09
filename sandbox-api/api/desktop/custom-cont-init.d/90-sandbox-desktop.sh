#!/bin/bash
# ============================================================================
#  sandbox-desktop personalisation (runs as root at every container start).
#   - Chromium wrapper: Electron/Chromium's kernel sandbox cannot create
#     namespaces under Docker's default seccomp profile, so inside a
#     container it needs --no-sandbox (the container is the boundary).
#   - A desktop shortcut + README so a human watching the agent knows what
#     they are looking at.
#   - X access for the agent: KasmVNC's X server trusts local connections;
#     we export a known XAUTHORITY path for xdotool/scrot anyway.
# ============================================================================
cat > /usr/local/bin/chromium-browser <<'EOF'
#!/bin/bash
exec /usr/bin/chromium --no-sandbox --disable-dev-shm-usage --no-first-run \
  --disable-features=TranslateUI --password-store=basic "$@"
EOF
chmod +x /usr/local/bin/chromium-browser

mkdir -p /config/Desktop /config/workspace
cat > /config/Desktop/README.txt <<'EOF'
This is an AgentOS computer-use sandbox.

  * The platform's GUI API can take screenshots and send clicks/keys here.
  * Chromium is installed:  chromium-browser http://frontend:3000
  * Your workspace volume:  /config/workspace
EOF
cat > /config/Desktop/chromium.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Chromium
Exec=/usr/local/bin/chromium-browser
Icon=chromium
Terminal=false
EOF
chmod +x /config/Desktop/chromium.desktop
chown -R abc:abc /config/Desktop /config/workspace
