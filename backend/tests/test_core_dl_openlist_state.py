from datetime import UTC, datetime

import pytest

from app.core.dl.driver import DownloadState
from app.core.dl.openlist import state as state_module
from app.core.dl.openlist.state import (
    poll_delay,
    pull_progress,
    rate_limit_delay,
    remote_progress,
    retry_state,
    state_progress,
    transient_delay,
)
from app.models.download import OfflineDownloadErrorKind

S = DownloadState


def test_retry_state():
    expected = {
        OfflineDownloadErrorKind.SUBMIT_UNKNOWN: None,
        OfflineDownloadErrorKind.INSTANCE_AUTH: S.PULLING,
        OfflineDownloadErrorKind.INSTANCE_RATE_LIMIT: S.PULLING,
        OfflineDownloadErrorKind.INSTANCE_TRANSIENT: S.PULLING,
        OfflineDownloadErrorKind.REMOTE_TASK_MISSING: None,
        OfflineDownloadErrorKind.REMOTE_FAILED: S.REMOTE,
        OfflineDownloadErrorKind.TRANSFER_FAILED: S.SETTLING,
        OfflineDownloadErrorKind.MANIFEST_INVALID: S.SETTLING,
        OfflineDownloadErrorKind.DIRECT_LINK_UNAVAILABLE: S.PULLING,
        OfflineDownloadErrorKind.LOCAL_PATH_INVALID: S.SETTLING,
        OfflineDownloadErrorKind.LOCAL_FILE_CONFLICT: S.PULLING,
        OfflineDownloadErrorKind.PULL_FAILED: S.PULLING,
        OfflineDownloadErrorKind.VERIFY_FAILED: S.SETTLING,
        OfflineDownloadErrorKind.CLEANUP_FAILED: None,
    }
    actual = {kind: retry_state(kind) for kind in OfflineDownloadErrorKind}

    assert actual == expected


def test_progress():
    cases = [
        (DownloadState.SUBMITTING, None, 0),
        (DownloadState.SUBMIT_UNKNOWN, 0, 0),
        (DownloadState.REMOTE, 20, 20),
        (DownloadState.SETTLING, 20, 100),
        (DownloadState.PULLING, 50, 0),
        (DownloadState.PAUSED, 75, 75),
        (DownloadState.VERIFYING, 75, 100),
        (DownloadState.ERROR, 75, 75),
        (DownloadState.COMPLETED, 99, 100),
    ]

    for state, current, expected in cases:
        update = state_progress(state, current)
        assert update.percentage == expected
        assert update.completed_size is update.total_size is update.dl_speed is None

    remote = remote_progress(10, 25)
    update = pull_progress(completed_size=25, total_size=100, dl_speed=2048)
    complete = pull_progress(100, 100, 0)

    assert remote.percentage == 25
    assert update.percentage == 25
    assert update.completed_size == 25
    assert update.total_size == 100
    assert update.dl_speed == 2048
    assert complete.percentage == 100


def test_poll_delay(monkeypatch):
    monkeypatch.setattr(state_module, "random", lambda: 0.5)

    assert poll_delay(True, 8) == (10, 0)
    assert poll_delay(False, 0) == (30, 1)
    assert poll_delay(False, 1) == (60, 2)


def test_transient_delay(monkeypatch):
    monkeypatch.setattr(state_module, "random", lambda: 0.5)
    delays = [transient_delay(count) for count in range(6)]

    assert delays == [
        (30, 1),
        (60, 2),
        (120, 3),
        (240, 4),
        (300, 5),
        (300, 6),
    ]
    monkeypatch.setattr(state_module, "random", lambda: 1)
    assert transient_delay(4)[0] == 300


def test_rate_limit(monkeypatch):
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)

    monkeypatch.setattr(state_module, "random", lambda: 0)
    assert rate_limit_delay("120", now) == 120
    monkeypatch.setattr(state_module, "random", lambda: 1)
    assert rate_limit_delay("120", now) == pytest.approx(144)
    monkeypatch.setattr(state_module, "random", lambda: 0.5)
    assert rate_limit_delay("Mon, 03 Aug 2026 12:02:00 GMT", now) == 120
    monkeypatch.setattr(state_module, "random", lambda: 0)
    assert rate_limit_delay(None, now) == 60
    assert rate_limit_delay("invalid", now) == 60
