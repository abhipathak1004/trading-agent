"""The ported engine must import inside this app's environment, with no
pandas, numpy, langchain, textual, rich or network client on the path.

That guarantee is the whole reason the engine is vendored rather than run as a
sidecar (ADR-053): upstream pins ``pandas<3``/``numpy<2`` against this project's
``pandas>=3.1``/``numpy>=2.5``, and the lock was unsatisfiable. If a future
upgrade reintroduces one of those imports, the port has silently lost its
reason to exist — so the check lives here rather than in review.

Each module is imported in a **fresh subprocess**: pytest has already imported
pandas and numpy by the time these tests run, so an in-process ``sys.modules``
check would pass no matter what the engine imports.
"""

from __future__ import annotations

import subprocess
import sys

# Modules the engine must not reach. `yaml` is absent on purpose: mandate files
# are YAML and PyYAML is a declared dependency (see pyproject.toml).
FORBIDDEN_ROOTS = (
    "pandas",
    "numpy",
    "scipy",
    "matplotlib",
    "textual",
    "rich",
    "dotenv",
    "requests",
    "kiteconnect",
    "langchain",
    "langchain_anthropic",
    "langchain_openai",
    "langchain_deepseek",
    "langchain_google_genai",
    "langchain_xai",
    "hedge_fund",
)

ENGINE_MODULES = (
    "bq.aihf.engine.api",
    "bq.aihf.engine.backtesting",
    "bq.aihf.engine.paper",
    "bq.aihf.engine.scorecard",
    "bq.aihf.engine.risk.exposures",
    "bq.aihf.engine.features.breakpoints",
)

_PROBE = """
import importlib, json, sys
for name in {modules!r}:
    importlib.import_module(name)
roots = {roots!r}
leaked = sorted({{
    module.split(".")[0] for module in sys.modules
    if module.split(".")[0] in roots
}})
print(json.dumps(leaked))
"""


def _import_graph() -> list[str]:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE.format(modules=ENGINE_MODULES, roots=FORBIDDEN_ROOTS),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    import json

    return list(json.loads(completed.stdout.strip().splitlines()[-1]))


def test_engine_imports_without_vendor_or_heavy_dependencies() -> None:
    assert _import_graph() == []


def test_engine_does_not_read_a_user_home() -> None:
    """`paths` is injected by the app. Importing the engine must not resolve
    ``~/.hedge-fund`` — a second hidden home outside the app's backup story."""
    from bq.aihf.engine import paths

    assert ".hedge-fund" not in str(paths.root())


def test_injected_root_wins_over_the_default() -> None:
    from bq.aihf.engine import paths

    original = paths.root()
    try:
        paths.set_root("/tmp/bq-engine-root")
        assert paths.paper_dir().as_posix() == "/tmp/bq-engine-root/paper"
        assert paths.llm_cache_dir().as_posix() == "/tmp/bq-engine-root/cache/llm"
    finally:
        paths.set_root(original)


def test_breakpoints_never_fetch_and_degrade_to_none(monkeypatch, tmp_path) -> None:
    """A blind prompt shows size as a percentile of US stocks. This app has no
    such table and must not download one; the prompt omits size instead."""
    from bq.aihf.engine.features import breakpoints

    monkeypatch.setattr(breakpoints, "DEFAULT_CACHE_PATH", tmp_path / "absent.csv")
    monkeypatch.setattr(breakpoints, "_shared", None)
    assert breakpoints.shared_breakpoints() is None
