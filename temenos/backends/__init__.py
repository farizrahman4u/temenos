"""Sandbox backends (Layer 1 — the only OS-touching layer).

v1 ships GVisorBackend only. Native/Seatbelt/Windows are post-v1 (see plan §13).
"""
from __future__ import annotations

from .base import Backend
from .gvisor import GVisorBackend

__all__ = ["Backend", "GVisorBackend"]
