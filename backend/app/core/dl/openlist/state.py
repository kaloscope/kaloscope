from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from random import random

from app.core.dl.driver import DownloadState
from app.models.download import OfflineDownloadErrorKind


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    """A normalized progress update produced by an OpenList download phase."""

    percentage: float
    completed_size: int | None = None
    total_size: int | None = None
    dl_speed: int | None = None


# resume each retryable failure from the earliest phase that can safely recover it
_S = DownloadState
_RETRY_TARGETS = {
    OfflineDownloadErrorKind.INSTANCE_AUTH: _S.PULLING,
    OfflineDownloadErrorKind.INSTANCE_RATE_LIMIT: _S.PULLING,
    OfflineDownloadErrorKind.INSTANCE_TRANSIENT: _S.PULLING,
    OfflineDownloadErrorKind.REMOTE_FAILED: _S.REMOTE,
    OfflineDownloadErrorKind.TRANSFER_FAILED: _S.SETTLING,
    OfflineDownloadErrorKind.MANIFEST_INVALID: _S.SETTLING,
    OfflineDownloadErrorKind.DIRECT_LINK_UNAVAILABLE: _S.PULLING,
    OfflineDownloadErrorKind.LOCAL_PATH_INVALID: _S.SETTLING,
    OfflineDownloadErrorKind.LOCAL_FILE_CONFLICT: _S.PULLING,
    OfflineDownloadErrorKind.PULL_FAILED: _S.PULLING,
    OfflineDownloadErrorKind.VERIFY_FAILED: _S.SETTLING,
}

# `DOWNLOADING` belongs to RPC drivers and is invalid in the OpenList lifecycle
_OPENLIST_STATES = set(DownloadState) - {DownloadState.DOWNLOADING}


def retry_state(kind: OfflineDownloadErrorKind) -> DownloadState | None:
    """Get the OpenList state from which a failed task can be retried.

    Args:
        kind: The persisted failure category.

    Returns:
        The safe recovery state, or `None` when retry is unsupported.
    """
    return _RETRY_TARGETS.get(kind)


def state_progress(state: DownloadState, current: float | None) -> ProgressUpdate:
    """Apply the progress assigned to an OpenList lifecycle state.

    Args:
        state: The lifecycle state being entered.
        current: The previously persisted progress percentage.

    Raises:
        ValueError: If `state` is not part of the OpenList lifecycle.

    Returns:
        The progress update for the state.
    """
    if state not in _OPENLIST_STATES:
        raise ValueError(f"Unsupported OpenList state: {state}")
    if state is DownloadState.PULLING:
        return ProgressUpdate(0)
    if state in {
        DownloadState.SETTLING,
        DownloadState.VERIFYING,
        DownloadState.COMPLETED,
    }:
        return ProgressUpdate(100)
    return ProgressUpdate(_percentage(current))


def remote_progress(current: float | None, percentage: float) -> ProgressUpdate:
    """Apply remote offline-tool progress directly.

    Args:
        current: The previously persisted remote progress percentage.
        percentage: The progress reported by the OpenList offline tool.

    Returns:
        A monotonic remote progress update.
    """
    return ProgressUpdate(_percentage(current, percentage))


def pull_progress(
    completed_size: int, total_size: int, dl_speed: int
) -> ProgressUpdate:
    """Calculate local file transfer progress from transferred bytes.

    Args:
        completed_size: The number of bytes already transferred locally.
        total_size: The total number of bytes to transfer locally.
        dl_speed: The current local transfer speed in bytes per second.

    Returns:
        The local file transfer progress update.
    """
    completed_size = max(completed_size, 0)
    total_size = max(total_size, 0)
    ratio = min(completed_size / total_size, 1) if total_size else 0
    return ProgressUpdate(
        ratio * 100,
        completed_size,
        total_size,
        max(dl_speed, 0),
    )


def poll_delay(
    changed: bool,
    unchanged_count: int,
    *,
    base_interval: float = 10,
    max_interval: float = 60,
) -> tuple[float, int]:
    """Choose the next poll delay from whether remote state changed.

    Args:
        changed: Whether the latest poll observed a meaningful state change.
        unchanged_count: The number of consecutive unchanged polls.
        base_interval: The configured active polling interval in seconds.
        max_interval: The configured idle polling interval in seconds.

    Returns:
        The jittered delay and updated unchanged-poll count.
    """
    count = 0 if changed else max(unchanged_count, 0) + 1
    base = max(base_interval, 5)
    maximum = max(max_interval, base)
    # poll actively after a change, then back off through `3x` to the idle interval
    delay = base if count == 0 else maximum
    if count == 1:
        delay = min(base * 3, maximum)
    return _jitter(delay), count


def transient_delay(retry_count: int) -> tuple[float, int]:
    """Calculate exponential backoff for a transient OpenList failure.

    Args:
        retry_count: The number of consecutive transient failures.

    Returns:
        The jittered delay and incremented retry count.
    """
    retry_count = max(retry_count, 0)
    delay = min(30 * 2 ** min(retry_count, 4), 300)
    return _jitter(delay, maximum=300), retry_count + 1


def rate_limit_delay(retry_after: str | None, now: datetime) -> float:
    """Calculate a rate-limit delay without retrying before `Retry-After`.

    Args:
        retry_after: A `Retry-After` value expressed as seconds or an HTTP date.
        now: The current timezone-aware timestamp used for HTTP-date calculation.

    Returns:
        A jittered delay in seconds that is never shorter than the requested delay.
    """
    delay = 60.0
    if retry_after:
        try:
            if retry_after.strip().isdigit():
                delay = float(int(retry_after))
            else:
                parsed = parsedate_to_datetime(retry_after)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                delay = max((parsed - now).total_seconds(), 0)
        except (TypeError, ValueError, OverflowError):
            pass
    minimum = max(delay, 5)
    return _jitter(minimum, minimum=minimum)


def _percentage(current: float | None, floor: float = 0) -> float:
    """Clamp a progress percentage to the valid range.

    Args:
        current: The current progress percentage.
        floor: The minimum valid progress.

    Returns:
        The clamped progress percentage.
    """
    return max(min(current if current is not None else 0, 100), floor)


def _jitter(delay: float, *, minimum: float = 5, maximum: float | None = None) -> float:
    """Add random jitter to a delay to avoid polling bursts.

    Args:
        delay: The original delay in seconds.
        minimum: The minimum delay in seconds.
        maximum: The maximum delay in seconds.

    Returns:
        The jittered delay in seconds.
    """
    factor = 0.8 + min(max(random(), 0), 1) * 0.4
    result = max(delay * factor, minimum)
    return min(result, maximum) if maximum is not None else result
