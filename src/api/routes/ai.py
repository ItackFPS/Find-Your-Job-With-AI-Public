"""Local AI platform routes (plan F8.1, F8.2).

Read-only (F8.1):

GET /api/ai/runtimes[?base_url=<OpenAI-compatible base, e.g. http://127.0.0.1:8080/v1>]
    Ollama, LM Studio and, when given, the user's OpenAI-compatible server. Loopback only:
    a remote ``base_url`` comes back with ``error="endpoint_not_local"`` and is never
    contacted. Statuses and errors are codes for the UI to translate.

GET /api/ai/hardware
    CPU cores and AVX2, RAM, free disk for the data directory, GPUs and VRAM.
    Unknown values are ``null``.

GET /api/ai/catalogue
    The shipped model catalogue (3 tiers) and the recommended tier for this machine,
    with reason codes.

Setup (F8.2) — nothing downloads without a request echoing the size the user was shown:

GET /api/ai/ollama/installer
    HEAD of Ollama's official Windows installer: size, release, published SHA-256.
POST /api/ai/ollama/install {"confirm_size_bytes": N}
    Background job: re-check the size (must equal N), download, verify size, SHA-256 (when
    published) and the Authenticode signature, then launch the installer's own window.
POST /api/ai/models/pull {"model": tag, "confirm_size_bytes": N}
    Background ``ollama pull`` of a catalogue model; N must equal its catalogue
    ``download_bytes``. Progress in bytes. ``DELETE`` cancels; starting it again resumes
    (Ollama keeps the partial download).

Every job has ``GET .../{job_id}`` (status) and ``DELETE .../{job_id}`` (cancel). Jobs are
kept in memory only: a backend restart forgets them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.ai.catalogue import Catalogue, Recommendation, load_catalogue, recommend
from src.ai.runtime import ollama, ollama_install
from src.ai.runtime.detect import RuntimeReport, detect_runtimes
from src.ai.runtime.hardware import HardwareProbe, probe_hardware
from src.ai.runtime.jobs import Job, JobFailed, JobRegistry

router = APIRouter(prefix="/api/ai", tags=["ai"])

PULL_JOBS = JobRegistry("pull")
INSTALL_JOBS = JobRegistry("install")


def _data_dir(request: Request) -> Path:
    return Path(request.app.state.db_path).parent


@router.get("/runtimes", response_model=RuntimeReport)
def list_runtimes(
    base_url: str | None = Query(None, min_length=1, max_length=2048,
                                 description="OpenAI-compatible base URL on this machine"),
) -> RuntimeReport:
    return detect_runtimes(base_url)


@router.get("/hardware", response_model=HardwareProbe)
def get_hardware(request: Request) -> HardwareProbe:
    return probe_hardware(_data_dir(request))


class CatalogueResponse(BaseModel):
    catalogue: Catalogue
    recommendation: Recommendation
    hardware: HardwareProbe


@router.get("/catalogue", response_model=CatalogueResponse)
def get_catalogue(request: Request) -> CatalogueResponse:
    hardware = probe_hardware(_data_dir(request))
    catalogue = load_catalogue()
    return CatalogueResponse(catalogue=catalogue, recommendation=recommend(hardware, catalogue),
                             hardware=hardware)


# --- Jobs -------------------------------------------------------------------------------


def _job_or_404(registry: JobRegistry, job_id: str) -> Job:
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"error": "job_not_found"})
    return job


def _cancel(registry: JobRegistry, job_id: str) -> dict[str, Any]:
    _job_or_404(registry, job_id)
    job = registry.cancel(job_id)
    assert job is not None
    return job.snapshot()


def _local_base(base_url: str | None) -> str:
    try:
        return ollama.checked_base(base_url)
    except JobFailed as exc:
        raise HTTPException(status_code=400, detail={"error": exc.code}) from exc


# --- Ollama install ---------------------------------------------------------------------


@router.get("/ollama/installer")
def get_installer_info() -> dict[str, Any]:
    try:
        return ollama_install.resolve_installer().model_dump()
    except JobFailed as exc:
        raise HTTPException(status_code=502,
                            detail={"error": exc.code, **exc.params}) from exc


class InstallRequest(BaseModel):
    confirm_size_bytes: int = Field(gt=0)


@router.post("/ollama/install", status_code=202)
def start_install(body: InstallRequest, request: Request) -> dict[str, Any]:
    download_dir = _data_dir(request) / "downloads" / "ollama"
    job, _ = INSTALL_JOBS.start(
        lambda j: ollama_install.install_worker(j, body.confirm_size_bytes, download_dir),
        key="ollama", phase="queued", completed_bytes=0, total_bytes=body.confirm_size_bytes)
    return job.snapshot()


@router.get("/ollama/install/{job_id}")
def install_status(job_id: str) -> dict[str, Any]:
    return _job_or_404(INSTALL_JOBS, job_id).snapshot()


@router.delete("/ollama/install/{job_id}")
def cancel_install(job_id: str) -> dict[str, Any]:
    return _cancel(INSTALL_JOBS, job_id)


# --- Model pull -------------------------------------------------------------------------


class PullRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    confirm_size_bytes: int = Field(gt=0)
    base_url: str | None = Field(None, max_length=2048,
                                 description="Ollama on this machine; default 127.0.0.1:11434")


@router.post("/models/pull", status_code=202)
def start_pull(body: PullRequest) -> dict[str, Any]:
    entry = load_catalogue().find(body.model)
    if entry is None:
        raise HTTPException(status_code=404, detail={"error": "model_not_in_catalogue"})
    if body.confirm_size_bytes != entry.download_bytes:
        raise HTTPException(status_code=409, detail={"error": "size_mismatch",
                                                     "expected": entry.download_bytes})
    base = _local_base(body.base_url)

    def worker(job: Job) -> None:
        def progress(p: ollama.PullProgress) -> None:
            # Ollama announces layers one by one: until all are seen, its sum undercounts.
            job.update(status_text=p.status, completed_bytes=p.completed_bytes,
                       total_bytes=max(p.total_bytes, entry.download_bytes))

        ollama.pull(entry.ollama_tag, base_url=base, on_progress=progress,
                    cancelled=job.cancel_event.is_set)

    job, _ = PULL_JOBS.start(worker, key=f"{base}|{entry.ollama_tag}", model=entry.ollama_tag,
                             completed_bytes=0, total_bytes=entry.download_bytes,
                             status_text=None)
    return job.snapshot()


@router.get("/models/pull/{job_id}")
def pull_status(job_id: str) -> dict[str, Any]:
    return _job_or_404(PULL_JOBS, job_id).snapshot()


@router.delete("/models/pull/{job_id}")
def cancel_pull(job_id: str) -> dict[str, Any]:
    return _cancel(PULL_JOBS, job_id)
