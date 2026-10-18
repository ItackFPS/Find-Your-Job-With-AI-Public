"""src/evaluation must never be imported by production code (ADR 0007)."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def imported_names(source: str, package: list[str]) -> set[str]:
    """Absolute dotted names imported by ``source`` living in dotted ``package``.

    Resolves relative imports and literal ``importlib.import_module`` / ``__import__``.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package[: len(package) - (node.level - 1)]
                base = ".".join([*anchor, *([node.module] if node.module else [])])
            names.add(base)
            names.update(f"{base}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Call) and ast.unparse(node.func) in {"importlib.import_module", "__import__"}:
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                names.add(node.args[0].value)
    return names


def _package_of(path: Path) -> list[str]:
    return ["src", *path.relative_to(SRC).parts[:-1]]


def _is_evaluation(name: str) -> bool:
    return name == "src.evaluation" or name.startswith("src.evaluation.")


def test_detector_sees_absolute_relative_and_dynamic_imports():
    pkg = ["src", "pipeline"]
    assert any(_is_evaluation(n) for n in imported_names("from src.evaluation import runner", pkg))
    assert any(_is_evaluation(n) for n in imported_names("from ..evaluation import runner", pkg))
    assert any(_is_evaluation(n) for n in imported_names("import src.evaluation.store", pkg))
    assert any(_is_evaluation(n) for n in imported_names("importlib.import_module('src.evaluation.store')", pkg))
    assert not any(_is_evaluation(n) for n in imported_names("from .filter import x\nimport os", pkg))


def test_production_never_imports_evaluation():
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.relative_to(SRC).parts[0] in ("evaluation", "lab"):   # lab = non-production tooling (ADR 0011)
            continue
        names = imported_names(path.read_text(encoding="utf-8"), _package_of(path))
        if any(_is_evaluation(n) for n in names):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, f"production modules import src.evaluation: {offenders}"


def test_evaluation_package_has_no_numeric_stack_and_confines_network_to_runner():
    banned = {"numpy", "scipy", "pandas", "sklearn"}
    for path in (SRC / "evaluation").glob("*.py"):
        top = {n.split(".")[0] for n in imported_names(path.read_text(encoding="utf-8"), _package_of(path))}
        assert not (top & banned), f"{path.name} imports {top & banned}"
        if path.name != "runner.py":
            assert not (top & {"httpx", "requests", "urllib", "socket"}), f"{path.name} touches the network stack"
