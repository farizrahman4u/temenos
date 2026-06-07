# Python API

The core library — zero runtime deps, pure-Python policy/data, synchronous `Box`. The CLI
and MCP server are thin layers over exactly this, so anything they do you can do directly.

```python
from temenos import Box, Policy

with Box("demo", Policy(write=["/tmp/out"], network=False)) as box:
    box.write_file("/tmp/out/run.py", "print(6 * 7)\n")
    r = box.exec(["python3", "/tmp/out/run.py"])
    print(r.stdout, r.ok)        # "42\n" True
```

## Policy

Frozen, hashable, plain data describing what code in a box may do. **Secure-by-default for
the filesystem** (no host writes, read-only system, tight limits); **network is on by
default** in v1 — pass `network=False` to isolate.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `read` | `tuple[str,…]` | `()` | host paths bound **read-only** at the same path |
| `write` | `tuple[str,…]` | `()` | host paths bound **read-write** (writes persist to host) |
| `mounts` | `tuple[Mount,…]` | `()` | explicit provider volumes (see [below](#storage-providers)) |
| `network` | `bool` | `True` | `True` = full host passthrough; `False` = isolated netns |
| `image` | `str \| None` | `None` | base [image](images.md) name; `None` = host `/usr` bind (read-only) |
| `scratch` | `"disk"\|"memory"` | `"disk"` | root-overlay medium (`disk` is checkpointable) |
| `checkpoint` | `"auto"\|"on-close"\|"off"` | `"auto"` | durability mode |
| `max_memory_mb` | `int` | `256` | memory cap (enforced via systemd scope) |
| `max_cpu_seconds` | `int` | `30` | CPU-time cap (`RLIMIT_CPU`) |
| `max_processes` | `int` | `16` | process cap (`RLIMIT_NPROC`) |
| `max_output_bytes` | `int` | `10 MiB` | exec output truncation threshold |

Construction accepts lists (coerced to deduped tuples) and `network` as `bool` or
`"host"`/`"none"`. A bare string for `read`/`write` is rejected (almost never intended).

### Methods

```python
child = policy.restrict(read=["/a"], network=False, max_memory_mb=128)
```
- **`restrict(**changes) -> Policy`** — derive a child that is *no more capable*. Set fields
  must be subsets, ints must be `<=`, and `network` can only go `True→False`. Any widening
  raises `PolicyViolation`. `mounts`/`image`/`scratch`/`checkpoint` are inherited. There is
  **no** `escalate()`.
- **`to_dict()` / `from_dict(d)`** — round-trip plain data (used by REST/MCP/CLI/config). The
  box's `config.json` is exactly `to_dict()`.
- **`allows_path_read(p)` / `allows_path_write(p)`** — semantic checks (write implies read).
  These are for validation/audit; the gVisor mounts are the real enforcer.

## Box

A started sandbox handle. Use it as a context manager (`with`) or call `start()`/`close()`.

```python
box = Box(name, policy, *, backend=None, tenant=None, env=None, restore_from=None)
```
The default backend is gVisor. `restore_from=<dir>` boots from a prior checkpoint. `env` is a
minimal, explicit environment (the host env is **not** inherited — secrets don't leak in).

| Method | Returns | Notes |
|---|---|---|
| `exec(cmd, *, cwd=None, env=None, stdin=None, timeout=None)` | `ExecResult` | run an argv (not a shell string) |
| `read_file(path)` | `str` | `cat` in-box; raises `FileNotFoundError` |
| `write_file(path, content)` | `None` | writes to the overlay (or a granted write path) |
| `list_dir(path="/")` | `list[str]` | directory entries |
| `writes()` | `list[str]` | files under the policy's write paths (review manifest) |
| `checkpoint(dest, *, leave_running=True)` | `None` | filesystem snapshot (needs `scratch="disk"`) |
| `commit()` | `None` | persist provider-backed volumes (disk/memory are no-ops) |
| `close()` | `None` | tear down the box |

Properties: `running`, `dirty`; plus `should_autocheckpoint(...)` (the loop heuristic) and a
per-box `audit` log (`box.audit.to_dicts()`).

## BoxManager

The multi-box registry the daemon owns — also usable directly for swarms.

```python
from temenos.manager import BoxManager, box_id

mgr = BoxManager()
bid = mgr.create("/path/to/boxdir", Policy())   # idempotent; restores from checkpoint if present
mgr.get(bid).exec(["echo", "hi"])
mgr.start_checkpoint_loop()                       # background durability
print(mgr.list())                                 # [{id, name, dir, running, …}]
mgr.shutdown()                                    # commit + close every box
```
`box_id(data_dir)` is `sha256(realpath(data_dir))[:16]` — the stable, path-derived id.
`create()` persists `config.json` and restores from `<dir>/checkpoint` if present.

## ExecResult

```python
@dataclass
class ExecResult:
    stdout: str; stderr: str; exit_code: int
    truncated: bool = False      # hit max_output_bytes
    duration_ms: int = 0
```
`.ok` (exit 0), `.raise_for_status()`, `.to_dict()`.

## Storage providers

For volumes beyond same-path `read`/`write` binds, attach `Mount`s to `Policy.mounts`:

```python
from temenos import Mount, DiskVolume, MemoryVolume, Policy

Policy(mounts=(
    Mount(target="/scratch", provider=MemoryVolume(size_mb=64), mode="rw"),
    Mount(target="/data",    provider=DiskVolume("/host/data", allowed_root="/host"), mode="ro"),
))
```
- **`MemoryVolume(size_mb=None)`** — tmpfs at the target (ephemeral, counts against memory).
- **`DiskVolume(host_dir, create=True, allowed_root=None)`** — a durable host dir, realpath
  resolved; `allowed_root` pins it under a root (multi-tenant containment — a tenant can't
  point a volume at `../other-tenant`).
- **`Mount(target, provider, mode="rw")`** — exposes a provider at an absolute, normalized
  box path. Providers implement `StorageProvider` (`oci_mount`/`prepare`/`commit`/`cleanup`),
  so you can write your own.

## Exceptions

All inherit `TemenosError`: `PolicyViolation` (illegal widen), `BoxNotFound`,
`QuotaExceeded`, `BackendError`, `StorageError`.
