"""Per-sandbox egress policy: a proxy container + an iptables fence.

    options.egress = {
        "mode": "deny" | "allow" | "record" | "replay",
        "allow": ["example.com", "*.pypi.org"],
        "secrets": {"PLACEHOLDER": {"host": "api.github.com", "value": "real"}},
        "peer_networks": ["sbx-<other-id>"]        # test hook: extra networks the proxy joins
    }

What the API does at create time (engine.create_sandbox_resources):

  1. generate a CA for this sandbox (openssl, in the API container);
  2. create the proxy container `sbx-<id>-egress` FIRST on the sandbox
     network (alias `egress`), upload the CA (cert+key) and policy.json to
     /sbx/, start it - it is a normal agent-wrapped container, so its
     files are reachable through the agent API;
  3. every other container gets the CA *certificate* at /sbx/ca.pem and
     the proxy environment (HTTP_PROXY, HTTPS_PROXY, NO_PROXY, and the CA
     bundle variables curl/python/node/git/pip honour);
  4. after all containers started, a dedicated iptables chain on the
     node fences the sandbox bridge:

        DOCKER-USER -i br-<net> -j SBX_<id>
        SBX_<id>:  -o br-<net>                       RETURN   (intra-sandbox)
                   --ctstate ESTABLISHED,RELATED     RETURN   (replies to inbound, e.g. the identity port)
                   -s <proxy ip>                     RETURN   (only the proxy may leave)
                   -j DROP                                    (everything else: dropped)

     so even a process that ignores HTTP_PROXY cannot reach the outside;
     only the proxy, which enforces the policy, can.

`--internal` networks were tried first (section "feasibility" in
IMPLEMENTATION.md): they block egress perfectly but also break published
ports, so the identity-port contract would be lost. iptables keeps both.
"""
import json
import os
import shlex
import subprocess
import tempfile

from . import config

PROXY_PORT = 8080
PROXY_ALIAS = "egress"
CA_SUBJECT_PREFIX = "/CN=sandbox-egress"

PROXY_ENV_KEYS = [
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "GIT_SSL_CAINFO", "PIP_CERT", "NO_PROXY", "no_proxy",
]


def generate_ca(sid: str) -> tuple[bytes, bytes]:
    """Returns (cert_pem, key_pem) for a fresh per-sandbox CA."""
    with tempfile.TemporaryDirectory() as d:
        key, cert = os.path.join(d, "key.pem"), os.path.join(d, "cert.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", key, "-out", cert, "-days", "365",
                        "-subj", f"{CA_SUBJECT_PREFIX} {sid}",
                        "-addext", "basicConstraints=critical,CA:TRUE",
                        "-addext", "keyUsage=critical,keyCertSign,cRLSign"],
                       check=True, capture_output=True)
        with open(cert, "rb") as f:
            cert_pem = f.read()
        with open(key, "rb") as f:
            key_pem = f.read()
    return cert_pem, key_pem


def policy_json(opts: dict) -> bytes:
    pol = {
        "mode": opts.get("mode", "deny"),
        "allow": list(opts.get("allow") or []),
        "cassette": "/sbx/cassette.jsonl",
        "secrets": dict(opts.get("secrets") or {}),
        "strict_replay": bool(opts.get("strict_replay", True)),
    }
    return json.dumps(pol, indent=1).encode()


def sandbox_env(aliases: list[str]) -> dict[str, str]:
    """Environment injected into every non-proxy container."""
    proxy = f"http://{PROXY_ALIAS}:{PROXY_PORT}"
    no_proxy = ",".join(["localhost", "127.0.0.1", PROXY_ALIAS] + sorted(set(aliases)))
    ca = "/sbx/ca.pem"
    return {
        "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy, "https_proxy": proxy,
        "NO_PROXY": no_proxy, "no_proxy": no_proxy,
        "SSL_CERT_FILE": ca, "REQUESTS_CA_BUNDLE": ca, "CURL_CA_BUNDLE": ca,
        "NODE_EXTRA_CA_CERTS": ca, "GIT_SSL_CAINFO": ca, "PIP_CERT": ca,
    }


def proxy_container_spec(sid: str, peer_networks: list[str]) -> dict:
    return {
        "name": f"sbx-{sid}-egress",
        "image": None,                       # filled by the engine (content-hashed tag)
        "role": "egress",
        "port": None,
        "command": None,
        "ports": {},
        "environment": {},
        "network_aliases": [PROXY_ALIAS],
        "peer_networks": list(peer_networks or []),
    }


def chain_name(sid: str) -> str:
    return f"SBX_{sid}"


def fence_script(sid: str, bridge: str, proxy_ip: str) -> str:
    """iptables commands applied on the node once all containers run."""
    ch = chain_name(sid)
    return (
        f"iptables -N {ch} 2>/dev/null || iptables -F {ch}; "
        f"iptables -A {ch} -o {shlex.quote(bridge)} -j RETURN; "
        f"iptables -A {ch} -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN; "
        f"iptables -A {ch} -s {shlex.quote(proxy_ip)} -j RETURN; "
        f"iptables -A {ch} -j DROP; "
        f"iptables -C DOCKER-USER -i {shlex.quote(bridge)} -j {ch} 2>/dev/null || "
        f"iptables -I DOCKER-USER 1 -i {shlex.quote(bridge)} -j {ch}"
    )


def unfence_script(sid: str) -> str:
    ch = chain_name(sid)
    return (
        f"while iptables -S DOCKER-USER | grep -q -- '-j {ch}'; do "
        f"  rule=$(iptables -S DOCKER-USER | grep -- '-j {ch}' | head -1 | sed 's/^-A DOCKER-USER //'); "
        f"  iptables -D DOCKER-USER $rule || break; done; "
        f"iptables -F {ch} 2>/dev/null; iptables -X {ch} 2>/dev/null; true"
    )


def fence_status_script(sid: str) -> str:
    ch = chain_name(sid)
    return f"iptables -S {ch} 2>/dev/null; iptables -S DOCKER-USER | grep -- '-j {ch}' || true"


def bridge_name(network_id: str) -> str:
    return "br-" + network_id[:12]


def ca_tar_files(cert_pem: bytes, key_pem: bytes, policy: bytes) -> dict[str, bytes]:
    """Files for the PROXY container (under /sbx/)."""
    return {
        "ca/mitmproxy-ca.pem": key_pem + cert_pem,      # mitmproxy wants key+cert in one file
        "ca/mitmproxy-ca-cert.pem": cert_pem,
        "policy.json": policy,
    }
