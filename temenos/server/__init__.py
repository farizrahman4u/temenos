"""temenos daemon surface (Layer 3): FastAPI control plane over a BoxManager, plus the
connect-or-spawn client. The per-box MCP sub-app is added in Phase 5."""
from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
