"""Reclaim disk held by finished kanban worktrees (SRLMM005 2026-10-22).

Kanban workers run a full ``pnpm install`` inside each ``<repo>/.worktrees/<id>``
(~1.9 GB of node_modules). Completion only removed a worktree when it was clean
and pushed; every preserved worktree kept its node_modules forever (53 trees /
46 GB on SRLMM005). This module:

* :func:`reclaim_build_artifacts` - delete regenerable, git-ignored build output
  (``node_modules`` / ``.turbo``) from a worktree that must be preserved.
* :func:`sweep_orphan_worktrees` - daily pass over ``<repo>/.worktrees/*``: entries
  idle > 24 h and not used by any live task are removed when clean+pushed
  (same predicates as completion cleanup), otherwise only their artifacts are
  reclaimed; then ``git worktree prune``.

Never touches tracked files, never uses ``--force``, never follows symlinks out
of the worktree. Run by hand: ``python -m hermes_cli.kanban_worktree_reclaim --dry-run <repo>``.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

ARTIFACT_DIR_NAMES = ("node_modules", ".turbo")
# Do not descend into these while looking for nested artifact dirs.
_SKIP_DESCEND = {".git", "node_modules", ".turbo", ".worktrees"}
_MAX_DEPTH = 4  # repo/apps/<app>/node_modules is depth 3
DEFAULT_MAX_AGE_SECONDS = 24 * 3600


def _git(cwd: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          timeout=timeout, env=env, check=False)


def _dir_size(path: Path) -> int:
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            # Hard-linked files (pnpm store) are not freed by deletion.
            if st.st_nlink <= 1:
                total += st.st_size
    return total


def find_artifact_dirs(worktree: Path) -> list[Path]:
    """Artifact dirs inside *worktree* (real dirs only, never symlinks)."""
    found: list[Path] = []
    base_depth = len(worktree.parts)
    for root, dirs, _files in os.walk(worktree, followlinks=False):
        rp = Path(root)
        keep = []
        for d in dirs:
            p = rp / d
            if p.is_symlink():
                continue
            if d in ARTIFACT_DIR_NAMES:
                found.append(p)
            elif d not in _SKIP_DESCEND and len(p.parts) - base_depth < _MAX_DEPTH:
                keep.append(d)
        dirs[:] = keep
    return found


def _is_git_ignored(worktree: Path, path: Path) -> bool:
    rel = os.path.relpath(path, worktree)
    r = _git(worktree, "check-ignore", "-q", "--no-index", rel + "/")
    if r.returncode != 0:
        return False
    # Paranoia: nothing tracked may live under it.
    tracked = _git(worktree, "ls-files", "--", rel)
    return tracked.returncode == 0 and not tracked.stdout.strip()


def reclaim_build_artifacts(worktree: Path | str, *, dry_run: bool = False) -> int:
    """Delete git-ignored node_modules/.turbo dirs under *worktree*. Returns bytes freed
    (estimated). Best-effort: never raises."""
    try:
        wt = Path(worktree).expanduser()
        if not wt.is_dir() or not (wt / ".git").exists():
            return 0
        freed = 0
        for d in find_artifact_dirs(wt):
            if not _is_git_ignored(wt, d):
                logger.info("worktree reclaim: keeping %s (not git-ignored or tracked content)", d)
                continue
            size = _dir_size(d)
            if not dry_run:
                shutil.rmtree(d, ignore_errors=True)
            freed += size
        if freed:
            logger.info("worktree reclaim: %s %.1f MiB of build artifacts in %s",
                        "would free" if dry_run else "freed", freed / 2**20, wt)
        return freed
    except Exception:
        logger.exception("worktree reclaim failed for %s", worktree)
        return 0


@dataclass
class SweepReport:
    removed: list[str] = field(default_factory=list)
    reclaimed: list[str] = field(default_factory=list)
    skipped_active: list[str] = field(default_factory=list)
    skipped_young: list[str] = field(default_factory=list)
    bytes_freed: int = 0


def _last_activity(wt: Path) -> float:
    """Newest mtime among the worktree dir, its git admin dir and HEAD/index."""
    stamps = []
    for p in (wt, wt / ".git"):
        try:
            stamps.append(p.stat().st_mtime)
        except OSError:
            pass
    r = _git(wt, "rev-parse", "--git-dir")
    if r.returncode == 0:
        gd = Path(r.stdout.strip())
        gd = gd if gd.is_absolute() else wt / gd
        for n in ("HEAD", "index", "logs/HEAD"):
            try:
                stamps.append((gd / n).stat().st_mtime)
            except OSError:
                pass
    return max(stamps) if stamps else 0.0


def _norm(p: Path | str) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(str(p))))


def sweep_orphan_worktrees(
    repo_roots: Iterable[Path | str],
    *,
    active_paths: Iterable[str] = (),
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    now: Optional[float] = None,
    dry_run: bool = False,
    is_dirty: Optional[Callable[[str, str], bool]] = None,
    has_unpushed: Optional[Callable[[str], bool]] = None,
) -> SweepReport:
    """One sweep over ``<repo>/.worktrees/*`` for each repo root."""
    if is_dirty is None or has_unpushed is None:
        from hermes_cli.worktree_ops import _worktree_has_unpushed_commits, _worktree_is_dirty
        is_dirty = is_dirty or _worktree_is_dirty
        has_unpushed = has_unpushed or _worktree_has_unpushed_commits
    now = time.time() if now is None else now
    active = {_norm(p) for p in active_paths if p}
    rep = SweepReport()
    for root in {_norm(r) for r in repo_roots if r}:
        repo = Path(root)
        wdir = repo / ".worktrees"
        if not wdir.is_dir():
            continue
        for wt in sorted(p for p in wdir.iterdir() if p.is_dir() and not p.is_symlink()):
            key = _norm(wt)
            if any(key == a or a.startswith(key + os.sep) for a in active):
                rep.skipped_active.append(str(wt)); continue
            if now - _last_activity(wt) < max_age_seconds:
                rep.skipped_young.append(str(wt)); continue
            if not (wt / ".git").is_file():
                continue  # not a linked worktree: never guess
            try:
                clean = not is_dirty(str(wt), str(repo)) and not has_unpushed(str(wt))
            except Exception:
                clean = False
            if clean:
                size = _dir_size(wt)
                if not dry_run:
                    r = _git(repo, "worktree", "remove", str(wt), timeout=120)  # no --force
                    if r.returncode != 0:
                        logger.warning("worktree sweep: remove failed for %s: %s", wt,
                                       (r.stderr or r.stdout).strip())
                        rep.bytes_freed += reclaim_build_artifacts(wt)
                        rep.reclaimed.append(str(wt)); continue
                rep.removed.append(str(wt)); rep.bytes_freed += size
            else:
                freed = reclaim_build_artifacts(wt, dry_run=dry_run)
                if freed:
                    rep.reclaimed.append(str(wt)); rep.bytes_freed += freed
        if not dry_run:
            _git(repo, "worktree", "prune")
    logger.info("worktree sweep%s: removed=%d reclaimed=%d active=%d young=%d freed=%.1f GiB",
                " (dry-run)" if dry_run else "", len(rep.removed), len(rep.reclaimed),
                len(rep.skipped_active), len(rep.skipped_young), rep.bytes_freed / 2**30)
    return rep


def kanban_sweep_inputs(kb) -> tuple[set[str], set[str]]:
    """(repo_roots, active_workspace_paths) across all boards. Never raises."""
    roots: set[str] = set()
    active: set[str] = set()
    try:
        from hermes_cli.kanban_db_boards import list_boards
        boards = list_boards(include_archived=False)
    except Exception:
        return roots, active
    for meta in boards:
        wd = (meta.get("default_workdir") or "").strip()
        root = None
        if wd:
            r = _git(Path(wd).expanduser(), "rev-parse", "--path-format=absolute", "--git-common-dir")
            if r.returncode == 0 and r.stdout.strip().endswith(".git"):
                root = str(Path(r.stdout.strip()).parent)
                roots.add(root)
        try:
            conn = kb.connect(board=meta["slug"])
            try:
                for row in conn.execute(
                        "SELECT workspace_path FROM tasks WHERE workspace_path IS NOT NULL "
                        "AND status NOT IN ('done', 'archived')"):
                    active.add(row[0])
            finally:
                conn.close()
        except Exception:
            logger.exception("worktree sweep: cannot read board %s; skipping its repos", meta.get("slug"))
            # Fail safe: if we cannot know what is active, do not sweep that repo.
            if root:
                roots.discard(root)
    return roots, active


class DailySweeper:
    """Gateway hook: run :func:`sweep_orphan_worktrees` at most once per interval.

    Config (``kanban.*``): ``worktree_sweep_enabled`` (default true),
    ``worktree_sweep_interval_hours`` (default 24), ``worktree_sweep_max_age_hours`` (24).
    """

    FIRST_RUN_DELAY_SECONDS = 600

    def __init__(self, kanban_cfg: Optional[dict] = None, *, clock: Callable[[], float] = time.time) -> None:
        cfg = kanban_cfg or {}
        self.enabled = str(cfg.get("worktree_sweep_enabled", True)).lower() not in ("0", "false", "no", "off")
        self.interval = float(cfg.get("worktree_sweep_interval_hours", 24)) * 3600
        self.max_age = float(cfg.get("worktree_sweep_max_age_hours", 24)) * 3600
        self._clock = clock
        self._next = clock() + self.FIRST_RUN_DELAY_SECONDS

    def due(self) -> bool:
        return self.enabled and self._clock() >= self._next

    def run(self, kb) -> Optional[SweepReport]:
        self._next = self._clock() + self.interval
        roots, active = kanban_sweep_inputs(kb)
        if not roots:
            return None
        return sweep_orphan_worktrees(roots, active_paths=active, max_age_seconds=self.max_age)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repos", nargs="*", help="repo roots (default: every board default_workdir)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-age-hours", type=float, default=24.0)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from hermes_cli import kanban_db as _kb
    roots, active = kanban_sweep_inputs(_kb)
    rep = sweep_orphan_worktrees(a.repos or roots, active_paths=active,
                                 max_age_seconds=a.max_age_hours * 3600, dry_run=a.dry_run)
    for k in ("removed", "reclaimed"):
        for p in getattr(rep, k):
            print(f"{k}: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
