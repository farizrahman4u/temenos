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
import tempfile
import urllib.request

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


def _extract_rootfs(tarball: str, rootfs: str) -> None:
    """Extract a base rootfs tarball as the current user → runner-owned. Drops ownership
    (`--no-same-owner`) and skips device nodes (`./dev/*` — non-root can't mknod, and
    gVisor provides /dev anyway)."""
    x = subprocess.run(
        ["tar", "-xf", tarball, "-C", rootfs, "--no-same-owner", "--exclude=./dev/*"],
        capture_output=True, text=True,
    )
    if x.returncode != 0:
        raise TemenosError(f"extracting rootfs tarball failed:\n{x.stderr.strip()[-400:]}")


def _prep_apt_image(rootfs: str) -> None:
    """Make a Debian/Ubuntu base usable in a box (verified e2e). apt in a rootless box
    needs: don't drop to the `_apt` user (uid mapping breaks it), and force IPv4 (IPv6
    doesn't route through the passthrough). DNS is handled at box start (the backend
    injects the host's resolver for network boxes — see GVisorBackend)."""
    if os.path.exists(os.path.join(rootfs, "usr/bin/apt-get")):
        d = os.path.join(rootfs, "etc", "apt", "apt.conf.d")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "99temenos"), "w") as f:
            f.write('APT::Sandbox::User "root";\nAcquire::ForceIPv4 "true";\n')


def build_download(name: str, url: str, data_dir: str | None = None) -> Image:
    """Download a prebuilt base rootfs tarball (e.g. an Ubuntu base image) and extract it
    as the current user → runner-owned, apt-capable. Robust everywhere (no build-time
    chroot), and the recommended builder where mmdebstrap's unshare mode misbehaves
    (e.g. WSL2). VERIFIED e2e: an Ubuntu-base box runs `apt-get install`."""
    img = Image(name, data_dir)
    if os.path.exists(img.dir):
        shutil.rmtree(img.dir)
    os.makedirs(img.rootfs)
    fd, tarball = tempfile.mkstemp(prefix="temenos-dl-", suffix=".tar")
    os.close(fd)
    try:
        with urllib.request.urlopen(url) as resp, open(tarball, "wb") as out:  # noqa: S310
            shutil.copyfileobj(resp, out)
        _extract_rootfs(tarball, img.rootfs)
    except Exception:
        shutil.rmtree(img.dir, ignore_errors=True)
        raise
    finally:
        os.path.exists(tarball) and os.remove(tarball)
    _prep_apt_image(img.rootfs)
    return img


def _host_suite_mirror() -> tuple[str, str]:
    """Default (suite, deb-line mirror) from the host distro + arch, so the host's apt
    keyring validates the build and the arch's mirror is used."""
    osr: dict[str, str] = {}
    try:
        for line in open("/etc/os-release"):
            if "=" in line:
                k, v = line.rstrip().split("=", 1)
                osr[k] = v.strip('"')
    except OSError:
        pass
    arch = subprocess.run(["dpkg", "--print-architecture"], capture_output=True,
                          text=True).stdout.strip() or "arm64"
    distro, codename = osr.get("ID", ""), osr.get("VERSION_CODENAME", "")
    if distro == "debian":
        return (codename or "bookworm", f"deb http://deb.debian.org/debian {codename or 'bookworm'} main")
    suite = codename or "noble"   # default to Ubuntu (host keyring)
    url = "http://archive.ubuntu.com/ubuntu" if arch in ("amd64", "i386") \
        else "http://ports.ubuntu.com/ubuntu-ports"
    return (suite, f"deb {url} {suite} main universe")


def build_mmdebstrap(
    name: str,
    *,
    suite: str | None = None,
    variant: str = "apt",   # essential + apt (apt-capable base); 'minbase' omits apt
    mirror: str | None = None,
    arch: str | None = None,
    include: tuple[str, ...] = (),
    data_dir: str | None = None,
) -> Image:
    """A clean apt base via `mmdebstrap`, runner-owned. Defaults to the host's distro so
    its keyring validates. Verified working on WSL2 with the recipe below: drive the user
    namespace ourselves (`unshare --map-root-user --map-auto`) + `--mode=root` + skip the
    privileged device/mount setup (gVisor provides /dev) — mmdebstrap's own `--mode=unshare`
    misbehaves on some WSL2 kernels. Needs `mmdebstrap`, `unshare`, `newuidmap` + /etc/subuid."""
    for tool in ("mmdebstrap", "unshare", "newuidmap"):
        if shutil.which(tool) is None:
            raise TemenosError(
                f"{tool} not found — mmdebstrap builder needs mmdebstrap + unshare + newuidmap "
                "(apt install mmdebstrap uidmap) and /etc/subuid configured. "
                "Use builder='download' for a prebuilt base, or 'minimal' for a thin one."
            )
    d_suite, d_mirror = _host_suite_mirror()
    suite = suite or d_suite
    mirror = mirror or d_mirror
    img = Image(name, data_dir)
    if os.path.exists(img.dir):
        shutil.rmtree(img.dir)
    os.makedirs(img.rootfs)
    tarball = os.path.join(img.dir, "rootfs.tar")
    mm = ["mmdebstrap", "--mode=root",
          "--skip=setup/mknod", "--skip=chroot/mount/dev", "--skip=chroot/mount",
          f"--variant={variant}"]
    if arch:
        mm.append(f"--architectures={arch}")
    if include:
        mm.append("--include=" + ",".join(include))
    mm += [suite, tarball, mirror]
    cmd = ["unshare", "--map-root-user", "--map-auto", "--setuid", "0", "--setgid", "0", "--", *mm]
    # mmdebstrap's tempdir must be on a normal local fs the mapped root can build on
    # (the session /tmp may be a special mount that yields an empty rootfs) AND short
    # (its AF_UNIX hook socket has a ~108-char path limit). Put it next to the data dir.
    data_root = os.path.dirname(os.path.dirname(img.dir))
    build_tmp = tempfile.mkdtemp(prefix="tmn-", dir=data_root)
    env = {**os.environ, "TMPDIR": build_tmp}
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    shutil.rmtree(build_tmp, ignore_errors=True)
    if r.returncode != 0 or not os.path.exists(tarball) or os.path.getsize(tarball) < 1_000_000:
        shutil.rmtree(img.dir, ignore_errors=True)
        raise TemenosError(f"mmdebstrap failed (rc={r.returncode}):\n{r.stderr.strip()[-800:]}")
    try:
        _extract_rootfs(tarball, img.rootfs)
    except Exception:
        shutil.rmtree(img.dir, ignore_errors=True)
        raise
    finally:
        os.path.exists(tarball) and os.remove(tarball)
    _prep_apt_image(img.rootfs)
    return img


def build_host_copy(name: str, *, force: bool = False, data_dir: str | None = None) -> Image:
    """A full runner-owned COPY of the host system dirs (usr/etc/bin/...). This is heavy
    (the host `/usr` is often many GB) and rarely what you want — prefer `mmdebstrap` for
    a clean base. Guarded: requires `force=True` (CLI `--force-copy`) so it never happens
    silently / as a fallback."""
    if not force:
        raise TemenosError(
            "host-copy duplicates the entire host /usr (can be many GB) — this is rarely "
            "what you want. Pass --force-copy to confirm, or use --from mmdebstrap for a "
            "clean apt base."
        )
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


# Pluggable builder registry. A builder is `f(name, *, data_dir=None, **opts) -> Image`
# producing a runner-owned rootfs. The runtime only needs the resulting dir.
BUILDERS = {
    "download": build_download,       # prebuilt rootfs tarball — robust everywhere
    "mmdebstrap": build_mmdebstrap,   # clean apt base, runner-owned (self-unshare recipe; WSL2-ok)
    "minimal": build_minimal,         # thin ldd-resolved (tests / lightweight)
    "host-copy": build_host_copy,     # full host /usr copy — guarded by force=True
}


def build(name: str, builder: str = "mmdebstrap", *, data_dir: str | None = None,
          **opts: object) -> Image:
    """Build image `name` with the named builder. `opts` pass through to the builder
    (e.g. suite=/include= for mmdebstrap, binaries= for minimal)."""
    fn = BUILDERS.get(builder)
    if fn is None:
        raise TemenosError(f"unknown image builder: {builder!r} (have: {sorted(BUILDERS)})")
    return fn(name, data_dir=data_dir, **opts)  # type: ignore[arg-type]


def remove(name: str, data_dir: str | None = None) -> bool:
    """Delete an image. Returns True if it existed."""
    img = Image(name, data_dir)
    if os.path.isdir(img.dir):
        shutil.rmtree(img.dir)
        return True
    return False
