"""The process source: the real psutil wrapper (on this test process only) and the static fake."""
import os
import sys

import pytest

from procs import ProcInfo, PsutilSource, StaticSource


def test_psutil_source_sees_this_process():
    pytest.importorskip("psutil")
    import psutil
    source = PsutilSource()
    assert source.available and source.platform == sys.platform
    me = next(p for p in source.processes() if p.pid == os.getpid())
    assert me.cmdline and me.create_time > 0
    assert me.ppid == os.getppid()
    env = source.environ(os.getpid())
    assert env is not None and "PATH" in {k.upper() for k in env}
    assert os.path.samefile(source.cwd(os.getpid()), os.getcwd())
    assert source.alive(os.getpid(), psutil.Process().create_time())
    assert not source.alive(os.getpid(), 0.0)  # same pid, different start: a reused pid


def test_psutil_source_answers_none_for_a_missing_process():
    pytest.importorskip("psutil")
    source = PsutilSource()
    gone = 2 ** 22 + 12345  # far above any real pid
    assert source.environ(gone) is None and source.cwd(gone) is None and not source.alive(gone, 0.0)


def test_static_source():
    p = ProcInfo(pid=7, ppid=1, name="x", cmdline=["x", "-y"], create_time=5.0)
    source = StaticSource([p], environ={7: {"A": "1"}, 8: PermissionError("denied")}, cwd={7: "/w"})
    assert source.processes() == [p] and source.environ(7) == {"A": "1"} and source.cwd(7) == "/w"
    assert source.alive(7, 5.5) and not source.alive(7, 60.0) and not source.alive(9, 5.0)
    with pytest.raises(PermissionError):
        source.environ(8)
    assert repr(p) == "ProcInfo(pid=7, ppid=1, name='x')"
