"""Unit tests for reading source discovery and filesystem event ownership."""

import asyncio
from pathlib import Path

import pytest
from lxml import etree
from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirModifiedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileOpenedEvent,
)

from app.core.media.handlers import reading
from app.core.media.handlers.base import get_handler
from app.models.media import LibType, MediaFormat, MediaLib


def _files(root: Path, *paths: str):
    """Create fixtures whose bytes are intentionally irrelevant to discovery.

    Args:
        root: The temporary library directory.
        paths: Relative files to create beneath the root.
    """
    for relative in paths:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"source")


def test_novel_layout(tmp_path):
    _files(
        tmp_path,
        "Book2/story.TXT",
        "Book2/metadata.opf",
        "Book10/story.EPUB",
        "Book10/cover.jpg",
        "Book10/assets/extra.png",
        "Empty/metadata.opf",
        "Empty/cover.jpg",
        "Ambiguous/story.txt",
        "Ambiguous/story.epub",
        "Author/Book/story.txt",
        "loose.txt",
        "Book2/story.txt.part",
    )
    result = get_handler(LibType.NOVEL).scan_sources(str(tmp_path))

    assert [
        (source.path.relative_to(tmp_path).as_posix(), source.format)
        for source in result.sources
    ] == [
        ("Book2/story.TXT", MediaFormat.TXT),
        ("Book10/story.EPUB", MediaFormat.EPUB),
    ]
    assert all(
        source.parent_path is None and not source.pages for source in result.sources
    )
    assert result.sources[0].directory == tmp_path / "Book2"
    assert result.issues == {
        tmp_path / "loose.txt": "unsupported_layout",
        tmp_path / "Ambiguous": "ambiguous_layout",
        tmp_path / "Author": "unsupported_layout",
    }


def test_comic_layout(tmp_path):
    _files(
        tmp_path,
        "Album/10.png",
        "Album/2.JPG",
        "Album/01.gif",
        "Album/1.webp",
        "Album/COVER.JPEG",
        "Album/Folder.png",
        "Album/poster.webp",
        "Album/ComicInfo.xml",
        "Archive/book.CBZ",
        "Archive/cover.jpg",
        "Series/cover.jpg",
        "Series/ComicInfo.xml",
        "Series/Chapter10/chapter.ZIP",
        "Series/Chapter10/ComicInfo.xml",
        "Series/Chapter2/1.jpg",
        "Series/Chapter2/2.jpg",
        "Series/Empty/cover.jpg",
        "Empty/ComicInfo.xml",
        "loose.zip",
    )
    result = get_handler(LibType.COMIC).scan_sources(str(tmp_path))

    assert [
        (source.path.relative_to(tmp_path).as_posix(), source.format)
        for source in result.sources
    ] == [
        ("Album", MediaFormat.DIR),
        ("Archive/book.CBZ", MediaFormat.CBZ),
        ("Series", None),
        ("Series/Chapter2", MediaFormat.DIR),
        ("Series/Chapter10/chapter.ZIP", MediaFormat.ZIP),
    ]
    assert [path.name for path in result.sources[0].pages] == [
        "01.gif",
        "1.webp",
        "2.JPG",
        "10.png",
    ]
    assert all(source.parent_path is None for source in result.sources[:3])
    assert all(
        source.parent_path == tmp_path / "Series" for source in result.sources[3:]
    )
    assert result.sources[0].directory == tmp_path / "Album"
    assert result.sources[-1].directory == tmp_path / "Series/Chapter10"
    assert result.issues == {tmp_path / "loose.zip": "unsupported_layout"}


@pytest.mark.parametrize(
    ("paths", "scope", "code"),
    [
        (("book.cbz", "other.zip"), "", "ambiguous_layout"),
        (("book.cbz", "1.jpg"), "", "ambiguous_layout"),
        (("1.jpg", "Chapter/1.jpg"), "", "ambiguous_layout"),
        (("book.cbz", "Chapter/1.jpg"), "", "ambiguous_layout"),
        (("Chapter/book.cbz", "Chapter/1.jpg"), "Chapter", "ambiguous_layout"),
        (("Volume/Chapter/1.jpg",), "Volume", "unsupported_layout"),
    ],
)
def test_comic_conflict(tmp_path, paths, scope, code):
    work = tmp_path / "Work"
    _files(work, *paths)
    result = get_handler(LibType.COMIC).scan_sources(str(tmp_path))

    assert not result.sources
    assert result.issues == {work / scope: code}
    assert all((work / path).read_bytes() == b"source" for path in paths)


def test_chapter_failure(tmp_path):
    _files(
        tmp_path, "Work/Chapter1/1.jpg", "Work/Chapter2/a.cbz", "Work/Chapter2/b.zip"
    )
    result = get_handler(LibType.COMIC).scan_sources(str(tmp_path))

    assert [source.path for source in result.sources] == [
        tmp_path / "Work",
        tmp_path / "Work/Chapter1",
    ]
    assert result.issues == {tmp_path / "Work/Chapter2": "ambiguous_layout"}


@pytest.mark.parametrize("lib_type", [LibType.NOVEL, LibType.COMIC])
def test_ignored_paths(tmp_path, lib_type):
    names = (
        ".hidden",
        "__MACOSX",
        "$RECYCLE.BIN",
        "System Volume Information",
        "Work.tmp",
        "Work.PART",
        "Work.download",
    )
    body = "story.txt" if lib_type == LibType.NOVEL else "1.jpg"
    _files(tmp_path, *(f"{name}/{body}" for name in names))
    _files(
        tmp_path,
        "Work/.hidden.txt",
        "Work/.hidden.jpg",
        "Work/1.jpg.crdownload",
        "Work/story.txt.partial",
        "Work/Thumbs.db",
    )
    (tmp_path / "Empty").mkdir()
    result = get_handler(lib_type).scan_sources(str(tmp_path))

    assert not result.sources
    assert not result.issues


def test_source_symlinks(tmp_path):
    root = tmp_path / "library"
    _files(root, "Work/1.jpg")
    _files(tmp_path, "outside/2.jpg")
    (root / "external").symlink_to(tmp_path / "outside", target_is_directory=True)
    (root / "alias").symlink_to(root / "Work", target_is_directory=True)
    (root / "Work/loop").symlink_to(root, target_is_directory=True)
    (root / "Work/2.jpg").symlink_to(tmp_path / "outside/2.jpg")
    (root / "Work/3.jpg").symlink_to(tmp_path / "missing.jpg")
    handler = get_handler(LibType.COMIC)
    result = handler.scan_sources(str(root))

    assert len(result.sources) == 1
    assert result.sources[0].pages == (root / "Work/1.jpg",)
    assert not result.issues
    scoped = handler.scan_sources(str(root), work_path=root / "external")
    assert not scoped.sources
    assert scoped.issues == {root / "external": "media_source_unavailable"}
    assert handler.scan_sources(str(root / "external")).issues == {
        root / "external": "media_source_unavailable"
    }


def test_natural_order():
    names = [
        "10.jpg",
        "2.jpg",
        "1.jpg",
        "01.jpg",
        "a10.jpg",
        "A2.jpg",
        "a2.jpg",
        "e\u03012.jpg",
        "é2.jpg",
    ]
    expected = [
        "01.jpg",
        "1.jpg",
        "2.jpg",
        "10.jpg",
        "A2.jpg",
        "a2.jpg",
        "a10.jpg",
        "e\u03012.jpg",
        "é2.jpg",
    ]
    assert sorted(names, key=reading.natural_key) == expected
    assert sorted(reversed(names), key=reading.natural_key) == expected
    assert reading.natural_key("１.jpg")[0] == ("１.jpg",)
    assert reading.natural_key("第十话")[0] == ("第十话",)


def test_source_limit(tmp_path, monkeypatch):
    _files(tmp_path, "Work/1.jpg", "Work/2.jpg", "Work/3.jpg")
    monkeypatch.setattr(reading, "MAX_PAGES", 2)
    result = get_handler(LibType.COMIC).scan_sources(str(tmp_path))

    assert not result.sources
    assert result.issues == {tmp_path / "Work": "media_limit_exceeded"}


def test_scoped_scan(tmp_path):
    _files(tmp_path, "Book1/1.txt", "Book2/2.epub")
    handler = get_handler(LibType.NOVEL)
    result = handler.scan_sources(str(tmp_path), work_path=tmp_path / "Book2")
    assert [source.path for source in result.sources] == [tmp_path / "Book2/2.epub"]

    for work in (
        tmp_path,
        tmp_path.parent / "outside",
        tmp_path / "Book1/deep",
        tmp_path / "..",
        tmp_path / ".hidden",
    ):
        with pytest.raises(ValueError):
            handler.scan_sources(str(tmp_path), work_path=work)
    missing = handler.scan_sources(str(tmp_path), work_path=tmp_path / "Missing")
    assert not missing.sources
    assert missing.issues == {tmp_path / "Missing": "media_source_unavailable"}

    for root in ("relative", str(tmp_path / "..")):
        with pytest.raises(ValueError):
            handler.scan_sources(root)
        with pytest.raises(ValueError):
            handler.resolve_event_targets(
                FileCreatedEvent(str(tmp_path / "Book1/1.txt")), base_path=root
            )


@pytest.mark.parametrize(
    "failed", ["", "Work", "Series/Chapter2", "Standalone/Unknown"]
)
def test_source_unavailable(tmp_path, monkeypatch, failed):
    _files(
        tmp_path,
        "Work/1.jpg",
        "Series/Chapter1/1.jpg",
        "Series/Chapter2/1.jpg",
        "Standalone/1.jpg",
    )
    (tmp_path / "Standalone/Unknown").mkdir()
    scandir = reading.os.scandir

    def denied(path):
        """Fail only the selected scope.

        Args:
            path: The directory requested by source discovery.

        Returns:
            The real directory iterator for accessible scopes.

        Raises:
            PermissionError: For the selected inaccessible scope.
        """
        if Path(path) == tmp_path / failed:
            raise PermissionError(str(path))
        return scandir(path)

    monkeypatch.setattr(reading.os, "scandir", denied)
    result = get_handler(LibType.COMIC).scan_sources(str(tmp_path))
    assert result.issues[tmp_path / failed] == "media_source_unavailable"
    if failed:
        assert tmp_path / "Series/Chapter1" in [
            source.path for source in result.sources
        ]
    else:
        assert not result.sources
    if failed == "Standalone/Unknown":
        assert result.issues[tmp_path / "Standalone"] == "media_source_unavailable"
        assert tmp_path / "Standalone" not in [source.path for source in result.sources]


@pytest.mark.parametrize(
    "event_type", [FileCreatedEvent, FileModifiedEvent, FileDeletedEvent]
)
@pytest.mark.parametrize(
    ("lib_type", "relative", "unit"),
    [
        (LibType.NOVEL, "Book/story.TXT", "Book"),
        (LibType.NOVEL, "Book/metadata.opf", "Book"),
        (LibType.NOVEL, "Book/content.opf", "Book"),
        (LibType.NOVEL, "Book/Novel.opf", "Book"),
        (LibType.NOVEL, "Book/Novel.OPF", "Book"),
        (LibType.NOVEL, "Book/cover.jpg", "Book"),
        (LibType.COMIC, "Book/1.jpg", "Book"),
        (LibType.COMIC, "Book/ComicInfo.XML", "Book"),
        (LibType.COMIC, "Book/Chapter/book.CBZ", "Book/Chapter"),
        (LibType.COMIC, "Book/Chapter/1.jpg", "Book/Chapter"),
        (LibType.COMIC, "Book/Chapter/ComicInfo.xml", "Book/Chapter"),
    ],
)
def test_file_events(tmp_path, event_type, lib_type, relative, unit):
    handler = get_handler(lib_type)
    assert isinstance(handler, reading.ReadingMediaHandler)
    event = event_type(str(tmp_path / relative))
    assert handler.resolve_event_targets(event, base_path=str(tmp_path)) == {
        tmp_path / "Book": {tmp_path / unit}
    }
    assert handler.filter_event(event, base_path=str(tmp_path)) is event


@pytest.mark.parametrize(
    "event_type", [DirCreatedEvent, DirModifiedEvent, DirDeletedEvent]
)
def test_directory_events(tmp_path, event_type):
    event = event_type(str(tmp_path / "Book/Chapter"))
    for lib_type, unit in ((LibType.NOVEL, "Book"), (LibType.COMIC, "Book/Chapter")):
        handler = get_handler(lib_type)
        assert isinstance(handler, reading.ReadingMediaHandler)
        assert handler.resolve_event_targets(event, base_path=str(tmp_path)) == {
            tmp_path / "Book": {tmp_path / unit}
        }


@pytest.mark.parametrize("lib_type", [LibType.NOVEL, LibType.COMIC])
@pytest.mark.parametrize(
    "relative",
    [
        "loose.txt",
        "loose.jpg",
        "../outside/1.jpg",
        "Book/.hidden/1.jpg",
        "Book/story.txt.tmp",
        "Book/page.jpg.PART",
        "Book/__MACOSX/page.jpg",
        "Book/file.nfo",
        "Book/readme.pdf",
    ],
)
def test_ignored_events(tmp_path, lib_type, relative):
    event = FileCreatedEvent(str(tmp_path / relative))
    assert get_handler(lib_type).filter_event(event, base_path=str(tmp_path)) is None


@pytest.mark.parametrize("event_type", [FileMovedEvent, DirMovedEvent])
def test_moved_events(tmp_path, event_type):
    suffix = "/1.jpg" if event_type is FileMovedEvent else ""
    source = str(tmp_path / f"Old/Chapter1{suffix}")
    destination = str(tmp_path / f"New/Chapter2{suffix}")
    event = event_type(source, destination)
    handler = get_handler(LibType.COMIC)
    assert handler.resolve_event_targets(event, base_path=str(tmp_path)) == {
        tmp_path / "Old": {tmp_path / "Old/Chapter1"},
        tmp_path / "New": {tmp_path / "New/Chapter2"},
    }
    assert handler.filter_event(event, base_path=str(tmp_path)) is event
    assert (event.src_path, event.dest_path, event.event_type) == (
        source,
        destination,
        "moved",
    )

    for moved in (
        event_type(source, str(tmp_path.parent / "outside")),
        event_type(str(tmp_path.parent / "outside"), source),
    ):
        assert handler.resolve_event_targets(moved, base_path=str(tmp_path)) == {
            tmp_path / "Old": {tmp_path / "Old/Chapter1"}
        }
        assert handler.filter_event(moved, base_path=str(tmp_path)) is moved
        assert moved.event_type == "moved"


def test_event_boundaries(tmp_path):
    handler = get_handler(LibType.COMIC)
    for event in (
        DirModifiedEvent(str(tmp_path)),
        FileOpenedEvent(str(tmp_path / "Book/1.jpg")),
        FileCreatedEvent(
            str(tmp_path.with_name(tmp_path.name + "extra") / "Book/1.jpg")
        ),
    ):
        assert handler.filter_event(event, base_path=str(tmp_path)) is None
    move = FileMovedEvent(
        str(tmp_path / "Book/1.jpg.part"), str(tmp_path / "Book/1.jpg")
    )
    assert handler.resolve_event_targets(move, base_path=str(tmp_path)) == {
        tmp_path / "Book": {tmp_path / "Book"}
    }
    encoded = FileDeletedEvent(bytes(tmp_path / "书/1.jpg"))
    assert handler.resolve_event_targets(encoded, base_path=str(tmp_path)) == {
        tmp_path / "书": {tmp_path / "书"}
    }


def test_registered_handlers(tmp_path):
    assert all(get_handler(lib_type) for lib_type in LibType)
    for lib_type in (LibType.NOVEL, LibType.COMIC):
        handler = get_handler(lib_type)
        with pytest.raises(NotImplementedError, match="source discovery"):
            handler.hierarchies()
        with pytest.raises(NotImplementedError, match="NFO"):
            handler.extract_meta(etree.ElementTree())
        with pytest.raises(NotImplementedError, match="content indexing"):
            asyncio.run(handler.gen_items(MediaLib(lib_type=lib_type), tmp_path))
