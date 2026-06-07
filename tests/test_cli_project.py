"""Phase 4 — project CLI end-to-end (create → exec → ls → audit → rm) through `main()`.

Drives the real connect-or-spawn daemon and a real gVisor box, so it's gVisor-gated. The
daemon, global data and $HOME are all redirected under tmp for isolation; the spawned
daemon is forced onto a free port and SIGTERM'd in teardown.
"""
from __future__ import annotations

import os
import signal
import socket
import time

import pytest

from temenos.backends.gvisor import GVisorBackend
from temenos.cli import main

gvisor = pytest.mark.skipif(not GVisorBackend.is_available(), reason="no gVisor")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def project_env(tmp_path, monkeypatch):
    """Isolated $HOME + daemon + global data, with the daemon forced onto a free port."""
    home = tmp_path / "home"
    repo = home / "repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TEMENOS_HOME", str(tmp_path / "daemon"))
    monkeypatch.setenv("TEMENOS_DATA", str(tmp_path / "data"))
    monkeypatch.chdir(repo)

    from temenos.server import client
    port = _free_port()
    real = client.connect_or_spawn
    monkeypatch.setattr(client, "connect_or_spawn",
                        lambda port=port, wait_s=20.0: real(port=port, wait_s=wait_s))
    try:
        yield repo
    finally:
        info = client.read_info()
        if info:
            try:
                os.kill(info["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
            time.sleep(1.0)


@gvisor
def test_project_create_exec_ls_audit_rm(project_env, capsys):
    repo = project_env

    assert main(["create", "default"]) == 0
    out = capsys.readouterr().out
    assert "initialized project" in out and "[project]" in out
    assert os.path.exists(repo / ".temenos" / "default" / "config.json")

    assert main(["exec", "default", "--", "echo", "hello-box"]) == 0
    assert capsys.readouterr().out.strip() == "hello-box"

    # the box sees the repo at its real path (live-writable mount, D16)
    assert main(["exec", "default", "--", "ls", str(repo)]) == 0
    capsys.readouterr()

    assert main(["ls"]) == 0
    assert "default" in capsys.readouterr().out

    assert main(["audit", "default"]) == 0
    assert "exec" in capsys.readouterr().out

    assert main(["rm", "default"]) == 0
    assert "removed box" in capsys.readouterr().out
    assert not os.path.exists(repo / ".temenos" / "default")


@gvisor
def test_exec_unknown_box_errors(project_env, capsys):
    assert main(["exec", "ghost", "--", "true"]) == 1
    assert "no such box" in capsys.readouterr().err
