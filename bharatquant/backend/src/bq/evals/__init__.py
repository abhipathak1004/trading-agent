"""Local model evaluation: Promptfoo sweeps, scoring, and scorecards.

Promptfoo runs as a separate sequential process (one local LLM resident at a
time); this package owns the config parsing, persistence and scoring around it.
Scoring itself is delegated to the repo-root ``evals/score_promptfoo_results.py``
so the CLI and the API always agree on the numbers.
"""

from bq.evals.config import (
    EvalConfigError,
    EvalsPaths,
    ProviderSpec,
    parse_promptfoo_config,
    read_categories,
    tool_availability,
)
from bq.evals.scoring import (
    EvalCase,
    ScoringError,
    analyze_results_file,
    export_scorecard_csv,
    load_cases,
    load_scorer,
)
from bq.evals.store import (
    EvalModelScore,
    EvalRun,
    EvalRunActive,
    EvalRunNotFound,
    EvalStore,
)

__all__ = [
    "EvalCase",
    "EvalConfigError",
    "EvalModelScore",
    "EvalRun",
    "EvalRunActive",
    "EvalRunNotFound",
    "EvalStore",
    "EvalsPaths",
    "ProviderSpec",
    "ScoringError",
    "analyze_results_file",
    "export_scorecard_csv",
    "load_cases",
    "load_scorer",
    "parse_promptfoo_config",
    "read_categories",
    "tool_availability",
]
