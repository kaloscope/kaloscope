import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.user import MediaProgressQuery, MediaProgressStatus, UserMediaProgress
from app.services.user import UserMediaProgressService


@pytest.mark.parametrize(
    ("percentage", "status"),
    [
        (0, MediaProgressStatus.WATCHING),
        (20, MediaProgressStatus.WATCHING),
        (79, MediaProgressStatus.WATCHING),
        (80, MediaProgressStatus.WATCHED),
        (100, MediaProgressStatus.WATCHED),
    ],
)
def test_media_progress_status_from_percentage(
    percentage: int, status: MediaProgressStatus
):
    assert UserMediaProgressService.status_from_percentage(percentage) == status


def test_list_media_progress_filters_restricted_libraries(monkeypatch):
    query_result = object()
    filter_mock = MagicMock(return_value=query_result)
    dump_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(UserMediaProgress, "filter", filter_mock)
    monkeypatch.setattr(UserMediaProgressService, "dump_list", dump_mock)
    user = SimpleNamespace(
        id=7, perms=SimpleNamespace(media_lib_ids=[11, 13])
    )

    result = asyncio.run(
        UserMediaProgressService.list(user, MediaProgressQuery(ids=[2, 3]))
    )

    assert result == []
    filter_mock.assert_called_once_with(
        user_id=7,
        media_id__in=[2, 3],
        media__lib_id__in=[11, 13],
    )
    dump_mock.assert_awaited_once_with(query_result)
