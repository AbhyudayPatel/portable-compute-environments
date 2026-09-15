"""Upstream client: the scheduler's ONLY view of the docker world is the
T01 sandbox-api. Every call is timeout-bounded and raises UpstreamDown on
connection/5xx-with-Retry-After so the scheduler degrades honestly
(edge case #7 - never a hot loop, jobs just stay QUEUED).
"""
import requests

from . import config


class UpstreamDown(Exception):
    """sandbox-api unreachable or answering 503."""


class UpstreamConflict(Exception):
    """409 from upstream (e.g. delete-while-CREATING, name conflict)."""


def _req(method: str, path: str, **kw) -> requests.Response:
    url = f"{config.SANDBOX_API_URL}{path}"
    kw.setdefault("timeout", config.UPSTREAM_TIMEOUT)
    try:
        r = requests.request(method, url, **kw)
    except requests.RequestException as e:
        raise UpstreamDown(str(e)) from e
    if r.status_code == 503:
        raise UpstreamDown("upstream 503 (dind down)")
    if r.status_code == 409:
        raise UpstreamConflict(r.json().get("detail", "conflict"))
    return r


def ping() -> bool:
    try:
        r = _req("GET", "/healthz")
        j = r.json()
        return j.get("api") == "up" and j.get("dind") == "up"
    except Exception:
        return False


def status() -> dict:
    return _req("GET", "/status").json()


def list_sandboxes() -> list[dict]:
    return _req("GET", "/sandboxes").json()


def create_sandbox(name: str, template: str) -> dict:
    """Idempotent by name upstream; 201 or 200 both fine."""
    r = _req("POST", "/sandboxes",
             json={"name": name, "template": template})
    if r.status_code == 429:
        raise UpstreamConflict("upstream capacity exhausted (429)")
    r.raise_for_status()
    return r.json()


def get_sandbox(sid: str) -> dict | None:
    r = _req("GET", f"/sandboxes/{sid}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def delete_sandbox(sid: str) -> None:
    """Idempotent: 204 and 404 both mean 'gone'."""
    r = _req("DELETE", f"/sandboxes/{sid}")
    if r.status_code in (204, 404):
        return
    r.raise_for_status()


def exec_sandbox(sid: str, cmd: list[str], timeout: int) -> dict:
    r = _req("POST", f"/sandboxes/{sid}/exec",
             json={"cmd": cmd, "timeout": timeout})
    r.raise_for_status()
    return r.json()
