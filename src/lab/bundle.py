"""Immutable strategy bundles: a base config plus data overrides, content-addressed.

A bundle holds only ``filters.yaml`` and ``scoring.yaml`` (what the evaluation replays); overrides
are deep-merged mappings, so a candidate is reviewable as a small diff and can never carry code.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from src.decisions.bundle_format import BUNDLE_FILES, bundle_hash, merge_overrides, render_bundle, rendered_hash

__all__ = ["BUNDLE_FILES", "BundleError", "bundle_hash", "create_bundle", "verify_bundle"]


class BundleError(ValueError):
    pass


def create_bundle(
    base_config_dir: str | Path, overrides: dict[str, dict[str, Any]], store_dir: str | Path,
) -> tuple[str, Path]:
    """Write the bundle ``base + overrides`` under ``store_dir/<hash>/`` → ``(hash, path)``.

    Unknown files in ``overrides`` are refused. Re-creating the same content is a no-op; an existing
    bundle is never rewritten (immutability).
    """
    unknown = sorted(set(overrides) - set(BUNDLE_FILES))
    if unknown:
        raise BundleError(f"overrides may only target {BUNDLE_FILES}; got {unknown}")
    for name in BUNDLE_FILES:
        if not (Path(base_config_dir) / name).is_file():
            raise BundleError(f"base config is missing {name}")
    rendered = render_bundle(base_config_dir, overrides)
    digest = rendered_hash(rendered)
    base_overrides = Path(base_config_dir) / "overrides.json"
    if base_overrides.is_file():      # built on another bundle: record the overrides relative to config/
        overrides = merge_overrides(json.loads(base_overrides.read_text(encoding="utf-8")), overrides)
    dest = Path(store_dir) / digest
    if dest.exists():
        verify_bundle(dest, digest)          # never silently reuse a modified bundle
        return digest, dest
    tmp = Path(store_dir) / f".tmp-{digest}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    # newline="\n": the hash above is over the LF text; platform newlines would break verify_bundle on Windows.
    for name, text in rendered.items():
        (tmp / name).write_text(text, encoding="utf-8", newline="\n")
    (tmp / "overrides.json").write_text(json.dumps(overrides, sort_keys=True, indent=1), encoding="utf-8",
                                        newline="\n")
    tmp.rename(dest)
    for p in dest.iterdir():
        p.chmod(0o444)
    return digest, dest


def verify_bundle(path: str | Path, expected_hash: str) -> None:
    if bundle_hash(path) != expected_hash:
        raise BundleError(f"bundle {path} no longer matches its recorded hash (modified after creation)")
