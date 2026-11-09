"""Stateful sandbox volumes: overlayfs layers mounted inside dind.

A "stateful" sandbox gets, for every data path its template declares, an
overlay mount prepared on the engine node (inside the dind container) and
bind-mounted into the sandbox container:

    /var/lib/sbx/vol/<sid>/<role>/
        base/     empty directory used as the bottom layer when no
                  checkpoint layers are given
        upper/    everything the sandbox writes (its delta)
        work/     overlayfs scratch
        merged/   what the container sees at its data path
    lowerdir = <layer1>:<layer2>:...:<base>   (checkpoint layers, newest first)

Why overlay: a checkpoint is then "copy upper/ somewhere immutable" (small:
only the delta since the container started) and a fork is "mount the same
immutable layers under a fresh upper/" - O(size of delta), not O(size of
the data). Overlay needs an ext4/xfs home, which is why dind has the
`sbx-state` volume at /var/lib/sbx (overlay cannot stack on the
container's own overlay rootfs - observed and documented).

All commands run on the node through the agent proxy's /node/exec (root in
the privileged dind container).
"""
import shlex

from .agent import NodeClient

BASE = "/var/lib/sbx"


class VolumeManager:
    def __init__(self, node: NodeClient):
        self.node = node

    @staticmethod
    def vol_dir(sid: str, role: str) -> str:
        return f"{BASE}/vol/{sid}/{role}"

    def prepare(self, sid: str, role: str, layers: list[str] | None = None,
                copy_upper_from: str | None = None) -> str:
        """Create and mount the overlay for (sid, role). Returns the merged
        path (bind-mount source for the container). Idempotent: an already
        mounted merged dir is left alone."""
        d = self.vol_dir(sid, role)
        lowers = list(layers or []) + [f"{d}/base"]
        lowerdir = ":".join(lowers)
        copy = ""
        if copy_upper_from:
            copy = f"cp -a {shlex.quote(copy_upper_from)}/. {d}/upper/ ;"
        self.node.sh(
            f"mkdir -p {d}/base {d}/upper {d}/work {d}/merged; "
            f"if ! mountpoint -q {d}/merged; then "
            f"  {copy} "
            f"  mount -t overlay overlay -o lowerdir={shlex.quote(lowerdir)},upperdir={d}/upper,workdir={d}/work {d}/merged; "
            f"fi", timeout=120)
        return f"{d}/merged"

    def plain_dir(self, sid: str, name: str, copy_from: str | None = None,
                  owner: str | None = None) -> str:
        """A plain (non-overlay) directory, e.g. a WAL archive. `owner`
        (uid:gid) matters: Postgres archives as uid 70 and a root-owned
        directory makes every archive_command fail silently."""
        d = f"{BASE}/vol/{sid}/{name}"
        copy = f"cp -a {shlex.quote(copy_from)}/. {d}/ ;" if copy_from else ""
        chown = f"chown -R {shlex.quote(owner)} {d};" if owner else ""
        self.node.sh(f"mkdir -p {d}; {copy} {chown} true", timeout=120)
        return d

    def mounts(self, sid: str) -> list[dict]:
        """Overlay mounts of this sandbox as seen on the node."""
        res = self.node.exec(["sh", "-c", f"grep ' {BASE}/vol/{sid}/' /proc/mounts || true"])
        out = []
        for line in res.get("stdout", "").splitlines():
            parts = line.split()
            if len(parts) >= 4:
                out.append({"mountpoint": parts[1], "fstype": parts[2], "options": parts[3]})
        return out

    def upper_size(self, sid: str, role: str) -> int:
        res = self.node.exec(["sh", "-c", f"du -sb {self.vol_dir(sid, role)}/upper 2>/dev/null | cut -f1"])
        try:
            return int(res.get("stdout", "0").strip() or 0)
        except ValueError:
            return 0

    def teardown(self, sid: str) -> None:
        """Unmount every overlay of the sandbox and remove its directory.
        Safe when nothing exists."""
        d = f"{BASE}/vol/{sid}"
        self.node.sh(
            f"if [ -d {d} ]; then "
            f"  for m in $(grep ' {d}/' /proc/mounts | awk '{{print $2}}' | sort -r); do umount -l \"$m\" || true; done; "
            f"  rm -rf {d}; "
            f"fi", timeout=120)

    def snapshot(self, sid: str, st: dict, dest: str) -> dict:
        """Copy upper layers and plain dirs of a frozen sandbox to dest."""
        if not dest.startswith(f"{BASE}/ckpt/"):
            raise ValueError("dest must be under /var/lib/sbx/ckpt/")
        out = {}
        for role, v in st.items():
            src = f"{self.vol_dir(sid, role)}/upper"
            self.node.sh(f"mkdir -p {shlex.quote(dest)}/{role}; "
                         f"cp -a {src}/. {shlex.quote(dest)}/{role}/", timeout=600)
            out[role] = {"kind": "layer", "dir": f"{dest}/{role}", "bytes": self.du(f"{dest}/{role}")}
            for bname in (v.get("binds") or {}):
                psrc = f"{BASE}/vol/{sid}/{bname}"
                self.node.sh(f"mkdir -p {shlex.quote(dest)}/{bname}; "
                             f"cp -a {psrc}/. {shlex.quote(dest)}/{bname}/", timeout=600)
                out[bname] = {"kind": "plain", "dir": f"{dest}/{bname}", "bytes": self.du(f"{dest}/{bname}")}
        self.node.sh(f"sync")
        return out

    def restore(self, sid: str, st: dict, layers: dict, copies: dict) -> dict:
        """Remount every role over the given layers with a fresh upper and
        re-seed plain dirs. The sandbox must be stopped."""
        out = {}
        for role, v in st.items():
            merged = self.remount(sid, role, layers.get(role) or [], keep_upper=False)
            out[role] = {"merged": merged, "layers": layers.get(role) or []}
            for bname in (v.get("binds") or {}):
                d = f"{BASE}/vol/{sid}/{bname}"
                src = copies.get(bname)
                if src:
                    owner = (v.get("chown") or {}).get(bname)
                    chown = f"chown -R {shlex.quote(owner)} {d};" if owner else ""
                    self.node.sh(f"rm -rf {d}; mkdir -p {d}; cp -a {shlex.quote(src)}/. {d}/; {chown} true", timeout=600)
                    out[bname] = {"dir": d, "from": src}
        return out

    def tar_stream(self, path: str):
        """Yield a tar of a node directory (streamed through the proxy)."""
        import requests
        r = requests.post(self.node.base + "/node/tarstream", headers=self.node.headers,
                          json={"path": path}, stream=True, timeout=600)
        if not r.ok:
            from .agent import AgentError
            raise AgentError(r.status_code, r.text[:300])
        return r.iter_content(chunk_size=64 * 1024)

    def untar(self, path: str, data) -> dict:
        """Extract a tar (bytes or a byte iterator) into a node dir. Streams
        through the proxy's /node/untarstream so size is not limited by
        the JSON exec channel (a 64 MB layer blew the 4 MiB body cap)."""
        import requests
        r = requests.put(self.node.base + "/node/untarstream", headers=self.node.headers,
                         params={"path": path}, data=data, timeout=600)
        if not r.ok:
            from .agent import AgentError
            raise AgentError(500, f"untar failed: {r.text[:300]}")
        return {"path": path, "ok": True}

    def rmdir(self, path: str) -> None:
        self.node.sh(f"rm -rf {shlex.quote(path)}", timeout=120)

    def du(self, path: str) -> int:
        """Bytes under path, or -1 when the path does not exist (callers
        use that to detect missing checkpoint layers)."""
        res = self.node.exec(["sh", "-c", f"if [ -e {shlex.quote(path)} ]; then du -sb {shlex.quote(path)} | cut -f1; else echo -1; fi"])
        try:
            return int(res.get("stdout", "-1").strip() or -1)
        except ValueError:
            return -1

    def remount(self, sid: str, role: str, layers: list[str], keep_upper: bool = False) -> str:
        """Replace the layer stack under a (stopped) sandbox's data path.
        Used by in-place restore: fresh upper on top of checkpoint layers."""
        d = self.vol_dir(sid, role)
        lowerdir = ":".join(list(layers) + [f"{d}/base"])
        reset = "" if keep_upper else f"rm -rf {d}/upper {d}/work; mkdir -p {d}/upper {d}/work;"
        self.node.sh(
            f"mountpoint -q {d}/merged && umount -l {d}/merged || true; "
            f"{reset} "
            f"mount -t overlay overlay -o lowerdir={shlex.quote(lowerdir)},upperdir={d}/upper,workdir={d}/work {d}/merged",
            timeout=120)
        return f"{d}/merged"
