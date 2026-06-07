"""Storage providers — pluggable backings for a box's volumes.

Unifying idea: gVisor can only mount host paths (or tmpfs), so every provider resolves
to ONE mount inside the box. They differ in what backs it and what commit/cleanup mean:

  MemoryVolume  — tmpfs (RAM). Ephemeral, dies with the box. For small/scratch tasks.
  DiskVolume    — a host directory you specify. Durable, live to disk. Contained: the
                  box's mount namespace stops in-box `..` from escaping, and we realpath
                  the host dir (and, multi-tenant, check it's under an allowed root).
  FsspecVolume  — (post-v1) maps to s3/azure/… via fsspec. Sync model: materialize the
                  remote prefix to a local dir on prepare(), upload on commit(). Remote
                  I/O runs on the HOST, so the box itself stays network-less.

Providers are plain Python classes implementing `StorageProvider`, so users can add
their own — that is the "pythonic" extension point.
"""
from __future__ import annotations

import os
import posixpath
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .exceptions import StorageError


class StorageProvider(ABC):
    kind: str = "abstract"

    @abstractmethod
    def oci_mount(self, target: str, mode: str) -> dict:
        """The OCI mount entry that exposes this volume at `target` (mode 'ro'|'rw')."""

    def prepare(self, box_id: str) -> None:
        """Set up the backing before the box starts (mkdir, download, …). Default no-op."""

    def commit(self, box_id: str) -> None:
        """Persist box changes (upload for fsspec). Live providers (disk) are no-ops."""

    def cleanup(self, box_id: str) -> None:
        """Release backing resources when the box is deleted. Default no-op."""

    def to_dict(self) -> dict:
        raise NotImplementedError

    @staticmethod
    def from_dict(data: dict) -> "StorageProvider":
        d = dict(data)
        kind = d.pop("kind", None)
        cls = _PROVIDERS.get(kind)
        if cls is None:
            raise StorageError(f"unknown storage provider: {kind!r}")
        return cls(**d)


@dataclass(frozen=True)
class MemoryVolume(StorageProvider):
    """Ephemeral tmpfs at the mount point. Counts against the box's memory limit."""

    size_mb: int | None = None
    kind = "memory"

    def oci_mount(self, target: str, mode: str) -> dict:
        opts = ["nosuid", "nodev", "mode=1777"]
        if self.size_mb:
            opts.append(f"size={self.size_mb}m")
        return {"destination": target, "type": "tmpfs", "source": "tmpfs", "options": opts}

    def to_dict(self) -> dict:
        return {"kind": "memory", "size_mb": self.size_mb}


@dataclass(frozen=True)
class DiskVolume(StorageProvider):
    """A host directory bound into the box. Durable; writes go straight to disk.

    `host_dir` is realpath-resolved. `allowed_root`, if set, is enforced: the resolved
    path must live under it (the multi-tenant containment knob — a tenant can't point a
    volume at `../other-tenant`). `create=True` makes the dir if missing (for rw output).
    """

    host_dir: str
    create: bool = True
    allowed_root: str | None = None
    kind = "disk"

    def resolved(self) -> str:
        return os.path.realpath(self.host_dir)

    def prepare(self, box_id: str) -> None:
        path = self.resolved()
        if self.allowed_root is not None:
            root = os.path.realpath(self.allowed_root)
            if path != root and not path.startswith(root.rstrip("/") + "/"):
                raise StorageError(
                    f"disk volume {self.host_dir!r} resolves outside allowed root {root!r}"
                )
        if self.create:
            os.makedirs(path, exist_ok=True)
        if not os.path.isdir(path):
            raise StorageError(f"disk volume backing dir does not exist: {path!r}")

    def oci_mount(self, target: str, mode: str) -> dict:
        return {"destination": target, "source": self.resolved(), "type": "bind",
                "options": ["rbind", "ro" if mode == "ro" else "rw"]}

    def to_dict(self) -> dict:
        return {"kind": "disk", "host_dir": self.host_dir,
                "create": self.create, "allowed_root": self.allowed_root}


@dataclass(frozen=True)
class Mount:
    """A storage provider exposed at a box path."""

    target: str
    provider: StorageProvider
    mode: str = "rw"      # "ro" | "rw"

    def __post_init__(self) -> None:
        if self.mode not in ("ro", "rw"):
            raise ValueError(f"mount mode must be 'ro' or 'rw', got {self.mode!r}")
        if not self.target.startswith("/"):
            raise ValueError(f"mount target must be absolute: {self.target!r}")
        if posixpath.normpath(self.target) != self.target or ".." in self.target.split("/"):
            raise ValueError(f"mount target must be normalized (no '..'): {self.target!r}")
        if self.target == "/":
            raise ValueError("mount target cannot be '/'")

    def oci_mount(self) -> dict:
        return self.provider.oci_mount(self.target, self.mode)

    def to_dict(self) -> dict:
        return {"target": self.target, "mode": self.mode, "provider": self.provider.to_dict()}

    @staticmethod
    def from_dict(data: dict) -> "Mount":
        return Mount(
            target=data["target"],
            provider=StorageProvider.from_dict(data["provider"]),
            mode=data.get("mode", "rw"),
        )


# Registry for (de)serialization. FsspecVolume registers here when added (post-v1).
_PROVIDERS: dict[str, type[StorageProvider]] = {
    "memory": MemoryVolume,
    "disk": DiskVolume,
}
