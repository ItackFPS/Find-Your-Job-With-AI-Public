"""Embedding model download (M3): GET its state, POST to start or retry."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from src.embeddings.model_download import ModelDownloader

router = APIRouter(prefix="/api/embedding-model", tags=["embedding-model"])


def _downloader(request: Request) -> ModelDownloader:
    state = request.app.state
    if getattr(state, "model_downloader", None) is None:
        state.model_downloader = ModelDownloader()
    downloader: ModelDownloader = state.model_downloader
    return downloader


@router.get("")
def get_status(request: Request) -> dict[str, Any]:
    return _downloader(request).status()


@router.post("/download", status_code=202)
def start_download(request: Request) -> dict[str, Any]:
    return _downloader(request).start()
