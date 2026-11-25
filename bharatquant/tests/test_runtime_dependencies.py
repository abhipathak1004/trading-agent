"""Guard the runtime dependency set against import-time regressions.

A production install only gets `[project].dependencies`. Development extras such
as pytest and mypy are absent. If a module that the CLI imports at startup
imports a dev-only package, the installed `bq` command fails to start — which is
exactly what happened when the news agent added a runtime `httpx` import while
`httpx` was still a dev dependency.

This test is deliberately import-graph based rather than a real install, so it
runs in milliseconds; `docs/architecture-decisions.md` ADR-010 records the
heavier clean-install check for release preparation.
"""

import ast
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
SOURCE_ROOT = PROJECT_ROOT / "backend" / "src" / "bq"

# Modules provided by the interpreter, plus packages that ship inside the
# wheel's own source tree.
STDLIB = sys.stdlib_module_names
INTERNAL_PREFIX = "bq"


def _declared() -> tuple[set[str], set[str]]:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text("utf-8"))
    runtime = {
        # "argon2-cffi" -> "argon2", "types-jsonschema" -> "types_jsonschema"
        name.split("[", 1)[0]
        .split(">=", 1)[0]
        .split("==", 1)[0]
        .strip()
        .lower()
        .replace("-", "_")
        for name in project["project"]["dependencies"]
    }
    return runtime, runtime


def _distribution_names() -> dict[str, str]:
    """Map import name to distribution for the packages we depend on directly.

    Only the handful that differ from their import name need an entry.
    """
    return {
        "argon2": "argon2_cffi",
        "yaml": "pyyaml",
    }


def _imported_top_level(path: Path) -> set[str]:
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` has no module name; relative imports are internal.
            if node.level == 0 and node.module:
                names.add(node.module.split(".", 1)[0])
    return names


def _cli_reachable_modules() -> set[Path]:
    """Every module the CLI pulls in at import time.

    Walks `import bq.x` statements from `bq/cli.py` transitively. A runtime
    import that sits inside a function body still executes when that function
    runs, so it is included too: the unsafe case is precisely a startup path.
    """
    seen: set[Path] = set()
    pending = [SOURCE_ROOT / "cli.py"]
    while pending:
        module = pending.pop()
        if module in seen or not module.is_file():
            continue
        seen.add(module)
        for name in _imported_top_level(module):
            if name == INTERNAL_PREFIX:
                for candidate in _internal_paths(module):
                    pending.append(candidate)
    return seen


def _internal_paths(path: Path) -> list[Path]:
    """Resolve `bq.something` imports originating in ``path`` to source files."""
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    found: list[Path] = []
    for node in ast.walk(tree):
        module: str | None = None
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("bq."):
                    module = alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module and node.module.startswith("bq"):
                module = node.module
        if not module:
            continue
        parts = module.split(".")
        # bq.a.b -> bq/a/b.py or bq/a/b/__init__.py
        base = SOURCE_ROOT.joinpath(*parts[1:])
        found.extend([base.with_suffix(".py"), base / "__init__.py"])
    return found


def test_cli_startup_imports_only_runtime_dependencies() -> None:
    runtime, _ = _declared()
    aliases = _distribution_names()
    offenders: dict[str, str] = {}

    for module in _cli_reachable_modules():
        for name in _imported_top_level(module):
            if name in STDLIB or name.startswith(INTERNAL_PREFIX):
                continue
            distribution = aliases.get(name, name)
            if distribution not in runtime:
                offenders.setdefault(
                    f"{name} ({module.relative_to(SOURCE_ROOT)})", distribution
                )

    assert not offenders, (
        "Modules reachable from `bq.cli` import packages that are not runtime "
        "dependencies. A production install would fail to start.\n"
        + "\n".join(
            f"  {where} -> undeclared {dist}"
            for where, dist in sorted(offenders.items())
        )
    )
