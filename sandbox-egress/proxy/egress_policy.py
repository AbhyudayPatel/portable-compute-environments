"""sandbox-egress - the mitmproxy addon that IS the egress policy.

Runs inside the per-sandbox proxy container (role `egress`), wrapped by the
sbx-agent like every other container, so the control plane can read and
write its files through the normal agent API. Policy is a JSON file; the
addon re-reads it whenever its mtime changes, so "change the policy" is
"write the file" (no extra control port).

    /sbx/policy.json
    {
      "mode": "deny" | "allow" | "record" | "replay",
      "allow": ["example.com", "*.pypi.org"],         # host globs (allow/record)
      "cassette": "/sbx/cassette.jsonl",             # record target / replay source
      "secrets": {"PLACEHOLDER": {"host": "api.github.com", "value": "ghp_real"}},
      "strict_replay": true                           # replay: unknown request -> 599
    }

Modes
  deny     every request answered 403 by the proxy (nothing leaves)
  allow    only hosts matching `allow` pass; others 403
  record   like allow, and every exchange is appended to the cassette
  replay   nothing leaves; responses come from the cassette (matched on
           method + URL + body hash); unknown -> 599 (or 404 if not strict)

Zero-secret sandboxes: the sandbox only ever holds PLACEHOLDER strings. The
addon swaps a placeholder for the real value in request headers/bodies
only when the destination host matches the secret's `host`; anywhere else
the placeholder goes out as-is (useless to an attacker). Responses are
scrubbed the other way (real value -> placeholder) so the secret never
enters the sandbox.

Recording is done AFTER secret substitution is reversed (cassettes hold
placeholders, never real secrets).
"""
import fnmatch
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

from mitmproxy import http

# mitmproxy >= 10 removed the ctx.log API; addons log via the stdlib
log = logging.getLogger("sandbox-egress")

POLICY_PATH = Path(os.environ.get("SBX_EGRESS_POLICY", "/sbx/policy.json"))
STATS_PATH = Path(os.environ.get("SBX_EGRESS_STATS", "/sbx/stats.json"))


class EgressPolicy:
    def __init__(self):
        self.policy = {"mode": "deny", "allow": [], "cassette": "/sbx/cassette.jsonl",
                       "secrets": {}, "strict_replay": True}
        self._mtime = 0.0
        self._cassette: dict[str, dict] = {}
        self._cassette_loaded_for = None
        self.stats = {"allowed": 0, "denied": 0, "recorded": 0, "replayed": 0, "replay_miss": 0,
                      "secrets_injected": 0, "mode": self.policy["mode"]}
        self._lock = threading.Lock()
        self.reload(force=True)

    # -- policy file ----------------------------------------------------------

    def reload(self, force=False):
        try:
            m = POLICY_PATH.stat().st_mtime
        except FileNotFoundError:
            return
        if force or m != self._mtime:
            try:
                self.policy.update(json.loads(POLICY_PATH.read_text() or "{}"))
                self._mtime = m
                self.stats["mode"] = self.policy["mode"]
                log.info(f"policy loaded: mode={self.policy['mode']} allow={self.policy.get('allow')}")
            except (OSError, ValueError) as e:
                log.warning(f"policy reload failed: {e}")
        if self.policy["mode"] == "replay" and self._cassette_loaded_for != self.policy["cassette"]:
            self._load_cassette()

    def _write_stats(self):
        try:
            STATS_PATH.write_text(json.dumps(self.stats))
        except OSError:
            pass

    # -- cassette -----------------------------------------------------------------

    @staticmethod
    def key(flow: http.HTTPFlow) -> str:
        body = hashlib.sha256(flow.request.raw_content or b"").hexdigest()[:16]
        return f"{flow.request.method} {flow.request.pretty_url} {body}"

    def _load_cassette(self):
        self._cassette = {}
        p = Path(self.policy["cassette"])
        if p.exists():
            for line in p.read_text().splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self._cassette[rec["key"]] = rec
        self._cassette_loaded_for = self.policy["cassette"]
        log.info(f"cassette loaded: {len(self._cassette)} exchanges from {p}")

    def _record(self, flow: http.HTTPFlow):
        rec = {
            "key": self.key(flow), "ts": time.time(),
            "request": {"method": flow.request.method, "url": flow.request.pretty_url,
                        "headers": dict(flow.request.headers),
                        "body_b64": _b64(flow.request.raw_content or b"")},
            "response": {"status": flow.response.status_code,
                         "headers": dict(flow.response.headers),
                         "body_b64": _b64(flow.response.raw_content or b"")},
        }
        with self._lock, open(self.policy["cassette"], "a") as f:
            f.write(json.dumps(rec) + "\n")
        self.stats["recorded"] += 1

    # -- host matching ----------------------------------------------------------------

    def allowed(self, host: str) -> bool:
        for pat in self.policy.get("allow") or []:
            if fnmatch.fnmatch(host, pat):
                return True
        return False

    # -- secrets ----------------------------------------------------------------------

    def inject(self, flow: http.HTTPFlow):
        host = flow.request.pretty_host
        for placeholder, spec in (self.policy.get("secrets") or {}).items():
            if not fnmatch.fnmatch(host, spec.get("host", "")):
                continue
            real = spec["value"]
            hit = False
            for k, v in list(flow.request.headers.items()):
                if placeholder in v:
                    flow.request.headers[k] = v.replace(placeholder, real)
                    hit = True
            if flow.request.content and placeholder.encode() in flow.request.content:
                flow.request.content = flow.request.content.replace(placeholder.encode(), real.encode())
                hit = True
            if hit:
                self.stats["secrets_injected"] += 1

    def scrub(self, flow: http.HTTPFlow):
        """real value -> placeholder in responses, so the secret never enters
        the sandbox even if the upstream echoes it."""
        for placeholder, spec in (self.policy.get("secrets") or {}).items():
            real = spec["value"].encode()
            if flow.response and flow.response.content and real in flow.response.content:
                flow.response.content = flow.response.content.replace(real, placeholder.encode())
            if flow.response:
                for k, v in list(flow.response.headers.items()):
                    if spec["value"] in v:
                        flow.response.headers[k] = v.replace(spec["value"], placeholder)

    # -- mitmproxy hooks ------------------------------------------------------------------

    def request(self, flow: http.HTTPFlow):
        self.reload()
        mode = self.policy["mode"]
        host = flow.request.pretty_host
        if mode == "deny":
            self.stats["denied"] += 1
            flow.response = http.Response.make(403, b"egress denied by sandbox policy\n",
                                               {"Content-Type": "text/plain", "X-Sandbox-Egress": "deny"})
        elif mode in ("allow", "record"):
            if not self.allowed(host):
                self.stats["denied"] += 1
                flow.response = http.Response.make(403, f"host {host} not in allowlist\n".encode(),
                                                   {"Content-Type": "text/plain", "X-Sandbox-Egress": "blocked"})
            else:
                self.stats["allowed"] += 1
                self.inject(flow)
        elif mode == "replay":
            self.reload()
            rec = self._cassette.get(self.key(flow))
            if rec:
                self.stats["replayed"] += 1
                r = rec["response"]
                headers = {k: v for k, v in r["headers"].items()
                           if k.lower() not in ("content-length", "transfer-encoding", "content-encoding")}
                headers["X-Sandbox-Egress"] = "replay"
                flow.response = http.Response.make(r["status"], _unb64(r["body_b64"]), headers)
            else:
                self.stats["replay_miss"] += 1
                code = 599 if self.policy.get("strict_replay", True) else 404
                flow.response = http.Response.make(code, b"no recorded response for this request\n",
                                                   {"Content-Type": "text/plain", "X-Sandbox-Egress": "replay-miss"})
        self._write_stats()

    def response(self, flow: http.HTTPFlow):
        if flow.response is None:
            return
        if flow.response.headers.get("X-Sandbox-Egress"):
            return                                  # synthesized by us
        self.scrub(flow)
        flow.response.headers["X-Sandbox-Egress"] = self.policy["mode"]
        if self.policy["mode"] == "record":
            # un-inject before recording so cassettes hold placeholders only
            self._unsubstitute_request(flow)
            self._record(flow)
        self._write_stats()

    def _unsubstitute_request(self, flow: http.HTTPFlow):
        for placeholder, spec in (self.policy.get("secrets") or {}).items():
            real = spec["value"]
            for k, v in list(flow.request.headers.items()):
                if real in v:
                    flow.request.headers[k] = v.replace(real, placeholder)
            if flow.request.content and real.encode() in flow.request.content:
                flow.request.content = flow.request.content.replace(real.encode(), placeholder.encode())


def _b64(b: bytes) -> str:
    import base64
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    import base64
    return base64.b64decode(s)


addons = [EgressPolicy()]
