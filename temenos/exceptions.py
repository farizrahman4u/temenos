"""temenos exceptions.

One base (`TemenosError`) so callers can catch everything temenos raises; specific
subclasses for the cases worth distinguishing.
"""
from __future__ import annotations


class TemenosError(Exception):
    """Base class for every error temenos raises."""


class PolicyViolation(TemenosError):
    """A policy operation tried to widen a capability (e.g. ``Policy.restrict`` asked to
    add a path/host, raise a limit, or raise the trust level). Escalation is an error,
    not an operation."""


class BoxNotFound(TemenosError):
    """No box with the given name exists for this tenant."""


class QuotaExceeded(TemenosError):
    """A tenant hit a quota (max boxes, aggregate memory/disk, …)."""


class BackendError(TemenosError):
    """The sandbox backend could not satisfy a request — e.g. no usable gVisor platform,
    the box failed to start, or an exec could not be dispatched."""
