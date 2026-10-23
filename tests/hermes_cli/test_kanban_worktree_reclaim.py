import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_worktree_reclaim as r


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text("node_modules/\n.turbo/\n.worktrees/\n")
    (repo / "vendor").mkdir(); (repo / "vendor" / "node_modules").mkdir()
    (repo / "vendor" / "node_modules" / "keep.js").write_text("tracked")
    _git(repo, "add", "-f", "."); _git(repo, "commit", "-qm", "init")
    return repo


def _wt(repo, name):
    wt = repo / ".worktrees" / name
    _git(repo, "worktree", "add", "-q", "-b", f"wt/{name}", str(wt), "HEAD")
    nm = wt / "apps" / "web" / "node_modules" / "pkg"
    nm.mkdir(parents=True); (nm / "index.js").write_text("x" * 4096)
    (wt / "node_modules").mkdir(); (wt / "node_modules" / "a.js").write_text("y" * 4096)
    (wt / ".turbo").mkdir(); (wt / ".turbo" / "log").write_text("z")
    return wt


def test_reclaim_removes_ignored_artifacts_keeps_tracked(repo):
    wt = _wt(repo, "t1")
    freed = r.reclaim_build_artifacts(wt)
    assert freed >= 8192
    assert not (wt / "node_modules").exists()
    assert not (wt / "apps" / "web" / "node_modules").exists()
    assert not (wt / ".turbo").exists()
    assert (wt / "vendor" / "node_modules" / "keep.js").exists()  # tracked -> kept


def test_reclaim_dry_run_and_symlink_not_followed(repo, tmp_path):
    wt = _wt(repo, "t2")
    outside = tmp_path / "outside"; outside.mkdir(); (outside / "f").write_text("keep")
    os.symlink(outside, wt / "apps" / "node_modules")
    assert r.reclaim_build_artifacts(wt, dry_run=True) > 0
    assert (wt / "node_modules").exists()
    r.reclaim_build_artifacts(wt)
    assert (outside / "f").exists()


def test_sweep_removes_clean_old_reclaims_dirty_skips_active_and_young(repo):
    clean, dirty, active, young = (_wt(repo, n) for n in ("clean", "dirty", "active", "young"))
    (dirty / "work.txt").write_text("uncommitted")
    old = 1_000_000.0
    for wt in (clean, dirty, active):
        for p in (wt, wt / ".git"):
            os.utime(p, (old, old))
    now = old + 2 * 86400
    real_last = r._last_activity
    # _last_activity reads git admin files too; pin it for determinism.
    r._last_activity = lambda wt: now if wt.name == "young" else old
    try:
        rep = r.sweep_orphan_worktrees(
            [repo], active_paths=[str(active)], now=now,
            is_dirty=lambda p, _r: Path(p).name == "dirty",
            has_unpushed=lambda p: False,
        )
    finally:
        r._last_activity = real_last
    assert not clean.exists() and str(clean) in [str(Path(p)) for p in rep.removed]
    assert dirty.exists() and (dirty / "work.txt").exists() and not (dirty / "node_modules").exists()
    assert (active / "node_modules").exists() and (young / "node_modules").exists()


def test_sweep_keeps_unpushed(repo):
    wt = _wt(repo, "unpushed")
    r._last_activity, real = (lambda w: 0.0), r._last_activity
    try:
        rep = r.sweep_orphan_worktrees([repo], now=10 * 86400, is_dirty=lambda p, _r: False,
                                       has_unpushed=lambda p: True)
    finally:
        r._last_activity = real
    assert wt.exists() and not rep.removed and not (wt / "node_modules").exists()


def test_daily_sweeper_schedule():
    t = [0.0]
    s = r.DailySweeper({}, clock=lambda: t[0])
    assert not s.due()
    t[0] = 601
    assert s.due()
    s._next = t[0] + s.interval
    assert not s.due()
    assert not r.DailySweeper({"worktree_sweep_enabled": False}, clock=lambda: 1e9).due()
