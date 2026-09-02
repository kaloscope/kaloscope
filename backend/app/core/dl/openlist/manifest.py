import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from weakref import WeakValueDictionary

from app.core.dl.openlist.client import OpenListClient

_PAGE_SIZE = 100
_MAX_PAGES = 1_000
_MAX_ENTRIES = 100_000
_MAX_DEPTH = 64
_HASH_LENGTHS = {"md5": 32, "sha1": 40, "sha256": 64, "sha512": 128}
_HEX_DIGITS = frozenset("0123456789abcdef")
STABILITY_WINDOW = timedelta(seconds=15)
REFRESH_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True, slots=True, kw_only=True)
class RemoteManifestEntry:
    """Represent one canonical entry in a remote result manifest."""

    path: str
    is_dir: bool
    size: int
    hashes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "path", normalize_relative_path(self.path))
        if self.size < 0:
            raise ValueError("Manifest entry size cannot be negative")
        hashes = tuple((algorithm, value) for algorithm, value in self.hashes)
        object.__setattr__(self, "hashes", hashes)


@dataclass(frozen=True, slots=True, kw_only=True)
class ManifestObservation:
    """Describe one manifest stability observation."""

    fingerprint: str
    changed_at: datetime
    changed: bool
    stable: bool
    needs_refresh: bool


class RefreshLimiter:
    """Rate-limit forced directory refreshes for one OpenList instance."""

    def __init__(self):
        self._last_refresh: datetime | None = None
        self._waiting: dict[str, datetime] = {}

    def acquire(self, key: str, now: datetime) -> bool:
        """Acquire permission to request a refreshed listing.

        Args:
            key: The stable identifier of the job requesting a refresh.
            now: The current observation timestamp.

        Returns:
            `True` when the refresh interval has elapsed, otherwise `False`.
        """
        if self._last_refresh is not None and now < self._last_refresh:
            self._last_refresh = None
            self._waiting.clear()
        cutoff = now - REFRESH_INTERVAL * 2
        self._waiting = {
            candidate: requested
            for candidate, requested in self._waiting.items()
            if requested >= cutoff
        }
        self._waiting[key] = now
        if self._last_refresh is not None and now < (
            self._last_refresh + REFRESH_INTERVAL
        ):
            return False
        if next(iter(self._waiting)) != key:
            return False
        self._waiting.pop(key)
        self._last_refresh = now
        return True


_REFRESH_LIMITERS: WeakValueDictionary[str, RefreshLimiter] = WeakValueDictionary()


def shared_refresh_limiter(endpoint: str) -> RefreshLimiter:
    """Get the forced-refresh limiter shared by an OpenList endpoint.

    Args:
        endpoint: The normalized OpenList endpoint URL.

    Returns:
        The process-local limiter shared by active drivers for the endpoint.
    """
    limiter = _REFRESH_LIMITERS.get(endpoint)
    if limiter is None:
        limiter = RefreshLimiter()
        _REFRESH_LIMITERS[endpoint] = limiter
    return limiter


def normalize_relative_path(path: str) -> str:
    """Normalize and validate a relative OpenList result path.

    Args:
        path: The POSIX-style relative path.

    Raises:
        ValueError: If `path` is absolute, ambiguous, or unsafe.

    Returns:
        The canonical relative POSIX path.
    """
    if path.startswith("/") or "\\" in path or "\0" in path:
        raise ValueError("Remote path must be a canonical relative POSIX path")

    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Remote path must be a canonical relative POSIX path")
    return "/".join(parts)


async def build_manifest(
    client: OpenListClient, root: str, *, refresh: bool = False
) -> tuple[RemoteManifestEntry, ...]:
    """Build a bounded recursive manifest for a remote result directory.

    Args:
        client: The OpenList client used to list remote directories.
        root: The absolute remote result root.
        refresh: Whether the first page of each directory should refresh storage.

    Raises:
        ValueError: If paths, pagination, hashes, depth, or entry counts are unsafe.

    Returns:
        The canonical manifest entries sorted by relative path.
    """
    entries: list[RemoteManifestEntry] = []
    paths: set[str] = set()
    root = root.rstrip("/")

    async def walk(remote_dir: str, parent: str, depth: int):
        # bound recursion before issuing another remote request
        if depth > _MAX_DEPTH:
            raise ValueError("Remote manifest depth limit exceeded")
        seen = 0
        for page_number in range(1, _MAX_PAGES + 1):
            page = await client.list(
                remote_dir,
                page_number,
                _PAGE_SIZE,
                refresh=refresh and page_number == 1,
            )
            if not page.content:
                # reject incomplete pagination instead of accepting a partial result
                if seen < page.total:
                    raise ValueError("Remote manifest pagination did not advance")
                return
            for remote in page.content:
                path = normalize_relative_path(
                    f"{parent}/{remote.name}" if parent else remote.name
                )
                if path in paths:
                    raise ValueError("Duplicate remote manifest path")
                paths.add(path)
                entry = RemoteManifestEntry(
                    path=path,
                    is_dir=remote.is_dir,
                    size=remote.size,
                    hashes=() if remote.is_dir else _normalize_hashes(remote.hash_info),
                )
                entries.append(entry)
                if len(entries) > _MAX_ENTRIES:
                    raise ValueError("Remote manifest entry limit exceeded")
                if remote.is_dir:
                    await walk(f"{root}/{path}", path, depth + 1)
            seen += len(page.content)
            if seen >= page.total:
                return
        raise ValueError("Remote manifest page limit exceeded")

    await walk(root or "/", "", 0)
    return tuple(sorted(entries, key=lambda entry: entry.path))


def _normalize_hashes(values: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """Normalize supported hashes and discard unusable values.

    Args:
        values: The hashes reported by OpenList.

    Raises:
        ValueError: If one algorithm has conflicting valid values.

    Returns:
        The sorted supported hash pairs.
    """
    result: dict[str, str] = {}
    for raw_algorithm, raw_value in values.items():
        algorithm = raw_algorithm.lower().replace("-", "")
        value = raw_value.lower()
        length = _HASH_LENGTHS.get(algorithm)
        if (
            length is None
            or len(value) != length
            or not set(value).issubset(_HEX_DIGITS)
        ):
            continue
        if algorithm in result and result[algorithm] != value:
            raise ValueError("Conflicting remote hashes")
        result[algorithm] = value
    return tuple(sorted(result.items()))


def manifest_fingerprint(entries: Iterable[RemoteManifestEntry]) -> str:
    """Calculate a deterministic fingerprint for manifest stability checks.

    Args:
        entries: The canonical manifest entries.

    Returns:
        The hexadecimal SHA-256 fingerprint.
    """
    canonical = [
        (entry.path, entry.is_dir, entry.size, sorted(entry.hashes))
        for entry in sorted(entries, key=lambda entry: entry.path)
    ]
    payload = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def serialize_manifest(
    entries: Iterable[RemoteManifestEntry],
) -> list[dict[str, object]]:
    """Serialize manifest entries for database storage.

    Args:
        entries: The canonical manifest entries.

    Returns:
        The JSON-compatible manifest records.
    """
    return [
        {
            "path": entry.path,
            "is_dir": entry.is_dir,
            "size": entry.size,
            "hashes": dict(entry.hashes),
        }
        for entry in entries
    ]


def observe_manifest(
    entries: Iterable[RemoteManifestEntry],
    *,
    previous_fingerprint: str | None,
    changed_at: datetime | None,
    now: datetime,
) -> ManifestObservation:
    """Compare a manifest with its persisted stability state.

    A result becomes stable only after the same non-empty fingerprint remains
    unchanged for `STABILITY_WINDOW`.

    Args:
        entries: The latest canonical manifest entries.
        previous_fingerprint: The fingerprint saved by the previous observation.
        changed_at: The start of the persisted unchanged period.
        now: The current observation timestamp.

    Returns:
        The updated fingerprint, stability state, and refresh requirement.
    """
    entries = tuple(entries)
    fingerprint = manifest_fingerprint(entries)
    mismatch = previous_fingerprint is not None and fingerprint != previous_fingerprint
    changed = fingerprint != previous_fingerprint or changed_at is None
    candidate_since = changed_at if not changed and changed_at is not None else now
    stable = bool(entries) and not changed and now >= candidate_since + STABILITY_WINDOW
    return ManifestObservation(
        fingerprint=fingerprint,
        changed_at=candidate_since,
        changed=changed,
        stable=stable,
        needs_refresh=not entries or mismatch,
    )
