"""temenos — untrusted-code containment for a trusted agent.

v1 public surface so far (Phase 1). Box / BoxManager land in later phases and will be
re-exported here.
"""
from __future__ import annotations

from .exceptions import (
    BackendError,
    BoxNotFound,
    PolicyViolation,
    QuotaExceeded,
    TemenosError,
)
from .box import Box
from .policy import Policy, TrustLevel
from .result import AuditEntry, AuditLog, ExecResult, PolicyDecision

__version__ = "0.1.0"

__all__ = [
    "Box",
    "Policy",
    "TrustLevel",
    "ExecResult",
    "AuditEntry",
    "AuditLog",
    "PolicyDecision",
    "TemenosError",
    "PolicyViolation",
    "BoxNotFound",
    "QuotaExceeded",
    "BackendError",
    "__version__",
]
