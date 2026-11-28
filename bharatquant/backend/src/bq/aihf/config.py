"""Where the ``aihf`` sidecar lives, and the files it owns.

``aihf`` (the PyPI distribution for ``virattt/ai-hedge-fund``) cannot be
installed into BharatQuant's environment: it requires ``pandas<3`` and
``numpy<2``, while this project is locked to ``pandas>=3.1`` and
``numpy>=2.5``. It is therefore provisioned into its own virtual environment
and driven as a subprocess (see ``bq.aihf.service``).

Its import name is ``hedge_fund``; ``aihf`` is only the console script. The
distribution depends on ``Path.home()``, so every run is given a scoped
``HOME`` under this project's data directory. That relocates ``mandates/``,
``paper/``, ``research/``, ``cache/`` and notably ``~/.hedge-fund/.env``, so a
key written by another tool can never be picked up by accident.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ENV_AIHF_VENV = "BQ_AIHF_VENV"
SIDECAR_DIRNAME = ".aihf-venv"
INSTALLED_VERSION = "2.7.2"

# aihf's own user directory, relative to the HOME we hand it.
_HEDGE_FUND_DIRNAME = ".hedge-fund"
MANDATES_DIRNAME = "mandates"
RESEARCH_DIRNAME = "research"
PAPER_DIRNAME = "paper"

# A fund name is also a directory name in aihf (see paper/deployed.py).
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class AiHfConfigError(ValueError):
    """The sidecar is missing, or a path/name cannot be used."""


def validate_name(name: str, *, what: str = "name") -> str:
    """Reject anything that is not a flat file name (no separators, no dots-only).

    These become path components under the aihf home, so traversal must be
    impossible here rather than at each call site.
    """
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
    root: Path
    venv: Path

    # ---- the aihf home (what aihf sees as HOME) ---------------------------

    @property
    def home(self) -> Path:
        """The ``HOME`` every aihf subprocess is given."""
        return self.root / "home"

    @property
    def user_dir(self) -> Path:
        """``$HOME/.hedge-fund`` — aihf's own data root."""
        return self.home / _HEDGE_FUND_DIRNAME

    @property
    def mandates(self) -> Path:
        return self.user_dir / MANDATES_DIRNAME

    @property
    def research(self) -> Path:
        return self.user_dir / RESEARCH_DIRNAME

    @property
    def paper(self) -> Path:
        return self.user_dir / PAPER_DIRNAME

    def fund_dir(self, name: str) -> Path:
        return self.paper / validate_name(name, what="paper fund")

    def mandate_path(self, name: str) -> Path:
        return self.mandates / f"{validate_name(name, what='mandate')}.yaml"

    # ---- BharatQuant-side working areas ----------------------------------

    @property
    def runs(self) -> Path:
        """One directory per backtest run: its log, progress journal and result."""
        return self.root / "runs"

    @property
    def previews(self) -> Path:
        """A computed-but-unapproved session, waiting for the owner's decision."""
        return self.root / "previews"

    @property
    def sandbox(self) -> Path:
        """Scratch HOMEs for preview runs. Never read by the live fund."""
        return self.root / "sandbox"

    def sandbox_home(self, token: str) -> Path:
        return self.sandbox / validate_name(token, what="sandbox token")

    # ---- the interpreter and console script ------------------------------

    @property
    def python(self) -> Path:
        return self.venv / "bin" / "python"

    @property
    def executable(self) -> Path:
        return self.venv / "bin" / "aihf"

    @property
    def available(self) -> bool:
        return self.python.is_file() and self.executable.is_file()

    def ensure(self) -> None:
        """Create only the directories BharatQuant owns.

        ``mandates/`` and ``paper/`` are deliberately *not* created here:
        aihf seeds the mandates example only when that directory is absent
        (``hedge_fund.paths.ensure_mandates_dir``), so pre-creating it would
        silently suppress the example.
        """
        for directory in (self.runs, self.previews, self.sandbox, self.home):
            directory.mkdir(parents=True, exist_ok=True)


def _default_sidecar(repo_root: Path | None = None) -> Path:
    override = os.environ.get(ENV_AIHF_VENV)
    if override:
        return Path(override).expanduser().resolve()
    return (repo_root or find_repo_root()) / SIDECAR_DIRNAME


def resolve_paths(
    *, database_path: Path | None = None, repo_root: Path | None = None
) -> AiHfPaths:
    """Anchor everything under the project's data directory (``bq.db``'s parent)."""
    if database_path is not None:
        root = Path(database_path).expanduser().parent / "aihf"
    elif override := os.environ.get("BQ_HOME"):
        root = Path(override).expanduser() / "aihf"
    else:
        root = _default_sidecar(repo_root).parent / "aihf"
    return AiHfPaths(root=root, venv=_default_sidecar(repo_root))


def installed_version(paths: AiHfPaths) -> str | None:
    """The installed aihf version, read from its dist-info without a subprocess."""
    if not paths.venv.is_dir():
        return None
    for metadata in paths.venv.glob(
        "lib/python*/site-packages/aihf-*.dist-info/METADATA"
    ):
        try:
            text = metadata.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
    return None
