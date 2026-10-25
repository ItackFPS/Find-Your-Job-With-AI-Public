"""Local AI platform routes (plan F8.1, F8.2, F8.3).

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

Verify (F8.3):

POST /api/ai/benchmark {"model": tag, "offers": 3, "offers_per_day": 30}
    Background job: load the model, analyse bundled synthetic offers, and return metrics
    (valid JSON, grounded quotes, French, seconds per offer, tokens/s) and a plain verdict
    code with its parameters. The model must be a catalogue tag or installed in that
    Ollama (404 ``model_not_available``); one benchmark runs at a time (409
    ``benchmark_busy``); cancelling closes the in-flight request at once.

Every job has ``GET .../{job_id}`` (status) and ``DELETE .../{job_id}`` (cancel). Jobs are
kept in memory only: a backend restart forgets them.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.ai.benchmark.runner import DEFAULT_OFFERS, load_fixtures, run_benchmark
from src.ai.benchmark.scoring import DEFAULT_OFFERS_PER_DAY
from src.ai.catalogue import (
    DISK_MARGIN_BYTES,
    Catalogue,
    Recommendation,
    load_catalogue,
    recommend,
)
from src.ai.runtime import detect, ollama, ollama_install
from src.ai.runtime.detect import RuntimeReport, detect_runtimes
from src.ai.runtime.hardware import HardwareProbe, probe_hardware
from src.ai.runtime.jobs import Job, JobFailed, JobRegistry

router = APIRouter(prefix="/api/ai", tags=["ai"])

PULL_JOBS = JobRegistry("pull")
INSTALL_JOBS = JobRegistry("install")
BENCHMARK_JOBS = JobRegistry("benchmark")


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
    # Ollama writes to its models folder (OLLAMA_MODELS or ~/.ollama/models) on this machine.
    # A resumed pull needs less than this; refusing early beats failing near the end.
    free = ollama.free_disk_bytes(ollama.models_dir())
    needed = entry.download_bytes + DISK_MARGIN_BYTES
    if free is not None and free < needed:
        raise HTTPException(status_code=409, detail={"error": "disk_space_low",
                                                     "free_bytes": free, "needed_bytes": needed})

    def worker(job: Job) -> None:
        def progress(p: ollama.PullProgress) -> None:
            # Ollama announces layers one by one: until all are seen, its sum undercounts.
            job.update(status_text=p.status, completed_bytes=p.completed_bytes,
                       total_bytes=max(p.total_bytes, entry.download_bytes))

        ollama.pull(entry.ollama_tag, base_url=base, on_progress=progress,
                    cancelled=job.cancel_event.is_set)

    job, _ = PULL_JOBS.start(worker, key=f"{ollama.endpoint_key(base)}|{entry.ollama_tag}",
                             model=entry.ollama_tag,
                             completed_bytes=0, total_bytes=entry.download_bytes,
                             status_text=None)
    return job.snapshot()


@router.get("/models/pull/{job_id}")
def pull_status(job_id: str) -> dict[str, Any]:
    return _job_or_404(PULL_JOBS, job_id).snapshot()


@router.delete("/models/pull/{job_id}")
def cancel_pull(job_id: str) -> dict[str, Any]:
    return _cancel(PULL_JOBS, job_id)


# --- Benchmark --------------------------------------------------------------------------


class BenchmarkRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    offers: int = Field(DEFAULT_OFFERS, ge=1)
    offers_per_day: int = Field(DEFAULT_OFFERS_PER_DAY, ge=1, le=10_000)
    base_url: str | None = Field(None, max_length=2048,
                                 description="Ollama on this machine; default 127.0.0.1:11434")


_BENCHMARK_START = threading.Lock()


def _installed_models(base: str) -> set[str]:
    """Model names the Ollama at ``base`` reports (``foo`` also matches ``foo:latest``)."""
    names = {m.name for m in detect.probe_ollama(base).models}
    return names | {n.removesuffix(":latest") for n in names}


@router.post("/benchmark", status_code=202)
def start_benchmark(body: BenchmarkRequest) -> dict[str, Any]:
    base = _local_base(body.base_url)
    entry = load_catalogue().find(body.model)
    if entry is None and body.model not in _installed_models(base):
        raise HTTPException(status_code=404, detail={"error": "model_not_available"})
    offers = min(body.offers, len(load_fixtures()["offers"]))

    def worker(job: Job) -> None:
        result = run_benchmark(
            body.model, base_url=base, offers=offers,
            think=entry.think if entry else False, offers_per_day=body.offers_per_day,
            smaller_tier_available=entry is None or entry.tier != "small",
            on_progress=lambda s: job.update(**s), cancelled=job.cancel_event.is_set)
        job.update(result=result)

    # One benchmark at a time on this machine: two would share the GPU and skew both.
    with _BENCHMARK_START:
        running = [j for j in BENCHMARK_JOBS.list() if j.status == "running"]
        if running:
            raise HTTPException(status_code=409, detail={"error": "benchmark_busy",
                                                         "job_id": running[0].id})
        job, _ = BENCHMARK_JOBS.start(worker, model=body.model, phase="queued",
                                      offers_done=0, offers_total=offers, result=None)
    return job.snapshot()


@router.get("/benchmark/{job_id}")
def benchmark_status(job_id: str) -> dict[str, Any]:
    return _job_or_404(BENCHMARK_JOBS, job_id).snapshot()


@router.delete("/benchmark/{job_id}")
def cancel_benchmark(job_id: str) -> dict[str, Any]:
    return _cancel(BENCHMARK_JOBS, job_id)
