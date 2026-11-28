"""The ``aihf`` sidecar: an isolated install of ``virattt/ai-hedge-fund`` v2.

``aihf`` requires ``pandas<3``/``numpy<2`` and cannot share BharatQuant's
environment, so it is provisioned into its own virtual environment
(``just aihf-install``) and driven as a subprocess. This package owns the
paths, the scoped environment, the process runners and the read-only parsers
over the files aihf writes.
"""

from bq.aihf.config import (
    AiHfConfigError,
    AiHfPaths,
    installed_version,
    resolve_paths,
    validate_name,
)

__all__ = [
    "AiHfConfigError",
    "AiHfPaths",
    "installed_version",
    "resolve_paths",
    "validate_name",
]
