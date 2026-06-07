"""temenos CLI (Layer 3). Thin wrapper over the core API.

v1 so far: `temenos image build|ls|rm`. The box lifecycle commands (create/exec/shell/rm,
serve) land in Phase 3 and will hang off this same parser. Stdlib argparse — no runtime
deps — so the core CLI works without the `[cli]` extra.
"""
from __future__ import annotations

import argparse
import os
import sys

from . import image
from .exceptions import TemenosError


def _cmd_image_build(args: argparse.Namespace) -> int:
    opts: dict[str, object] = {}
    if args.builder == "mmdebstrap":
        # suite/mirror default to None so the builder auto-detects the host distro
        # (passing a mismatched suite vs the host mirror yields an empty rootfs)
        if args.suite:
            opts["suite"] = args.suite
        if args.variant:
            opts["variant"] = args.variant
        if args.mirror:
            opts["mirror"] = args.mirror
        if args.arch:
            opts["arch"] = args.arch
        if args.include:
            opts["include"] = tuple(p for p in args.include.split(",") if p)
    elif args.builder == "host-copy":
        opts["force"] = args.force_copy   # never copies silently — see --force-copy
    elif args.builder == "download":
        if not args.url:
            print("error: --from download requires --url <rootfs tarball>", file=sys.stderr)
            return 2
        opts["url"] = args.url
    img = image.build(args.name, builder=args.builder, **opts)
    print(f"built image {args.name!r} ({args.builder}) -> {img.rootfs}")
    return 0


def _cmd_image_ls(args: argparse.Namespace) -> int:
    names = image.list_images()
    if not names:
        print("(no images)")
    for n in names:
        print(n)
    return 0


def _cmd_image_rm(args: argparse.Namespace) -> int:
    if image.remove(args.name):
        print(f"removed image {args.name!r}")
        return 0
    print(f"no such image: {args.name!r}", file=sys.stderr)
    return 1


def _cmd_serve(args: argparse.Namespace) -> int:
    import json
    import secrets

    import uvicorn

    from .manager import BoxManager
    from .server.app import create_app
    from .server.client import daemon_home, info_path

    os.makedirs(daemon_home(), mode=0o700, exist_ok=True)
    token = secrets.token_urlsafe(32)
    url = f"http://127.0.0.1:{args.port}"
    with open(info_path(), "w") as f:
        json.dump({"url": url, "token": token, "pid": os.getpid()}, f)
    mgr = BoxManager()
    mgr.start_checkpoint_loop()                 # D17: periodic checkpoint of dirty boxes
    app = create_app(manager=mgr, token=token)
    print(f"temenos daemon on {url}")
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        mgr.shutdown()                          # tear down every box
        try:
            os.remove(info_path())
        except OSError:
            pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="temenos", description="trusted-agent box runtime")
    sub = p.add_subparsers(dest="group", required=True)

    img = sub.add_parser("image", help="manage box base images")
    isub = img.add_subparsers(dest="action", required=True)

    b = isub.add_parser("build", help="build a base image")
    b.add_argument("name")
    b.add_argument("--from", dest="builder", default="mmdebstrap",
                   choices=sorted(image.BUILDERS), help="builder (default: mmdebstrap)")
    b.add_argument("--suite", default=None, help="mmdebstrap suite (default: host distro)")
    b.add_argument("--variant", default="apt", help="mmdebstrap variant (default: apt)")
    b.add_argument("--mirror", default=None, help="mmdebstrap mirror URL")
    b.add_argument("--arch", default=None, help="mmdebstrap architecture")
    b.add_argument("--include", default=None, help="mmdebstrap extra packages (comma-sep)")
    b.add_argument("--url", default=None, help="rootfs tarball URL (for --from download)")
    b.add_argument("--force-copy", dest="force_copy", action="store_true",
                   help="confirm a full host /usr copy (required for --from host-copy)")
    b.set_defaults(func=_cmd_image_build)

    ls = isub.add_parser("ls", help="list images")
    ls.set_defaults(func=_cmd_image_ls)

    rm = isub.add_parser("rm", help="remove an image")
    rm.add_argument("name")
    rm.set_defaults(func=_cmd_image_rm)

    srv = sub.add_parser("serve", help="run the temenos daemon (one per user)")
    srv.add_argument("--port", type=int, default=int(os.environ.get("TEMENOS_PORT", "8839")))
    srv.set_defaults(func=_cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TemenosError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
