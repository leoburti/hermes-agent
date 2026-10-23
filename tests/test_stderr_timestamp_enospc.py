import errno
import io

from hermes_cli import stderr_timestamp as st


class FullDisk(io.StringIO):
    def write(self, s):
        raise OSError(errno.ENOSPC, "No space left on device")


def test_write_drops_line_on_enospc_and_warns_once(monkeypatch):
    warnings = []
    monkeypatch.setattr(st, "_WRITE_FAILURE_REPORTED", False)
    monkeypatch.setattr(st.os, "write", lambda fd, b: warnings.append(b) or len(b))
    f = FullDisk()
    st._write_timestamped_line(f, "a\n")
    st._write_timestamped_line(f, "b\n")
    assert len(warnings) == 1 and b"dropping lines" in warnings[0]


def test_logging_treats_enospc_as_unavailable():
    import hermes_logging
    assert hermes_logging._is_unavailable_log_stream(OSError(errno.ENOSPC, "full"))
    assert hermes_logging._is_unavailable_log_stream(OSError(errno.EIO, "io"))
    assert not hermes_logging._is_unavailable_log_stream(OSError(errno.EACCES, "perm"))
