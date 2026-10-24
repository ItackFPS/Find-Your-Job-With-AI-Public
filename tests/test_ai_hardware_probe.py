"""Hardware probe (plan F8.1): parsers and fallbacks with fakes; real calls only where harmless."""

from __future__ import annotations

import sys

import pytest

from src.ai.runtime import hardware
from src.ai.runtime.hardware import (
    GpuInfo,
    parse_cpuinfo_avx2,
    parse_meminfo_available,
    parse_nvidia_smi,
    probe_hardware,
    qword_from_registry,
    vendor_from_device_id,
)


def test_parse_nvidia_smi_reports_vram_in_bytes():
    gpus = parse_nvidia_smi("NVIDIA GeForce RTX 3060, 12288\nTesla T4, [N/A]\n\n")
    assert gpus == [
        GpuInfo(name="NVIDIA GeForce RTX 3060", vendor="nvidia", vram_bytes=12288 * 1024**2,
                source="nvidia-smi"),
        GpuInfo(name="Tesla T4", vendor="nvidia", vram_bytes=None, source="nvidia-smi"),
    ]


def test_parse_cpuinfo_avx2():
    assert parse_cpuinfo_avx2("processor\t: 0\nflags\t\t: fpu sse4_2 avx avx2 fma\n") is True
    assert parse_cpuinfo_avx2("flags\t\t: fpu sse4_2 avx\n") is False
    assert parse_cpuinfo_avx2("Features\t: fp asimd\n") is False
    assert parse_cpuinfo_avx2("model name : something\n") is None


def test_parse_meminfo_available():
    assert parse_meminfo_available("MemTotal: 16 kB\nMemAvailable:    2048 kB\n") == 2048 * 1024
    assert parse_meminfo_available("MemTotal: 16 kB\n") is None


def test_registry_qword_is_64_bit_not_capped_at_4_gb():
    twelve_gb = 12 * 1024**3
    assert qword_from_registry(twelve_gb) == twelve_gb
    assert qword_from_registry(twelve_gb.to_bytes(8, "little")) == twelve_gb
    assert qword_from_registry(b"") is None
    assert qword_from_registry(0) is None
    assert qword_from_registry("12") is None


def test_vendor_from_device_id():
    assert vendor_from_device_id(r"pci\ven_10de&dev_2504") == "nvidia"
    assert vendor_from_device_id(r"PCI\VEN_1002&DEV_73BF") == "amd"
    assert vendor_from_device_id(r"PCI\VEN_8086&DEV_46A6") == "intel"
    assert vendor_from_device_id("root\\parsecvda") is None
    assert vendor_from_device_id(None) is None


def test_nvidia_gpus_use_the_csv_query(monkeypatch):
    calls = []
    monkeypatch.setattr(hardware, "_nvidia_smi_path", lambda: "nvidia-smi")
    monkeypatch.setattr(hardware, "_run", lambda args: calls.append(args) or "RTX 4090, 24564\n")
    gpus = hardware._nvidia_gpus()
    assert calls == [["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]]
    assert gpus[0].vram_bytes == 24564 * 1024**2


def test_nvidia_smi_failure_gives_no_gpus(monkeypatch):
    monkeypatch.setattr(hardware, "_nvidia_smi_path", lambda: "nvidia-smi")
    monkeypatch.setattr(hardware, "_run", lambda args: None)
    assert hardware._nvidia_gpus() == []


def test_run_returns_none_on_missing_binary_or_failure():
    assert hardware._run(["definitely-not-a-real-binary-fyj"]) is None
    assert hardware._run([sys.executable, "-c", "import sys; sys.exit(3)"]) is None
    assert hardware._run([sys.executable, "-c", "print('ok')"]).strip() == "ok"


def test_gpu_fallback_order_on_windows(monkeypatch):
    monkeypatch.setattr(hardware.sys, "platform", "win32")
    dxgi = [GpuInfo(name="Radeon", vendor="amd", vram_bytes=16 * 1024**3, source="dxgi")]
    reg = [GpuInfo(name="Radeon", vendor="amd", vram_bytes=16 * 1024**3, source="registry")]
    monkeypatch.setattr(hardware, "_nvidia_gpus", lambda: [])
    monkeypatch.setattr(hardware, "_dxgi_gpus", lambda: dxgi)
    monkeypatch.setattr(hardware, "_registry_gpus", lambda: reg)
    assert hardware._gpus() == dxgi

    def _boom():
        raise OSError("dxgi unavailable")

    monkeypatch.setattr(hardware, "_dxgi_gpus", _boom)
    assert hardware._gpus() == reg
    monkeypatch.setattr(hardware, "_registry_gpus", _boom)
    assert hardware._gpus() is None


def test_nvidia_smi_wins_over_other_sources(monkeypatch):
    nv = [GpuInfo(name="RTX", vendor="nvidia", vram_bytes=1, source="nvidia-smi")]
    monkeypatch.setattr(hardware, "_nvidia_gpus", lambda: nv)
    monkeypatch.setattr(hardware, "_dxgi_gpus", lambda: pytest.fail("DXGI should not be queried"))
    assert hardware._gpus() == nv


def test_gpus_unknown_off_windows_without_nvidia_smi(monkeypatch):
    monkeypatch.setattr(hardware.sys, "platform", "linux")
    monkeypatch.setattr(hardware, "_nvidia_gpus", lambda: [])
    assert hardware._gpus() is None


def test_probe_never_raises_and_reports_unknowns(monkeypatch, tmp_path):
    def _boom(*a, **k):
        raise RuntimeError("probe failure")

    for name in ("_avx2", "_memory", "_disk", "_gpus"):
        monkeypatch.setattr(hardware, name, _boom)
    monkeypatch.setattr(hardware.os, "cpu_count", _boom)
    result = probe_hardware(tmp_path)
    assert result.model_dump() == {
        "platform": sys.platform, "cpu_logical_cores": None, "avx2": None,
        "ram_total_bytes": None, "ram_available_bytes": None, "disk_path": None,
        "disk_free_bytes": None, "disk_total_bytes": None, "gpus": None,
    }


def test_probe_with_fakes(monkeypatch, tmp_path):
    gpu = GpuInfo(name="RTX 3060", vendor="nvidia", vram_bytes=12 * 1024**3, source="nvidia-smi")
    monkeypatch.setattr(hardware, "_avx2", lambda: True)
    monkeypatch.setattr(hardware, "_memory", lambda: (64 * 1024**3, 40 * 1024**3))
    monkeypatch.setattr(hardware, "_gpus", lambda: [gpu])
    monkeypatch.setattr(hardware.os, "cpu_count", lambda: 16)
    result = probe_hardware(tmp_path / "data" / "not-created-yet")
    assert (result.cpu_logical_cores, result.avx2) == (16, True)
    assert (result.ram_total_bytes, result.ram_available_bytes) == (64 * 1024**3, 40 * 1024**3)
    assert result.gpus == [gpu]
    # data/ may not exist yet: the nearest existing parent's disk is measured, nothing is created
    assert result.disk_path == str(tmp_path.resolve())
    assert result.disk_free_bytes and result.disk_total_bytes
    assert not (tmp_path / "data").exists()


def test_real_cpu_and_memory_probe_is_sane():
    """Runs the real standard-library probes (no subprocess, no GPU) on the CI machine."""
    cores = hardware._safe(hardware.os.cpu_count, None)
    total, available = hardware._memory()
    assert cores is None or cores >= 1
    assert total is None or total > 0
    assert available is None or (total is not None and 0 <= available <= total)
    assert hardware._safe(hardware._avx2, None) in (True, False, None)


@pytest.mark.skipif(sys.platform != "win32", reason="DXGI and the registry exist only on Windows")
def test_windows_gpu_sources_return_lists():
    for source in (hardware._dxgi_gpus, hardware._registry_gpus):
        gpus = source()
        assert isinstance(gpus, list)
        assert all(g.vram_bytes is None or g.vram_bytes > 0 for g in gpus)
