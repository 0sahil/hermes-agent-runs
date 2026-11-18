"""The process list, behind a small interface so tests can inject fake processes.

`PsutilSource` is the real one. psutil is imported only when it is constructed, so the rest of the
backend (and the test suite) works without psutil installed.

Every lookup that can be refused by the operating system (another user's process, a protected
system process, a process that just exited) returns None instead of raising: one unreadable
process must never break the whole list.
"""
from __future__ import annotations

import sys
from typing import Any, Dict, Iterable, List, Optional, Protocol


class ProcInfo:
    """What one cheap pass over the process table tells us about a process.

    A plain class on purpose: `@dataclass` looks its module up in `sys.modules`, and Hermes loads
    this file by path without registering it there.
    """

    __slots__ = ("pid", "ppid", "name", "exe", "cmdline", "create_time")

    def __init__(self, pid: int, ppid: Optional[int] = None, name: Optional[str] = None,
                 exe: Optional[str] = None, cmdline: Optional[List[str]] = None, create_time: float = 0.0):
        self.pid = pid
        self.ppid = ppid
        self.name = name
        self.exe = exe
        self.cmdline = list(cmdline or [])
        self.create_time = create_time  # seconds since the epoch, as psutil reports it

    def __repr__(self) -> str:
        return f"ProcInfo(pid={self.pid}, ppid={self.ppid}, name={self.name!r})"


class ProcessSource(Protocol):
    """Where processes come from. Only `processes()` is called for every process; the rest is
    called for candidates only, because reading another process's environment is not free."""

    platform: str
    available: bool

    def processes(self) -> List[ProcInfo]: ...

    def environ(self, pid: int) -> Optional[Dict[str, str]]: ...

    def cwd(self, pid: int) -> Optional[str]: ...

    def alive(self, pid: int, create_time: float) -> bool: ...


class PsutilSource:
    """The real process table, read through psutil."""

    def __init__(self) -> None:
        self.platform = sys.platform
        try:
            import psutil  # noqa: PLC0415 - optional at import time on purpose
        except Exception:  # ImportError, or a broken native extension
            self._psutil = None
            self.available = False
            self.version: Optional[str] = None
        else:
            self._psutil = psutil
            self.available = True
            self.version = getattr(psutil, "__version__", None)

    def _quiet_errors(self) -> tuple:
        ps = self._psutil
        return (ps.AccessDenied, ps.NoSuchProcess, ps.ZombieProcess, OSError) if ps else (OSError,)

    def processes(self) -> List[ProcInfo]:
        if self._psutil is None:
            return []
        out: List[ProcInfo] = []
        attrs = ["pid", "ppid", "name", "exe", "cmdline", "create_time"]
        # ad_value=None: fields the OS refuses come back as None instead of raising.
        for proc in self._psutil.process_iter(attrs, ad_value=None):
            try:
                info = proc.info
            except self._quiet_errors():
                continue
            out.append(ProcInfo(
                pid=int(info.get("pid") or proc.pid),
                ppid=info.get("ppid"),
                name=info.get("name"),
                exe=info.get("exe"),
                cmdline=list(info.get("cmdline") or []),
                create_time=float(info.get("create_time") or 0.0),
            ))
        return out

    def _process(self, pid: int):
        if self._psutil is None:
            return None
        try:
            return self._psutil.Process(pid)
        except self._quiet_errors():
            return None

    def environ(self, pid: int) -> Optional[Dict[str, str]]:
        proc = self._process(pid)
        if proc is None:
            return None
        try:
            return dict(proc.environ())
        except self._quiet_errors():
            return None

    def cwd(self, pid: int) -> Optional[str]:
        proc = self._process(pid)
        if proc is None:
            return None
        try:
            return proc.cwd() or None
        except self._quiet_errors():
            return None

    def alive(self, pid: int, create_time: float) -> bool:
        proc = self._process(pid)
        if proc is None:
            return False
        try:
            # A matching start time proves it is the same process, not a reused pid.
            return proc.is_running() and abs(proc.create_time() - create_time) < 2.0
        except self._quiet_errors():
            return False


class StaticSource:
    """A fixed process table: used by the tests, and handy for trying the backend by hand."""

    def __init__(
        self,
        procs: Iterable[ProcInfo] = (),
        environ: Optional[Dict[int, Any]] = None,  # a value may be an exception, raised on read
        cwd: Optional[Dict[int, Optional[str]]] = None,
        platform: str = "linux",
    ) -> None:
        self.platform = platform
        self.available = True
        self.version = None
        self.procs: Dict[int, ProcInfo] = {p.pid: p for p in procs}
        self._environ = dict(environ or {})
        self._cwd = dict(cwd or {})

    def processes(self) -> List[ProcInfo]:
        return list(self.procs.values())

    def environ(self, pid: int) -> Optional[Dict[str, str]]:
        value = self._environ.get(pid)
        if isinstance(value, Exception):
            raise value
        return value

    def cwd(self, pid: int) -> Optional[str]:
        return self._cwd.get(pid)

    def alive(self, pid: int, create_time: float) -> bool:
        proc = self.procs.get(pid)
        return proc is not None and abs(proc.create_time - create_time) < 2.0
