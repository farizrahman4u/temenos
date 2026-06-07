"""Policy — the semantic layer (Layer 0, pure data, no OS calls).

A ``Policy`` describes what code executed inside a box may do. It is **frozen**
(immutable, hashable) and **secure by default**: ``Policy()`` grants no network, no host
writes (overlay only), and tight limits. You opt *in* to capability.

``restrict()`` is the only way to derive a child policy, and it can never widen a
capability — escalation raises ``PolicyViolation`` rather than being an operation.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass
from enum import IntEnum

from .exceptions import PolicyViolation
from .storage import Mount


class TrustLevel(IntEnum):
    """How much we trust the *code* being run (not the agent). Higher = more capable.

    In v1 the backend is always gVisor; TrustLevel gates *policy strictness*, and
    ``HOST`` is the explicit no-sandbox escape hatch.
    """

    UNTRUSTED = 0   # tightest: no network, read-only root, tight limits (default)
    RESTRICTED = 1  # explicit network allowlist permitted
    SANDBOXED = 2   # looser limits / broader mounts
    HOST = 3        # no sandbox — must be explicit


def _coerce_trust(value: "TrustLevel | int | str") -> TrustLevel:
    if isinstance(value, TrustLevel):
        return value
    if isinstance(value, bool):  # bool is an int subclass — reject to avoid surprises
        raise ValueError(f"invalid trust level: {value!r}")
    if isinstance(value, int):
        return TrustLevel(value)
    if isinstance(value, str):
        try:
            return TrustLevel[value.strip().upper()]
        except KeyError:
            raise ValueError(f"unknown trust level: {value!r}") from None
    raise ValueError(f"invalid trust level: {value!r}")


_SET_FIELDS = ("read", "write", "network")
_INT_FIELDS = ("max_memory_mb", "max_cpu_seconds", "max_processes", "max_output_bytes")
_ALL_FIELDS = _SET_FIELDS + _INT_FIELDS + ("trust",)


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

    # Network — default deny. Entries are "host" or "host:port". (v1: non-empty network
    # requires the post-v1 egress filter; an empty tuple = no network.)
    network: tuple[str, ...] = ()

    # Resource limits (enforced per-box via the systemd scope; see plan §9/D6).
    max_memory_mb: int = 256
    max_cpu_seconds: int = 30
    max_processes: int = 16
    max_output_bytes: int = 10 * 1024 * 1024  # 10 MiB

    trust: TrustLevel = TrustLevel.UNTRUSTED

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
        object.__setattr__(self, "trust", _coerce_trust(self.trust))
        for f in _INT_FIELDS:
            v = getattr(self, f)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise ValueError(f"{f} must be a non-negative int, got {v!r}")

    # -- deriving child policies ------------------------------------------------------

    def restrict(self, **changes: object) -> "Policy":
        """Return a new Policy no more capable than self — the only way to derive a child.

        - set fields (read/write/network): each new value must be a **subset** of self's
        - int fields: each new value must be **<=** self's
        - trust: must be **<=** self.trust

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
            else:  # trust
                nt = _coerce_trust(value)  # type: ignore[arg-type]
                if nt > self.trust:
                    raise PolicyViolation(
                        f"restrict() cannot raise trust: {nt.name} > {self.trust.name}"
                    )
                merged[field_name] = nt
        # mounts are inherited unchanged (v1: restrict narrows the simple capabilities;
        # provider-backed volumes are not subset-narrowed — see plan).
        merged["mounts"] = self.mounts
        return Policy(**merged)  # type: ignore[arg-type]

    # -- plain-data round trip (shared by REST/MCP/CLI/config) -----------------------

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Policy":
        unknown = set(data) - set(_ALL_FIELDS) - {"mounts"}
        if unknown:
            raise ValueError(f"unknown policy field(s): {sorted(unknown)}")
        kwargs: dict[str, object] = {}
        for f in _SET_FIELDS:
            if f in data:
                kwargs[f] = tuple(data[f])  # type: ignore[arg-type]
        for f in _INT_FIELDS:
            if f in data:
                kwargs[f] = int(data[f])  # type: ignore[call-overload]
        if "trust" in data:
            kwargs["trust"] = _coerce_trust(data["trust"])  # type: ignore[arg-type]
        if "mounts" in data:
            kwargs["mounts"] = tuple(Mount.from_dict(m) for m in data["mounts"])  # type: ignore[union-attr]
        return cls(**kwargs)  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, object]:
        return {
            "read": list(self.read),
            "write": list(self.write),
            "network": list(self.network),
            "mounts": [m.to_dict() for m in self.mounts],
            "max_memory_mb": self.max_memory_mb,
            "max_cpu_seconds": self.max_cpu_seconds,
            "max_processes": self.max_processes,
            "max_output_bytes": self.max_output_bytes,
            "trust": self.trust.name,
        }

    # -- semantic checks (for validation/audit; gVisor mounts are the real enforcer) --

    def allows_path_read(self, path: str) -> bool:
        """True if ``path`` is under a read *or* write mount (writable implies readable)."""
        return self._under_any(path, self.read) or self._under_any(path, self.write)

    def allows_path_write(self, path: str) -> bool:
        return self._under_any(path, self.write)

    def allows_host(self, host: str, port: int | None = None) -> bool:
        for entry in self.network:
            ehost, sep, eport = entry.partition(":")
            if ehost != host:
                continue
            if not sep:               # bare host -> any port
                return True
            if port is None:          # asking about the host in general
                return True
            if str(port) == eport:
                return True
        return False

    @staticmethod
    def _under_any(path: str, bases: tuple[str, ...]) -> bool:
        p = posixpath.normpath(path)
        for base in bases:
            nb = posixpath.normpath(base)
            if p == nb or p.startswith(nb.rstrip("/") + "/"):
                return True
        return False
