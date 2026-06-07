"""Phase 2 — `temenos image` CLI (pure Python; minimal builder, no runsc)."""
from __future__ import annotations

import pytest

from temenos import image
from temenos.cli import main


def test_image_build_minimal_ls_rm(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TEMENOS_DATA", str(tmp_path))
    assert main(["image", "build", "mini", "--from", "minimal"]) == 0
    assert image.Image("mini").exists()

    assert main(["image", "ls"]) == 0
    assert "mini" in capsys.readouterr().out

    assert main(["image", "rm", "mini"]) == 0
    assert not image.Image("mini").exists()


def test_image_rm_missing_returns_error(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMENOS_DATA", str(tmp_path))
    assert main(["image", "rm", "ghost"]) == 1


def test_build_dispatch_unknown_builder(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMENOS_DATA", str(tmp_path))
    with pytest.raises(Exception, match="unknown image builder"):
        image.build("x", builder="nope")


def test_mmdebstrap_gated_when_absent(tmp_path, monkeypatch):
    import shutil
    if shutil.which("mmdebstrap"):
        pytest.skip("mmdebstrap present; gating test only meaningful when absent")
    monkeypatch.setenv("TEMENOS_DATA", str(tmp_path))
    # CLI surfaces the clear 'not installed' error as exit 1, not a traceback
    assert main(["image", "build", "deb", "--from", "mmdebstrap"]) == 1
