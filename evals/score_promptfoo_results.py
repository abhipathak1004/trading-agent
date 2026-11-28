#!/usr/bin/env python3
"""Parse Promptfoo results.json and compute per-model trading evaluation metrics.

Computes:
  - Overall and per-category pass rates (earnings, rbi_macro, corporate_action,
    noise_filtering, edge_tool_calls)
  - Latency statistics (p50, p95, max in milliseconds)
  - JSON parse rate on news_extraction tasks
  - Brier calibration score and 5-bin Expected Calibration Error (ECE) on
    extracted `confidence` values vs. task accuracy

Usage:
  PROMPTFOO_DISABLE_TELEMETRY=1 npx promptfoo@latest eval --max-concurrency 1 -o results.json
  python3 score_promptfoo_results.py results.json
  python3 score_promptfoo_results.py results.json --csv-out scorecard.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_result_items(payload: Any) -> list[dict[str, Any]]:
    """Extract the flat list of evaluation result records from Promptfoo JSON."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ValueError("Unsupported Promptfoo results.json root structure.")

    results_node = payload.get("results", payload)
    if isinstance(results_node, list):
        return results_node
    if isinstance(results_node, dict):
        for key in ("results", "outputs"):
            if isinstance(results_node.get(key), list):
                return results_node[key]
    raise ValueError("Could not locate result rows in Promptfoo JSON.")


def get_provider_name(item: dict[str, Any]) -> str:
    prov = item.get("provider")
    if isinstance(prov, dict):
        return str(prov.get("label") or prov.get("id") or "unknown")
    if prov:
        return str(prov)
    return str(item.get("providerId") or "unknown")


def get_category(item: dict[str, Any]) -> str:
    test_case = item.get("testCase") or {}
    meta = test_case.get("metadata") or item.get("metadata") or {}
    if isinstance(meta, dict) and meta.get("category"):
        return str(meta["category"])
    vars_dict = item.get("vars") or test_case.get("vars") or {}
    if isinstance(vars_dict, dict):
        for k in ("__metadata:category", "category"):
            if vars_dict.get(k):
                return str(vars_dict[k])
    return "uncategorized"


def get_task_type(item: dict[str, Any]) -> str:
    test_case = item.get("testCase") or {}
    vars_dict = item.get("vars") or test_case.get("vars") or {}
    if isinstance(vars_dict, dict) and vars_dict.get("task_type"):
        return str(vars_dict["task_type"])
    return "unknown"


def get_raw_output(item: dict[str, Any]) -> str:
    resp = item.get("response")
    if isinstance(resp, dict) and resp.get("output") is not None:
        out = resp["output"]
    else:
        out = item.get("output", "")
    if isinstance(out, str):
        return out
    return json.dumps(out)


def get_accuracy_outcome(item: dict[str, Any]) -> bool:
    """Prefer the golden_task_accuracy assertion if present; else fall back to overall success."""
    grading = item.get("gradingResult") or {}
    components = grading.get("componentResults") or []
    for comp in components:
        assertion = comp.get("assertion") or {}
        if assertion.get("metric") == "golden_task_accuracy":
            return bool(comp.get("pass"))
    return bool(item.get("success"))


def percentile(values: list[float], pct: float) -> float | None:
    """Compute linear-interpolated percentile (0-100) without external deps."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    weight = rank - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def compute_ece(pairs: list[tuple[float, float]], num_bins: int = 5) -> float | None:
    """Compute Expected Calibration Error over equal-width bins in [0, 1]."""
    if not pairs:
        return None
    n = len(pairs)
    ece = 0.0
    for b in range(num_bins):
        lo = b / num_bins
        hi = (b + 1) / num_bins
        bucket = [
            (p, y)
            for p, y in pairs
            if (lo <= p < hi) or (b == num_bins - 1 and p == 1.0)
        ]
        if not bucket:
            continue
        avg_conf = sum(p for p, _ in bucket) / len(bucket)
        avg_acc = sum(y for _, y in bucket) / len(bucket)
        ece += (len(bucket) / n) * abs(avg_conf - avg_acc)
    return ece


def analyze_results(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_categories: set[str] = set()

    for item in items:
        model = get_provider_name(item)
        cat = get_category(item)
        all_categories.add(cat)
        by_model[model].append(item)

    sorted_cats = sorted(all_categories)
    summaries: list[dict[str, Any]] = []

    for model, rows in sorted(by_model.items()):
        total = len(rows)
        overall_pass = sum(1 for r in rows if bool(r.get("success")))
        acc_pass = sum(1 for r in rows if get_accuracy_outcome(r))

        latencies = [
            float(r["latencyMs"])
            for r in rows
            if isinstance(r.get("latencyMs"), (int, float)) and r["latencyMs"] >= 0
        ]

        cat_stats: dict[str, str] = {}
        cat_rates: dict[str, float | None] = {}
        for cat in sorted_cats:
            cat_rows = [r for r in rows if get_category(r) == cat]
            if not cat_rows:
                cat_stats[cat] = "-"
                cat_rates[cat] = None
            else:
                passed = sum(1 for r in cat_rows if bool(r.get("success")))
                rate = passed / len(cat_rows)
                cat_rates[cat] = rate
                cat_stats[cat] = f"{rate * 100:.1f}% ({passed}/{len(cat_rows)})"

        # Brier calibration & JSON parse tracking on news_extraction tasks
        news_rows = [r for r in rows if get_task_type(r) == "news_extraction"]
        json_valid = 0
        brier_pairs: list[tuple[float, float]] = []

        for r in news_rows:
            raw = get_raw_output(r).strip()
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(parsed, dict):
                continue
            json_valid += 1
            conf = parsed.get("confidence")
            if isinstance(conf, (int, float)) and 0.0 <= float(conf) <= 1.0:
                y = 1.0 if get_accuracy_outcome(r) else 0.0
                brier_pairs.append((float(conf), y))

        brier_score = (
            sum((p - y) ** 2 for p, y in brier_pairs) / len(brier_pairs)
            if brier_pairs
            else None
        )
        ece = compute_ece(brier_pairs, num_bins=5)

        summaries.append(
            {
                "model": model,
                "total": total,
                "overall_pass_rate": overall_pass / total if total else 0.0,
                "accuracy_pass_rate": acc_pass / total if total else 0.0,
                "p50_ms": percentile(latencies, 50),
                "p95_ms": percentile(latencies, 95),
                "max_ms": max(latencies) if latencies else None,
                "json_parse_rate": (json_valid / len(news_rows)) if news_rows else None,
                "brier_score": brier_score,
                "ece_5bin": ece,
                "brier_n": len(brier_pairs),
                "cat_stats": cat_stats,
                "cat_rates": cat_rates,
            }
        )

    return summaries, sorted_cats


def fmt_pct(val: float | None) -> str:
    return f"{val * 100:.1f}%" if val is not None else "n/a"


def fmt_ms(val: float | None) -> str:
    return f"{val:.0f} ms" if val is not None else "n/a"


def fmt_num(val: float | None, decimals: int = 4) -> str:
    return f"{val:.{decimals}f}" if val is not None else "n/a"


def print_report(summaries: list[dict[str, Any]], categories: list[str]) -> None:
    print("\n=== Promptfoo Local Trading LLM Scorecard ===\n")
    headers = [
        "Model",
        "Overall",
        "Task Acc",
        "JSON Parse",
        "p50 Lat",
        "p95 Lat",
        "Brier (↓)",
        "ECE-5 (↓)",
        "Calib N",
    ]
    rows = [
        [
            s["model"],
            fmt_pct(s["overall_pass_rate"]),
            fmt_pct(s["accuracy_pass_rate"]),
            fmt_pct(s["json_parse_rate"]),
            fmt_ms(s["p50_ms"]),
            fmt_ms(s["p95_ms"]),
            fmt_num(s["brier_score"]),
            fmt_num(s["ece_5bin"]),
            str(s["brier_n"]),
        ]
        for s in summaries
    ]
    _print_table(headers, rows)

    print("\n=== Pass Rate by Category ===\n")
    cat_headers = ["Model"] + categories
    cat_rows = [
        [s["model"]] + [s["cat_stats"].get(c, "-") for c in categories]
        for s in summaries
    ]
    _print_table(cat_headers, cat_rows)
    print()


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    sep = " | "
    print(sep.join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(sep.join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def export_csv(summaries: list[dict[str, Any]], categories: list[str], out_path: Path) -> None:
    fieldnames = [
        "model",
        "total_cases",
        "overall_pass_rate",
        "accuracy_pass_rate",
        "json_parse_rate",
        "p50_ms",
        "p95_ms",
        "max_ms",
        "brier_score",
        "ece_5bin",
        "brier_n",
    ] + [f"cat_{c}_pass_rate" for c in categories]

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for s in summaries:
            row: dict[str, Any] = {
                "model": s["model"],
                "total_cases": s["total"],
                "overall_pass_rate": round(s["overall_pass_rate"], 4),
                "accuracy_pass_rate": round(s["accuracy_pass_rate"], 4),
                "json_parse_rate": round(s["json_parse_rate"], 4) if s["json_parse_rate"] is not None else "",
                "p50_ms": round(s["p50_ms"], 1) if s["p50_ms"] is not None else "",
                "p95_ms": round(s["p95_ms"], 1) if s["p95_ms"] is not None else "",
                "max_ms": round(s["max_ms"], 1) if s["max_ms"] is not None else "",
                "brier_score": round(s["brier_score"], 5) if s["brier_score"] is not None else "",
                "ece_5bin": round(s["ece_5bin"], 5) if s["ece_5bin"] is not None else "",
                "brier_n": s["brier_n"],
            }
            for c in categories:
                r = s["cat_rates"].get(c)
                row[f"cat_{c}_pass_rate"] = round(r, 4) if r is not None else ""
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute per-model category pass rates, p95 latency, and Brier calibration from Promptfoo results.json."
    )
    parser.add_argument("results_json", type=Path, help="Path to Promptfoo results.json")
    parser.add_argument("--csv-out", type=Path, default=None, help="Optional path to export scorecard CSV")
    args = parser.parse_args()

    with args.results_json.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    items = load_result_items(payload)
    summaries, categories = analyze_results(items)
    print_report(summaries, categories)

    if args.csv_out:
        export_csv(summaries, categories, args.csv_out)
        print(f"Exported scorecard CSV to {args.csv_out}")


if __name__ == "__main__":
    main()