"""Policy — the semantic layer (Layer 0, pure data, no OS calls).

A ``Policy`` describes what code executed inside a box may do. It is **frozen**
(immutable, hashable). The *filesystem* is locked down by default — ``Policy()`` grants no
host writes (overlay only) and tight resource limits — but **network defaults to full host
passthrough** in v1 (a deliberate convenience default; set ``network=False`` to isolate a
box, and do so for adversarial/multi-tenant workloads). You opt *in* to broader filesystem
and resource capability.

``restrict()`` is the only way to derive a child policy, and it can never widen a
capability — escalation raises ``PolicyViolation`` rather than being an operation.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass

from .exceptions import PolicyViolation
from .storage import Mount


def _coerce_network(value: "bool | str") -> bool:
    """v1 network is a simple toggle: off (False/'none') or full passthrough
    (True/'host'). No firewalling — filtered allowlists are post-v1."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("host", "on", "true", "yes"):
            return True
        if s in ("none", "off", "false", "no", ""):
            return False
        raise ValueError(f"invalid network mode: {value!r} (use bool or 'host'/'none')")
    raise ValueError(f"network must be bool or 'host'/'none', got {type(value).__name__}")


_SET_FIELDS = ("read", "write")
_INT_FIELDS = ("max_memory_mb", "max_cpu_seconds", "max_processes", "max_output_bytes")
_ALL_FIELDS = _SET_FIELDS + _INT_FIELDS + ("network",)


@dataclass(frozen=True)
class Policy:
    # Filesystem — HOST paths made visible inside the box, at the same path.
    # These are sugar for DiskVolume mounts: `read` = read-only disk bind, `write` =
    # durable read-write disk bind (writes persist to the host dir). For ephemeral
    # scratch use a MemoryVolume mount (or /tmp); for remapped/remote storage use `mounts`.
    read: tuple[str, ...] = ()
    write: tuple[str, ...] = ()

    # Explicit provider-backed volumes (memory / disk / fsspec / custom) at chosen paths.
    mounts: tuple[Mount, ...] = ()

    # Network — simple toggle (v1): True = full host passthrough (no firewalling, the
    # default), False = no network (isolated netns). Filtered allowlists are post-v1.
    # ⚠️ The True default gives every box full host network reach (localhost, LAN, cloud
    # metadata, arbitrary egress). Set network=False for adversarial/multi-tenant boxes;
    # filtered egress is post-v1.
    network: bool = True

    # Box base image (a runner-owned rootfs under $TEMENOS_DATA/images/<name>). None =
    # default host-`/usr`-bind base (read-only system). An image gives a writable system
    # (pip/apt/npm) — see image.py.
    image: str | None = None

    # Root-overlay scratch medium: "disk" (default — disk-backed, **checkpointable**,
    # not RAM-bound) or "memory" (RAM — fast, but RAM-bound AND **cannot be
    # checkpointed**; opt-in, backend warns).
    scratch: str = "disk"

    # Filesystem persistence (D17): "auto" (background checkpoint loop + on close, the
    # default), "on-close" (commit only when the box closes — loop off), "off"
    # (--ephemeral-fs: never checkpoint, throwaway fs). The box dir's checkpoint is also
    # what a box restores from on next use. (memory scratch can't checkpoint → treated as off.)
    checkpoint: str = "auto"

    # Resource limits (enforced per-box via the systemd scope; see plan §9/D6).
    max_memory_mb: int = 256
    max_cpu_seconds: int = 30
    max_processes: int = 16
    max_output_bytes: int = 10 * 1024 * 1024  # 10 MiB

    def __post_init__(self) -> None:
        # Ergonomic API: accept lists; store frozen tuples (deduped order-preserving).
        for f in _SET_FIELDS:
            value = getattr(self, f)
            if isinstance(value, (str, bytes)):
                raise TypeError(f"{f} must be a sequence of strings, not {type(value).__name__}")
            object.__setattr__(self, f, tuple(dict.fromkeys(value)))
        object.__setattr__(self, "mounts", tuple(self.mounts))
        for m in self.mounts:
            if not isinstance(m, Mount):
                raise TypeError(f"mounts must contain Mount instances, got {type(m).__name__}")
        object.__setattr__(self, "network", _coerce_network(self.network))
        if self.image is not None and not isinstance(self.image, str):
            raise TypeError(f"image must be a str name or None, got {type(self.image).__name__}")
        if self.scratch not in ("disk", "memory"):
            raise ValueError(f"scratch must be 'disk' or 'memory', got {self.scratch!r}")
        if self.checkpoint not in ("auto", "on-close", "off"):
            raise ValueError(f"checkpoint must be 'auto'|'on-close'|'off', got {self.checkpoint!r}")
        for f in _INT_FIELDS:
            v = getattr(self, f)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise ValueError(f"{f} must be a non-negative int, got {v!r}")

    # -- deriving child policies ------------------------------------------------------

    def restrict(self, **changes: object) -> "Policy":
        """Return a new Policy no more capable than self — the only way to derive a child.

        - set fields (read/write/network): each new value must be a **subset** of self's
        - int fields: each new value must be **<=** self's

        Any widening raises ``PolicyViolation``. Fields not passed are inherited. There is
        no ``escalate()`` — widening is an error, not an operation.
        """
        unknown = set(changes) - set(_ALL_FIELDS)
        if unknown:
            raise TypeError(f"restrict() got unexpected field(s): {sorted(unknown)}")

        merged: dict[str, object] = {f: getattr(self, f) for f in _ALL_FIELDS}
        for field_name, value in changes.items():
            if field_name in _SET_FIELDS:
                if isinstance(value, (str, bytes)):
                    raise TypeError(f"{field_name} must be a sequence of strings")
                new = tuple(dict.fromkeys(value))  # type: ignore[arg-type]
                extra = set(new) - set(getattr(self, field_name))
                if extra:
                    raise PolicyViolation(
                        f"restrict() cannot widen {field_name}: {sorted(extra)} not in parent"
                    )
                merged[field_name] = new
            elif field_name in _INT_FIELDS:
                iv = int(value)  # type: ignore[call-overload]
                if iv > getattr(self, field_name):
                    raise PolicyViolation(
                        f"restrict() cannot raise {field_name}: {iv} > {getattr(self, field_name)}"
                    )
                merged[field_name] = iv
            else:  # network
                nb = _coerce_network(value)  # type: ignore[arg-type]
                if nb and not self.network:
                    raise PolicyViolation("restrict() cannot enable network (parent has none)")
                merged[field_name] = nb
        # mounts and image are inherited unchanged (restrict narrows simple capabilities;
        # provider volumes and the base image are not subset-narrowed — see plan).
        merged["mounts"] = self.mounts
        merged["image"] = self.image
        merged["scratch"] = self.scratch
        merged["checkpoint"] = self.checkpoint
        return Policy(**merged)  # type: ignore[arg-type]

    # -- plain-data round trip (shared by REST/MCP/CLI/config) -----------------------

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Policy":
        unknown = set(data) - set(_ALL_FIELDS) - {"mounts", "image", "scratch", "checkpoint"}
        if unknown:
            raise ValueError(f"unknown policy field(s): {sorted(unknown)}")
        kwargs: dict[str, object] = {}
        for f in _SET_FIELDS:
            if f in data:
                kwargs[f] = tuple(data[f])  # type: ignore[arg-type]
        for f in _INT_FIELDS:
            if f in data:
                kwargs[f] = int(data[f])  # type: ignore[call-overload]
        if "network" in data:
            kwargs["network"] = _coerce_network(data["network"])  # type: ignore[arg-type]
        if "mounts" in data:
            kwargs["mounts"] = tuple(Mount.from_dict(m) for m in data["mounts"])  # type: ignore[union-attr]
        if "image" in data:
            kwargs["image"] = data["image"]
        if "scratch" in data:
            kwargs["scratch"] = data["scratch"]
        if "checkpoint" in data:
            kwargs["checkpoint"] = data["checkpoint"]
        return cls(**kwargs)  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, object]:
        return {
            "read": list(self.read),
            "write": list(self.write),
            "network": self.network,
            "mounts": [m.to_dict() for m in self.mounts],
            "image": self.image,
            "scratch": self.scratch,
            "checkpoint": self.checkpoint,
            "max_memory_mb": self.max_memory_mb,
            "max_cpu_seconds": self.max_cpu_seconds,
            "max_processes": self.max_processes,
            "max_output_bytes": self.max_output_bytes,
        }

    # -- semantic checks (for validation/audit; gVisor mounts are the real enforcer) --

    def allows_path_read(self, path: str) -> bool:
        """True if ``path`` is under a read *or* write mount (writable implies readable)."""
        return self._under_any(path, self.read) or self._under_any(path, self.write)

    def allows_path_write(self, path: str) -> bool:
        return self._under_any(path, self.write)

    @staticmethod
    def _under_any(path: str, bases: tuple[str, ...]) -> bool:
        p = posixpath.normpath(path)
        for base in bases:
            nb = posixpath.normpath(base)
            if p == nb or p.startswith(nb.rstrip("/") + "/"):
                return True
        return False
