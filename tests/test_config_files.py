"""Local personal config overrides (config/<name>.local.yaml) — issue #8."""

from __future__ import annotations

from pathlib import Path

from src.config_files import local_config_path, resolve_config_path


def test_local_path_is_sibling_with_local_suffix(tmp_path: Path) -> None:
    assert local_config_path(tmp_path, "profile.yaml") == tmp_path / "profile.local.yaml"
    assert (
        local_config_path(str(tmp_path), "notifications.yaml")
        == tmp_path / "notifications.local.yaml"
    )


def test_resolves_to_default_when_no_local_file(tmp_path: Path) -> None:
    (tmp_path / "profile.yaml").write_text("a: 1\n", encoding="utf-8")
    assert resolve_config_path(tmp_path, "profile.yaml") == tmp_path / "profile.yaml"


def test_resolves_to_default_path_even_when_nothing_exists(tmp_path: Path) -> None:
    assert resolve_config_path(tmp_path, "profile.yaml") == tmp_path / "profile.yaml"


def test_local_file_takes_precedence(tmp_path: Path) -> None:
    (tmp_path / "profile.yaml").write_text("a: 1\n", encoding="utf-8")
    (tmp_path / "profile.local.yaml").write_text("a: 2\n", encoding="utf-8")
    assert resolve_config_path(tmp_path, "profile.yaml") == tmp_path / "profile.local.yaml"


def test_local_directory_is_not_treated_as_override(tmp_path: Path) -> None:
    (tmp_path / "profile.local.yaml").mkdir()
    assert resolve_config_path(tmp_path, "profile.yaml") == tmp_path / "profile.yaml"


def test_local_config_files_are_gitignored() -> None:
    import subprocess

    repo_root = Path(__file__).resolve().parent.parent
    for name in ("config/profile.local.yaml", "config/notifications.local.yaml"):
        result = subprocess.run(
            ["git", "check-ignore", "-q", name], cwd=repo_root, check=False
        )
        assert result.returncode == 0, f"{name} must be gitignored"


# ---------------------------------------------------------------------------
# Consumers resolve the personal override
# ---------------------------------------------------------------------------

_DEFAULT_PROFILE = "target_domains: [software_development]\ntarget_skills: [python]\n"
_LOCAL_PROFILE = "target_domains: [data_science]\ntarget_skills: [rust]\n"


def _config_dir_with_override(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "profile.yaml").write_text(_DEFAULT_PROFILE, encoding="utf-8")
    (config_dir / "profile.local.yaml").write_text(_LOCAL_PROFILE, encoding="utf-8")
    (config_dir / "filters.yaml").write_text("{}\n", encoding="utf-8")
    return config_dir


def test_orchestrator_uses_local_profile(tmp_path: Path) -> None:
    from src.pipeline.orchestrator import _hash_file, run_pipeline

    config_dir = _config_dir_with_override(tmp_path)
    result = run_pipeline(db_path=str(tmp_path / "t.db"), config_dir=str(config_dir))

    assert result.status == "completed", result.error
    assert result.stats.profile_hash == _hash_file(str(config_dir / "profile.local.yaml"))


def test_jobs_route_profile_loader_uses_local_profile(tmp_path: Path) -> None:
    from src.api.routes.jobs import _load_profile_safe

    profile = _load_profile_safe(_config_dir_with_override(tmp_path))
    assert profile.target_skills == ["rust"]
