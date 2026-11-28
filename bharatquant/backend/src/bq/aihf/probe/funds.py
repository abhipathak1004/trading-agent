"""List deployed paper funds, read through aihf's own ``DeployedFund`` model.

``fund.yaml`` is YAML and BharatQuant does not depend on PyYAML, so the file is
parsed here — in the interpreter that already has it — and validated by the
model aihf itself uses. A malformed fund reports ``error`` instead of being
silently guessed at.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    try:
        from hedge_fund.paper import list_deployed, load_deployed
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(json.dumps({"ok": False, "error": f"aihf unavailable: {exc}"}))
        return 0

    root = Path(sys.stdin.read().strip() or Path.home() / ".hedge-fund" / "paper")
    funds: list[dict[str, object]] = []
    try:
        directories = list_deployed(root)
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(
            json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        )
        return 0

    for directory in directories:
        entry: dict[str, object] = {"name": directory.name, "error": None}
        try:
            deployed = load_deployed(directory)
        except Exception as exc:  # noqa: BLE001 - one bad fund must not hide the rest
            entry["error"] = str(exc)
            funds.append(entry)
            continue
        spec = deployed.spec
        entry.update(
            {
                "mandate": spec.name,
                "universe": list(deployed.universe),
                "capital": spec.capital,
                "rebalance": spec.rebalance,
                "benchmark": spec.benchmark,
                "is_intraday": spec.is_intraday,
                "cadence_label": spec.cadence_label,
                "created": deployed.created,
                "strategies": [
                    {
                        "name": strategy.name,
                        "title": strategy.title,
                        "weight": strategy.weight,
                    }
                    for strategy in spec.strategies
                ],
            }
        )
        funds.append(entry)
    sys.stdout.write(json.dumps({"ok": True, "funds": funds}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
