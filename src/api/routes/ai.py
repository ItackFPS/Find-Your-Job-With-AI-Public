"""Local AI platform routes (plan F8.1): read-only runtime detection and hardware probe.

GET /api/ai/runtimes[?base_url=<OpenAI-compatible base, e.g. http://127.0.0.1:8080/v1>]
    Ollama, LM Studio and, when given, the user's OpenAI-compatible server. Loopback only:
    a remote ``base_url`` comes back with ``error="endpoint_not_local"`` and is never
    contacted. Statuses and errors are codes for the UI to translate.

GET /api/ai/hardware
    CPU cores and AVX2, RAM, free disk for the data directory, GPUs and VRAM.
    Unknown values are ``null``.

Nothing here installs, launches or downloads anything.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query, Request

from src.ai.runtime.detect import RuntimeReport, detect_runtimes
from src.ai.runtime.hardware import HardwareProbe, probe_hardware

router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.get("/runtimes", response_model=RuntimeReport)
def list_runtimes(
    base_url: str | None = Query(None, min_length=1, max_length=2048,
                                 description="OpenAI-compatible base URL on this machine"),
) -> RuntimeReport:
    return detect_runtimes(base_url)


@router.get("/hardware", response_model=HardwareProbe)
def get_hardware(request: Request) -> HardwareProbe:
    return probe_hardware(Path(request.app.state.db_path).parent)
