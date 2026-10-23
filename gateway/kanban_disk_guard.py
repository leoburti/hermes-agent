"""Disk-space guard for the embedded kanban dispatcher.

Incident SRLMM005 2026-10-21 11:10: the data volume hit ENOSPC, the stderr
timestamp wrapper died writing gateway.error.log, launchd tore the gateway
down with SIGTERM and every running worker was lost. This guard stops new
worker spawns before the volume is full, while reclaim/promote keep running.

Config (``kanban.*`` in config.yaml, all optional):
  disk_guard_min_free_gib  (default 15)  -> below this, dispatch with max_spawn=0
  disk_guard_pause_free_gib (default 8)  -> below this, skip the whole dispatch tick
                                            (no claims/promotions); running workers continue
  disk_guard_alert_command (default unset) -> argv list run (no shell) on every state
                                            change and rate-limited alert; message in
                                            $HERMES_DISK_GUARD_MESSAGE, state in $HERMES_DISK_GUARD_STATE
  disk_guard_alert_pct     (default 90)  -> at/above this used %, log a warning
  disk_guard_path          (default HERMES_HOME) -> volume to measure
Set disk_guard_min_free_gib: 0 / disk_guard_pause_free_gib: 0 to disable each level.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_GIB = 1024 ** 3
DEFAULT_MIN_FREE_GIB = 15.0
DEFAULT_PAUSE_FREE_GIB = 8.0
DEFAULT_ALERT_PCT = 90.0

STATE_OK = "ok"
STATE_NO_SPAWN = "no_spawn"
STATE_PAUSED = "paused"
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
        self.pause_free_gib = _float_setting(cfg, "disk_guard_pause_free_gib", DEFAULT_PAUSE_FREE_GIB)
        self.alert_pct = _float_setting(cfg, "disk_guard_alert_pct", DEFAULT_ALERT_PCT)
        cmd = cfg.get("disk_guard_alert_command")
        self.alert_command = [str(c) for c in cmd] if isinstance(cmd, (list, tuple)) and cmd else None
        self.path = str(cfg.get("disk_guard_path") or os.environ.get("HERMES_HOME") or os.path.expanduser("~"))
        self._usage = usage
        self._clock = clock
        self._last_alert: Optional[float] = None
        self._blocking = False
        self._state = STATE_OK
        self._last_state_at: float = clock()
        self._run = subprocess.run

    def measure(self) -> Optional[DiskStatus]:
        try:
            u = self._usage(self.path)
        except OSError as exc:  # unreadable volume: never block on a failed measurement
            logger.warning("kanban disk guard: cannot measure %s (%s); not blocking spawns", self.path, exc)
            return None
        return DiskStatus(self.path, int(u.free), int(u.total))

    def _alert(self, state: str, message: str) -> None:
        if not self.alert_command:
            return
        env = dict(os.environ, HERMES_DISK_GUARD_STATE=state, HERMES_DISK_GUARD_MESSAGE=message)
        try:
            self._run(self.alert_command, env=env, timeout=30, check=False,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:  # an alert hook must never break dispatch
            logger.warning("kanban disk guard: alert command failed: %s", exc)

    def state(self) -> str:
        """``ok`` | ``no_spawn`` (no new workers) | ``paused`` (skip the tick).
        Logs + alerts on every transition and, while degraded, every 30 min."""
        st = self.measure()
        if st is None:
            return STATE_OK
        now = self._clock()
        if self.pause_free_gib > 0 and st.free_gib < self.pause_free_gib:
            new = STATE_PAUSED
        elif self.min_free_gib > 0 and st.free_gib < self.min_free_gib:
            new = STATE_NO_SPAWN
        else:
            new = STATE_OK
        msg = (f"kanban disk guard: {st.path} {st.free_gib:.1f} GiB free ({st.used_pct:.1f}% used); "
               f"state={new} (no-spawn < {self.min_free_gib:g} GiB, pause < {self.pause_free_gib:g} GiB)")
        if new != self._state:
            level = logging.CRITICAL if new == STATE_PAUSED else (logging.ERROR if new == STATE_NO_SPAWN else logging.WARNING)
            logger.log(level, "%s [was %s]", msg, self._state)
            self._alert(new, msg)
            self._state, self._last_state_at = new, now
            self._last_alert = now
        elif (new != STATE_OK or st.used_pct >= self.alert_pct) and (
                self._last_alert is None or now - self._last_alert >= ALERT_REPEAT_SECONDS):
            self._last_alert = now
            logger.warning(msg)
            if new != STATE_OK:
                self._alert(new, msg)
        self._blocking = new != STATE_OK
        return new

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
