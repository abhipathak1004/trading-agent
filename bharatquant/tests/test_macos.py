from datetime import UTC, datetime, timedelta

from bq.macos import detect_sleep_gap, launchd_plist


def test_detect_sleep_gap_uses_wall_minus_monotonic() -> None:
    before = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    after = before + timedelta(seconds=120)
    gap = detect_sleep_gap(before, 100.0, after, 105.0)
    assert gap is not None
    assert gap.duration == timedelta(seconds=120)


def test_detect_sleep_gap_ignores_normal_elapsed_drift() -> None:
    before = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    after = before + timedelta(seconds=11)
    gap = detect_sleep_gap(before, 100.0, after, 111.0)
    assert gap is None


def test_launchd_plist_contains_local_loopback_process() -> None:
    plist = launchd_plist(
        label="com.bharatquant.scheduler",
        program="/opt/homebrew/bin/uv",
        start_interval_seconds=30,
        working_directory="/Users/owner/BharatQuant",
    )
    assert "<key>StartInterval</key><integer>30</integer>" in plist
    assert "<string>/opt/homebrew/bin/uv</string>" in plist
