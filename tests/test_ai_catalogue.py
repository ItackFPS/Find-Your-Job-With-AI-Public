"""Model catalogue (plan §C5, F8.2): shipped JSON schema and the hardware recommendation."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.ai import catalogue as catalogue_mod
from src.ai.catalogue import CATALOGUE_PATH, Catalogue, load_catalogue, recommend
from src.ai.runtime.hardware import GpuInfo, HardwareProbe
from src.api.routes import ai as ai_routes

GB = 1024**3


def _model(tier: str, tag: str, vram: float, ram: float, download: float,
           default: bool = True, cpu: bool = False) -> dict:
    return {"tier": tier, "default_for_tier": default, "ollama_tag": tag, "family": "x",
            "parameters": "1B", "quantization": "Q4_K_M", "download_bytes": int(download * GB),
            "min_ram_bytes": int(ram * GB), "min_vram_bytes": int(vram * GB),
            "runs_on_cpu": cpu, "licence": "Apache-2.0", "licence_url": "https://x", "why": "w"}


def _catalogue(**overrides) -> Catalogue:
    data = {"schema_version": 1, "catalogue_version": "test", "models": [
        _model("small", "s:4b", vram=4, ram=8, download=3, cpu=True),
        _model("standard", "m:9b", vram=8, ram=16, download=6),
        _model("large", "l:20b", vram=12, ram=24, download=13),
        _model("standard", "m2:8b", vram=7, ram=16, download=5, default=False),
    ]}
    data.update(overrides)
    return Catalogue.model_validate(data)


def _hw(vram: float | None = None, vendor: str = "nvidia", ram: float | None = 64,
        disk: float | None = 500, avx2: bool | None = True, gpus: bool = True) -> HardwareProbe:
    gpu_list = None if not gpus else (
        [] if vram is None else [GpuInfo(name="g", vendor=vendor, vram_bytes=int(vram * GB),
                                         source="nvidia-smi")])
    return HardwareProbe(platform="win32", cpu_logical_cores=8, avx2=avx2,
                         ram_total_bytes=None if ram is None else int(ram * GB),
                         disk_free_bytes=None if disk is None else int(disk * GB),
                         gpus=gpu_list)


# --- shipped JSON ------------------------------------------------------------------------


def test_shipped_catalogue_is_valid_and_complete():
    cat = load_catalogue()
    assert cat.schema_version == 1 and cat.catalogue_version
    for tier in ("small", "standard", "large"):
        model = cat.default(tier)
        assert ":" in model.ollama_tag
        assert model.licence and model.licence_url.startswith("https://")
        assert model.why and model.benchmark is not None
        assert model.download_bytes < model.min_ram_bytes
    assert cat.default("small").runs_on_cpu
    # Tiers grow: a larger tier never needs less memory than a smaller one.
    sizes = [cat.default(t).min_vram_bytes for t in ("small", "standard", "large")]
    assert sizes == sorted(sizes)


def test_shipped_catalogue_is_plain_json_with_a_trailing_newline():
    raw = CATALOGUE_PATH.read_text(encoding="utf-8")
    assert raw.endswith("\n") and "\r" not in raw
    json.loads(raw)


def test_catalogue_rejects_two_defaults_a_missing_tier_or_duplicates():
    base = _catalogue().model_dump()
    two = json.loads(json.dumps(base))
    two["models"][3]["default_for_tier"] = True
    with pytest.raises(ValidationError, match="exactly one default"):
        Catalogue.model_validate(two)
    missing = json.loads(json.dumps(base))
    missing["models"] = [m for m in missing["models"] if m["tier"] != "large"]
    with pytest.raises(ValidationError, match="'large'"):
        Catalogue.model_validate(missing)
    dup = json.loads(json.dumps(base))
    dup["models"][3]["ollama_tag"] = "s:4b"
    with pytest.raises(ValidationError, match="duplicate"):
        Catalogue.model_validate(dup)
    with pytest.raises(ValidationError, match="schema_version"):
        Catalogue.model_validate({**base, "schema_version": 2})


def test_find_and_default():
    cat = _catalogue()
    assert cat.default("standard").ollama_tag == "m:9b"
    assert cat.find("m2:8b").tier == "standard"  # type: ignore[union-attr]
    assert cat.find("nope") is None


# --- recommendation ----------------------------------------------------------------------


@pytest.mark.parametrize(("vram", "tier", "tag"), [
    (24, "large", "l:20b"), (12, "large", "l:20b"), (11.9, "standard", "m:9b"),
    (8, "standard", "m:9b"), (6, "small", "s:4b"), (4, "small", "s:4b"),
])
def test_recommends_the_largest_tier_the_gpu_fits(vram, tier, tag):
    rec = recommend(_hw(vram=vram), _catalogue())
    assert (rec.tier, rec.ollama_tag, rec.run_on) == (tier, tag, "gpu")
    assert rec.reasons == ["gpu_vram_fits_tier"]


def test_no_gpu_means_small_on_cpu():
    rec = recommend(_hw(vram=None), _catalogue())
    assert (rec.tier, rec.run_on) == ("small", "cpu")
    assert rec.reasons == ["no_discrete_gpu", "cpu_only_small_tier"]


def test_integrated_gpu_memory_does_not_count():
    rec = recommend(_hw(vram=16, vendor="intel"), _catalogue())
    assert (rec.tier, rec.run_on) == ("small", "cpu")


def test_gpu_too_small_falls_back_to_cpu():
    rec = recommend(_hw(vram=2), _catalogue())
    assert (rec.tier, rec.run_on) == ("small", "cpu")
    assert rec.reasons == ["gpu_vram_below_smallest_tier", "cpu_only_small_tier"]


def test_unknown_gpu_is_reported_and_small_is_offered():
    rec = recommend(_hw(gpus=False), _catalogue())
    assert (rec.tier, rec.run_on) == ("small", "cpu")
    assert rec.reasons[0] == "gpu_unknown"


def test_no_avx2_warns_on_cpu():
    rec = recommend(_hw(vram=None, avx2=False), _catalogue())
    assert rec.reasons[-1] == "no_avx2_expect_slow"


def test_low_ram_steps_down_a_tier():
    rec = recommend(_hw(vram=24, ram=20), _catalogue())
    assert rec.tier == "standard"
    assert rec.reasons == ["gpu_vram_fits_tier", "ram_below_tier_need"]


def test_low_disk_steps_down_a_tier():
    rec = recommend(_hw(vram=24, disk=10), _catalogue())  # large needs 13 + 2 GB margin
    assert rec.tier == "standard"
    assert "disk_below_download_size" in rec.reasons


def test_nothing_fits():
    rec = recommend(_hw(vram=None, ram=4), _catalogue())
    assert (rec.tier, rec.ollama_tag, rec.run_on) == (None, None, None)
    assert rec.reasons == ["no_discrete_gpu", "ram_below_tier_need",
                           "no_tier_fits_continue_without_ai"]
    rec = recommend(_hw(vram=24, disk=1), _catalogue())
    assert rec.tier is None and "disk_below_download_size" in rec.reasons


def test_unknown_ram_and_disk_do_not_block_but_are_reported():
    rec = recommend(_hw(vram=12, ram=None, disk=None), _catalogue())
    assert rec.tier == "large"
    assert rec.reasons == ["ram_unknown", "disk_unknown", "gpu_vram_fits_tier"]


# --- route -------------------------------------------------------------------------------


def test_catalogue_route_returns_catalogue_and_recommendation(isolated_app, monkeypatch):
    monkeypatch.setattr(ai_routes, "probe_hardware", lambda _dir: _hw(vram=None))
    body = TestClient(isolated_app).get("/api/ai/catalogue").json()
    tiers = {m["tier"] for m in body["catalogue"]["models"]}
    assert tiers == {"small", "standard", "large"}
    small = catalogue_mod.load_catalogue().default("small")
    assert body["recommendation"] == {"tier": "small", "ollama_tag": small.ollama_tag,
                                      "run_on": "cpu",
                                      "reasons": ["no_discrete_gpu", "cpu_only_small_tier"]}
    assert body["hardware"]["gpus"] == []
