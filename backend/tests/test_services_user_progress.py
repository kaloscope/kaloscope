import pytest

from app.models.user import MediaProgressStatus
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
