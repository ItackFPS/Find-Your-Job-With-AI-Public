"""Strategy versioning: a content hash of everything that defines how decisions are made."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.decisions.active_strategy import ActiveStrategy


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def strategy_fingerprint(
    files: dict[str, str | Path], extra: dict[str, object] | None = None
) -> tuple[str, dict[str, str]]:
    """Return ``(fingerprint, {logical_name: sha256})`` for the given files.

    A missing file is recorded as the literal ``"missing"`` so that adding or removing a
    config file changes the fingerprint. ``extra`` carries non-file inputs (rule/prompt
    versions, run mode, LLM spec).
    """
    hashes: dict[str, str] = {}
    for name in sorted(files):
        p = Path(files[name])
        hashes[name] = sha256_file(p) if p.is_file() else "missing"
    blob = json.dumps({"files": hashes, "extra": extra}, sort_keys=True, default=str).encode()
    return sha256_bytes(blob), hashes


def rule_versions() -> dict[str, str]:
    """Versions of code-defined rules/prompts that influence decisions."""
    from src.requirements.models import GateResult

    from src.pipeline.filter import FRESHNESS_RULE_VERSION

    from src.models.profile import PROFILE_SCHEMA_VERSION

    out = {"requirements_gate": GateResult(passes=True).rule_version, "freshness": FRESHNESS_RULE_VERSION,
           "profile_schema": str(PROFILE_SCHEMA_VERSION)}
    try:
        from src.ai.prompt import ELIGIBILITY_PROMPT_VERSION, PERSONALIZED_PROMPT_VERSION, PROMPT_VERSION

        out.update(eligibility_prompt=ELIGIBILITY_PROMPT_VERSION, phase2_prompt=PROMPT_VERSION,
                   phase2_personalized_prompt=PERSONALIZED_PROMPT_VERSION)
    except Exception:  # noqa: BLE001 — versions are metadata
        pass
    return out


def build_strategy_manifest(
    config_dir: str | Path, profile_path: str | Path, *, use_ai: bool = False,
    active: ActiveStrategy | None = None,
) -> dict[str, Any]:
    """Manifest for the strategy active in a run: file hashes + rule versions + run mode.

    ``active``: the resolved ``active_strategy``; when it runs an approved bundle, the bundle's
    ``filters.yaml`` / ``scoring.yaml`` are hashed and the pointer is recorded (otherwise the manifest is
    exactly the one of a run on ``config_dir``). ``strategy_version`` is ``sv-`` + the first 16 hex chars
    of the fingerprint.
    """
    cdir = Path(config_dir)
    use_bundle = active is not None and active.uses_bundle
    files: dict[str, str | Path] = {
        "profile": profile_path,
        "filters": active.path("filters.yaml") if active is not None and use_bundle else cdir / "filters.yaml",
        "scoring": active.path("scoring.yaml") if active is not None and use_bundle else cdir / "scoring.yaml",
        "semantic": cdir / "semantic_config.yaml",
        "personalization": cdir / "personalization.yaml",
    }
    extra: dict[str, Any] = {"rule_versions": rule_versions(), "use_ai": bool(use_ai)}
    if active is not None and use_bundle:
        pointer = active.pointer or {}
        extra["active_strategy"] = {"pointer_id": pointer.get("pointer_id"), "bundle_hash": pointer.get("bundle_hash")}
    fp, hashes = strategy_fingerprint(files, extra)
    return {"strategy_version": f"sv-{fp[:16]}", "fingerprint": fp, "files": hashes, "extra": extra}
