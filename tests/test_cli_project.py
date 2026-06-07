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


@gvisor
def test_exec_interactive_attach_non_tty(project_env, capfd):
    """`exec -it` with no controlling terminal inherits fds directly — proves the daemon
    attach-context endpoint + local `runsc exec` reconstruction work end to end."""
    assert main(["create", "default"]) == 0
    capfd.readouterr()
    assert main(["exec", "-it", "default", "--", "python3", "-c", "print(6*7)"]) == 0
    assert "42" in capfd.readouterr().out


@gvisor
def test_shell_drives_real_repl_over_pty(project_env, monkeypatch):
    """A genuine interactive REPL: drive `python3` over a PTY and confirm it evaluates
    input instead of exiting on EOF (the bug this fixes)."""
    import os
    import pty
    import select
    import threading
    import time

    from temenos import cli

    assert main(["create", "default"]) == 0

    master, slave = pty.openpty()

    class _FakeStd:
        def __init__(self, fd: int) -> None:
            self._fd = fd

        def fileno(self) -> int:
            return self._fd

    monkeypatch.setattr(cli.sys, "stdin", _FakeStd(slave))
    monkeypatch.setattr(cli.sys, "stdout", _FakeStd(slave))

    def feed() -> None:
        time.sleep(2.0)                       # let the REPL come up inside the box
        os.write(master, b"print(6 * 7)\n")
        time.sleep(1.0)
        os.write(master, b"exit()\n")

    t = threading.Thread(target=feed, daemon=True)
    t.start()
    rc = cli._interactive_exec(_box_attach_ctx("default"), ["python3"])
    os.close(slave)

    out = b""
    while True:
        ready, _, _ = select.select([master], [], [], 0.5)
        if not ready:
            break
        try:
            chunk = os.read(master, 65536)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    os.close(master)
    assert b"42" in out
    assert rc == 0


def _box_attach_ctx(name: str) -> dict:
    from temenos.cli import _resolve_scoped
    from temenos.server.client import connect_or_spawn

    r = _resolve_scoped(name, glob=False)
    client = connect_or_spawn()
    from temenos.manager import box_id
    return client.attach_context(box_id(r.data_dir))


@gvisor
def test_claude_dry_run_wires_mcp_and_bans_natives(project_env, capsys):
    import json
    repo = project_env
    assert main(["claude", "--box", "default", "--dry-run", "--", "--model", "opus"]) == 0
    out = capsys.readouterr().out

    cfg_path = repo / ".temenos" / "default" / "mcp.json"
    assert cfg_path.exists()
    cfg = json.loads(cfg_path.read_text())
    srv = cfg["mcpServers"]["temenos"]
    assert srv["type"] == "http" and srv["url"].endswith("/mcp/" + out.split("id=")[1].split()[0])
    assert srv["headers"]["Authorization"].startswith("Bearer ")

    # the launch line: strict config, natives denied, only temenos tools allowed
    assert "--strict-mcp-config" in out
    assert "--disallowedTools" in out and "Bash" in out and "WebFetch" in out
    assert "--allowedTools" in out and "mcp__temenos__exec" in out
    assert "--model opus" in out                      # user args forwarded after `--`
