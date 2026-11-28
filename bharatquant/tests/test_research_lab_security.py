"""Path confinement, and proof the package never grows a code/network surface."""

import ast
from pathlib import Path

import pytest

from bq.research_lab.security import (
    FORBIDDEN_CALLS,
    FORBIDDEN_IMPORTS,
    PathEscapeError,
    safe_resolve_path,
)

PACKAGE = Path(__file__).parents[1] / "backend" / "src" / "bq" / "research_lab"


def test_traversal_is_blocked(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with pytest.raises(PathEscapeError):
        safe_resolve_path("../../etc/passwd", root)
    with pytest.raises(PathEscapeError):
        safe_resolve_path("/etc/passwd", root)


def test_a_symlink_escape_is_blocked(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = root / "link.txt"
    link.symlink_to(outside)
    with pytest.raises(PathEscapeError):
        safe_resolve_path("link.txt", root)


def test_relative_and_absolute_paths_inside_the_root_are_allowed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    (root / "runs").mkdir(parents=True)
    relative = safe_resolve_path("runs/inputs.parquet", root)
    assert relative == (root / "runs" / "inputs.parquet").resolve()
    absolute = safe_resolve_path(root / "runs" / "inputs.parquet", root)
    assert absolute == relative


def _violations() -> list[str]:
    found: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _forbidden_module(alias.name):
                        found.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                if node.module and _forbidden_module(node.module):
                    found.append(f"{path.name}: from {node.module}")
                # `from asyncio import subprocess` is the dotted case Python
                # cannot express as a single module name here.
                if node.module == "asyncio" and any(
                    alias.name == "subprocess" for alias in node.names
                ):
                    found.append(f"{path.name}: from asyncio import subprocess")
            elif isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(
                    node.func, "attr", None
                )
                if name in FORBIDDEN_CALLS:
                    found.append(f"{path.name}: call {name}()")
    return found


def _forbidden_module(name: str) -> bool:
    """Exact match, or a top-level ban when the entry names a bare module.

    A dotted entry such as ``asyncio.subprocess`` must not ban all of
    ``asyncio``: the package legitimately uses the event loop.
    """
    if name in FORBIDDEN_IMPORTS:
        return True
    top_level = name.split(".", 1)[0]
    return any("." not in item and item == top_level for item in FORBIDDEN_IMPORTS)


def test_the_package_has_no_shell_network_or_eval_surface() -> None:
    assert _violations() == []


def test_no_shell_or_read_url_tool_is_registered() -> None:
    """The CVE-2026-114107 mitigation is absence, so assert the absence."""
    registered: set[str] = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in {"bash", "shell", "read_url", "run_command"}:
                    registered.add(node.value)
    assert registered == set()
