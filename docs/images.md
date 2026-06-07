# Box images

## Why images exist

By default a box binds the host's `/usr` and `/etc` **read-only**, so the system works
(`python3`, `git`, coreutils) but isn't writable — `apt`, system `pip`, and `npm -g` can't
install into it. That read-only bind is also unavoidable: host-root-owned files show up as
`nobody` to the rootless box, so they can't be made writable in place.

An **image** is a base rootfs **owned by the box runner**, which the box boots from instead of
the host bind. With it as the box root, the whole tree (`/usr`, `/etc`, `/var`) is
**writable-but-ephemeral** (the writes go to the box overlay, off the host) — so package
installs work and stay contained. The image is the shared overlay *lower* (built once); each
box gets its own upper, so there's no per-box copy.

Select an image per box with `--image NAME` (CLI) or `Policy(image="NAME")`.

## Builders

```bash
temenos image build NAME [--from <builder>] [options]
temenos image ls
temenos image rm NAME
```

| Builder | What it produces | When to use |
|---|---|---|
| `mmdebstrap` **(default)** | a clean Debian/Ubuntu apt base, runner-owned (~hundreds of MB) | the recommended real base; boxes can `apt`/`pip`/`npm` |
| `minimal` | a thin, `ldd`-resolved rootfs (a shell + a few coreutils) | tests / lightweight boxes |
| `download` | a rootfs extracted from a tarball URL | bring your own base; robust everywhere |
| `host-copy` | a full copy of the host's `/usr` etc. | "clone this host" — **guarded**, see below |

Images live at `$TEMENOS_DATA/images/<name>/rootfs` (see [Configuration](configuration.md)).

### mmdebstrap (default)

Needs the `mmdebstrap` binary on the host (`sudo apt-get install mmdebstrap`); without it the
build fails with a clear error rather than silently falling back. Options:

```bash
temenos image build base                       # auto-detects the host suite/mirror
temenos image build base --suite bookworm --mirror http://deb.debian.org/debian
temenos image build base --variant apt --include git,curl,ca-certificates
temenos image build base --arch arm64
```

`--suite`/`--mirror` default to the **host distro** — passing a suite that mismatches the
mirror yields an empty rootfs, so leave them unset unless you know you need them.

### host-copy (guarded)

Copying the host's `/usr` is rarely what you want (it can be many GB) and is easy to trigger
by accident, so it **requires `--force-copy`**:

```bash
temenos image build snapshot --from host-copy --force-copy
```

### download

```bash
temenos image build deb --from download --url https://example.com/rootfs.tar.gz
```

## Installing packages inside a box

With an image (writable system), package managers work in-box. Note v1 facts:

- **Network is on by default**, so `apt`/`pip`/`npm` can reach their indexes. (Isolate with
  `--no-net` once deps are baked, if you like.)
- The mmdebstrap images ship an apt config that runs apt as root and forces IPv4, so
  `apt-get install` works in the rootless box without the usual sandbox-user/IPv6 snags.
- Installs land in the box overlay; with `checkpoint=auto` (default) they're snapshotted, so
  the next boot of that box still has them. To bake deps into a *shared* base instead, build
  them into a custom image (e.g. via `--include` or a `download` tarball you prepared).

## Without an image

You don't need an image to run code — the default host-`/usr` bind already gives you the
host's interpreters and tools (read-only). Reach for an image when the box needs to *install*
into its system. See [Concepts → Images](concepts.md#images).
