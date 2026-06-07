"""Phase 2 — box images: build a runner-owned base and verify writable /usr."""
from __future__ import annotations

import os

import pytest

from temenos import Box, Policy, build_minimal, list_images
from temenos.backends.gvisor import GVisorBackend
from temenos.image import Image, _default_data_dir

gvisor = pytest.mark.skipif(
    not GVisorBackend.is_available(),
    reason="gVisor (runsc) with a usable platform not available",
)


# -- pure-Python: building a minimal image (no runsc) ---------------------------------

def test_build_minimal_produces_runner_owned_rootfs(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMENOS_DATA", str(tmp_path))
    img = build_minimal("mini")
    assert img.exists()
    sh = os.path.join(img.rootfs, "usr/bin/sh")
    assert os.path.lexists(sh)                                  # dash + sh symlink
    # files are owned by the test runner (the whole point — box-root can write them)
    assert os.stat(img.rootfs).st_uid == os.getuid()
    assert "mini" in list_images()


def test_default_data_dir_honours_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMENOS_DATA", str(tmp_path))
    assert _default_data_dir() == str(tmp_path)


def test_policy_image_round_trips():
    p = Policy(image="ubuntu-base")
    assert Policy.from_dict(p.to_dict()).image == "ubuntu-base"


# -- integration: a box booted from the image has a WRITABLE /usr ---------------------

@gvisor
def test_image_box_has_writable_usr(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMENOS_DATA", str(tmp_path))
    build_minimal("mini")
    with Box("img-box", Policy(image="mini")) as box:
        # the headline payoff: /usr is writable in an image box (read-only in host-bind base)
        r = box.exec(["/bin/sh", "-c", "echo hi > /usr/lib/NEWF && cat /usr/lib/NEWF"])
        assert r.ok and r.stdout.strip() == "hi", (r.stdout, r.stderr)


@gvisor
def test_image_writes_are_ephemeral_not_on_host(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMENOS_DATA", str(tmp_path))
    img = build_minimal("mini2")
    with Box("img-box2", Policy(image="mini2")) as box:
        box.exec(["/bin/sh", "-c", "echo MODIFIED > /usr/lib/marker"])
    # the image (overlay lower) must be untouched — writes went to the ephemeral upper
    assert not os.path.exists(os.path.join(img.rootfs, "usr/lib/marker"))


@gvisor
def test_missing_image_errors():
    from temenos.exceptions import TemenosError
    with pytest.raises(TemenosError, match="image not found"):
        Box("noimg", Policy(image="does-not-exist")).start()
