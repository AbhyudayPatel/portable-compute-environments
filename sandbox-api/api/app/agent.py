"""Clients for the in-sandbox agent and the engine-side node proxy.

Topology (see docs/ARCHITECTURE.md, "the agent path"):

    api  --HTTP-->  dind:7799 (sbx-agent proxy)  --HTTP-->  <inner-ip>:7700 (sbx-agent)
                    /sbx/<ip>/<path>                          /<path>

The proxy lives in the dind container, which hosts every inner bridge, so
it can dial any sandbox container by IP. The API never needs a published
port per sandbox for control traffic, and never uses `docker exec`.
"""
import requests

from . import config


class AgentError(Exception):
    """Agent/proxy unreachable or returned an error."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class AgentClient:
    """Talks to ONE sandbox container's agent through the node proxy."""

    def __init__(self, ip: str, token: str | None,
                 node_url: str = config.NODE_URL, timeout: int = config.AGENT_TIMEOUT):
        self.base = f"{node_url}/sbx/{ip}"
        self.timeout = timeout
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    # -- transport -------------------------------------------------------------

    def _req(self, method: str, path: str, timeout: float | None = None,
             **kw) -> requests.Response:
        try:
            r = requests.request(method, self.base + path, headers=self.headers,
                                 timeout=timeout or self.timeout, **kw)
        except requests.RequestException as e:
            raise AgentError(502, f"agent unreachable: {e}") from e
        if r.status_code == 502:
            raise AgentError(502, _detail(r))
        if r.status_code >= 400:
            raise AgentError(r.status_code, _detail(r))
        return r

    # -- health ------------------------------------------------------------------

    def healthz(self) -> dict:
        return self._req("GET", "/healthz", timeout=3).json()

    def alive(self) -> bool:
        try:
            return bool(self.healthz().get("ok"))
        except AgentError:
            return False

    # -- one-shot exec ---------------------------------------------------------------

    def exec(self, cmd: list[str], timeout: int, env: dict | None = None,
             cwd: str | None = None, max_bytes: int = config.EXEC_OUTPUT_LIMIT) -> dict:
        body = {"cmd": cmd, "timeout_seconds": timeout, "max_bytes": max_bytes}
        if env:
            body["env"] = env
        if cwd:
            body["cwd"] = cwd
        # the agent enforces the deadline; give the HTTP call headroom
        return self._req("POST", "/exec", json=body, timeout=timeout + 15).json()

    # -- sessions --------------------------------------------------------------------

    def session_create(self, body: dict) -> dict:
        return self._req("POST", "/sessions", json=body).json()

    def session_list(self) -> list[dict]:
        return self._req("GET", "/sessions").json().get("sessions", [])

    def session_get(self, sess: str) -> dict:
        return self._req("GET", f"/sessions/{sess}").json()

    def session_stream(self, sess: str, since: int, follow: bool, timeout: int):
        """Yields raw NDJSON lines (bytes). Caller streams them onward."""
        params = {"since": since, "follow": "1" if follow else "0", "timeout": timeout}
        r = self._req("GET", f"/sessions/{sess}/stream", params=params,
                      stream=True, timeout=timeout + 15)
        for line in r.iter_lines():
            if line:
                yield line

    def session_stdin(self, sess: str, data: bytes, eof: bool) -> dict:
        return self._req("POST", f"/sessions/{sess}/stdin",
                         params={"eof": "1" if eof else "0"}, data=data).json()

    def session_signal(self, sess: str, signal: str) -> dict:
        return self._req("POST", f"/sessions/{sess}/signal", json={"signal": signal}).json()

    def session_resize(self, sess: str, cols: int, rows: int) -> dict:
        return self._req("POST", f"/sessions/{sess}/resize",
                         json={"cols": cols, "rows": rows}).json()

    def session_delete(self, sess: str) -> None:
        self._req("DELETE", f"/sessions/{sess}")

    # -- introspection ---------------------------------------------------------------

    def ps(self) -> dict:
        return self._req("GET", "/ps").json()

    def stats(self) -> dict:
        return self._req("GET", "/stats").json()

    def fingerprint(self, path: str, exclude: str = "", detail: bool = False) -> dict:
        params = {"path": path, "exclude": exclude, "detail": "1" if detail else "0"}
        return self._req("GET", "/fingerprint", params=params, timeout=120).json()

    # -- files ---------------------------------------------------------------------------

    def fs_list(self, path: str) -> dict:
        return self._req("GET", "/fs/list", params={"path": path}).json()

    def fs_stat(self, path: str) -> dict:
        return self._req("GET", "/fs/stat", params={"path": path}).json()

    def fs_read(self, path: str, max_bytes: int = 1 << 20) -> bytes:
        return self._req("GET", "/fs/read", params={"path": path, "max": max_bytes},
                         timeout=60).content

    def fs_write(self, path: str, data: bytes, mode: str = "0644") -> dict:
        return self._req("PUT", "/fs/file", params={"path": path, "mode": mode},
                         data=data, timeout=120).json()

    def fs_mkdir(self, path: str) -> dict:
        return self._req("POST", "/fs/mkdir", params={"path": path}).json()

    def fs_delete(self, path: str) -> dict:
        return self._req("DELETE", "/fs", params={"path": path}).json()

    def fs_get_tar(self, path: str):
        """Streams a tar of `path` (file or directory)."""
        r = self._req("GET", "/fs/get", params={"path": path}, stream=True, timeout=600)
        return r.iter_content(chunk_size=64 * 1024)

    def fs_put_tar(self, path: str, data) -> dict:
        return self._req("PUT", "/fs/put", params={"path": path}, data=data,
                         timeout=600).json()

    # -- gui -------------------------------------------------------------------------------

    def gui_display(self) -> dict:
        return self._req("GET", "/gui/display").json()

    def gui_screenshot(self) -> bytes:
        return self._req("POST", "/gui/screenshot", timeout=30).content

    def gui_action(self, action: dict) -> dict:
        return self._req("POST", "/gui/action", json=action, timeout=30).json()

    def gui_windows(self) -> dict:
        return self._req("GET", "/gui/windows", timeout=30).json()


class NodeClient:
    """Privileged operations inside the dind container (mount, iptables...)."""

    def __init__(self, node_url: str = config.NODE_URL, token: str = config.NODE_TOKEN):
        self.base = node_url
        self.headers = {"X-Node-Token": token}

    def healthz(self) -> bool:
        try:
            r = requests.get(self.base + "/healthz", timeout=3)
            return r.ok
        except requests.RequestException:
            return False

    def info(self) -> dict:
        try:
            r = requests.get(self.base + "/node/info", headers=self.headers, timeout=5)
        except requests.RequestException as e:
            raise AgentError(502, f"node proxy unreachable: {e}") from e
        if not r.ok:
            raise AgentError(r.status_code, _detail(r))
        return r.json()

    def exec(self, cmd: list[str], timeout: int = 60, stdin: str = "") -> dict:
        try:
            r = requests.post(self.base + "/node/exec", headers=self.headers,
                              json={"cmd": cmd, "timeout_seconds": timeout, "stdin": stdin},
                              timeout=timeout + 10)
        except requests.RequestException as e:
            raise AgentError(502, f"node proxy unreachable: {e}") from e
        if not r.ok:
            raise AgentError(r.status_code, _detail(r))
        return r.json()

    def sh(self, script: str, timeout: int = 60) -> dict:
        """Run a shell snippet; raises AgentError on non-zero exit."""
        res = self.exec(["sh", "-ec", script], timeout=timeout)
        if res.get("exit_code") != 0:
            raise AgentError(500, f"node command failed (exit {res.get('exit_code')}): "
                                  f"{res.get('stderr', '')[:500]} {res.get('stdout', '')[:200]}")
        return res


def _detail(r: requests.Response) -> str:
    try:
        j = r.json()
        return j.get("error") or j.get("detail") or r.text[:300]
    except ValueError:
        return r.text[:300]
