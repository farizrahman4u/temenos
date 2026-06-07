"""Phase 4 — project-mode discovery + name resolution (pure; no daemon/runsc)."""
from __future__ import annotations

import os

import pytest

from temenos import project


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake $HOME under tmp, with a global data dir alongside it."""
    h = tmp_path / "home"
    (h / "sub" / "deep").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("TEMENOS_DATA", str(tmp_path / "data"))
    return h


# -- discovery (walk-up, stops at $HOME) ----------------------------------------------

def test_find_project_none_when_no_marker(home):
    assert project.find_project(str(home / "sub" / "deep")) is None


def test_find_project_walks_up_to_marker(home):
    (home / "sub" / project.MARKER).mkdir()
    found = project.find_project(str(home / "sub" / "deep"))
    assert found == os.path.realpath(str(home / "sub"))


def test_find_project_stops_at_home_does_not_ascend_above(home, tmp_path):
    # a marker ABOVE $HOME must never be picked up (that's where the global dir lives)
    (tmp_path / project.MARKER).mkdir()
    assert project.find_project(str(home / "sub")) is None


def test_find_project_includes_home_itself(home):
    (home / project.MARKER).mkdir()
    assert project.find_project(str(home / "sub")) == os.path.realpath(str(home))


# -- ensure_project -------------------------------------------------------------------

def test_ensure_project_creates_marker_and_gitignore(home):
    p = project.ensure_project(str(home / "sub"))
    assert p.created and not p.in_home
    assert os.path.isdir(p.temenos_dir)
    gi = os.path.join(p.temenos_dir, ".gitignore")
    assert os.path.exists(gi)
    assert "*" in open(gi).read()


def test_ensure_project_finds_existing(home):
    (home / "sub" / project.MARKER).mkdir()
    p = project.ensure_project(str(home / "sub" / "deep"))
    assert not p.created
    assert p.root == os.path.realpath(str(home / "sub"))


def test_ensure_project_flags_home(home):
    p = project.ensure_project(str(home))
    assert p.in_home


# -- name resolution (project-first, then global; shadow flag) ------------------------

def test_resolve_global_when_no_project(home):
    r = project.resolve_box("default", start=str(home / "sub"))
    assert r.scope == "global"
    assert r.data_dir == os.path.join(project.global_boxes_dir(), "default")
    assert not r.exists


def test_resolve_prefers_existing_project_box(home):
    (home / "sub" / project.MARKER / "default").mkdir(parents=True)
    r = project.resolve_box("default", start=str(home / "sub" / "deep"))
    assert r.scope == "project" and r.exists
    assert r.data_dir == os.path.join(os.path.realpath(str(home / "sub")),
                                      project.MARKER, "default")


def test_resolve_project_shadows_global(home):
    (home / "sub" / project.MARKER / "default").mkdir(parents=True)
    os.makedirs(os.path.join(project.global_boxes_dir(), "default"))
    r = project.resolve_box("default", start=str(home / "sub"))
    assert r.scope == "project"
    assert r.shadows_global is True


def test_resolve_falls_back_to_existing_global(home):
    (home / "sub" / project.MARKER).mkdir(parents=True)        # project exists, box doesn't
    os.makedirs(os.path.join(project.global_boxes_dir(), "g1"))
    r = project.resolve_box("g1", start=str(home / "sub"))
    assert r.scope == "global" and r.exists


def test_resolve_prefer_global_and_project(home):
    (home / "sub" / project.MARKER).mkdir(parents=True)
    rg = project.resolve_box("x", start=str(home / "sub"), prefer="global")
    assert rg.scope == "global"
    rp = project.resolve_box("x", start=str(home / "sub"), prefer="project")
    assert rp.scope == "project"
    (home / "other").mkdir()                                   # no marker on this path
    with pytest.raises(ValueError):
        project.resolve_box("x", start=str(home / "other"),
                            prefer="project")  # no project above → can't force project
