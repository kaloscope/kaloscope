import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from app.core.dl.openlist.client import OpenListClient
from app.core.dl.openlist.manifest import (
    RemoteManifestEntry,
    build_manifest,
)
from app.core.dl.openlist.models import RemoteEntry, RemoteEntryPage

ROOT = "/Kaloscope/task-uuid"
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


class FakeClient:
    def __init__(self, pages: dict[tuple[str, int], RemoteEntryPage]):
        self.pages = pages
        self.calls: list[tuple[str, int, bool]] = []

    async def list(
        self, path: str, page: int, per_page: int, refresh: bool = False
    ) -> RemoteEntryPage:
        self.calls.append((path, page, refresh))
        return self.pages[(path, page)]


def _entry(
    name: str,
    *,
    size: int = 0,
    is_dir: bool = False,
) -> RemoteEntry:
    return RemoteEntry(
        name=name,
        size=size,
        is_dir=is_dir,
    )


def _page(*entries: RemoteEntry, total: int | None = None) -> RemoteEntryPage:
    return RemoteEntryPage(
        content=list(entries),
        total=len(entries) if total is None else total,
    )


def _build(pages: dict[tuple[str, int], RemoteEntryPage]):
    return asyncio.run(build_manifest(cast(OpenListClient, FakeClient(pages)), ROOT))


def test_manifest():
    result = _build(
        {
            (ROOT, 1): _page(
                _entry("z.mkv", size=3),
                _entry("folder", is_dir=True),
                total=3,
            ),
            (ROOT, 2): _page(_entry("a.mkv", size=1), total=3),
            (f"{ROOT}/folder", 1): _page(
                _entry("b.mkv", size=2),
                _entry("empty", is_dir=True),
            ),
            (f"{ROOT}/folder/empty", 1): _page(),
        }
    )

    assert [(entry.path, entry.is_dir, entry.size) for entry in result] == [
        ("a.mkv", False, 1),
        ("folder", True, 0),
        ("folder/b.mkv", False, 2),
        ("folder/empty", True, 0),
        ("z.mkv", False, 3),
    ]


def test_recursive_refresh():
    client = FakeClient(
        {
            (ROOT, 1): _page(_entry("folder", is_dir=True)),
            (f"{ROOT}/folder", 1): _page(),
        }
    )

    asyncio.run(build_manifest(cast(OpenListClient, client), ROOT, refresh=True))

    assert client.calls == [
        (ROOT, 1, True),
        (f"{ROOT}/folder", 1, True),
    ]


@pytest.mark.parametrize(("seconds", "stable"), [(14, False), (15, True)])
def test_stability(seconds: int, stable: bool):
    from app.core.dl.openlist import manifest

    entries = (RemoteManifestEntry(path="movie.mkv", is_dir=False, size=1),)
    first = manifest.observe_manifest(
        entries,
        previous_fingerprint=None,
        changed_at=None,
        now=NOW,
    )
    observation = manifest.observe_manifest(
        entries,
        previous_fingerprint=first.fingerprint,
        changed_at=first.changed_at,
        now=NOW + timedelta(seconds=seconds),
    )

    assert observation.changed is False
    assert observation.changed_at == NOW
    assert observation.stable is stable
    assert not observation.needs_refresh
