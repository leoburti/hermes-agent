"""HERMES_WORKTREE_ROOT placement + external-root guard."""
import os
import subprocess
from collections import namedtuple
from pathlib import Path

import pytest

from hermes_cli import worktree_ops, worktree_root
from hermes_cli.worktree_root import WorktreeRootError, ensure_worktree_root_ready, worktrees_dir_for


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture
def clean_env(monkeypatch):
    for k in (worktree_root.ENV_ROOT, worktree_root.ENV_MIN_FREE_GB, worktree_root.ENV_REQUIRE_MOUNT):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _set_root(mp, root, min_gb="0"):
    mp.setenv(worktree_root.ENV_ROOT, str(root))
    mp.setenv(worktree_root.ENV_MIN_FREE_GB, min_gb)
    mp.setenv(worktree_root.ENV_REQUIRE_MOUNT, "0")  # tmp_path shares the home disk in CI


def test_unset_is_legacy_layout(clean_env, git_repo):
    assert worktrees_dir_for(git_repo) == Path(git_repo) / ".worktrees"
    ensure_worktree_root_ready()  # no-op
    info = worktree_ops._setup_worktree(str(git_repo), sync_base=False)
    assert info and Path(info["path"]).parent == git_repo / ".worktrees"


def test_set_and_ok_creates_under_root(clean_env, git_repo, tmp_path):
    root = tmp_path / "ssd"
    root.mkdir()
    _set_root(clean_env, root)
    d = worktrees_dir_for(git_repo)
    assert d.parent.parent == root and d.name == ".worktrees" and d.parent.name.startswith("proj-")
    info = worktree_ops._setup_worktree(str(git_repo), sync_base=False)
    assert info and Path(info["path"]).parent == d and Path(info["path"], "README.md").exists()
    assert not (git_repo / ".worktrees").exists() or not any((git_repo / ".worktrees").iterdir())


def test_missing_root_refused_without_fallback(clean_env, git_repo, tmp_path):
    root = tmp_path / "unmounted"
    _set_root(clean_env, root)
    with pytest.raises(WorktreeRootError, match="does not exist"):
        ensure_worktree_root_ready()
    assert worktree_ops._setup_worktree(str(git_repo), sync_base=False) is None
    assert not root.exists()  # root never created by us
    assert not (git_repo / ".worktrees").exists()  # no internal-disk fallback


def test_same_device_refused_when_mount_required(clean_env, tmp_path, monkeypatch):
    root = tmp_path / "r"
    root.mkdir()
    monkeypatch.setenv(worktree_root.ENV_ROOT, str(root))
    monkeypatch.setenv(worktree_root.ENV_MIN_FREE_GB, "0")
    monkeypatch.setattr(worktree_root.Path, "home", classmethod(lambda cls: root))
    with pytest.raises(WorktreeRootError, match="same filesystem"):
        ensure_worktree_root_ready()


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="chmod semantics")
def test_unwritable_root_refused(clean_env, tmp_path):
    root = tmp_path / "ro"
    root.mkdir()
    _set_root(clean_env, root)
    root.chmod(0o500)
    try:
        with pytest.raises(WorktreeRootError, match="not writable"):
            ensure_worktree_root_ready()
    finally:
        root.chmod(0o700)


def test_low_space_refused(clean_env, git_repo, tmp_path, monkeypatch):
    root = tmp_path / "ssd"
    root.mkdir()
    _set_root(clean_env, root, min_gb="20")
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(worktree_root.shutil, "disk_usage", lambda p: Usage(10 ** 12, 0, 5 * 1024 ** 3))
    with pytest.raises(WorktreeRootError, match="below the 20.0 GiB minimum"):
        ensure_worktree_root_ready()
    assert worktree_ops._setup_worktree(str(git_repo), sync_base=False) is None
    assert list(root.iterdir()) == []


def test_default_min_free_is_20gb(clean_env):
    assert worktree_root._min_free_bytes() == 20 * 1024 ** 3


def test_bad_min_free_value(clean_env, tmp_path):
    _set_root(clean_env, tmp_path, min_gb="lots")
    with pytest.raises(WorktreeRootError, match="not a number"):
        ensure_worktree_root_ready()


def test_web_git_and_subagent_honour_guard(clean_env, git_repo, tmp_path):
    from hermes_cli import web_git
    from tools import subagent_worktree
    _set_root(clean_env, tmp_path / "gone")
    with pytest.raises(RuntimeError, match="does not exist"):
        web_git._prepare_worktrees_dir(str(git_repo))
    assert subagent_worktree.create_subagent_worktree(str(git_repo), "x1") is None
    assert not (git_repo / ".worktrees").exists()
