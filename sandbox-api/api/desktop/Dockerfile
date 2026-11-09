# ============================================================================
#  sandbox-desktop - a computer-use sandbox image, BUILT INSIDE DIND.
#
#  Base: linuxserver/webtop (Debian + XFCE + KasmVNC). The desktop streams to
#  a browser tab over HTTP on container port 3000 (identity-mapped by the
#  API). We add exactly what a GUI agent needs:
#    chromium            something to drive
#    xdotool             synthesize clicks / keys / mouse (the /gui/action API)
#    scrot, x11-utils    screenshots and display queries (/gui/screenshot)
#    xclip               clipboard access from scripts
#
#  The sbx-agent runs here in `serve` mode (started with docker exec by the
#  API) because s6-overlay must remain PID 1 in this image.
# ============================================================================
FROM lscr.io/linuxserver/webtop:debian-xfce

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       chromium xdotool scrot x11-utils xclip curl ca-certificates jq \
    && rm -rf /var/lib/apt/lists/*

# linuxserver init hooks: run once per container start, as root, before the
# desktop session comes up. The init REFUSES non-executable files ("is not
# an executable file"), and a build context uploaded from Windows carries
# no exec bit - so set it explicitly.
COPY custom-cont-init.d/ /custom-cont-init.d/
RUN chmod +x /custom-cont-init.d/*
