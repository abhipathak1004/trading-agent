"""Authenticate a paper fund's ledger with aihf's own hash check.

Structural verification (session order, ``prev_hash`` links) is cheap and lives
in ``bq.aihf.ledger``. This probe answers the other half — is every ``hash``
still ``compute_hash()`` of its contents — which only aihf can answer, because
the digest covers its own serialisation.

Reads the fund directory path on stdin; writes one JSON object.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    directory = Path(sys.stdin.read().strip())
    try:
        from hedge_fund.paper import Ledger, LedgerError
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(json.dumps({"ok": False, "error": f"aihf unavailable: {exc}"}))
        return 0

    try:
        ledger = Ledger(directory)
        # full=True ignores checkpoints and walks every link, which is what
        # "chain verified" has to mean for the UI's badge.
        ledger.replay(0.0, full=True)
    except LedgerError as exc:
        sys.stdout.write(json.dumps({"ok": True, "verified": False, "error": str(exc)}))
        return 0
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(
            json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        )
        return 0

    sys.stdout.write(json.dumps({"ok": True, "verified": True, "error": None}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
