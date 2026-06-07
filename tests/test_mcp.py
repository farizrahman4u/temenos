"""Phase 5 — the per-box MCP data plane, driven by the OFFICIAL mcp client.

If the reference client can initialize, list, and call tools against `/mcp/<id>`, Claude
Code's client (same protocol) can too. gVisor-gated (it runs real commands in a box) and
uses a real spawned daemon over real HTTP (most faithful to the deployed path).
"""
from __future__ import annotations

import os
import signal
import socket
import time

import pytest

from temenos.backends.gvisor import GVisorBackend

gvisor = pytest.mark.skipif(not GVisorBackend.is_available(), reason="no gVisor")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    """A real spawned daemon on a free port; yields (url, token, DaemonClient)."""
    monkeypatch.setenv("TEMENOS_HOME", str(tmp_path / "daemon"))
    monkeypatch.setenv("TEMENOS_DATA", str(tmp_path / "data"))
    from temenos.server import client
    cl = client.connect_or_spawn(port=_free_port(), wait_s=20.0)
    info = client.read_info()
    try:
        yield info["url"], info["token"], cl
    finally:
        if info:
            try:
                os.kill(info["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
            time.sleep(1.0)


def test_unknown_box_is_rejected(daemon):
    url, token, _ = daemon
    import httpx
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
         "Content-Type": "application/json"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "x", "version": "1"}}}
    r = httpx.post(f"{url}/mcp/deadbeefdeadbeef", json=body, headers=h, timeout=10)
    assert r.status_code == 404


def test_missing_token_is_rejected(daemon, tmp_path):
    url, token, cl = daemon
    bid = cl.create_box(str(tmp_path / "b0"), {})["id"]
    import httpx
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "x", "version": "1"}}}
    r = httpx.post(f"{url}/mcp/{bid}", json=body, headers=h, timeout=10)  # no Authorization
    assert r.status_code == 401


@gvisor
@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore:Use `streamable_http_client`:DeprecationWarning")
async def test_mcp_tools_drive_a_real_box(daemon, tmp_path):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client as streamable_http_client

    url, token, cl = daemon
    bid = cl.create_box(str(tmp_path / "boxM"), {})["id"]
    headers = {"Authorization": f"Bearer {token}"}

    import json

    def payload(result):
        return json.loads(result.content[0].text)   # how Claude consumes the tool output

    async with streamable_http_client(f"{url}/mcp/{bid}", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            assert {"exec", "read", "write", "list"} <= names

            out = await session.call_tool("exec", {"command": ["echo", "hi-mcp"]})
            assert payload(out)["stdout"].strip() == "hi-mcp"

            await session.call_tool("write", {"path": "/tmp/m.txt", "content": "hello"})
            got = await session.call_tool("read", {"path": "/tmp/m.txt"})
            assert payload(got)["content"] == "hello"

            ls = await session.call_tool("list", {"path": "/tmp"})
            assert "m.txt" in payload(ls)["entries"]
