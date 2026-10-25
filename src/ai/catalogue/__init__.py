"""The local model catalogue (plan §C5, F8.2): three tiers shipped as versioned JSON.

``models.json`` lists, per tier, the Ollama model to offer by default (and measured
alternatives), with its exact download size, measured memory need, quantization and licence,
and why it was chosen (the F8.3 benchmark table). :func:`recommend` picks a tier for this
machine from the F8.1 hardware probe and says why, as reason codes for the UI to translate.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from src.ai.runtime.hardware import HardwareProbe

CATALOGUE_PATH = Path(__file__).with_name("models.json")
SCHEMA_VERSION = 1

Tier = Literal["small", "standard", "large"]
TIER_ORDER: tuple[Tier, ...] = ("small", "standard", "large")
DISCRETE_GPU_VENDORS = frozenset({"nvidia", "amd"})
# Keep this much free disk after a download (Ollama also needs room to verify the blob).
DISK_MARGIN_BYTES = 2 * 1024**3

# Reason codes (stable, for the UI to translate).
R_GPU_FITS = "gpu_vram_fits_tier"
R_GPU_TOO_SMALL = "gpu_vram_below_smallest_tier"
R_NO_GPU = "no_discrete_gpu"
R_GPU_UNKNOWN = "gpu_unknown"
R_CPU_ONLY = "cpu_only_small_tier"
R_NO_AVX2 = "no_avx2_expect_slow"
R_LOW_RAM = "ram_below_tier_need"
R_RAM_UNKNOWN = "ram_unknown"
R_LOW_DISK = "disk_below_download_size"
R_DISK_UNKNOWN = "disk_unknown"
R_NOTHING_FITS = "no_tier_fits_continue_without_ai"


class BenchmarkSummary(BaseModel):
    """The F8.3 measurement that justified the choice (8 synthetic offers)."""

    measured_on: str
    json_valid_rate: float
    grounding_rate: float
    french_rate: float
    seconds_per_offer: float
    tokens_per_second: float | None = None


class CatalogueModel(BaseModel):
    tier: Tier
    default_for_tier: bool
    ollama_tag: str = Field(min_length=1)
    family: str
    parameters: str
    quantization: str
    download_bytes: int = Field(gt=0)  # exact: sum of the registry manifest's layers
    min_ram_bytes: int = Field(gt=0)
    min_vram_bytes: int = Field(gt=0)  # measured loaded size with the app's context window
    runs_on_cpu: bool  # usable without a GPU (slowly)
    think: bool | str = False  # Ollama "think" value the app sends
    licence: str
    licence_url: str
    why: str
    benchmark: BenchmarkSummary | None = None


class Catalogue(BaseModel):
    schema_version: int
    catalogue_version: str
    notes: str = ""
    models: list[CatalogueModel]

    @model_validator(mode="after")
    def _one_default_per_tier(self) -> Catalogue:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported catalogue schema_version {self.schema_version}")
        tags = [m.ollama_tag for m in self.models]
        if len(tags) != len(set(tags)):
            raise ValueError("duplicate ollama_tag in catalogue")
        for tier in TIER_ORDER:
            defaults = [m for m in self.models if m.tier == tier and m.default_for_tier]
            if len(defaults) != 1:
                raise ValueError(f"tier {tier!r} needs exactly one default model")
        return self

    def default(self, tier: Tier) -> CatalogueModel:
        return next(m for m in self.models if m.tier == tier and m.default_for_tier)

    def find(self, ollama_tag: str) -> CatalogueModel | None:
        return next((m for m in self.models if m.ollama_tag == ollama_tag), None)


@lru_cache(maxsize=1)
def load_catalogue(path: Path = CATALOGUE_PATH) -> Catalogue:
    return Catalogue.model_validate_json(path.read_text(encoding="utf-8"))


class Recommendation(BaseModel):
    tier: Tier | None
    ollama_tag: str | None
    run_on: Literal["gpu", "cpu"] | None
    reasons: list[str]


def _best_vram(hw: HardwareProbe) -> int | None:
    """Largest dedicated VRAM of a discrete GPU; 0 when none; None when unknown."""
    if hw.gpus is None:
        return None
    vrams = [g.vram_bytes for g in hw.gpus
             if g.vendor in DISCRETE_GPU_VENDORS and g.vram_bytes is not None]
    return max(vrams, default=0)


def recommend(hw: HardwareProbe, catalogue: Catalogue | None = None) -> Recommendation:
    """The largest tier whose default model fits this machine's GPU, RAM and disk.

    A tier fits on the GPU when the best discrete GPU's VRAM covers the model's measured
    loaded size; only the small tier is offered on CPU. Unknown RAM or disk does not block
    a tier (the benchmark will tell), but is reported.
    """
    catalogue = catalogue or load_catalogue()
    reasons: list[str] = []
    vram = _best_vram(hw)
    if vram is None:
        reasons.append(R_GPU_UNKNOWN)
    elif vram == 0:
        reasons.append(R_NO_GPU)
    if hw.ram_total_bytes is None:
        reasons.append(R_RAM_UNKNOWN)
    if hw.disk_free_bytes is None:
        reasons.append(R_DISK_UNKNOWN)

    def ram_ok(model: CatalogueModel) -> bool:
        return hw.ram_total_bytes is None or hw.ram_total_bytes >= model.min_ram_bytes

    def disk_ok(model: CatalogueModel) -> bool:
        return (hw.disk_free_bytes is None
                or hw.disk_free_bytes >= model.download_bytes + DISK_MARGIN_BYTES)

    low_ram = low_disk = False
    if vram:
        for tier in reversed(TIER_ORDER):
            model = catalogue.default(tier)
            if vram < model.min_vram_bytes:
                continue
            if not ram_ok(model):
                low_ram = True
                continue
            if not disk_ok(model):
                low_disk = True
                continue
            reasons.append(R_GPU_FITS)
            reasons += [R_LOW_RAM] * low_ram + [R_LOW_DISK] * low_disk
            return Recommendation(tier=tier, ollama_tag=model.ollama_tag, run_on="gpu",
                                  reasons=reasons)
        if vram < catalogue.default("small").min_vram_bytes:
            reasons.append(R_GPU_TOO_SMALL)

    small = catalogue.default("small")
    if small.runs_on_cpu and ram_ok(small) and disk_ok(small):
        reasons += [R_LOW_RAM] * low_ram + [R_LOW_DISK] * low_disk
        reasons.append(R_CPU_ONLY)
        if hw.avx2 is False:
            reasons.append(R_NO_AVX2)
        return Recommendation(tier="small", ollama_tag=small.ollama_tag, run_on="cpu",
                              reasons=reasons)
    if not ram_ok(small) or low_ram:
        reasons.append(R_LOW_RAM)
    if not disk_ok(small) or low_disk:
        reasons.append(R_LOW_DISK)
    reasons.append(R_NOTHING_FITS)
    return Recommendation(tier=None, ollama_tag=None, run_on=None,
                          reasons=list(dict.fromkeys(reasons)))
