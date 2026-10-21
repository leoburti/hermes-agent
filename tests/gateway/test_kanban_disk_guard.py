import errno
import logging
from types import SimpleNamespace

from gateway.kanban_disk_guard import DiskGuard

GIB = 1024 ** 3


def _usage(free_gib, total_gib=228):
    return lambda _p: SimpleNamespace(free=int(free_gib * GIB), total=int(total_gib * GIB))


def test_spawn_blocked_below_floor(caplog):
    g = DiskGuard({"disk_guard_path": "/x"}, usage=_usage(11))
    with caplog.at_level(logging.WARNING):
        assert g.spawn_allowed() is False
    assert "pausing new worker spawns" in caplog.text
    assert "95.2% used" in caplog.text  # alert at >= 90%


def test_spawn_allowed_above_floor_and_resume_logged(caplog):
    free = [10]
    g = DiskGuard({"disk_guard_path": "/x"}, usage=lambda p: _usage(free[0])(p))
    assert g.spawn_allowed() is False
    free[0] = 40
    with caplog.at_level(logging.WARNING):
        assert g.spawn_allowed() is True
    assert "resuming worker spawns" in caplog.text


def test_alert_rate_limited():
    t = [0.0]
    g = DiskGuard({"disk_guard_path": "/x", "disk_guard_min_free_gib": 0}, usage=_usage(20), clock=lambda: t[0])
    g.spawn_allowed(); first = g._last_alert
    t[0] = 10; g.spawn_allowed()
    assert g._last_alert == first


def test_zero_floor_disables_block_and_measure_error_never_blocks():
    assert DiskGuard({"disk_guard_min_free_gib": 0}, usage=_usage(1)).spawn_allowed() is True

    def boom(_p):
        raise OSError(errno.EIO, "io")
    assert DiskGuard({}, usage=boom).spawn_allowed() is True


def test_dispatcher_forces_max_spawn_zero(monkeypatch):
    from gateway import kanban_watchers_dispatcher as m
    seen = {}

    class FakeKbd:
        def dispatch_once(self, conn, board, **kw):
            seen.update(kw)
            return "ok"

    class FakeConn:
        def close(self):
            pass

    monkeypatch.setattr(m, "_kbd", lambda: FakeKbd())
    monkeypatch.setattr(m, "_kbc", lambda: SimpleNamespace(connect=lambda board: FakeConn()))
    settings = m._DispatcherSettings(interval=60, max_spawn=None, max_in_progress=4, failure_limit=2,
                                     stale_timeout_seconds=0, reconcile_orphans=True, default_assignee=None,
                                     max_in_progress_per_profile=None)
    d = m._KanbanDispatcher(SimpleNamespace(), settings, disk_guard=SimpleNamespace(spawn_allowed=lambda: False))
    monkeypatch.setattr(d, "_quarantine_lifted", lambda s, f: True)
    monkeypatch.setattr(d, "board_db_fingerprint", lambda s: ("p", None, None))
    assert d.tick_once_for_board("plano-h") == "ok"
    assert seen["max_spawn"] == 0 and seen["max_in_progress"] == 4
