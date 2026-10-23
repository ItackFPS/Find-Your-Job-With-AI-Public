"""Run a lab agent in a Windows AppContainer: an OS boundary that needs no administrator and no second account.

The in-process guard (``src/lab/guard.py``) is not a sandbox, and the separate-account setup of ADR 0011
(``scripts/lab_isolation.ps1``) needs an administrator. An AppContainer is the Windows sandbox that Edge
and UWP apps run in, and Microsoft services it as a security boundary. A process inside one runs as
the same user, but every file, registry or object access must *also* be granted to the container's
SID (or to ALL APPLICATION PACKAGES); ordinary user ACLs (you, Users, Authenticated Users) grant it
nothing. Without the ``internetClient`` capability it has no network at all. Even with it, the OS
refuses loopback connections from AppContainers unless an administrator exempts it
(``CheckNetIsolation LoopbackExempt``), so it cannot reach the application's API or a relay.

``grant`` gives the container exactly:

* read and execute on the application folder (the code, ``config/``, ``data/`` read-only) and on the
  Python installation it runs with;
* modify on ``data/lab/`` only (the lab database and the bundle store);
* nothing on secret files (``.env*`` except ``.env.example``, ``config/*.local.*``). Windows ignores a
  deny ACE for an AppContainer SID (measured), so they are closed by never being granted: the root and
  ``config/`` pass only a folder grant down, each other entry gets its own, and a file created there
  later starts closed.

Nothing else on the machine is granted, so the places you execute from (PATH, scheduled tasks, Startup,
Run keys, another drive's root) stay out of reach without listing them. ``run`` starts a command inside
the container; ``python -m src.lab isolation-check`` run that way probes it from the inside.

Limits, stated rather than hidden:

* the lab may read production data (``jobs.db``, ``config/``), which it needs for replays; it cannot
  write them;
* a file whose inheritance is disabled does not receive the read grant (it stays closed, which fails
  safe); so does a new top-level file until ``grant`` runs again;
* granting is ACL work on the user's own files and needs no elevation; ``revoke`` removes exactly the
  ACEs ``grant`` added (they name the container's SID only).
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

DEFAULT_NAME = "FindYourJobWithAI.Lab"
INTERNET_CLIENT_SID = "S-1-15-3-1"            # well-known capability SID: outbound internet, never loopback
# the environment a sandboxed command keeps from yours; everything else (API keys, tokens) is dropped
KEEP_ENV = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "NUMBER_OF_PROCESSORS",
            "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "OS", "USERNAME", "USERDOMAIN")


class SandboxError(RuntimeError):
    pass


def supported() -> bool:
    return sys.platform == "win32"


def secret_files(root: Path) -> list[Path]:
    """Credential files the container must never read (the same set ``isolation-check`` probes)."""
    from src.lab.isolation import secret_files as probed

    return probed(root, root / "config")


def runtime_dirs(root: Path) -> list[Path]:
    """Python installation folders outside ``root`` that the sandboxed interpreter must read."""
    real_root = Path(os.path.realpath(root))
    out: list[Path] = []
    for p in {Path(os.path.realpath(sys.prefix)), Path(os.path.realpath(sys.base_prefix))}:
        if p != real_root and real_root not in p.parents and p not in out:
            out.append(p)
    return sorted(out)


if sys.platform == "win32":
    from ctypes import wintypes

    _S_OK = 0
    _HR_ALREADY_EXISTS = ctypes.c_long(0x800700B7).value
    _EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    _CREATE_UNICODE_ENVIRONMENT = 0x00000400
    _PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
    _PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
    _STARTF_USESTDHANDLES = 0x00000100
    _HANDLE_FLAG_INHERIT = 0x00000001
    _SE_GROUP_ENABLED = 0x00000004
    _INFINITE = 0xFFFFFFFF
    _TOKEN_QUERY = 0x0008
    _TOKEN_ELEVATION = 20
    _TOKEN_IS_APP_CONTAINER = 29

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _SecurityCapabilities(ctypes.Structure):
        _fields_ = [("AppContainerSid", ctypes.c_void_p), ("Capabilities", ctypes.POINTER(_SidAndAttributes)),
                    ("CapabilityCount", wintypes.DWORD), ("Reserved", wintypes.DWORD)]

    class _StartupInfo(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class _StartupInfoEx(ctypes.Structure):
        _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]

    class _ProcessInformation(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    _userenv = ctypes.WinDLL("userenv")
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _ole32 = ctypes.WinDLL("ole32")
    _userenv.CreateAppContainerProfile.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    _userenv.CreateAppContainerProfile.restype = ctypes.c_long
    _userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    _userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    _userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
    _userenv.DeleteAppContainerProfile.restype = ctypes.c_long
    _userenv.GetAppContainerFolderPath.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_wchar_p)]
    _userenv.GetAppContainerFolderPath.restype = ctypes.c_long
    _advapi32.FreeSid.argtypes = [ctypes.c_void_p]
    _advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    _advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    _kernel32.GetStdHandle.restype = wintypes.HANDLE
    _kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    _kernel32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                                            ctypes.POINTER(ctypes.c_size_t)]
    _kernel32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t,
                                                    ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                                    ctypes.c_void_p]
    _kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    _kernel32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                         wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                         ctypes.c_void_p, ctypes.POINTER(_ProcessInformation)]
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    def _token_dword(info_class: int) -> int:
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
            raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
        try:
            value, size = wintypes.DWORD(0), wintypes.DWORD(0)
            if not _advapi32.GetTokenInformation(token, info_class, ctypes.byref(value), ctypes.sizeof(value),
                                                 ctypes.byref(size)):
                raise OSError(ctypes.get_last_error(), "GetTokenInformation failed")
            return int(value.value)
        finally:
            _kernel32.CloseHandle(token)

    def in_app_container() -> bool:
        """Whether THIS process runs inside an AppContainer (read from its token, not from the environment)."""
        try:
            return bool(_token_dword(_TOKEN_IS_APP_CONTAINER))
        except OSError:
            return False

    def elevated() -> bool:
        try:
            return bool(_token_dword(_TOKEN_ELEVATION))
        except OSError:
            return True                                            # cannot tell: fail closed

    def _sid_string(sid: int) -> str:
        text = ctypes.c_wchar_p()
        if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)) or text.value is None:
            raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
        try:
            return text.value
        finally:
            _kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))

    def _profile_sid(name: str) -> int:
        """The container's SID, creating its profile on first use. Caller frees it with ``FreeSid``."""
        sid = ctypes.c_void_p()
        hr = _userenv.CreateAppContainerProfile(name, name, "Find Your Job With AI lab sandbox", None, 0,
                                                ctypes.byref(sid))
        if hr == _HR_ALREADY_EXISTS:
            hr = _userenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid))
        if hr != _S_OK or not sid.value:
            raise SandboxError(f"cannot create or open the AppContainer profile {name!r} (HRESULT {hr & 0xFFFFFFFF:#010x})")
        return sid.value

    def container_sid(name: str = DEFAULT_NAME) -> str:
        sid = _profile_sid(name)
        try:
            return _sid_string(sid)
        finally:
            _advapi32.FreeSid(sid)

    def container_folder(sid: str) -> Path:
        """The container's own writable folder (``%LOCALAPPDATA%\\Packages\\<name>\\AC``)."""
        path = ctypes.c_wchar_p()
        hr = _userenv.GetAppContainerFolderPath(sid, ctypes.byref(path))
        if hr != _S_OK or path.value is None:
            raise SandboxError(f"cannot locate the AppContainer folder (HRESULT {hr & 0xFFFFFFFF:#010x})")
        try:
            return Path(path.value)
        finally:
            _ole32.CoTaskMemFree(ctypes.cast(path, ctypes.c_void_p))

    def delete_profile(name: str = DEFAULT_NAME) -> None:
        _userenv.DeleteAppContainerProfile(name)

    def _command_line(argv: list[str]) -> str:
        return subprocess.list2cmdline(argv)

    def _environment_block(env: Mapping[str, str]) -> ctypes.Array[ctypes.c_wchar]:
        text = "".join(f"{k}={v}\0" for k, v in sorted(env.items(), key=lambda kv: kv[0].upper())) + "\0"
        return ctypes.create_unicode_buffer(text, len(text))

    def run(argv: list[str], cwd: str | Path, *, name: str = DEFAULT_NAME, internet: bool = False,
            env: Mapping[str, str] | None = None) -> int:
        """Run ``argv`` inside the AppContainer, wait for it and return its exit code.

        It shares this console's standard handles (and no other handle). Refused from an elevated
        process: the container must derive from an ordinary user token."""
        if not argv:
            raise SandboxError("nothing to run")
        if elevated():
            raise SandboxError("refusing to start the sandbox from an elevated (administrator) process")
        if in_app_container():
            raise SandboxError("already inside an AppContainer")
        sid = _profile_sid(name)
        cap_sid = ctypes.c_void_p()
        attrs = None
        pi = _ProcessInformation()
        try:
            caps = (_SidAndAttributes * 1)()
            count = 0
            if internet:
                if not _advapi32.ConvertStringSidToSidW(INTERNET_CLIENT_SID, ctypes.byref(cap_sid)):
                    raise OSError(ctypes.get_last_error(), "ConvertStringSidToSidW failed")
                caps[0] = _SidAndAttributes(cap_sid.value, _SE_GROUP_ENABLED)
                count = 1
            sc = _SecurityCapabilities(sid, caps if count else None, count, 0)
            handles = []
            for std in (-10, -11, -12):                         # STD_INPUT/OUTPUT/ERROR_HANDLE
                h = _kernel32.GetStdHandle(std & 0xFFFFFFFF)
                if h and h != wintypes.HANDLE(-1).value and h not in handles:
                    _kernel32.SetHandleInformation(h, _HANDLE_FLAG_INHERIT, _HANDLE_FLAG_INHERIT)
                    handles.append(h)
            handle_list = (wintypes.HANDLE * max(1, len(handles)))(*handles)
            size = ctypes.c_size_t(0)
            _kernel32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
            attrs = ctypes.create_string_buffer(size.value)
            if not _kernel32.InitializeProcThreadAttributeList(attrs, 2, 0, ctypes.byref(size)):
                raise OSError(ctypes.get_last_error(), "InitializeProcThreadAttributeList failed")
            if not _kernel32.UpdateProcThreadAttribute(attrs, 0, _PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                                                       ctypes.byref(sc), ctypes.sizeof(sc), None, None):
                raise OSError(ctypes.get_last_error(), "UpdateProcThreadAttribute (capabilities) failed")
            if handles and not _kernel32.UpdateProcThreadAttribute(
                    attrs, 0, _PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handle_list,
                    ctypes.sizeof(wintypes.HANDLE) * len(handles), None, None):
                raise OSError(ctypes.get_last_error(), "UpdateProcThreadAttribute (handles) failed")
            si = _StartupInfoEx()
            si.StartupInfo.cb = ctypes.sizeof(si)
            si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
            if len(handles) == 3:
                si.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
                si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput, si.StartupInfo.hStdError = handles
            cmd = ctypes.create_unicode_buffer(_command_line(argv))
            block = _environment_block(env if env is not None else sandbox_env(container_folder(_sid_string(sid))))
            if not _kernel32.CreateProcessW(None, cmd, None, None, bool(handles),
                                            _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT,
                                            block, str(cwd), ctypes.byref(si), ctypes.byref(pi)):
                raise OSError(ctypes.get_last_error(), f"CreateProcessW failed for {argv[0]}")
            _kernel32.WaitForSingleObject(pi.hProcess, _INFINITE)
            code = wintypes.DWORD(0)
            _kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
            return int(code.value)
        finally:
            for h in (pi.hThread, pi.hProcess):
                if h:
                    _kernel32.CloseHandle(h)
            if attrs is not None:
                _kernel32.DeleteProcThreadAttributeList(attrs)
            if cap_sid.value:
                _kernel32.LocalFree(cap_sid)
            _advapi32.FreeSid(sid)
else:
    def in_app_container() -> bool:
        return False

    def elevated() -> bool:
        return os.geteuid() == 0

    def container_sid(name: str = DEFAULT_NAME) -> str:
        raise SandboxError("the AppContainer sandbox exists on Windows only")

    def container_folder(sid: str) -> Path:
        raise SandboxError("the AppContainer sandbox exists on Windows only")

    def delete_profile(name: str = DEFAULT_NAME) -> None:
        raise SandboxError("the AppContainer sandbox exists on Windows only")

    def run(argv: list[str], cwd: str | Path, *, name: str = DEFAULT_NAME, internet: bool = False,
            env: Mapping[str, str] | None = None) -> int:
        raise SandboxError("the AppContainer sandbox exists on Windows only")


def sandbox_env(folder: Path, environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """The environment of a sandboxed command: system variables only (no API keys or tokens), with every
    per-user location pointed at the container's own folder, and the lab-agent marker set."""
    upper = {k.upper(): v for k, v in environ.items()}
    env = {k: upper[k] for k in KEEP_ENV if k in upper}
    temp = folder / "Temp"
    env.update({"TEMP": str(temp), "TMP": str(temp), "LOCALAPPDATA": str(folder), "APPDATA": str(folder),
                "USERPROFILE": str(folder), "HOME": str(folder), "FYJ_LAB_AGENT": "appcontainer",
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"})
    return env


def _granted_entries(root: Path) -> tuple[list[Path], list[Path]]:
    """``(folders, files)`` that get their own read grant: every entry of ``root`` and of ``config/``
    except secrets and links. Folders get an inheritable grant; the two folders holding secrets
    (``root`` and ``config/``) only pass a folder-only grant down, so a file created there later (a
    new ``.env``) starts closed."""
    secrets_ = set(secret_files(root))
    folders: list[Path] = []
    files: list[Path] = []
    for parent in (root, root / "config"):
        if not parent.is_dir():
            continue
        for p in sorted(parent.iterdir()):
            if p.is_symlink() or p in secrets_ or p == root / "config":
                continue
            (folders if p.is_dir() else files).append(p)
    return folders, files


def grant_plan(root: Path, lab_dir: Path, sid: str, extra_read: Iterable[Path] = (),
               runtimes: Iterable[Path] | None = None) -> list[list[str]]:
    """The ``icacls`` commands ``grant`` runs, in order (each names the container's SID only).

    No deny ACE: Windows ignores a deny for an AppContainer SID (measured), so secrets are kept
    closed by never granting them, not by denying them."""
    who = f"*{sid}"
    folders, files = _granted_entries(root)
    cmds = [["icacls", str(root), "/grant", f"{who}:(CI)RX"]]
    cmds += [["icacls", str(p), "/grant", f"{who}:(OI)(CI)RX"] for p in folders]
    cmds += [["icacls", str(p), "/grant", f"{who}:RX"] for p in files]
    runtimes = runtime_dirs(root) if runtimes is None else list(runtimes)
    cmds += [["icacls", str(p), "/grant", f"{who}:(OI)(CI)RX"] for p in [*runtimes, *extra_read]]
    cmds.append(["icacls", str(lab_dir), "/grant", f"{who}:(OI)(CI)M"])
    return cmds


def revoke_plan(root: Path, lab_dir: Path, sid: str, extra_read: Iterable[Path] = (),
                runtimes: Iterable[Path] | None = None) -> list[list[str]]:
    """Remove every ACE naming the container's SID from what ``grant_plan`` touches."""
    who = f"*{sid}"
    folders, files = _granted_entries(root)
    runtimes = runtime_dirs(root) if runtimes is None else list(runtimes)
    paths = [root, *folders, *files, *runtimes, *extra_read, lab_dir]
    return [["icacls", str(p), "/remove", who] for p in paths if p.exists()]


def apply(cmds: list[list[str]]) -> None:
    for cmd in cmds:
        done = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if done.returncode != 0:
            raise SandboxError(f"{subprocess.list2cmdline(cmd)} failed: {(done.stdout + done.stderr).strip()}")
