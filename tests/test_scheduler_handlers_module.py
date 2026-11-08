"""The source handlers live in scheduler/handlers.py; service.py re-exports them so existing
imports from the service module keep working."""

from __future__ import annotations

import pytest

from src.scheduler import handlers, service


MOVED = [
    *sorted(n for n in vars(handlers) if n.endswith("Handler")),
    "DispatchResult", "PIPELINE_POLL_SECONDS", "_scanner_dispatch",
]


def test_the_three_scan_and_pipeline_handlers_moved() -> None:
    assert sum(n != "SourceHandler" and n.endswith("Handler") for n in MOVED) == 3


@pytest.mark.parametrize("name", MOVED)
def test_service_reexports_the_handlers_module(name: str) -> None:
    assert getattr(service, name) is getattr(handlers, name)


def test_handlers_module_does_not_depend_on_the_service() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(Path(handlers.__file__).read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert "src.scheduler.service" not in imported
