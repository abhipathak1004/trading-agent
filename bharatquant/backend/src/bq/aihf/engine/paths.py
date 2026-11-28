"""Where a fund's user data lives.

Upstream anchors everything under ``~/.hedge-fund``. This port anchors it under
BharatQuant's own data directory instead (``<BQ_HOME>/aihf``): the app already
owns one home, one backup story and one gitignore, and a second hidden directory
in the user's home would be state the app cannot see or back up.

The root is therefore **injected**, not derived from ``Path.home()``. Call
``set_root`` once at process start (``bq/aihf/config.py::resolve_paths`` does);
before that every accessor falls back to a directory beside this package that
nothing writes to, so a stray import cannot touch a real home. Consumers read
the accessors at call time rather than binding a constant at import, so a root
set later than the import still takes effect.

    mandates/<name>.yaml           templates: the desk, no state
    paper/<name>/                  a deployed fund: fund.yaml, ledger/, broker.json,
                                   control.json, events.jsonl
    research/<fund>-<start>-<end>-<stamp>.json   backtest results, disposable
    cache/                         prompt cache and bar memo
"""

from __future__ import annotations

import os
from pathlib import Path

EXAMPLE_MANDATE = Path(__file__).resolve().parent / "fund" / "example.yaml"

# Until set_root runs, this points somewhere inert: a sibling of the package
# directory that no other code writes to and that is never created on import.
_UNSET_ROOT = Path(__file__).resolve().parent / ".unconfigured-home"
_root: Path | None = None


def set_root(root: Path | str) -> Path:
    """Anchor every path under *root*. Idempotent; returns the resolved root."""
    global _root
    _root = Path(root).expanduser()
    return _root


def root() -> Path:
    """The fund data root. Injected by the app; never ``Path.home()``."""
    return _root if _root is not None else _UNSET_ROOT


def mandates_dir() -> Path:
    return root() / "mandates"


def paper_dir() -> Path:
    return root() / "paper"


def research_dir() -> Path:
    return root() / "research"


def cache_dir() -> Path:
    return root() / "cache"


def llm_cache_dir() -> Path:
    return cache_dir() / "llm"


def data_cache_dir() -> Path:
    return cache_dir() / "data"


def ensure_mandates_dir() -> Path:
    """Create the mandates dir on first use, seeded with the example."""
    target = mandates_dir()
    if not target.exists():
        target.mkdir(parents=True)
        if EXAMPLE_MANDATE.is_file():
            target.joinpath("example.yaml").write_text(EXAMPLE_MANDATE.read_text())
    return target


def write_atomic(path: Path, text: str) -> None:
    """Write *text* to *path* via a sibling temp file and rename, so a crash
    mid-write leaves the old file intact rather than a truncated one."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
