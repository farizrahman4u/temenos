"""Phase 2 — storage providers (pure Python; no runsc)."""
from __future__ import annotations

import pytest

from temenos import DiskVolume, MemoryVolume, Mount, Policy, StorageError
from temenos.storage import StorageProvider


# -- Mount validation -----------------------------------------------------------------

def test_mount_rejects_relative_target():
    with pytest.raises(ValueError):
        Mount("work", MemoryVolume())

def test_mount_rejects_dotdot_target():
    with pytest.raises(ValueError):
        Mount("/a/../b", MemoryVolume())

def test_mount_rejects_root_target():
    with pytest.raises(ValueError):
        Mount("/", MemoryVolume())

def test_mount_rejects_bad_mode():
    with pytest.raises(ValueError):
        Mount("/x", MemoryVolume(), mode="rwx")


# -- provider OCI shape ---------------------------------------------------------------

def test_memory_volume_oci_is_tmpfs():
    m = MemoryVolume(size_mb=128).oci_mount("/scratch", "rw")
    assert m["type"] == "tmpfs"
    assert "size=128m" in m["options"]

def test_disk_volume_oci_is_bind(tmp_path):
    m = DiskVolume(str(tmp_path)).oci_mount("/data", "ro")
    assert m["type"] == "bind"
    assert m["source"] == str(tmp_path.resolve())
    assert "ro" in m["options"]


# -- disk containment -----------------------------------------------------------------

def test_disk_volume_allowed_root_blocks_escape(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    vol = DiskVolume(str(tmp_path), allowed_root=str(sub))   # parent is outside sub
    with pytest.raises(StorageError, match="outside allowed root"):
        vol.prepare("box")

def test_disk_volume_allowed_root_permits_inside(tmp_path):
    sub = tmp_path / "sub"
    DiskVolume(str(sub), allowed_root=str(tmp_path)).prepare("box")  # creates sub, ok
    assert sub.is_dir()


# -- (de)serialization ----------------------------------------------------------------

def test_provider_round_trip():
    for prov in (MemoryVolume(size_mb=64), DiskVolume("/tmp/x", create=False)):
        assert StorageProvider.from_dict(prov.to_dict()).to_dict() == prov.to_dict()

def test_unknown_provider_kind_raises():
    with pytest.raises(StorageError):
        StorageProvider.from_dict({"kind": "quantum"})

def test_policy_mounts_round_trip():
    pol = Policy(
        read=["/proj"],
        mounts=[Mount("/scratch", MemoryVolume(size_mb=64)),
                Mount("/data", DiskVolume("/tmp/d"), mode="ro")],
    )
    back = Policy.from_dict(pol.to_dict())
    assert back == pol

def test_policy_rejects_non_mount_in_mounts():
    with pytest.raises(TypeError):
        Policy(mounts=["/not-a-mount"])  # type: ignore[list-item]
