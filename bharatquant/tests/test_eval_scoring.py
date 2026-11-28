from pathlib import Path

from bq.evals.config import (
    parse_promptfoo_config,
    read_categories,
    resolve_paths,
)
from bq.evals.scoring import (
    analyze_results_file,
    export_scorecard_csv,
    load_cases,
    load_scorer,
)

FIXTURE = Path(__file__).parent / "fixtures" / "eval_results_sample.json"


def _scorer_path() -> Path:
    return resolve_paths().scorer


def test_analyze_results_computes_per_model_scorecards() -> None:
    summaries, categories = analyze_results_file(_scorer_path(), FIXTURE)

    assert categories == ["earnings", "edge_tool_calls", "noise_filtering"]
    by_model = {summary["model"]: summary for summary in summaries}

    gemma = by_model["Gemma 4 12B (MLX)"]
    assert gemma["total"] == 2
    assert gemma["overall_pass_rate"] == 0.5
    assert gemma["accuracy_pass_rate"] == 0.5
    assert gemma["json_parse_rate"] == 1.0
    assert gemma["p50_ms"] == 6100.0
    assert gemma["cat_rates"]["earnings"] == 1.0
    assert gemma["cat_rates"]["noise_filtering"] == 0.0
    assert gemma["brier_n"] == 2

    qwen = by_model["Qwen3.5-9B"]
    assert qwen["total"] == 3
    assert qwen["overall_pass_rate"] == 1.0
    assert qwen["cat_rates"]["edge_tool_calls"] == 1.0
    assert qwen["p95_ms"] is not None


def test_brier_and_ece_are_computed_on_confidence() -> None:
    summaries, _ = analyze_results_file(_scorer_path(), FIXTURE)
    gemma = next(s for s in summaries if s["model"] == "Gemma 4 12B (MLX)")
    # (0.9 - 1)^2 + (0.55 - 0)^2 => (0.01 + 0.3025) / 2
    assert gemma["brier_score"] is not None
    assert round(gemma["brier_score"], 4) == 0.1563
    assert gemma["ece_5bin"] is not None


def test_export_csv_round_trips_the_scorecard() -> None:
    summaries, categories = analyze_results_file(_scorer_path(), FIXTURE)
    out = Path(__file__).parent / "fixtures" / "_scorecard.csv"
    try:
        export_scorecard_csv(_scorer_path(), summaries, categories, out)
        header = out.read_text(encoding="utf-8").splitlines()[0]
        assert header.startswith("model,total_cases,overall_pass_rate")
        assert "cat_earnings_pass_rate" in header
        assert len(out.read_text(encoding="utf-8").splitlines()) == 3
    finally:
        out.unlink(missing_ok=True)


def test_load_cases_exposes_outputs_and_failures() -> None:
    cases = load_cases(_scorer_path(), FIXTURE)
    assert len(cases) == 5

    failing = [case for case in cases if not case.passed]
    assert len(failing) == 1
    failed = failing[0]
    assert failed.model == "Gemma 4 12B (MLX)"
    assert failed.category == "noise_filtering"
    assert failed.confidence == 0.55
    assert failed.latency_ms == 8000.0
    assert failed.failure_reasons  # the assertion reason text is captured
    assert "Rumour only" in failed.raw_output


def test_scorer_module_loads_once_and_is_cached() -> None:
    first = load_scorer(_scorer_path())
    second = load_scorer(_scorer_path())
    assert first is second


def test_our_promptfoo_config_parses_active_providers() -> None:
    paths = resolve_paths()
    providers = parse_promptfoo_config(paths.config.read_text(encoding="utf-8"))
    assert [p.id for p in providers] == [
        "ollama:chat:gemma4:12b-mlx",
        "ollama:chat:qwen3.5:9b",
        "ollama:chat:granite4.2:8b",
    ]
    assert providers[0].label == "Gemma 4 12B (MLX)"
    assert providers[0].model == "gemma4:12b-mlx"
    # Commented-out mlx_lm.server providers must be ignored.
    assert all("openai" not in provider.id for provider in providers)


def test_categories_are_five_sorted_and_unique() -> None:
    assert read_categories(resolve_paths().dataset) == [
        "corporate_action",
        "earnings",
        "edge_tool_calls",
        "noise_filtering",
        "rbi_macro",
    ]
