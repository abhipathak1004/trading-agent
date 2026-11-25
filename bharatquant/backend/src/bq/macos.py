from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


class MacOSOperationsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SleepGap:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.utcoffset() is None or self.end.utcoffset() is None:
            raise MacOSOperationsError("Sleep-gap timestamps must be timezone-aware.")
        if self.end < self.start:
            raise MacOSOperationsError("Sleep-gap end cannot precede start.")

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


def detect_sleep_gap(
    wall_before: datetime,
    monotonic_before: float,
    wall_after: datetime,
    monotonic_after: float,
    *,
    threshold_seconds: float = 10,
) -> SleepGap | None:
    if wall_before.utcoffset() is None or wall_after.utcoffset() is None:
        raise MacOSOperationsError("Heartbeat timestamps must be timezone-aware.")
    if (
        not isinstance(monotonic_before, (int, float))
        or isinstance(monotonic_before, bool)
        or not isinstance(monotonic_after, (int, float))
        or isinstance(monotonic_after, bool)
        or monotonic_after < monotonic_before
    ):
        raise MacOSOperationsError("Heartbeat monotonic values are invalid.")
    wall_elapsed = (wall_after - wall_before).total_seconds()
    mono_elapsed = monotonic_after - monotonic_before
    if wall_elapsed - mono_elapsed > threshold_seconds:
        return SleepGap(start=wall_before, end=wall_after)
    return None


def launchd_plist(
    *,
    label: str,
    program: str,
    start_interval_seconds: int,
    working_directory: str,
) -> str:
    if not label or not label.replace(".", "").replace("-", "").isalnum():
        raise MacOSOperationsError("Invalid launchd label.")
    if not program or not working_directory:
        raise MacOSOperationsError(
            "Launchd program and working directory are required."
        )
    if (
        isinstance(start_interval_seconds, bool)
        or not isinstance(start_interval_seconds, int)
        or start_interval_seconds <= 0
    ):
        raise MacOSOperationsError("Launchd interval must be positive.")
    escaped_program = (
        program.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    escaped_directory = (
        working_directory.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"https://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f"  <key>Label</key><string>{label}</string>\n"
        "  <key>RunAtLoad</key><true/>\n"
        f"  <key>StartInterval</key><integer>{start_interval_seconds}</integer>\n"
        "  <key>ProgramArguments</key><array>\n"
        f"    <string>{escaped_program}</string>\n"
        "  </array>\n"
        f"  <key>WorkingDirectory</key><string>{escaped_directory}</string>\n"
        "</dict></plist>\n"
    )


def write_launchd_plist(path: Path, content: str) -> None:
    if not isinstance(path, Path) or not content:
        raise MacOSOperationsError("Launchd plist path/content is invalid.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
