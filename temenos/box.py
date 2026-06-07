"""Box — a persistent, named sandbox handle (Layer 2).

Wraps a Backend's open/exec/close with ergonomic helpers (read_file/write_file/list_dir/
writes) and a per-box audit log. Synchronous; the daemon exposes it asynchronously by
running these in a threadpool (plan §7/§8).

    with Box("my_box", Policy(write=["/work"])) as box:
        box.write_file("/work/a.py", "print(6*7)\\n")
        print(box.exec(["python3", "/work/a.py"]).stdout)   # -> 42
"""
from __future__ import annotations

import time

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
        restore_from: str | None = None,
    ) -> None:
        self.name = name
        self.policy = policy
        self.tenant = tenant
        self.audit = AuditLog()
        self._env = env
        self._restore_from = restore_from
        self._opened = False
        # dirty-tracking for the checkpoint heuristic (D17): set on exec, cleared on checkpoint
        self._dirty = False
        self._dirty_since = 0.0
        self._last_activity = 0.0
        if backend is None:
            from .backends.gvisor import GVisorBackend
            backend = GVisorBackend()
        self._backend = backend

    # -- lifecycle --------------------------------------------------------------------

    def start(self) -> "Box":
        if not self._opened:
            self._backend.open(self.policy, name=self.name, env=self._env,
                               restore_from=self._restore_from)
            self._opened = True
            self.audit.record("open", PolicyDecision.ALLOW,
                              {"backend": self._backend.name,
                               "restored_from": self._restore_from}, box=self.name)
        return self

    def commit(self) -> None:
        """Persist provider-backed volumes (e.g. fsspec upload). Disk/memory are no-ops."""
        self._require_open()
        commit = getattr(self._backend, "commit", None)
        if callable(commit):
            commit()

    def checkpoint(self, dest: str, *, leave_running: bool = True) -> None:
        """Save the box's filesystem to `dest`, keeping the box running by default. Needs
        scratch='disk' (the default); a scratch='memory' box cannot be checkpointed.
        Restore by creating a new box with `Box(name, policy, restore_from=dest)`."""
        self._require_open()
        fscheckpoint = getattr(self._backend, "fscheckpoint", None)
        if not callable(fscheckpoint):
            raise RuntimeError("backend does not support checkpointing")
        fscheckpoint(dest, leave_running=leave_running)
        self._dirty = False                     # clean since this checkpoint
        self.audit.record("checkpoint", PolicyDecision.ALLOW,
                          {"dest": dest, "leave_running": leave_running}, box=self.name)

    def close(self) -> None:
        if self._opened:
            self._backend.close()
            self._opened = False

    @property
    def dirty(self) -> bool:
        """True if the box has exec'd (changed) since its last checkpoint."""
        return self._dirty

    def should_autocheckpoint(self, *, idle_debounce: float, max_staleness: float,
                              now: float | None = None) -> bool:
        """Checkpoint heuristic (D17): only if dirty, and either quiet for `idle_debounce`
        (captures a stable resting state, coalesces bursts) or continuously dirty for
        `max_staleness` (bounds work-at-risk on a busy box)."""
        if not self._dirty:
            return False
        now = time.monotonic() if now is None else now
        return (now - self._last_activity) >= idle_debounce \
            or (now - self._dirty_since) >= max_staleness

    @property
    def running(self) -> bool:
        if not self._opened:
            return False
        alive = getattr(self._backend, "alive", None)
        return alive() if callable(alive) else True

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
        now = time.monotonic()
        if not self._dirty:
            self._dirty_since = now
        self._dirty = True
        self._last_activity = now
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

    def attach_context(self) -> dict:
        """Backend pieces for an interactive (PTY) attach — see GVisorBackend.attach_context.
        Used by the daemon to let the local CLI wire a terminal straight into the box."""
        self._require_open()
        fn = getattr(self._backend, "attach_context", None)
        if fn is None:
            raise RuntimeError(f"backend {self._backend.name!r} does not support interactive attach")
        return fn()

    # -- internal ---------------------------------------------------------------------

    def _require_open(self) -> None:
        if not self._opened:
            raise RuntimeError(f"box {self.name!r} is not started; call start() or use "
                               "it as a context manager")
