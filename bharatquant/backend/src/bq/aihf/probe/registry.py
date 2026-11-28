"""Emit aihf's model registry as JSON. Runs inside the sidecar interpreter.

The registry is aihf's own source of truth for provider routing, so reading it
through aihf (rather than copying it into BharatQuant) means an upstream
registry change is picked up on the next install.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    try:
        from importlib.metadata import PackageNotFoundError, version

        from hedge_fund.llm.registry import PROVIDER_ENV_VARS, load_api_models

        try:
            installed = version("aihf")
        except PackageNotFoundError:
            installed = None

        payload = {
            "ok": True,
            "version": installed,
            "models": [list(entry) for entry in load_api_models()],
            "env_vars": dict(PROVIDER_ENV_VARS),
        }
        sys.stdout.write(json.dumps(payload))
        return 0
    except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
        sys.stdout.write(
            json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
