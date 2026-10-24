"""Where Hermes puts linked git worktrees, and the safety guard for an external root.

Unset ``HERMES_WORKTREE_ROOT`` (the default) keeps the historical layout
``<repo>/.worktrees/<name>`` exactly. When it is set, trees go under
``$HERMES_WORKTREE_ROOT/<repo-name>-<hash>/.worktrees/<name>``: the per-repo
``<name>-<hash>`` bucket keeps repos with the same basename apart, and the
trailing ``.worktrees`` segment keeps every ``parent.name == ".worktrees"``
heuristic elsewhere in the codebase working.

Before a tree is created under an external root, :func:`ensure_worktree_root_ready`
checks that the root exists, is a mounted volume distinct from the home disk
(unless ``HERMES_WORKTREE_REQUIRE_MOUNT=0``), is writable, and has at least
``HERMES_WORKTREE_MIN_FREE_GB`` (default 20) free. On any failure it raises
:class:`WorktreeRootError`; it never falls back to the internal disk, never
creates the root itself, and never deletes anything.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

ENV_ROOT = "HERMES_WORKTREE_ROOT"
ENV_MIN_FREE_GB = "HERMES_WORKTREE_MIN_FREE_GB"
ENV_REQUIRE_MOUNT = "HERMES_WORKTREE_REQUIRE_MOUNT"
DEFAULT_MIN_FREE_GB = 20.0


class WorktreeRootError(RuntimeError):
    """HERMES_WORKTREE_ROOT is set but not safe to create a worktree under."""


def configured_root() -> Optional[Path]:
    raw = (os.environ.get(ENV_ROOT) or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _bucket(repo_root: str | os.PathLike) -> str:
    real = os.path.realpath(str(repo_root))
    name = os.path.basename(real.rstrip(os.sep)) or "repo"
    return f"{name}-{hashlib.sha256(real.encode('utf-8', 'surrogateescape')).hexdigest()[:8]}"


def worktrees_dir_for(repo_root: str | os.PathLike) -> Path:
    """The directory holding Hermes-managed worktrees for ``repo_root``."""
    root = configured_root()
    if root is None:
        return Path(repo_root) / ".worktrees"
    return root / _bucket(repo_root) / ".worktrees"


def _min_free_bytes() -> int:
    raw = (os.environ.get(ENV_MIN_FREE_GB) or "").strip()
    try:
        gb = float(raw) if raw else DEFAULT_MIN_FREE_GB
    except ValueError:
        raise WorktreeRootError(f"{ENV_MIN_FREE_GB}={raw!r} is not a number")
    if gb < 0:
        raise WorktreeRootError(f"{ENV_MIN_FREE_GB}={raw!r} must be >= 0")
    return int(gb * (1024 ** 3))


def _require_mount() -> bool:
    return (os.environ.get(ENV_REQUIRE_MOUNT) or "1").strip().lower() not in {"0", "false", "no", "off"}


def ensure_worktree_root_ready() -> None:
    """Raise :class:`WorktreeRootError` unless the external root is safe; no-op when unset."""
    root = configured_root()
    if root is None:
        return
    if not root.is_absolute():
        raise WorktreeRootError(f"{ENV_ROOT}={str(root)!r} must be an absolute path")
    if not root.is_dir():
        raise WorktreeRootError(
            f"{ENV_ROOT}={str(root)!r} does not exist or is not a directory "
            "(is the volume mounted?); refusing to create a worktree (no fallback to the internal disk)"
        )
    if _require_mount():
        try:
            same_dev = root.stat().st_dev == Path.home().stat().st_dev
        except OSError as exc:
            raise WorktreeRootError(f"cannot stat {ENV_ROOT}={str(root)!r}: {exc}")
        if same_dev:
            raise WorktreeRootError(
                f"{ENV_ROOT}={str(root)!r} is on the same filesystem as the home directory "
                "(volume not mounted?); refusing to create a worktree. "
                f"Set {ENV_REQUIRE_MOUNT}=0 to allow a same-disk root."
            )
    if not os.access(root, os.W_OK | os.X_OK):
        raise WorktreeRootError(f"{ENV_ROOT}={str(root)!r} is not writable; refusing to create a worktree")
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix=".hermes-wtroot-probe-"):
            pass
    except OSError as exc:
        raise WorktreeRootError(f"{ENV_ROOT}={str(root)!r} is not writable ({exc}); refusing to create a worktree")
    need = _min_free_bytes()
    free = shutil.disk_usage(root).free
    if free < need:
        raise WorktreeRootError(
            f"{ENV_ROOT}={str(root)!r} has {free / 1024 ** 3:.1f} GiB free, below the "
            f"{need / 1024 ** 3:.1f} GiB minimum ({ENV_MIN_FREE_GB}); refusing to create a worktree"
        )


def prepare_worktrees_dir(repo_root: str | os.PathLike) -> Path:
    """Guard (when an external root is set), then create and return the worktrees dir."""
    ensure_worktree_root_ready()
    d = worktrees_dir_for(repo_root)
    d.mkdir(parents=True, exist_ok=True)
    return d
