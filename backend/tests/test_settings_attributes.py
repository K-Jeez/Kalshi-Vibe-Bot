"""Fail if live code reads a Settings attribute that no longer exists."""

from __future__ import annotations

import ast
from pathlib import Path

from src.config import Settings

ROOT = Path(__file__).resolve().parents[1] / "src"
NAMES = {"settings", "app_settings", "_s"}
ALLOWED = set(Settings.model_fields) | {
    "model_config",
    "model_dump",
    "model_fields",
    "model_validate",
}


def _problems(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in NAMES:
            if node.attr not in ALLOWED:
                found.append(f"{path.relative_to(ROOT.parent)}:{node.lineno} {node.value.id}.{node.attr}")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in NAMES
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and node.args[1].value not in ALLOWED
        ):
            found.append(
                f"{path.relative_to(ROOT.parent)}:{node.lineno} getattr({node.args[0].id}, {node.args[1].value!r})"
            )
    return found


def test_live_code_only_reads_real_settings_fields():
    problems = []
    for path in sorted(ROOT.rglob("*.py")):
        problems.extend(_problems(path))
    assert problems == []
