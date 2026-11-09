# sandbox-egress-proxy: the per-sandbox egress proxy, BUILT INSIDE DIND by
# sandbox-api (build context = this directory, content-hashed tag).
#
#   mitmproxy in "regular" proxy mode on :8080 (HTTP + HTTPS via CONNECT,
#   TLS interception with the sandbox's own CA injected by the API), the
#   policy addon, and a system CA bundle so upstream TLS is verified.
#
# The CA (cert + key) arrives at /sbx/ca/mitmproxy-ca.pem via put_archive
# before start; mitmproxy uses it as its confdir. The agent wraps mitmdump
# as PID 1 so /sbx/policy.json, /sbx/cassette.jsonl and /sbx/stats.json are
# reachable through the normal agent file API.
FROM mitmproxy/mitmproxy:11.0.2
# root: the agent (PID 1) writes /sbx/policy.json, mitmdump writes
# /sbx/cassette.jsonl + stats.json, and mitmproxy derives cert files in
# its confdir - one uid keeps that simple inside an isolated container.
USER root
# The build context is a deterministic tar (fixed mtimes). mitmproxy loads a
# script only when its mtime is newer than its last-load marker (0), so a
# file with mtime 0 is silently never loaded - touch it to be safe.
COPY egress_policy.py /sbx-addon/egress_policy.py
RUN touch /sbx-addon/egress_policy.py
ENV SBX_EGRESS_POLICY=/sbx/policy.json SBX_EGRESS_STATS=/sbx/stats.json
EXPOSE 8080
ENTRYPOINT []
CMD ["mitmdump", "--listen-host", "0.0.0.0", "--listen-port", "8080", "--set", "confdir=/sbx/ca", \
     "--set", "ssl_insecure=true", "--set", "block_global=false", "-s", "/sbx-addon/egress_policy.py"]
