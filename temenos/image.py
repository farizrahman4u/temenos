"""Box images — a runner-owned base rootfs a box boots from.

Why images: binding the host's read-only `/usr` can't be made writable in a rootless
box (host-root-owned files show as `nobody` → box-root can't write — verified). An image
is a rootfs **owned by the box-runner**, so when it's the box root with
`--overlay2=root:memory`, the whole tree (incl. `/usr`,`/etc`,`/var`) is writable-
ephemeral → `pip`/`apt`/`npm` work, host untouched. The image is the shared overlay
*lower* (built once); each box gets its own upper, so there's no per-box copy.

An image is just a directory: `$TEMENOS_DATA/images/<name>/rootfs`, owned by the runner.
The *builder* is pluggable — this module ships a `minimal` builder (a thin, ldd-resolved
rootfs, used for tests/lightweight boxes) and a `host_snapshot` builder (copy the host's
system dirs). `mmdebstrap`/`skopeo` builders can be added later; the runtime only needs a
runner-owned rootfs dir.
"""
from __future__ import annotations

import os
import shutil
import subprocess

from .exceptions import TemenosError

# Default thin-image binaries: a shell + a few coreutils. dash provides `echo`/redirection
# as builtins, so this is enough to exercise a writable image root.
DEFAULT_MINIMAL_BINS = (
    "/usr/bin/dash", "/usr/bin/sleep", "/usr/bin/env", "/usr/bin/cat", "/usr/bin/ls",
    "/usr/bin/mkdir", "/usr/bin/touch", "/usr/bin/rm", "/usr/bin/chmod",
)
# Real host dirs copied by the host_snapshot builder.
_SNAPSHOT_DIRS = ("usr", "etc", "bin", "sbin", "lib", "lib64")
_USRMERGE = (("bin", "usr/bin"), ("sbin", "usr/sbin"), ("lib", "usr/lib"))


def _default_data_dir() -> str:
    return os.environ.get("TEMENOS_DATA", os.path.expanduser("~/.local/share/temenos"))


class Image:
    def __init__(self, name: str, data_dir: str | None = None) -> None:
        self.name = name
        self._data_dir = data_dir or _default_data_dir()

    @property
    def dir(self) -> str:
        return os.path.join(self._data_dir, "images", self.name)

    @property
    def rootfs(self) -> str:
        return os.path.join(self.dir, "rootfs")

    def exists(self) -> bool:
        return os.path.isdir(self.rootfs)


def resolve(name: str, data_dir: str | None = None) -> Image:
    img = Image(name, data_dir)
    if not img.exists():
        raise TemenosError(f"box image not found: {name!r} (looked in {img.rootfs})")
    return img


def list_images(data_dir: str | None = None) -> list[str]:
    root = os.path.join(data_dir or _default_data_dir(), "images")
    if not os.path.isdir(root):
        return []
    return sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d, "rootfs")))


# -- rootfs scaffolding ---------------------------------------------------------------

def _scaffold(rootfs: str) -> None:
    for d in ("usr/bin", "usr/sbin", "usr/lib", "lib64", "etc", "proc", "tmp",
              "dev", "opt", "var", "root", "run"):
        os.makedirs(os.path.join(rootfs, d), exist_ok=True)
    for link, target in _USRMERGE:
        p = os.path.join(rootfs, link)
        if not os.path.lexists(p):
            os.symlink(target, p)
    # a minimal /etc so tools that stat these don't choke
    for f, content in (("etc/passwd", "root:x:0:0:root:/root:/bin/sh\n"),
                       ("etc/group", "root:x:0:\n"),
                       ("etc/hostname", "box\n")):
        fp = os.path.join(rootfs, f)
        if not os.path.exists(fp):
            with open(fp, "w") as fh:
                fh.write(content)


def _copy_into(rootfs: str, host_path: str) -> None:
    """Place the real content of host_path at the same path under rootfs, following the
    usrmerge symlinks already created (so /lib/... lands in usr/lib/...)."""
    dst = rootfs + host_path
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not os.path.exists(dst):
        shutil.copy2(os.path.realpath(host_path), dst)


def _ldd_libs(binary: str) -> list[str]:
    try:
        out = subprocess.run(["ldd", binary], capture_output=True, text=True).stdout
    except Exception:  # noqa: BLE001
        return []
    libs = []
    for line in out.splitlines():
        line = line.strip()
        tok = line.split("=>")[1].strip().split(" ")[0] if "=>" in line else line.split(" ")[0]
        if tok.startswith("/"):
            libs.append(tok)
    return libs


# -- builders -------------------------------------------------------------------------

def build_minimal(name: str, binaries: tuple[str, ...] = DEFAULT_MINIMAL_BINS,
                  data_dir: str | None = None) -> Image:
    """A thin, runner-owned rootfs: the given binaries + their ldd-resolved libs. Fast;
    for tests and lightweight boxes. Files are owned by the caller → writable in-box."""
    img = Image(name, data_dir)
    if os.path.exists(img.rootfs):
        shutil.rmtree(img.rootfs)
    os.makedirs(img.rootfs)
    _scaffold(img.rootfs)
    libs: set[str] = set()
    for b in binaries:
        if os.path.exists(b):
            _copy_into(img.rootfs, b)
            libs.update(_ldd_libs(b))
    for lib in libs:
        _copy_into(img.rootfs, lib)
    # /bin/sh -> dash
    sh = os.path.join(img.rootfs, "usr/bin/sh")
    if not os.path.lexists(sh) and os.path.exists(os.path.join(img.rootfs, "usr/bin/dash")):
        os.symlink("dash", sh)
    return img


def build_host_snapshot(name: str, data_dir: str | None = None) -> Image:
    """A full runner-owned snapshot of the host system dirs (usr/etc/bin/...). Heavy
    (copies ~GB) and slow; the production builder for 'behaves like this host'. Copied as
    the caller → runner-owned → writable in-box. (For a clean apt base, a future
    mmdebstrap builder is preferable; this one needs no extra tools.)"""
    img = Image(name, data_dir)
    if os.path.exists(img.rootfs):
        shutil.rmtree(img.rootfs)
    os.makedirs(img.rootfs)
    _scaffold(img.rootfs)
    for d in _SNAPSHOT_DIRS:
        src = "/" + d
        if os.path.islink(src) or not os.path.isdir(src):
            continue  # usrmerge symlink (handled) or absent
        shutil.copytree(src, os.path.join(img.rootfs, d), symlinks=True, dirs_exist_ok=True)
    return img
