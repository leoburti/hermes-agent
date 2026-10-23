"""Disk-space guard for the embedded kanban dispatcher.

Incident SRLMM005 2026-10-21 11:10: the data volume hit ENOSPC, the stderr
timestamp wrapper died writing gateway.error.log, launchd tore the gateway
down with SIGTERM and every running worker was lost. This guard stops new
worker spawns before the volume is full, while reclaim/promote keep running.

Config (``kanban.*`` in config.yaml, all optional):
  disk_guard_min_free_gib  (default 15)  -> below this, dispatch with max_spawn=0
  disk_guard_alert_pct     (default 90)  -> at/above this used %, log a warning
  disk_guard_path          (default HERMES_HOME) -> volume to measure
Set disk_guard_min_free_gib: 0 to disable the spawn block.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_GIB = 1024 ** 3
DEFAULT_MIN_FREE_GIB = 15.0
DEFAULT_ALERT_PCT = 90.0
ALERT_REPEAT_SECONDS = 1800


@dataclass(frozen=True)
class DiskStatus:
    path: str
    free_bytes: int
    total_bytes: int

    @property
    def used_pct(self) -> float:
        return 0.0 if self.total_bytes <= 0 else 100.0 * (self.total_bytes - self.free_bytes) / self.total_bytes

    @property
    def free_gib(self) -> float:
        return self.free_bytes / _GIB


def _float_setting(cfg: dict, key: str, default: float) -> float:
    raw = cfg.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("kanban disk guard: invalid kanban.%s=%r; using %s", key, raw, default)
        return default
    return max(value, 0.0)


class DiskGuard:
    def __init__(self, kanban_cfg: Optional[dict] = None, *,
                 usage: Callable[[str], object] = shutil.disk_usage,
                 clock: Callable[[], float] = time.monotonic) -> None:
        cfg = kanban_cfg or {}
        self.min_free_gib = _float_setting(cfg, "disk_guard_min_free_gib", DEFAULT_MIN_FREE_GIB)
        self.alert_pct = _float_setting(cfg, "disk_guard_alert_pct", DEFAULT_ALERT_PCT)
        self.path = str(cfg.get("disk_guard_path") or os.environ.get("HERMES_HOME") or os.path.expanduser("~"))
        self._usage = usage
        self._clock = clock
        self._last_alert: Optional[float] = None
        self._blocking = False

    def measure(self) -> Optional[DiskStatus]:
        try:
            u = self._usage(self.path)
        except OSError as exc:  # unreadable volume: never block on a failed measurement
            logger.warning("kanban disk guard: cannot measure %s (%s); not blocking spawns", self.path, exc)
            return None
        return DiskStatus(self.path, int(u.free), int(u.total))

    def spawn_allowed(self) -> bool:
        """True when new workers may be spawned this tick. Logs alerts as a side effect."""
        st = self.measure()
        if st is None:
            return True
        now = self._clock()
        if st.used_pct >= self.alert_pct and (self._last_alert is None or now - self._last_alert >= ALERT_REPEAT_SECONDS):
            self._last_alert = now
            logger.warning("kanban disk guard: %s at %.1f%% used (%.1f GiB free; alert >= %.0f%%)",
                           st.path, st.used_pct, st.free_gib, self.alert_pct)
        blocked = self.min_free_gib > 0 and st.free_gib < self.min_free_gib
        if blocked and not self._blocking:
            logger.error("kanban disk guard: %.1f GiB free on %s < %.1f GiB; pausing new worker spawns "
                         "(running workers, reclaim and promote continue)", st.free_gib, st.path, self.min_free_gib)
        elif not blocked and self._blocking:
            logger.warning("kanban disk guard: %.1f GiB free on %s; resuming worker spawns", st.free_gib, st.path)
        self._blocking = blocked
        return not blocked
