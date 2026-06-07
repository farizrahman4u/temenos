"""Project mode (D15/D16): git-style `.temenos/` discovery + box name resolution.

A box is identified by the realpath of its data dir (`manager.box_id` hashes it), so the
user never types a data path. This module maps a bare box *name* to a data *dir*:

  - **discovery** — walk up from CWD to the nearest dir holding `.temenos/` (stop at
    `$HOME` and `/`, so a `.temenos` *above* home is never mistaken for a project marker;
    the global dir lives at `$XDG_DATA_HOME/temenos`, deliberately not `~/.temenos`).
  - **resolution** — a bare name resolves to the project box `<root>/.temenos/<name>` if
    it exists, **else** the global box `$TEMENOS_DATA/boxes/<name>`. If both exist the
    project box wins and `shadows_global` is set so the CLI can warn (D15).

Everything for a box lives under its data dir (config + overlay + checkpoint — D16), so a
project's `.temenos/` is portable and `rm -rf`-able; the whole dir is gitignored.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

MARKER = ".temenos"
DEFAULT_BOX = "default"
_GITIGNORE = "# temenos box state — never commit\n*\n"


def _home() -> str:
    return os.path.realpath(os.path.expanduser("~"))


def global_data_dir() -> str:
    """Where global (non-project) state lives — images and global boxes. Matches
    `image._default_data_dir()`; NOT `~/.temenos` (that name is the project marker)."""
    return os.environ.get("TEMENOS_DATA", os.path.expanduser("~/.local/share/temenos"))


def global_boxes_dir() -> str:
    return os.path.join(global_data_dir(), "boxes")


def find_project(start: str | None = None) -> str | None:
    """Nearest ancestor dir containing `.temenos/`, walking up from `start` (default CWD).
    Stops at `$HOME` and the filesystem root — a marker found *above* home is not treated
    as a project (it would almost certainly be unrelated)."""
    cur = os.path.realpath(start or os.getcwd())
    home = _home()
    while True:
        if os.path.isdir(os.path.join(cur, MARKER)):
            return cur
        if cur == home or cur == "/":
            return None
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


@dataclass
class Project:
    root: str            # the dir that holds `.temenos/`
    created: bool        # True if this call just created the marker
    in_home: bool        # root == $HOME (worth a warning — likely not intended)

    @property
    def temenos_dir(self) -> str:
        return os.path.join(self.root, MARKER)


def ensure_project(start: str | None = None) -> Project:
    """Find the enclosing project, or create `.temenos/` (with a `.gitignore`) in CWD."""
    start = os.path.realpath(start or os.getcwd())
    found = find_project(start)
    if found is not None:
        return Project(root=found, created=False, in_home=(found == _home()))
    td = os.path.join(start, MARKER)
    os.makedirs(td, exist_ok=True)
    gi = os.path.join(td, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as f:
            f.write(_GITIGNORE)
    return Project(root=start, created=True, in_home=(start == _home()))


@dataclass
class Resolved:
    name: str
    data_dir: str        # the box's data dir (realpath-stable id source)
    scope: str           # "project" | "global"
    exists: bool         # the data dir is already a box on disk
    shadows_global: bool # resolved to a project box while a same-named global one exists


def resolve_box(name: str, *, start: str | None = None,
                prefer: str = "auto") -> Resolved:
    """Map a bare box name to its data dir (project-first, then global — D15).

    `prefer="project"` forces the project location (used by `create` after ensuring a
    project); `prefer="global"` forces the global location (`--global`); `"auto"` (default)
    picks an existing box project-first, and otherwise the project location if a project is
    discoverable, else global.
    """
    proj = find_project(start)
    proj_dir = os.path.join(proj, MARKER, name) if proj else None
    glob_dir = os.path.join(global_boxes_dir(), name)
    proj_exists = proj_dir is not None and os.path.isdir(proj_dir)
    glob_exists = os.path.isdir(glob_dir)

    if prefer == "global":
        return Resolved(name, glob_dir, "global", glob_exists, shadows_global=False)
    if prefer == "project":
        if proj_dir is None:
            raise ValueError("no project (.temenos/) to create a project box in")
        return Resolved(name, proj_dir, "project", proj_exists, shadows_global=glob_exists)

    # auto: existing project box wins; else existing global; else where it *would* live.
    if proj_exists:
        return Resolved(name, proj_dir, "project", True, shadows_global=glob_exists)
    if glob_exists:
        return Resolved(name, glob_dir, "global", True, shadows_global=False)
    if proj_dir is not None:
        return Resolved(name, proj_dir, "project", False, shadows_global=glob_exists)
    return Resolved(name, glob_dir, "global", False, shadows_global=False)
