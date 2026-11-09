"""Upstream clients: sandbox-api (sandboxes) and sandbox-checkpoint
(checkpoint/fork for preemption). Every call is timeout-bounded; connection
errors and 503s raise Down so the loops degrade honestly."""
import requests

from . import config


class UpstreamDown(Exception):
    """sandbox-api unreachable or answering 503."""


class UpstreamConflict(Exception):
    """409 from upstream."""


class CheckpointDown(Exception):
    """sandbox-checkpoint unreachable or failing."""


class Upstream:
    Down = UpstreamDown
    Conflict = UpstreamConflict

    def __init__(self, api_url=config.SANDBOX_API_URL, ckpt_url=config.CHECKPOINT_URL,
                 timeout=config.UPSTREAM_TIMEOUT):
        self.api_url = api_url
        self.ckpt_url = ckpt_url
        self.timeout = timeout

    def _req(self, method: str, path: str, base: str | None = None, **kw) -> requests.Response:
        url = f"{base or self.api_url}{path}"
        kw.setdefault("timeout", self.timeout)
        try:
            r = requests.request(method, url, **kw)
        except requests.RequestException as e:
            raise UpstreamDown(str(e)) from e
        if r.status_code == 503:
            raise UpstreamDown("upstream 503 (dind down)")
        if r.status_code == 409:
            raise UpstreamConflict(r.json().get("detail", "conflict"))
        return r

    def ping(self) -> bool:
        try:
            j = self._req("GET", "/healthz").json()
            return j.get("api") == "up" and j.get("dind") == "up"
        except Exception:
            return False

    def ping_checkpoint(self) -> bool:
        try:
            return requests.get(self.ckpt_url + "/healthz", timeout=3).ok
        except requests.RequestException:
            return False

    def status(self) -> dict:
        return self._req("GET", "/status").json()

    def list_sandboxes(self) -> list[dict]:
        return self._req("GET", "/sandboxes").json()

    def create_sandbox(self, name: str, template: str, stateful: bool = False) -> dict:
        body = {"name": name, "template": template}
        if stateful:
            body["options"] = {"stateful": True}
        r = self._req("POST", "/sandboxes", json=body)
        if r.status_code == 429:
            raise UpstreamConflict("upstream capacity exhausted (429)")
        r.raise_for_status()
        return r.json()

    def get_sandbox(self, sid: str) -> dict | None:
        r = self._req("GET", f"/sandboxes/{sid}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def delete_sandbox(self, sid: str) -> None:
        r = self._req("DELETE", f"/sandboxes/{sid}")
        if r.status_code in (204, 404):
            return
        r.raise_for_status()

    def exec_sandbox(self, sid: str, cmd: list[str], timeout: int) -> dict:
        r = self._req("POST", f"/sandboxes/{sid}/exec", json={"cmd": cmd, "timeout": timeout},
                      timeout=timeout + 15)
        r.raise_for_status()
        return r.json()

    def sandbox_memory(self, sid: str) -> int:
        """Sum of memory.current over the sandbox's containers (cgroup via agent)."""
        r = self._req("GET", f"/sandboxes/{sid}/stats")
        if not r.ok:
            return 0
        total = 0
        for c in r.json().values():
            cur = ((c.get("cgroup") or {}).get("memory") or {}).get("current")
            if isinstance(cur, int):
                total += cur
        return total

    # -- checkpoint service ------------------------------------------------------

    def checkpoint_create(self, sid: str, label: str = "") -> dict:
        try:
            r = requests.post(f"{self.ckpt_url}/sandboxes/{sid}/checkpoints",
                              json={"label": label, "ship": False}, timeout=300)
        except requests.RequestException as e:
            raise CheckpointDown(str(e)) from e
        if not r.ok:
            raise CheckpointDown(f"checkpoint {r.status_code}: {r.text[:200]}")
        return r.json()

    def fork_checkpoint(self, cid: str, name: str) -> dict:
        try:
            r = requests.post(f"{self.ckpt_url}/checkpoints/{cid}/fork", json={"name": name}, timeout=60)
        except requests.RequestException as e:
            raise CheckpointDown(str(e)) from e
        if r.status_code == 409:
            raise UpstreamConflict(r.json().get("detail", "conflict"))
        if not r.ok:
            raise CheckpointDown(f"fork {r.status_code}: {r.text[:200]}")
        return r.json()
