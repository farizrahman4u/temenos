"""Connect-or-spawn client for the single per-user daemon.

There is exactly one daemon per user, discovered via a daemon-info file
(`$TEMENOS_HOME/daemon.json` = {url, token, pid}). `connect_or_spawn()` returns a client
to the running daemon, spawning one (under a flock, so concurrent CLI calls don't race
two daemons) if none is up. All temenos processes attach to this one daemon (D15).
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time

import httpx

from ..exceptions import TemenosError

DEFAULT_PORT = int(os.environ.get("TEMENOS_PORT", "8839"))


def daemon_home() -> str:
    """Per-user runtime dir for the daemon's token/lock/pid. Deliberately NOT `~/.temenos`
    — that name belongs to the project-local marker (`<repo>/.temenos/`); colliding them
    would make `temenos` in $HOME mistake the global dir for a project. Uses
    `$XDG_RUNTIME_DIR/temenos` (transient, 0700), falling back to `$XDG_CACHE_HOME`.
    `$TEMENOS_HOME` overrides (tests, custom setups)."""
    override = os.environ.get("TEMENOS_HOME")
    if override:
        return override
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get(
        "XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    return os.path.join(base, "temenos")


def info_path() -> str:
    return os.path.join(daemon_home(), "daemon.json")


def read_info() -> dict | None:
    try:
        with open(info_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class DaemonClient:
    def __init__(self, url: str, token: str) -> None:
        self._c = httpx.Client(base_url=url, timeout=300.0,
                               headers={"Authorization": f"Bearer {token}"})

    def healthz(self) -> bool:
        try:
            return self._c.get("/healthz", timeout=2.0).status_code == 200
        except httpx.HTTPError:
            return False

    def _json(self, r: httpx.Response) -> dict:
        if r.status_code >= 400:
            detail = r.json().get("detail", r.text) if r.content else r.reason_phrase
            raise TemenosError(f"daemon error ({r.status_code}): {detail}")
        return r.json()

    def create_box(self, data_dir: str, policy: dict, *, name: str | None = None,
                   restore_from: str | None = None, cwd: str | None = None) -> dict:
        return self._json(self._c.post("/v1/boxes", json={
            "data_dir": data_dir, "policy": policy, "name": name,
            "restore_from": restore_from, "cwd": cwd}))

    def list_boxes(self) -> list[dict]:
        return self._json(self._c.get("/v1/boxes"))  # type: ignore[return-value]

    def get_box(self, bid: str) -> dict:
        return self._json(self._c.get(f"/v1/boxes/{bid}"))

    def delete_box(self, bid: str) -> dict:
        return self._json(self._c.delete(f"/v1/boxes/{bid}"))

    def exec(self, bid: str, cmd: list[str], *, cwd: str | None = None,
             timeout: float | None = None, stdin: str | None = None) -> dict:
        return self._json(self._c.post(f"/v1/boxes/{bid}/exec", json={
            "cmd": cmd, "cwd": cwd, "timeout": timeout, "stdin": stdin}))

    def attach_context(self, bid: str) -> dict:
        return self._json(self._c.get(f"/v1/boxes/{bid}/attach"))

    def audit(self, bid: str) -> list[dict]:
        return self._json(self._c.get(f"/v1/boxes/{bid}/audit"))  # type: ignore[return-value]

    def writes(self, bid: str) -> list[str]:
        return self._json(self._c.get(f"/v1/boxes/{bid}/writes")).get("writes", [])


def connect() -> DaemonClient | None:
    """Return a client to a running daemon, or None if none is reachable."""
    info = read_info()
    if not info:
        return None
    c = DaemonClient(info["url"], info["token"])
    return c if c.healthz() else None


def connect_or_spawn(port: int = DEFAULT_PORT, wait_s: float = 15.0) -> DaemonClient:
    c = connect()
    if c is not None:
        return c
    os.makedirs(daemon_home(), mode=0o700, exist_ok=True)
    with open(os.path.join(daemon_home(), "daemon.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)        # only one spawner at a time
        c = connect()                            # double-check: someone may have started it
        if c is not None:
            return c
        subprocess.Popen([sys.executable, "-m", "temenos.cli", "serve", "--port", str(port)],
                         start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            c = connect()
            if c is not None:
                return c
            time.sleep(0.2)
        raise TemenosError("temenos daemon failed to start (see `temenos serve`)")
