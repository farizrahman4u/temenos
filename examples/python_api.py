#!/usr/bin/env python3
"""temenos Python API — the core surface the CLI and MCP layers wrap.

Run on a host with gVisor (`runsc`) installed:
    PYTHONPATH=. python examples/python_api.py
"""
from __future__ import annotations

import tempfile

from temenos import Box, Policy, PolicyViolation


def main() -> None:
    # A scratch host dir the box may write to (writes persist to disk here).
    out = tempfile.mkdtemp(prefix="temenos-demo-")

    # Filesystem locked by default (no host writes); network is ON by default, so we pass
    # network=False here to isolate the box. We grant exactly one writable dir.
    policy = Policy(network=False, write=[out], max_memory_mb=256)

    with Box("demo", policy) as box:
        # 1. Run code in the sandbox.
        r = box.exec(["python3", "-c", "print(6 * 7)"])
        print("exec:", r.stdout.strip(), "exit", r.exit_code)   # -> 42 exit 0

        # 2. Write + read through the box (writes land on the host dir we granted).
        box.write_file(f"{out}/hello.txt", "from inside the box\n")
        print("read:", box.read_file(f"{out}/hello.txt").strip())

        # 3. Containment: the host filesystem is invisible beyond policy.
        denied = box.exec(["cat", "/etc/shadow"])
        print("read /etc/shadow ok?", denied.ok)                # -> False

        # 4. Network is off — this connection has nowhere to go.
        net = box.exec(["python3", "-c",
                        "import socket; socket.setdefaulttimeout(3);"
                        "socket.create_connection(('1.1.1.1', 53))"])
        print("network reachable?", net.ok)                     # -> False

    # 5. Policies only narrow — escalation is an error, not an operation.
    try:
        policy.restrict(network=True)
    except PolicyViolation as e:
        print("restrict cannot widen:", e)


if __name__ == "__main__":
    main()
