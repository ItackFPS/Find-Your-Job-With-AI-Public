"""Standard-library hardware probe: what local models this machine can run.

Reports CPU logical cores and AVX2, total/available RAM, free disk for the data
directory, and GPUs with their dedicated memory (VRAM). Every field may be ``None``,
meaning "unknown"; :func:`probe_hardware` never raises.

VRAM sources, in order: ``nvidia-smi``; on Windows DXGI (``IDXGIFactory1::EnumAdapters1``
→ ``DXGI_ADAPTER_DESC1.DedicatedVideoMemory``), then the display-class registry value
``HardwareInformation.qwMemorySize`` (64-bit). CIM/WMI ``AdapterRAM`` is never used: it is
a 32-bit field that caps at 4 GB.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

SUBPROCESS_TIMEOUT_S = 5.0
PF_AVX2_INSTRUCTIONS_AVAILABLE = 40
DISPLAY_CLASS_KEY = (r"SYSTEM\CurrentControlSet\Control\Class"
                     r"\{4d36e968-e325-11ce-bfc1-08002be10318}")
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data"

_VENDORS = {0x10DE: "nvidia", 0x1002: "amd", 0x1022: "amd", 0x8086: "intel",
            0x1414: "microsoft", 0x5143: "qualcomm", 0x106B: "apple"}

T = TypeVar("T")


class GpuInfo(BaseModel):
    name: str | None = None
    vendor: str | None = None  # nvidia / amd / intel / ... ; None = unknown
    vram_bytes: int | None = None
    source: Literal["nvidia-smi", "dxgi", "registry"]


class HardwareProbe(BaseModel):
    platform: str
    cpu_logical_cores: int | None = None
    avx2: bool | None = None
    ram_total_bytes: int | None = None
    ram_available_bytes: int | None = None
    disk_path: str | None = None
    disk_free_bytes: int | None = None
    disk_total_bytes: int | None = None
    gpus: list[GpuInfo] | None = None  # None = could not tell; [] = none found


def _safe(fn: Callable[[], T], default: T) -> T:
    try:
        return fn()
    except Exception:  # noqa: BLE001 - a probe must never raise
        logger.debug("Hardware probe step %s failed", getattr(fn, "__name__", fn), exc_info=True)
        return default


def _run(args: list[str]) -> str | None:
    """Run a command without a console window; stdout on success, else ``None``."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S,
                              check=False, creationflags=flags, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def vendor_from_id(vendor_id: int | None) -> str | None:
    return _VENDORS.get(vendor_id) if vendor_id is not None else None


# --- CPU ---------------------------------------------------------------------------


def parse_cpuinfo_avx2(text: str) -> bool | None:
    """``True``/``False`` from ``/proc/cpuinfo`` flags; ``None`` when no flags line is present."""
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip() in ("flags", "Features"):
            return "avx2" in value.split()
    return None


def _avx2() -> bool | None:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")
        return bool(kernel32.IsProcessorFeaturePresent(PF_AVX2_INSTRUCTIONS_AVAILABLE))
    if sys.platform.startswith("linux"):
        return parse_cpuinfo_avx2(Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace"))
    return None


# --- Memory ------------------------------------------------------------------------


def parse_meminfo_available(text: str) -> int | None:
    match = re.search(r"^MemAvailable:\s+(\d+)\s*kB", text, re.MULTILINE)
    return int(match.group(1)) * 1024 if match else None


def _memory() -> tuple[int | None, int | None]:
    if sys.platform == "win32":
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.WinDLL("kernel32").GlobalMemoryStatusEx(ctypes.byref(status)):
            return None, None
        return int(status.ullTotalPhys), int(status.ullAvailPhys)
    page = os.sysconf("SC_PAGE_SIZE")
    total = page * os.sysconf("SC_PHYS_PAGES")
    available: int | None = None
    if sys.platform.startswith("linux"):
        # MemAvailable counts reclaimable cache; SC_AVPHYS_PAGES (MemFree) does not.
        available = _safe(lambda: parse_meminfo_available(
            Path("/proc/meminfo").read_text(encoding="utf-8")), None)
    if available is None and "SC_AVPHYS_PAGES" in os.sysconf_names:
        available = page * os.sysconf("SC_AVPHYS_PAGES")
    return total, available


# --- Disk --------------------------------------------------------------------------


def _disk(data_dir: Path) -> tuple[str | None, int | None, int | None]:
    path = data_dir.resolve()
    while not path.exists() and path.parent != path:  # data/ may not exist yet
        path = path.parent
    usage = shutil.disk_usage(path)
    return str(path), int(usage.free), int(usage.total)


# --- GPU ---------------------------------------------------------------------------


def parse_nvidia_smi(text: str) -> list[GpuInfo]:
    """Lines of ``name, memory.total`` (MiB) from ``--format=csv,noheader,nounits``."""
    gpus = []
    for line in text.splitlines():
        if not line.strip():
            continue
        name, _, mem = line.rpartition(",")
        mib = mem.strip()
        gpus.append(GpuInfo(name=name.strip() or None, vendor="nvidia", source="nvidia-smi",
                            vram_bytes=int(mib) * 1024 * 1024 if mib.isdigit() else None))
    return gpus


def _nvidia_smi_path() -> str | None:
    found = shutil.which("nvidia-smi")
    if found or sys.platform != "win32":
        return found
    for root in (os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles")):
        if root:
            candidate = Path(root) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def _nvidia_gpus() -> list[GpuInfo]:
    exe = _nvidia_smi_path()
    if exe is None:
        return []
    out = _run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    return parse_nvidia_smi(out) if out else []


def _dxgi_gpus() -> list[GpuInfo]:
    """Hardware adapters from DXGI (Windows only), software adapters skipped."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class DXGI_ADAPTER_DESC1(ctypes.Structure):
        _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", ctypes.c_uint),
                    ("DeviceId", ctypes.c_uint), ("SubSysId", ctypes.c_uint),
                    ("Revision", ctypes.c_uint), ("DedicatedVideoMemory", ctypes.c_size_t),
                    ("DedicatedSystemMemory", ctypes.c_size_t),
                    ("SharedSystemMemory", ctypes.c_size_t), ("AdapterLuid", LUID),
                    ("Flags", ctypes.c_uint)]

    def method(obj: ctypes.c_void_p, index: int, *argtypes: Any) -> Any:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[index])

    # IUnknown: 0 QueryInterface, 1 AddRef, 2 Release. IDXGIFactory1::EnumAdapters1 = 12,
    # IDXGIAdapter1::GetDesc1 = 10 (after IDXGIObject's 4 and IDXGIAdapter's 3 methods).
    release, enum_adapters1, get_desc1 = 2, 12, 10
    software_flag = 0x2  # DXGI_ADAPTER_FLAG_SOFTWARE (Microsoft Basic Render Driver)

    iid = GUID(0x770AAE78, 0xF26F, 0x4DBA, (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))
    factory = ctypes.c_void_p()
    dxgi = ctypes.WinDLL("dxgi")
    dxgi.CreateDXGIFactory1.restype = ctypes.c_long
    if dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory)) != 0 or not factory:
        return []
    gpus: list[GpuInfo] = []
    # The same card can be enumerated twice under different LUIDs (seen with a virtual
    # display driver installed): keep one entry per identical hardware description.
    seen: set[tuple[int, int, int, int, int]] = set()
    try:
        for index in range(16):
            adapter = ctypes.c_void_p()
            hr = method(factory, enum_adapters1, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(
                factory, index, ctypes.byref(adapter))
            if hr != 0 or not adapter:
                break  # DXGI_ERROR_NOT_FOUND: no more adapters
            try:
                desc = DXGI_ADAPTER_DESC1()
                if method(adapter, get_desc1, ctypes.POINTER(DXGI_ADAPTER_DESC1))(
                        adapter, ctypes.byref(desc)) != 0 or desc.Flags & software_flag:
                    continue
                identity = (desc.VendorId, desc.DeviceId, desc.SubSysId, desc.Revision,
                            int(desc.DedicatedVideoMemory))
                if identity not in seen:
                    seen.add(identity)
                    gpus.append(GpuInfo(name=desc.Description or None,
                                        vendor=vendor_from_id(desc.VendorId),
                                        vram_bytes=int(desc.DedicatedVideoMemory), source="dxgi"))
            finally:
                method(adapter, release)(adapter)
    finally:
        method(factory, release)(factory)
    return gpus


def qword_from_registry(value: Any) -> int | None:
    """``HardwareInformation.qwMemorySize`` is REG_QWORD (int) or REG_BINARY (8 bytes LE)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value > 0 else None
    if isinstance(value, (bytes, bytearray)) and 1 <= len(value) <= 8:
        number = int.from_bytes(bytes(value), "little")
        return number if number > 0 else None
    return None


def vendor_from_device_id(device_id: str | None) -> str | None:
    match = re.search(r"VEN_([0-9A-F]{4})", device_id or "", re.IGNORECASE)
    return vendor_from_id(int(match.group(1), 16)) if match else None


def _registry_gpus() -> list[GpuInfo]:
    import winreg  # type: ignore[import-not-found,unused-ignore]

    def value(key: Any, name: str) -> Any:
        try:
            return winreg.QueryValueEx(key, name)[0]
        except OSError:
            return None

    gpus: list[GpuInfo] = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, DISPLAY_CLASS_KEY) as root:
        for index in range(64):
            try:
                sub = winreg.EnumKey(root, index)
            except OSError:
                break
            if not sub.isdigit():  # "Properties" and the like
                continue
            try:
                with winreg.OpenKey(root, sub) as key:
                    name = value(key, "DriverDesc")
                    vram = qword_from_registry(value(key, "HardwareInformation.qwMemorySize"))
                    device = value(key, "MatchingDeviceId")
            except OSError:
                continue
            if name is None and vram is None:
                continue
            gpus.append(GpuInfo(name=name if isinstance(name, str) else None,
                                vendor=vendor_from_device_id(device if isinstance(device, str) else None),
                                vram_bytes=vram, source="registry"))
    return gpus


def _gpus() -> list[GpuInfo] | None:
    none_found: list[GpuInfo] = []
    nvidia = _safe(_nvidia_gpus, none_found)
    if nvidia:
        return nvidia
    if sys.platform != "win32":
        return None  # no portable way to list other GPUs with the standard library
    for source in (_dxgi_gpus, _registry_gpus):
        found = _safe(source, none_found)
        if found:
            return found
    return None


# --- Entry point -------------------------------------------------------------------


def probe_hardware(data_dir: Path | str | None = None) -> HardwareProbe:
    """Probe this machine. Never raises; an unknown value is ``None``."""
    result = HardwareProbe(platform=sys.platform)
    result.cpu_logical_cores = _safe(os.cpu_count, None)
    result.avx2 = _safe(_avx2, None)
    no_memory: tuple[int | None, int | None] = (None, None)
    result.ram_total_bytes, result.ram_available_bytes = _safe(_memory, no_memory)
    target = Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR
    no_disk: tuple[str | None, int | None, int | None] = (None, None, None)
    result.disk_path, result.disk_free_bytes, result.disk_total_bytes = _safe(
        lambda: _disk(target), no_disk)
    result.gpus = _safe(_gpus, None)
    return result
