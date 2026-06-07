"""Integration: mmdebstrap actually builds an apt-capable box.

Opt-in — needs gVisor + mmdebstrap + network (downloads ~90 MB), so it's gated behind
`TEMENOS_NET_TESTS=1` and stays out of the default suite. Run with:
    TEMENOS_NET_TESTS=1 PYTHONPATH=. pytest tests/test_image_mmdebstrap.py -v
"""
from __future__ import annotations

import os
import shutil

import pytest

from temenos import Box, Policy
from temenos.backends.gvisor import GVisorBackend
from temenos.image import build_mmdebstrap

pytestmark = pytest.mark.skipif(
    not (GVisorBackend.is_available() and shutil.which("mmdebstrap")
         and os.environ.get("TEMENOS_NET_TESTS")),
    reason="needs gVisor + mmdebstrap + TEMENOS_NET_TESTS=1 (network, ~90MB)",
)


def test_mmdebstrap_builds_apt_capable_box(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMENOS_DATA", str(tmp_path))
    img = build_mmdebstrap("itest")                       # host distro, variant=apt
    assert os.path.exists(os.path.join(img.rootfs, "usr/bin/apt-get"))
    with Box("itest-box", Policy(image="itest")) as box:  # boots from the built image
        assert box.exec(["apt-get", "--version"]).ok      # apt runs in the box
