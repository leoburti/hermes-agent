"""Shared pnpm store for kanban workers (SRLMM005 2026-10-22).

Every kanban worktree ran its own ``pnpm install`` and ended up with ~1.9 GB of
node_modules whose files had link count 1 (not shared with the store), so 53
worktrees held 46 GB. The dispatcher now pins every worker to ONE global pnpm
store and an import method that shares file data with it, so a fresh worktree
costs tens of MB of directory entries instead of GBs.

Applied through ``npm_config_*`` environment variables (pnpm reads them with
higher precedence than project ``.npmrc`` files), so nothing is written to the
repos or to the user's ``~/.npmrc``. A variable already present in the
dispatcher environment always wins (operator override).

Config (``kanban.*`` in config.yaml, all optional):
  pnpm_shared_store             (default true)  -> false disables this module
  pnpm_store_dir                (default: pnpm's platform default store)
  pnpm_package_import_method    (default "hardlink"; "clone-or-copy" for APFS/btrfs CoW)
  pnpm_global_virtual_store     (default false) -> pnpm >= 10.12 enable-global-virtual-store
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import MutableMapping, Optional

logger = logging.getLogger(__name__)

VALID_IMPORT_METHODS = ("auto", "hardlink", "copy", "clone", "clone-or-copy")
DEFAULT_IMPORT_METHOD = "hardlink"


def _truthy(v, default: bool) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


def default_store_dir(home: Optional[str] = None) -> str:
    """pnpm's own default store location, so the existing store is reused."""
    home = home or os.path.expanduser("~")
    if os.environ.get("PNPM_HOME"):
        return str(Path(os.environ["PNPM_HOME"]) / "store")
    if sys.platform == "darwin":
        return str(Path(home) / "Library" / "pnpm" / "store")
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or str(Path(home) / "AppData" / "Local")
        return str(Path(local) / "pnpm" / "store")
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path(home) / ".local" / "share")
    return str(Path(xdg) / "pnpm" / "store")


def apply_pnpm_store_env(env: MutableMapping[str, str], kanban_cfg: Optional[dict] = None) -> MutableMapping[str, str]:
    """Add shared-store pnpm settings to a worker *env* in place (and return it)."""
    cfg = kanban_cfg or {}
    if not _truthy(cfg.get("pnpm_shared_store"), True):
        return env
    store = str(cfg.get("pnpm_store_dir") or "").strip() or default_store_dir(env.get("HOME"))
    method = str(cfg.get("pnpm_package_import_method") or DEFAULT_IMPORT_METHOD).strip()
    if method not in VALID_IMPORT_METHODS:
        logger.warning("kanban: invalid kanban.pnpm_package_import_method=%r; using %s",
                       method, DEFAULT_IMPORT_METHOD)
        method = DEFAULT_IMPORT_METHOD
    wanted = {
        "npm_config_store_dir": os.path.expanduser(store),
        "npm_config_package_import_method": method,
        # Same store for every worktree -> never re-download into a per-tree cache.
        "npm_config_prefer_offline": "true",
    }
    if _truthy(cfg.get("pnpm_global_virtual_store"), False):
        wanted["npm_config_enable_global_virtual_store"] = "true"
    for key, value in wanted.items():
        # Respect explicit operator settings in either case spelling.
        if key in env or key.upper() in env:
            continue
        env[key] = value
    return env


def load_kanban_cfg() -> dict:
    try:
        from hermes_cli.config import load_config
        return (load_config() or {}).get("kanban", {}) or {}
    except Exception:
        return {}
