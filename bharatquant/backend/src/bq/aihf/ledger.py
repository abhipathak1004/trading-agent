"""Read a paper fund's ledger and check its hash chain.

Two different questions, deliberately separated:

* **Structure** (this module, cheap, always available): are the records in
  session order, is each one's ``prev_hash`` the previous record's ``hash``,
  and are there duplicates? This catches a gap, a reorder, or an edit.
* **Authenticity** (``probe/verify_ledger.py``, runs in the sidecar): does
  every ``hash`` still equal ``compute_hash()`` of its contents? Only aihf's
  own model can answer that, because the hash covers its serialisation.

``chain_verified`` therefore means "structured and aihf-verified"; a fund whose
ledger cannot be read structurally is reported with ``problem`` rather than
failing the endpoint.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from bq.aihf.config import AiHfConfigError, AiHfPaths, validate_name
from bq.aihf.parsing import (
    AiHfParseError,
    LedgerView,
    SessionView,
    parse_session,
)

LEDGER_DIRNAME = "ledger"
SUPERSEDED_DIRNAME = "superseded"
DAY_FILE = re.compile(r"\d{4}-\d{2}-\d{2}")
BAR_FILE = re.compile(r"(\d{2})(\d{2})(\d{2})")
MAX_LEDGER_RECORDS = 5_000


class LedgerReadError(RuntimeError):
    """The fund's ledger directory cannot be read at all."""


def ledger_dir(paths: AiHfPaths, fund: str) -> Path:
    return paths.fund_dir(fund) / LEDGER_DIRNAME


def session_keys(directory: Path) -> list[str]:
    """Every recorded session in chain order (daily files, then bar dirs).

    Mirrors ``Ledger.sessions`` in aihf: ``2026-10-05.json`` for a daily fund,
    ``2026-10-05/100500.json`` for a minute fund.
    """
    if not directory.is_dir():
        return []
    found: list[str] = []
    for entry in directory.iterdir():
        if entry.name == SUPERSEDED_DIRNAME:
            continue
        if (
            entry.is_file()
            and entry.suffix == ".json"
            and DAY_FILE.fullmatch(entry.stem)
        ):
            found.append(entry.stem)
        elif entry.is_dir() and DAY_FILE.fullmatch(entry.name):
            for bar in entry.glob("*.json"):
                match = BAR_FILE.fullmatch(bar.stem)
                if match:
                    found.append(f"{entry.name}T{':'.join(match.groups())}")
    return sorted(found)


def record_path(directory: Path, session: str) -> Path:
    if "T" in session:
        day, clock = session.split("T", 1)
        return directory / day / f"{clock.replace(':', '')}.json"
    return directory / f"{session}.json"


def read_records(directory: Path, sessions: list[str]) -> list[SessionView]:
    records: list[SessionView] = []
    for session in sessions:
        path = record_path(directory, session)
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError) as exc:
            raise AiHfParseError(
                f"{path.name}: cannot read the session record: {exc}"
            ) from exc
        record = parse_session(payload)
        if record.session != session:
            raise AiHfParseError(
                f"{path.name}: file is named {session} but records {record.session}"
            )
        records.append(record)
    return records


def structural_problem(records: list[SessionView]) -> str | None:
    """The first link that does not hold, or ``None`` when the chain is sound."""
    previous: SessionView | None = None
    for record in records:
        if previous is not None:
            if record.session <= previous.session:
                return f"session {record.session} is not after {previous.session}"
            if record.prev_hash != previous.hash:
                return (
                    f"chain broken at session {record.session}: prev_hash does not "
                    f"match the record before it"
                )
        elif record.prev_hash is not None:
            return (
                f"the first session {record.session} links to a record that is not here"
            )
        if not record.hash:
            return f"session {record.session} carries no hash"
        previous = record
    return None


def read_ledger(
    paths: AiHfPaths,
    fund: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> LedgerView:
    """The fund's chain with a structural verdict.

    ``limit``/``offset`` page from the **newest** end, because the ledger of a
    long-running fund is mostly history nobody scrolls to.
    """
    name = validate_name(fund, what="paper fund")
    directory = ledger_dir(paths, name)
    if not directory.is_dir():
        raise LedgerReadError(f"paper fund {name!r} has no ledger yet")
    sessions = session_keys(directory)
    if len(sessions) > MAX_LEDGER_RECORDS:
        sessions = sessions[-MAX_LEDGER_RECORDS:]
    try:
        records = read_records(directory, sessions)
    except AiHfParseError as exc:
        return LedgerView(sessions=(), chain_verified=False, problem=str(exc))

    problem = structural_problem(records)
    if limit is not None:
        start = max(0, len(records) - offset - limit)
        records = records[start : len(records) - offset]
    return LedgerView(
        sessions=tuple(records), chain_verified=problem is None, problem=problem
    )


def read_head(paths: AiHfPaths, fund: str) -> tuple[str | None, int]:
    """``(head hash, session count)`` — what a preview is pinned to."""
    view = read_ledger(paths, fund)
    return view.head, len(view.sessions)


def control_state(paths: AiHfPaths, fund: str) -> dict[str, object]:
    """The kill switch, read exactly as aihf writes it (``control.json``)."""
    name = validate_name(fund, what="paper fund")
    path = paths.fund_dir(name) / "control.json"
    if not path.is_file():
        return {"halted": False, "reason": None, "since": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        raise AiHfConfigError(f"{name}: cannot read the kill switch: {exc}") from exc
    if not isinstance(payload, dict):
        raise AiHfConfigError(f"{name}: the kill switch is not an object")
    return {
        "halted": bool(payload.get("halted")),
        "reason": payload.get("reason")
        if isinstance(payload.get("reason"), str)
        else None,
        "since": payload.get("since")
        if isinstance(payload.get("since"), str)
        else None,
    }


def recent_events(
    paths: AiHfPaths, fund: str, *, limit: int = 50
) -> list[dict[str, object]]:
    """The tail of ``events.jsonl`` (halts, failures, gaps, checkpoints)."""
    name = validate_name(fund, what="paper fund")
    path = paths.fund_dir(name) / "events.jsonl"
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    events: list[dict[str, object]] = []
    for line in lines[-limit:]:
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events
