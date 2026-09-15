# ─────────────────────────────────────────────────────────────────────────────
# OUTER container
#
# Base: docker:27-dind
#   This image already contains a full Docker engine: `dockerd`, `containerd`,
#   `runc`, the Docker CLI, and `iptables`. It is the canonical Docker-in-Docker
#   base image. We only ADD tooling for networking/debug and our entrypoint.
#
# Why -dind and not plain docker:27?
#   docker:27     -> contains ONLY the docker CLI (a client). No daemon.
#   docker:27-dind-> contains the CLI AND the daemons + certs for nested use.
# ─────────────────────────────────────────────────────────────────────────────
FROM docker:27-dind

# Tooling:
#   socat   -> TCP relay that bridges the OUTER network to the INNER container
#              (this is what makes the inner app reachable from Windows).
#   curl    -> health checks and verification inside the outer container.
#   iproute2, iputils, net-tools, procps, bind-tools
#           -> observability: `ip addr`, `ip route`, `ss`, `ping`, `ps`, `nslookup`.
RUN apk add --no-cache \
        socat \
        curl \
        iproute2 \
        iputils \
        net-tools \
        procps \
        bind-tools \
        bash

# Copy the startup orchestration script.
# Build context = repo root, so the script lives at outer/entrypoint.sh.
COPY outer/entrypoint.sh /usr/local/bin/outer-entrypoint.sh
RUN chmod +x /usr/local/bin/outer-entrypoint.sh

# The inner application source is placed where the INNER daemon can reach it.
# The build context for this image is the REPO ROOT (see docker-compose.yml),
# so `inner/` here refers to the top-level inner app directory.
COPY inner/ /opt/inner-app/

# Persistent storage for the inner Docker daemon (images, layers, volumes).
# Declared as a volume so the inner /var/lib/docker survives container recreate.
# See docker-compose.yml for the named-volume definition.
VOLUME ["/var/lib/docker"]

# Port published by Docker Desktop to reach Windows (see compose `ports`).
EXPOSE 8080

# Our entrypoint is responsible for:
#   1) starting the inner dockerd
#   2) waiting until it is healthy
#   3) building the inner image
#   4) running the inner container
#   5) starting the TCP relay to expose it
ENTRYPOINT ["/usr/local/bin/outer-entrypoint.sh"]
