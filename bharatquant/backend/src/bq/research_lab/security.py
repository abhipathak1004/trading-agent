"""Path confinement and the no-code-surface invariant.

This module exists because of two published vulnerabilities in the upstream
project this feature is modelled on: CVE-2026-114107 (a shell tool passing
model-emitted commands to ``subprocess.run(shell=True)``, plus an unrestricted
``read_url``) and CVE-2026-114106 (path traversal).

The response here is **absence, not filtering**: no shell tool and no `read_url`
tool is ever registered by this package, so there is no capability to filter.
There is also no AST sandbox, because there is no generated-code surface —
strategies are a whitelist-validated declarative DSL. A validator for a
capability the app does not have would be theatre. What remains is the one real
boundary: every artifact read or write must resolve inside the workspace.

``FORBIDDEN_*`` is kept as data so a test can assert the package never acquires
these imports or calls, and so a future contributor sees the rule explicitly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

# Never import these from the Research Lab: each one is a network or process
# capability the agent must not have.
FORBIDDEN_IMPORTS: Final = frozenset(
    {
        "subprocess",
        "socket",
        "urllib",
        "http",
        "requests",
        "httpx",
        "ftplib",
        "telnetlib",
        "smtplib",
        "asyncio.subprocess",
    }
)

FORBIDDEN_CALLS: Final = frozenset(
    {"eval", "exec", "compile", "__import__", "system", "popen"}
)


class PathEscapeError(ValueError):
    """A path resolved outside the permitted workspace."""


def safe_resolve_path(user_path: str | Path, workspace_root: str | Path) -> Path:
    """Resolve ``user_path`` and require it to live inside ``workspace_root``.

    Resolution happens first, so a symlink pointing outside the root is caught:
    ``resolve()`` follows it and ``is_relative_to`` then fails. Traversal via
    ``..`` fails for the same reason — after resolution there is no ``..`` left.
    """
    root = Path(workspace_root).expanduser().resolve()
    candidate = Path(user_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved != root and not resolved.is_relative_to(root):
        raise PathEscapeError(
            f"Path resolves outside the workspace: {resolved.name!r}."
        )
    return resolved


def ensure_within(path: str | Path, workspace_root: str | Path) -> Path:
    """Alias kept separate so call sites read as an assertion, not a transform."""
    return safe_resolve_path(path, workspace_root)


__all__ = [
    "FORBIDDEN_CALLS",
    "FORBIDDEN_IMPORTS",
    "PathEscapeError",
    "ensure_within",
    "safe_resolve_path",
]
