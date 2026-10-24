"""Windows process plumbing for the launcher: owned children that cannot outlive it.

* ``JobObject`` — a kill-on-close job. Every child the launcher starts is put in it
  before its first instruction runs (created suspended, assigned, then resumed), so the
  child and anything it spawns die with the launcher, even when the launcher is killed
  or its window closed. Nothing outside the job is ever touched.
* ``StopEvent`` — a named event in this logon session. The running launcher owns it
  (it doubles as the single-instance check); ``python -m src.launcher stop`` sets it.
* ``process_alive`` — pid + creation time, so a recycled pid is never mistaken for ours.

On other platforms the job and the event degrade to no-ops/None: the launcher targets
Windows, and its tests that need the real objects are skipped elsewhere.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200

if IS_WINDOWS:
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    _ntdll = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined,unused-ignore]

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JobObjectExtendedLimitInformation = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SUSPEND_RESUME = 0x0800
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SYNCHRONIZE = 0x00100000
    _EVENT_MODIFY_STATE = 0x0002
    _ERROR_ALREADY_EXISTS = 183
    _STILL_ACTIVE = 259
    _WAIT_OBJECT_0 = 0

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimit),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _FileTime(ctypes.Structure):
        _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                               wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _JobObjectBasicProcessIdList = 3
    _MAX_JOB_PIDS = 256

    class _PidList(ctypes.Structure):
        _fields_ = [("NumberOfAssignedProcesses", wintypes.DWORD),
                    ("NumberOfProcessIdsInList", wintypes.DWORD),
                    ("ProcessIdList", ctypes.c_size_t * _MAX_JOB_PIDS)]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(_FileTime)] * 4
    _k32.CreateEventW.restype = wintypes.HANDLE
    _k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.OpenEventW.restype = wintypes.HANDLE
    _k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.SetEvent.argtypes = [wintypes.HANDLE]
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long


def _winerror(what: str) -> OSError:
    return ctypes.WinError(ctypes.get_last_error(), what)  # type: ignore[attr-defined,no-any-return,unused-ignore]


class JobObject:
    """Kill-on-close job holding every process the launcher owns."""

    def __init__(self) -> None:
        self._handle: int | None = None
        if not IS_WINDOWS:
            return
        handle = _k32.CreateJobObjectW(None, None)
        if not handle:
            raise _winerror("CreateJobObjectW")
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(handle, _JobObjectExtendedLimitInformation,
                                            ctypes.byref(info), ctypes.sizeof(info)):
            err = _winerror("SetInformationJobObject")
            _k32.CloseHandle(handle)
            raise err
        self._handle = handle

    def spawn(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str],
              log_path: Path) -> subprocess.Popen[bytes]:
        """Start ``args`` inside the job, output appended to ``log_path``.

        The child gets its own process group (so console Ctrl+C reaches only the
        launcher, which then stops children in order) and starts suspended until it is
        in the job, so nothing it spawns can escape.
        """
        log_path.parent.mkdir(parents=True, exist_ok=True)
        flags = (CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED) if IS_WINDOWS else 0
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, never a shell
                list(args), cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, creationflags=flags,
                start_new_session=not IS_WINDOWS,
            )
        if IS_WINDOWS:
            try:
                self._assign_and_resume(proc.pid)
            except OSError:
                proc.kill()
                raise
        return proc

    def _assign_and_resume(self, pid: int) -> None:
        h = _k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_SUSPEND_RESUME, False, pid)
        if not h:
            raise _winerror("OpenProcess")
        try:
            if not _k32.AssignProcessToJobObject(self._handle, h):
                raise _winerror("AssignProcessToJobObject")
            status = _ntdll.NtResumeProcess(h)
            if status != 0:
                raise OSError(f"NtResumeProcess failed (NTSTATUS 0x{status & 0xFFFFFFFF:08X})")
        finally:
            _k32.CloseHandle(h)

    def pids(self) -> set[int]:
        """Every process now in the job: children and whatever they spawned (a venv's
        python.exe is a redirector that starts the real interpreter as its child)."""
        if not self._handle:
            return set()
        info = _PidList()
        if not _k32.QueryInformationJobObject(self._handle, _JobObjectBasicProcessIdList,
                                              ctypes.byref(info), ctypes.sizeof(info), None):
            return set()
        return {int(info.ProcessIdList[i]) for i in range(info.NumberOfProcessIdsInList)}

    def terminate_all(self) -> None:
        if self._handle:
            _k32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        """Closing the last handle kills whatever is still in the job."""
        if self._handle:
            _k32.CloseHandle(self._handle)
            self._handle = None


# The launcher names a stop event for each child it starts and passes the name in this
# environment variable; the backend watches it (src/api/stop_signal.py). A name, not the
# child's pid: a venv's python.exe is a redirector whose pid is not the interpreter's.
STOP_EVENT_ENV = "FYJ_STOP_EVENT"


def request_graceful_stop(proc: subprocess.Popen[bytes], stop_event: str | None = None) -> None:
    """Ask a child started by ``JobObject.spawn`` to shut down (Ctrl+Break / SIGTERM)."""
    if proc.poll() is not None:
        return
    if IS_WINDOWS and stop_event:
        # Without a shared console (the tray app runs under pythonw) Ctrl+Break cannot
        # reach the child; the backend also watches this event and stops the same way.
        StopEvent(stop_event).signal_existing()
    try:
        if IS_WINDOWS:
            # Reaches the child's own process group only; uvicorn and node treat
            # Ctrl+Break as a shutdown request. Needs the shared console.
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        else:
            proc.terminate()
    except OSError:
        pass


def process_creation_time(pid: int) -> int | None:
    """Creation time (100 ns ticks) of a live process, or None if it is gone/unreadable."""
    if not IS_WINDOWS:
        try:
            return int(Path(f"/proc/{pid}/stat").read_text().split()[21])
        except (OSError, IndexError, ValueError):
            return None
    h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        code = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
            return None
        created, exited, kernel, user = _FileTime(), _FileTime(), _FileTime(), _FileTime()
        if not _k32.GetProcessTimes(h, ctypes.byref(created), ctypes.byref(exited),
                                    ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return int((created.hi << 32) | created.lo)
    finally:
        _k32.CloseHandle(h)


def process_alive(pid: int, created: int | None) -> bool:
    """True only if ``pid`` is running AND is the same process (same creation time)."""
    now = process_creation_time(pid)
    return now is not None and (created is None or now == created)


if IS_WINDOWS:
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
    _SE_FILE_OBJECT = 1
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.LocalFree.argtypes = [ctypes.c_void_p]
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    _advapi32.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                                                    ctypes.POINTER(ctypes.c_void_p),
                                                    ctypes.POINTER(wintypes.BOOL)]
    _advapi32.SetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                                                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                                ctypes.c_void_p]
    _advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD


def current_user_sid() -> str | None:
    """The SID string of the account running this process (Windows), else None."""
    if not IS_WINDOWS:
        return None
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(_k32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        needed = wintypes.DWORD(0)
        _advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
        buf = ctypes.create_string_buffer(needed.value)
        if not needed.value or not _advapi32.GetTokenInformation(token, _TOKEN_USER, buf, needed,
                                                                 ctypes.byref(needed)):
            return None
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid
        text = ctypes.c_void_p()
        if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)) or not text.value:
            return None
        try:
            return ctypes.wstring_at(text.value)
        finally:
            _k32.LocalFree(text)
    finally:
        _k32.CloseHandle(token)


def _restrict_to_current_user(path: Path) -> None:
    """Replace the file's DACL with a protected one: this account and SYSTEM only."""
    sid = current_user_sid()
    if sid is None:
        raise OSError("cannot read the current account's SID")
    sd = ctypes.c_void_p()
    if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"D:P(A;;FA;;;{sid})(A;;FA;;;SY)", 1, ctypes.byref(sd), None):
        raise _winerror("ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        if not _advapi32.GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(dacl),
                                                   ctypes.byref(defaulted)):
            raise _winerror("GetSecurityDescriptorDacl")
        rc = _advapi32.SetNamedSecurityInfoW(
            str(path), _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION, None, None, dacl, None)
        if rc != 0:
            raise ctypes.WinError(rc)  # type: ignore[attr-defined,unused-ignore]
    finally:
        _k32.LocalFree(sd)


def write_private_text(path: Path, text: str) -> None:
    """Atomically write ``text`` to a file only this OS account can read (and SYSTEM, on Windows).

    The temporary file is restricted *before* anything is written to it, and the rename keeps its
    DACL, so the content is never readable by another account, not even for a moment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.unlink()
    except OSError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if IS_WINDOWS:
            _restrict_to_current_user(tmp)
        os.write(fd, text.encode("utf-8"))
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.replace(tmp, path)


class StopEvent:
    """Named, session-local event: owned by the running launcher, set by ``stop``."""

    def __init__(self, name: str) -> None:
        self.name = f"Local\\{name}"
        self._handle: int | None = None
        self.already_existed = False

    def create(self) -> bool:
        """Create (and own) the event. False if another launcher already owns it."""
        if sys.platform != "win32":
            return True
        ctypes.set_last_error(0)
        handle = _k32.CreateEventW(None, True, False, self.name)
        if not handle:
            raise _winerror("CreateEventW")
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            _k32.CloseHandle(handle)
            self.already_existed = True
            return False
        self._handle = handle
        return True

    def wait(self, timeout_s: float) -> bool:
        """True once the event is set (a stop was requested); sleeps elsewhere."""
        if not IS_WINDOWS or not self._handle:
            time.sleep(timeout_s)
            return False
        return bool(_k32.WaitForSingleObject(self._handle, int(timeout_s * 1000)) == _WAIT_OBJECT_0)

    def signal_existing(self) -> bool:
        """Set the event owned by another launcher. False if none is running."""
        if not IS_WINDOWS:
            return False
        handle = _k32.OpenEventW(_EVENT_MODIFY_STATE | _SYNCHRONIZE, False, self.name)
        if not handle:
            return False
        try:
            return bool(_k32.SetEvent(handle))
        finally:
            _k32.CloseHandle(handle)

    def owner_stopping(self) -> bool:
        """True while another launcher owns the event and a stop has been requested of it."""
        if not IS_WINDOWS:
            return False
        handle = _k32.OpenEventW(_SYNCHRONIZE, False, self.name)
        if not handle:
            return False
        try:
            return bool(_k32.WaitForSingleObject(handle, 0) == _WAIT_OBJECT_0)
        finally:
            _k32.CloseHandle(handle)

    def close(self) -> None:
        if IS_WINDOWS and self._handle:
            _k32.CloseHandle(self._handle)
            self._handle = None
