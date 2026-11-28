"""Validate a mandate YAML and summarise it, using aihf's own ``FundSpec``.

Runs inside the sidecar interpreter, because ``FundSpec`` is a pydantic model
and BharatQuant does not depend on PyYAML. The real schema decides what is
valid — not a reimplementation of it — so the form and aihf can never drift.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


def main() -> int:
    raw = sys.stdin.read()
    try:
        from hedge_fund.fund import load_spec
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(json.dumps({"ok": False, "error": f"aihf unavailable: {exc}"}))
        return 0

    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(raw)
        candidate = Path(handle.name)
    try:
        spec = load_spec(candidate)
    except Exception as exc:  # noqa: BLE001 - ValueError carries the schema detail
        sys.stdout.write(json.dumps({"ok": False, "error": f"{exc}"}))
        return 0
    finally:
        candidate.unlink(missing_ok=True)

    summary = {
        "name": spec.name,
        "schema_version": spec.schema_version,
        "capital": spec.capital,
        "rebalance": spec.rebalance,
        "benchmark": spec.benchmark,
        "is_intraday": spec.is_intraday,
        "has_universe_rule": spec.universe is not None,
        "allocation": spec.allocation is not None,
        "costs": spec.costs.model_dump() if hasattr(spec.costs, "model_dump") else None,
        "risk": spec.risk.model_dump(),
        "strategies": [
            {
                "name": strategy.name,
                "title": strategy.title,
                "weight": strategy.weight,
                "mode": strategy.blend.mode,
                "models": [
                    {"name": model.name, "weight": model.weight}
                    for model in strategy.models
                ],
            }
            for strategy in spec.strategies
        ],
    }
    sys.stdout.write(json.dumps({"ok": True, "summary": summary}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
