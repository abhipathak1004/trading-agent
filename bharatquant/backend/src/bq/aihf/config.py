"""Where the AI Hedge Fund's data lives, and what a fund name may be.

Upstream the engine anchored everything under ``~/.hedge-fund`` and ran from a
separate virtual environment. Both are gone (ADR-053): the engine shares this
app's environment, and its data lives under ``<BQ_HOME>/aihf`` beside ``bq.db``
so one backup, one gitignore and one data directory cover the whole app.

What remains here is the paths and the name rule. The name rule is not cosmetic:
a fund name is a directory name, and a mandate name is a file name, so
``validate_name`` is the path-traversal guard every route depends on.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from bq.aihf.engine import paths as engine_paths

MANDATES_DIRNAME = "mandates"
RESEARCH_DIRNAME = "research"
PAPER_DIRNAME = "paper"

# A fund name is also a directory name; a mandate name is a file name. Rejects
# separators, traversal and a bare "." or "..".
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class AiHfConfigError(ValueError):
    """The paths or a name cannot be used."""


def validate_name(name: str, *, what: str = "name") -> str:
    """Reject anything that is not a flat file name (no separators, no dots-only)."""
    if not NAME_PATTERN.fullmatch(name) or name in (".", ".."):
        raise AiHfConfigError(
            f"invalid {what} {name!r}: use letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    return name


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up from this file until a directory holding ``pyproject.toml``."""
    anchor = (start or Path(__file__)).resolve()
    for candidate in [anchor, *anchor.parents]:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise AiHfConfigError(f"Could not locate pyproject.toml above {anchor}.")


@dataclass(frozen=True, slots=True)
class AiHfPaths:
    """The data root, and the directories under it.

    ``root`` is injected (``<BQ_HOME>/aihf``). The engine's own ``paths`` module
    is pointed at the same root by `resolve_paths`, so the engine's ledger,
    prompt cache and this service's research/previews never disagree about where
    a fund is.
    """

    root: Path

    @property
    def mandates(self) -> Path:
        return self.root / MANDATES_DIRNAME

    @property
    def research(self) -> Path:
        return self.root / RESEARCH_DIRNAME

    @property
    def paper(self) -> Path:
        return self.root / PAPER_DIRNAME

    def fund_dir(self, name: str) -> Path:
        return self.paper / validate_name(name, what="paper fund")

    def mandate_path(self, name: str) -> Path:
        return self.mandates / f"{validate_name(name, what='mandate')}.yaml"

    def fund_meta_path(self, name: str) -> Path:
        """Where this service describes a fund to the UI (not the engine's files)."""
        return self.root / "funds" / f"{validate_name(name, what='paper fund')}.json"

    @property
    def runs(self) -> Path:
        """One directory per backtest run: its log, progress journal and result."""
        return self.root / "runs"

    @property
    def previews(self) -> Path:
        """A computed-but-unapproved session, waiting for the owner's decision."""
        return self.root / "previews"

    def ensure(self) -> None:
        """Create only the directories this service owns.

        ``mandates/`` is deliberately *not* created here: seeding it is what
        shows the owner an example to edit, and that happens on first use, not on
        a server start.
        """
        for directory in (self.runs, self.previews, self.root):
            directory.mkdir(parents=True, exist_ok=True)


def resolve_paths(
    *, database_path: Path | None = None, repo_root: Path | None = None
) -> AiHfPaths:
    """Anchor everything under the project's data directory (``bq.db``'s parent).

    As a side effect the engine's own ``paths.set_root`` is pointed at the same
    root: the engine writes a fund's ledger and prompt cache through its own
    module, so the two must agree or a preview and an approval would look at
    different directories.
    """
    if database_path is not None:
        root = Path(database_path).expanduser().parent / "aihf"
    elif override := os.environ.get("BQ_HOME"):
        root = Path(override).expanduser() / "aihf"
    else:
        root = (repo_root or find_repo_root()).parent / "aihf"
    engine_paths.set_root(root)
    return AiHfPaths(root=root)


def installed_version() -> str:
    """The vendored engine's version.

    The sidecar's dist-info is gone; the engine is source now, so the version is
    this app's own. Reported on the status card so the UI never has to guess.
    """
    return "in-process"


__all__ = [
    "AiHfConfigError",
    "AiHfPaths",
    "find_repo_root",
    "installed_version",
    "resolve_paths",
    "validate_name",
]
