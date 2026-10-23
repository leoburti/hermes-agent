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


def test_state_levels_and_transitions_alert(caplog):
    free = [40.0]
    calls = []
    g = DiskGuard({"disk_guard_path": "/x", "disk_guard_alert_command": ["notify", "--x"]},
                  usage=lambda p: _usage(free[0])(p))
    g._run = lambda argv, env, **kw: calls.append((argv, env["HERMES_DISK_GUARD_STATE"]))
    assert g.state() == "ok" and calls == []
    free[0] = 12
    with caplog.at_level(logging.WARNING):
        assert g.state() == "no_spawn"
    free[0] = 5
    with caplog.at_level(logging.CRITICAL):
        assert g.state() == "paused"
    assert "state=paused" in caplog.text
    free[0] = 30
    assert g.state() == "ok"
    assert [s for _a, s in calls] == ["no_spawn", "paused", "ok"]
    assert calls[0][0] == ["notify", "--x"]


def test_paused_reminder_rate_limited_and_alert_failure_tolerated():
    t = [0.0]
    g = DiskGuard({"disk_guard_path": "/x", "disk_guard_alert_command": ["x"]}, usage=_usage(3), clock=lambda: t[0])
    n = []

    def run(*a, **k):
        n.append(1)
        raise OSError("boom")
    g._run = run
    assert g.state() == "paused"
    t[0] = 60; g.state()
    assert len(n) == 1
    t[0] = 60 + 1801; g.state()
    assert len(n) == 2


def test_pause_zero_disables_pause():
    assert DiskGuard({"disk_guard_pause_free_gib": 0}, usage=_usage(1)).state() == "no_spawn"


def test_dispatcher_skips_tick_when_paused(monkeypatch):
    from gateway import kanban_watchers_dispatcher as m
    called = []
    monkeypatch.setattr(m, "_kbd", lambda: SimpleNamespace(dispatch_once=lambda *a, **k: called.append(1)))
    settings = m._DispatcherSettings(interval=60, max_spawn=None, max_in_progress=4, failure_limit=2,
                                     stale_timeout_seconds=0, reconcile_orphans=True, default_assignee=None,
                                     max_in_progress_per_profile=None)
    d = m._KanbanDispatcher(SimpleNamespace(), settings, disk_guard=SimpleNamespace(state=lambda: "paused"))
    monkeypatch.setattr(d, "_quarantine_lifted", lambda s, f: True)
    monkeypatch.setattr(d, "board_db_fingerprint", lambda s: ("p", None, None))
    assert d.tick_once_for_board("plano-h") is None and called == []
