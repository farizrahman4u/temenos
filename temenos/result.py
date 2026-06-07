"""Results and audit records (Layer 0, pure data).

``ExecResult`` is what an ``exec`` returns. The audit types record what the agent *did*
at the level temenos can observe (exec / network decision / spawn / write-set) — not
per-syscall (see plan §D10).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


@dataclass
class ExecResult:
    """The outcome of running one command in a box."""

    stdout: str
    stderr: str
    exit_code: int
    truncated: bool = False          # output hit max_output_bytes and was cut
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def raise_for_status(self) -> "ExecResult":
        if not self.ok:
            raise RuntimeError(f"command exited {self.exit_code}:\n{self.stderr}")
        return self

    def to_dict(self) -> dict[str, object]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "truncated": self.truncated,
            "duration_ms": self.duration_ms,
        }


class PolicyDecision(Enum):
    ALLOW = "allow"
    DENY = "deny"
    MODIFY = "modify"   # e.g. a path/host was rewritten


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AuditEntry:
    """One semantic event: an exec, a network-connection decision, a spawn, a write."""

    kind: str                                   # "exec" | "network" | "spawn" | "write" | "denied"
    decision: PolicyDecision
    details: dict[str, object] = field(default_factory=dict)
    box: str | None = None                      # box name this happened in
    timestamp: datetime = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "kind": self.kind,
            "decision": self.decision.value,
            "details": self.details,
            "box": self.box,
        }


@dataclass
class AuditLog:
    """An append-only log of ``AuditEntry`` for one box."""

    entries: list[AuditEntry] = field(default_factory=list)

    def record(
        self,
        kind: str,
        decision: PolicyDecision,
        details: dict[str, object] | None = None,
        *,
        box: str | None = None,
        timestamp: datetime | None = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            kind=kind,
            decision=decision,
            details=details or {},
            box=box,
            timestamp=timestamp or _utcnow(),
        )
        self.entries.append(entry)
        return entry

    def __iter__(self):
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def to_dicts(self) -> list[dict[str, object]]:
        return [e.to_dict() for e in self.entries]
