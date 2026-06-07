"""Backend ABC — the session contract every sandbox backend implements.

A backend owns ONE box's lifecycle: `open()` stands up a persistent sandbox, `exec()`
runs a process inside it (called many times), `close()` tears it down. Synchronous by
design — the real work is blocking subprocess calls; the async/concurrent story lives at
the daemon layer (FastAPI runs these in a threadpool).
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..policy import Policy
from ..result import ExecResult


class Backend(ABC):
    @abstractmethod
    def open(self, policy: Policy, *, name: str, env: dict[str, str] | None = None) -> None:
        """Stand up the persistent sandbox for `policy`. Idempotent failure → BackendError."""

    @abstractmethod
    def exec(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        """Run `cmd` inside the standing sandbox and return its result."""

    @abstractmethod
    def close(self) -> None:
        """Tear the sandbox down and reclaim its resources. Safe to call more than once."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short backend identifier, e.g. 'gvisor'."""

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        """True if this backend can actually run on the current host (probe, don't guess)."""
