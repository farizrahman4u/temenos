"""Phase 3 — FastAPI daemon + connect-or-spawn."""
from __future__ import annotations

import os
import signal
import socket
import time

import pytest
from fastapi.testclient import TestClient

from temenos.backends.gvisor import GVisorBackend
from temenos.manager import BoxManager
from temenos.server.app import create_app

gvisor = pytest.mark.skipif(not GVisorBackend.is_available(), reason="no gVisor")


# -- app (TestClient, no real server) -------------------------------------------------

def test_healthz_needs_no_auth():
    client = TestClient(create_app(manager=BoxManager(), token="secret"))
    assert client.get("/healthz").json() == {"ok": True}

def test_endpoints_require_token():
    client = TestClient(create_app(manager=BoxManager(), token="secret"))
    assert client.get("/v1/boxes").status_code == 401
    assert client.get("/v1/boxes", headers={"Authorization": "Bearer secret"}).status_code == 200

@gvisor
def test_rest_box_lifecycle(tmp_path):
    mgr = BoxManager()
    client = TestClient(create_app(manager=mgr, token="t"))
    h = {"Authorization": "Bearer t"}
    try:
        bid = client.post("/v1/boxes", json={"data_dir": str(tmp_path / "b"), "policy": {}},
                          headers=h).json()["id"]
        out = client.post(f"/v1/boxes/{bid}/exec", json={"cmd": ["echo", "via-rest"]},
                          headers=h).json()
        assert out["stdout"].strip() == "via-rest"
        assert client.delete(f"/v1/boxes/{bid}", headers=h).status_code == 200
        assert client.get(f"/v1/boxes/{bid}", headers=h).status_code == 404
    finally:
        mgr.shutdown()


# -- connect-or-spawn (spawns a real daemon; no gVisor needed for healthz/list) -------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port

def test_connect_or_spawn_starts_single_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMENOS_HOME", str(tmp_path))
    port = _free_port()
    from temenos.server import client
    cl = client.connect_or_spawn(port=port)
    try:
        assert cl.healthz()
        assert cl.list_boxes() == []
        assert client.connect() is not None         # a second call attaches, doesn't respawn
    finally:
        info = client.read_info()
        if info:
            try:
                os.kill(info["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
            time.sleep(1.0)
