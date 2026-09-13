"""Unit tests for core media handlers."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core.media.handlers.tvshow import TVShowMediaHandler
from app.models.media import Language, LibType, MediaItem, MediaLib
from app.services.media import MediaItemService


@pytest.mark.parametrize(
    ("title", "expected"),
    [(None, "Example"), ("", "Example"), ("Library title", "Library title")],
)
def test_tvshow_title(monkeypatch, tmp_path: Path, title: str | None, expected: str):
    path = tmp_path / "Example (2024)" / "Example S01E01.mkv"
    path.parent.mkdir()
    path.touch()
    lib = MediaLib(
        id=1, dir=str(tmp_path), lib_type=LibType.TV_SHOW, language=Language.EN_US
    )
    monkeypatch.setattr(
        MediaItemService,
        "create",
        AsyncMock(side_effect=[MediaItem(id=1, title=title), MediaItem(id=2)]),
    )

    result = asyncio.run(TVShowMediaHandler().gen_items(lib, path))

    assert result[1].title == expected
