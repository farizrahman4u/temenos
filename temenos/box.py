"""Box — a persistent, named sandbox handle (Layer 2).

Wraps a Backend's open/exec/close with ergonomic helpers (read_file/write_file/list_dir/
writes) and a per-box audit log. Synchronous; the daemon exposes it asynchronously by
running these in a threadpool (plan §7/§8).

    with Box("my_box", Policy(write=["/work"])) as box:
        box.write_file("/work/a.py", "print(6*7)\\n")
        print(box.exec(["python3", "/work/a.py"]).stdout)   # -> 42
"""
from __future__ import annotations

from .backends.base import Backend
from .policy import Policy
from .result import AuditLog, ExecResult, PolicyDecision


class Box:
    def __init__(
        self,
        name: str,
        policy: Policy,
        *,
        backend: Backend | None = None,
        tenant: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.policy = policy
        self.tenant = tenant
        self.audit = AuditLog()
        self._env = env
        self._opened = False
        if backend is None:
            from .backends.gvisor import GVisorBackend
            backend = GVisorBackend()
        self._backend = backend

    # -- lifecycle --------------------------------------------------------------------

    def start(self) -> "Box":
        if not self._opened:
            self._backend.open(self.policy, name=self.name, env=self._env)
            self._opened = True
            self.audit.record("open", PolicyDecision.ALLOW,
                              {"backend": self._backend.name}, box=self.name)
        return self

    def commit(self) -> None:
        """Persist provider-backed volumes (e.g. fsspec upload). Disk/memory are no-ops."""
        self._require_open()
        commit = getattr(self._backend, "commit", None)
        if callable(commit):
            commit()

    def close(self) -> None:
        if self._opened:
            self._backend.close()
            self._opened = False

    def __enter__(self) -> "Box":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- execution --------------------------------------------------------------------

    def exec(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin: bytes | str | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        self._require_open()
        data = stdin.encode() if isinstance(stdin, str) else stdin
        result = self._backend.exec(cmd, cwd=cwd, env=env, stdin=data, timeout=timeout)
        self.audit.record(
            "exec", PolicyDecision.ALLOW,
            {"cmd": cmd, "exit_code": result.exit_code, "duration_ms": result.duration_ms},
            box=self.name,
        )
        return result

    # -- file helpers (all operate INSIDE the box; writes hit the overlay, not host) --

    def read_file(self, path: str) -> str:
        r = self.exec(["cat", path])
        if not r.ok:
            raise FileNotFoundError(f"{path}: {r.stderr.strip()}")
        return r.stdout

    def write_file(self, path: str, content: str | bytes) -> None:
        data = content.encode() if isinstance(content, str) else content
        # path is a single argv ($0), never interpolated into the shell — no injection.
        r = self.exec(["/bin/sh", "-c", 'cat > "$0"', path], stdin=data)
        if not r.ok:
            raise OSError(f"write {path}: {r.stderr.strip()}")
        self.audit.record("write", PolicyDecision.ALLOW,
                          {"path": path, "bytes": len(data)}, box=self.name)

    def list_dir(self, path: str = "/") -> list[str]:
        r = self.exec(["ls", "-1A", path])
        if not r.ok:
            raise FileNotFoundError(f"{path}: {r.stderr.strip()}")
        return [line for line in r.stdout.splitlines() if line]

    def writes(self) -> list[str]:
        """Manifest of files currently present under the policy's write paths — the
        merged box view (original + the box's changes), for human review before commit.
        (A true diff-vs-original is a post-v1 refinement; see plan §13.)"""
        if not self.policy.write:
            return []
        r = self.exec(["find", *self.policy.write, "-type", "f"])
        return sorted(line for line in r.stdout.splitlines() if line) if r.ok else []

    # -- internal ---------------------------------------------------------------------

    def _require_open(self) -> None:
        if not self._opened:
            raise RuntimeError(f"box {self.name!r} is not started; call start() or use "
                               "it as a context manager")
