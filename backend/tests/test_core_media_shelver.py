"""Unit tests for NFO publication and media shelving."""

import asyncio
import threading
from pathlib import Path

import pytest
from lxml import etree

from app.core.media import shelver
from app.models.media import NFOType


def test_nfo_publish(tmp_path, monkeypatch):
    path = tmp_path / "movie.nfo"
    publish = shelver.rename_exclusive
    observed = []

    def inspect_publication(source, destination):
        assert not destination.exists()
        observed.append(etree.parse(source).getroot().findtext("title"))
        publish(source, destination)

    monkeypatch.setattr(shelver, "rename_exclusive", inspect_publication)

    async def run():
        assert await shelver.gen_nfo(NFOType.MOVIE, str(path), {"title": "First"})
        assert not await shelver.gen_nfo(NFOType.MOVIE, str(path), {"title": "Second"})
        assert observed == ["First"]
        assert etree.parse(path).getroot().findtext("title") == "First"
        path.chmod(0o600)
        assert await shelver.gen_nfo(
            NFOType.MOVIE, str(path), {"title": "Updated"}, overwrite=True
        )
        assert path.stat().st_mode & 0o777 == 0o600
        assert etree.parse(path).getroot().findtext("title") == "Updated"
        assert list(tmp_path.iterdir()) == [path]

    asyncio.run(run())


@pytest.mark.parametrize("title", ["A" * 240, "影" * 80])
def test_long_name(tmp_path, title):
    path = tmp_path / f"{title}.nfo"

    async def run():
        assert await shelver.gen_nfo(NFOType.MOVIE, str(path), {"title": title})
        assert etree.parse(path).getroot().findtext("title") == title

        assert await shelver.gen_nfo(
            NFOType.MOVIE, str(path), {"title": "Updated"}, overwrite=True
        )
        assert etree.parse(path).getroot().findtext("title") == "Updated"
        assert list(tmp_path.iterdir()) == [path]

    asyncio.run(run())


@pytest.mark.parametrize("overwrite", [False, True])
def test_publication_failure(tmp_path, monkeypatch, overwrite):
    path = tmp_path / "movie.nfo"
    original = "<movie><title>Original</title></movie>"
    if overwrite:
        path.write_text(original)

    def failed_render(*args, **kwargs):
        raise ValueError("Render failed")

    monkeypatch.setattr(shelver, "render", failed_render)

    async def run():
        with pytest.raises(ValueError, match="Render failed"):
            await shelver.gen_nfo(
                NFOType.MOVIE, str(path), {"title": "Updated"}, overwrite=overwrite
            )

    asyncio.run(run())

    if overwrite:
        assert path.read_text() == original
        assert list(tmp_path.iterdir()) == [path]
    else:
        assert not list(tmp_path.iterdir())


def test_publication_conflict(tmp_path, monkeypatch):
    path = tmp_path / "movie.nfo"
    original = "<movie><title>Existing</title></movie>"
    publish = shelver.rename_exclusive

    def competing_publication(source, destination):
        destination.write_text(original)
        publish(source, destination)

    monkeypatch.setattr(shelver, "rename_exclusive", competing_publication)

    result = asyncio.run(shelver.gen_nfo(NFOType.MOVIE, str(path), {"title": "New"}))

    assert result is False
    assert path.read_text() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    ("failure", "cleanup_failure"),
    [(None, False), (OSError, False), (FileExistsError, False), (OSError, True)],
)
def test_publish_cancellation(tmp_path, monkeypatch, failure, cleanup_failure):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    publish = shelver.rename_exclusive
    unlink = Path.unlink

    def failed_cleanup(path, *args, **kwargs):
        if path.name.startswith(".nfo-"):
            raise OSError("Temporary file cleanup failed")
        return unlink(path, *args, **kwargs)

    def delayed_publish(source, destination):
        started.set()
        assert release.wait(timeout=5)
        try:
            if failure is not None:
                raise failure("Publication failed")
            publish(source, destination)
        finally:
            finished.set()

    monkeypatch.setattr(shelver, "rename_exclusive", delayed_publish)
    if cleanup_failure:
        monkeypatch.setattr(Path, "unlink", failed_cleanup)

    async def run():
        nfo = tmp_path / "movie.nfo"
        task = asyncio.create_task(
            shelver.gen_nfo(NFOType.MOVIE, str(nfo), {"title": "Movie"})
        )
        try:
            assert await asyncio.to_thread(started.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
            assert not finished.is_set()
            assert not task.done()
            assert len(list(tmp_path.glob(".nfo-*.tmp"))) == 1
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert finished.is_set()
        if failure is None:
            assert etree.parse(nfo).getroot().findtext("title") == "Movie"
        else:
            assert not nfo.exists()
        temporary = list(tmp_path.glob(".nfo-*.tmp"))
        if cleanup_failure:
            assert len(temporary) == 1
            assert etree.parse(temporary[0]).getroot().findtext("title") == "Movie"
        else:
            assert not temporary

    asyncio.run(run())
