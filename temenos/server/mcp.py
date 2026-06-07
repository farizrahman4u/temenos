"""Per-box MCP data plane (Phase 5, plan §8c/§8e).

One FastMCP server exposes four box-scoped tools (`exec`/`read`/`write`/`list`) — Claude
sees them as `mcp__temenos__{exec,read,write,list}`. There is deliberately **no**
create/delete/commit tool: the agent can run/read/write inside the box but cannot touch
the host or change the box's lifecycle (writes stay in the overlay; a human reviews them
with `temenos diff`).

Routing: every MCP connection is bound to exactly one box by the URL path `/mcp/<id>`.
A tiny ASGI wrapper (`BoxMCPRouter`) pulls the id off the path, checks the daemon token,
verifies the box exists, and stashes `(manager, id)` in a contextvar that the tools read —
verified to propagate into FastMCP's stateless request handling (incl. the sync-tool
threadpool). Stateless + JSON responses keep each request self-contained, so one server
safely fans out to every box.
"""
from __future__ import annotations

import contextvars

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings

from ..box import Box
from ..exceptions import BoxNotFound
from ..manager import BoxManager

# Set per-request by the router; tools resolve their box from it. ContextVars are
# task-local and copied into FastMCP's stateless handler + sync-tool threads (spiked).
_CURRENT: contextvars.ContextVar[tuple[BoxManager, str]] = contextvars.ContextVar("temenos_box")


def _box() -> Box:
    manager, bid = _CURRENT.get()
    return manager.get(bid)


def _build_fastmcp() -> FastMCP:
    # Loopback-only daemon, and the box id is already a hash → DNS-rebinding protection
    # (which rejects non-allowlisted Host headers) only gets in the way; the daemon's
    # Bearer token is the real gate (checked in the router).
    mcp = FastMCP(
        "temenos",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool(description="Run a command in your sandbox (argv list, not a shell string). "
                          "This is your ONLY way to execute code.")
    def exec(command: list[str], cwd: str | None = None,
             timeout_s: float | None = None) -> dict:
        r = _box().exec(command, cwd=cwd, timeout=timeout_s)
        return {"stdout": r.stdout, "stderr": r.stderr, "exit_code": r.exit_code,
                "truncated": r.truncated}

    @mcp.tool(description="Read a file from your sandbox. Host files outside the box are "
                          "not visible. This is your ONLY way to read files.")
    def read(path: str) -> dict:
        try:
            return {"content": _box().read_file(path), "truncated": False}
        except FileNotFoundError as e:
            return {"error": str(e)}

    @mcp.tool(description="Write a file in your sandbox (lands in the box overlay, never on "
                          "the host). This is your ONLY way to write files.")
    def write(path: str, content: str) -> dict:
        _box().write_file(path, content)
        return {"bytes": len(content.encode())}

    @mcp.tool(description="List a directory in your sandbox.")
    def list(path: str = "/") -> dict:
        try:
            return {"entries": _box().list_dir(path)}
        except FileNotFoundError as e:
            return {"error": str(e)}

    return mcp


class BoxMCPRouter:
    """ASGI app mounted at `/mcp`: routes `/mcp/<id>` to a box-bound MCP session.

    `mcp.session_manager.run()` must be entered by the host app's lifespan (the daemon
    wires this); without it the streamable handler has no task group.
    """

    def __init__(self, manager: BoxManager, token: str | None) -> None:
        self._manager = manager
        self._token = token
        self.mcp = _build_fastmcp()
        self.mcp.streamable_http_app()              # force lazy session-manager creation
        self._asgi = StreamableHTTPASGIApp(self.mcp._session_manager)

    @property
    def session_manager(self):
        return self.mcp._session_manager

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self._asgi(scope, receive, send)
            return
        # The id is the first path segment, tolerating either a stripped mount path
        # ("/<id>") or the full path ("/mcp/<id>").
        parts = [p for p in scope.get("path", "/").split("/") if p]
        if parts and parts[0] == "mcp":
            parts = parts[1:]
        bid = parts[0] if parts else ""
        headers = dict(scope.get("headers") or [])
        authz = headers.get(b"authorization", b"").decode()
        if self._token and authz != f"Bearer {self._token}":
            await _reject(send, 401, "invalid or missing token")
            return
        try:
            self._manager.get(bid)
        except BoxNotFound:
            await _reject(send, 404, f"no such box: {bid}")
            return
        # Normalize the path to the streamable handler's own root so it matches regardless
        # of how we were mounted.
        inner = dict(scope)
        inner["path"] = "/"
        inner["raw_path"] = b"/"
        reset = _CURRENT.set((self._manager, bid))
        try:
            await self._asgi(inner, receive, send)
        finally:
            _CURRENT.reset(reset)


async def _reject(send, status: int, detail: str) -> None:
    import json
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})
