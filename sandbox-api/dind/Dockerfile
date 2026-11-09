# ============================================================================
#  sandbox-dind - the inner engine image, three additions over docker:27-dind
#
#   1. sbx-agent (proxy mode) runs beside dockerd. It is the control plane's
#      ONE door into every sandbox: /sbx/<ip>/... relays to the agent inside
#      a sandbox container; /node/* runs privileged node operations
#      (overlay mounts, iptables) that checkpoints and egress policy need.
#   2. gVisor (runsc) is installed and registered as a second OCI runtime,
#      so a template can ask for isolation=gvisor and get a userspace
#      kernel between the workload and the host.
#   3. daemon.json registers the runtimes; the wrapper entrypoint starts the
#      proxy then hands over to the stock dockerd-entrypoint.sh unchanged.
#
#  Build: the sbx-agent binary comes from the `agent` additional build
#  context (compose: additional_contexts: agent: ./agent).
# ============================================================================
FROM golang:1.23-alpine AS agentbuild
WORKDIR /src
COPY --from=agent . .
RUN go mod tidy && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -trimpath -ldflags="-s -w" -o /out/sbx-agent .

FROM docker:27-dind

# Pinned gVisor release. Since 2025 the tarball holds runsc, the containerd
# shim AND a gvisor-bin/ directory of sidecars (gvisor_sentry, ...) that runsc
# expects NEXT TO ITSELF - so the whole layout is extracted into /usr/local/bin.
# Reproducibility: bump GVISOR_RELEASE deliberately, never use "latest".
ARG GVISOR_RELEASE=20261012
ARG GVISOR_URL=https://storage.googleapis.com/gvisor/releases/release/${GVISOR_RELEASE}/x86_64
RUN set -eux; \
    apk add --no-cache curl bash coreutils util-linux e2fsprogs tar bzip2 rsync; \
    cd /tmp; \
    curl -fsSLo gvisor.tar.bz2 "${GVISOR_URL}/gvisor.tar.bz2"; \
    curl -fsSLo gvisor.tar.bz2.sha512 "${GVISOR_URL}/gvisor.tar.bz2.sha512"; \
    sha512sum -c gvisor.tar.bz2.sha512; \
    tar -xjf gvisor.tar.bz2 -C /usr/local/bin; \
    chmod +x /usr/local/bin/runsc /usr/local/bin/containerd-shim-runsc-v1 /usr/local/bin/gvisor-bin/*; \
    rm -f /tmp/gvisor.tar.bz2 /tmp/gvisor.tar.bz2.sha512; \
    runsc --version

COPY --from=agentbuild /out/sbx-agent /usr/local/bin/sbx-agent
COPY daemon.json /etc/docker/daemon.json
COPY entrypoint.sh /usr/local/bin/sandbox-dind-entrypoint.sh
RUN chmod +x /usr/local/bin/sandbox-dind-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/sandbox-dind-entrypoint.sh"]
