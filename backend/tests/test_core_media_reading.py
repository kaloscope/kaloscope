"""Tests for reading source discovery, stability and filesystem event ownership."""

import asyncio
import os
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

from app.core.media.common import ContentError
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
    ("lib_type", "filename"),
    [
        (LibType.NOVEL, "Book.txt"),
        (LibType.NOVEL, "Book.epub"),
        (LibType.COMIC, "Book.cbz"),
        (LibType.COMIC, "Book.zip"),
        (LibType.COMIC, "1.png"),
    ],
)
def test_source_snapshot(tmp_path, monkeypatch, lib_type, filename):
    """Observe body attributes without parsing bytes, including same-size edits.

    Args:
        tmp_path: The isolated library directory.
        monkeypatch: The fixture preventing body reads during observation.
        lib_type: The reading library type.
        filename: The body filename to observe.
    """
    _files(tmp_path, f"Work/{filename}")
    handler = get_handler(lib_type)
    assert isinstance(handler, reading.ReadingMediaHandler)
    work = tmp_path / "Work"
    path = work / filename
    before = path.stat()
    first = handler.snapshot_sources(str(tmp_path), work_path=work)
    path.write_bytes(b"edited")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    def forbidden(*args, **kwargs):
        """Reject any attempt to open a body file.

        Args:
            *args: Positional arguments supplied to Path.open.
            **kwargs: Keyword arguments supplied to Path.open.
        """
        raise AssertionError("source observation must not open body files")

    monkeypatch.setattr(Path, "open", forbidden)
    second = handler.snapshot_sources(str(tmp_path), work_path=work)
    assert len(first) == 64
    assert second != first
    assert handler.snapshot_sources(str(tmp_path), work_path=work) == second


def test_snapshot_scopes(tmp_path):
    """Ignore unrelated writes but include selected pages and work-level layout.

    Args:
        tmp_path: The isolated library directory.
    """
    _files(tmp_path, "Work/A/1.png", "Work/B/1.png", "Work/ComicInfo.xml")
    handler = get_handler(LibType.COMIC)
    work = tmp_path / "Work"
    targets = {work / "A"}

    def snapshot():
        """Return the selected chapter's current source digest."""
        return handler.snapshot_sources(str(tmp_path), work_path=work, targets=targets)

    first = snapshot()
    whole = handler.snapshot_sources(str(tmp_path), work_path=work)
    _files(tmp_path, "Work/B/2.png", "Work/.temporary", "Other/1.png")
    (work / "B/1.png").write_bytes(b"changed")
    assert snapshot() == first
    assert handler.snapshot_sources(str(tmp_path), work_path=work) != whole
    (work / "ComicInfo.xml").write_bytes(b"changed metadata")
    metadata = snapshot()
    assert metadata != first
    _files(tmp_path, "Work/C/1.png")
    assert snapshot() != metadata
    layout = snapshot()
    (work / "A/1.png").unlink()
    assert snapshot() != layout


def test_snapshot_absence(tmp_path):
    """Distinguish missing, empty and populated containers without deleting anything.

    Args:
        tmp_path: The isolated library directory.
    """
    handler = get_handler(LibType.COMIC)
    work = tmp_path / "Work"
    missing = handler.snapshot_sources(str(tmp_path), work_path=work)
    assert handler.snapshot_sources(str(tmp_path), work_path=work) == missing
    work.mkdir()
    empty = handler.snapshot_sources(str(tmp_path), work_path=work)
    assert empty != missing
    _files(tmp_path, "Work/A/1.png")
    assert handler.snapshot_sources(str(tmp_path), work_path=work) != empty
    chapter = handler.snapshot_sources(
        str(tmp_path), work_path=work, targets={work / "Missing"}
    )
    (work / "Missing").mkdir()
    assert (
        handler.snapshot_sources(
            str(tmp_path), work_path=work, targets={work / "Missing"}
        )
        != chapter
    )


@pytest.mark.parametrize("scope", ["root", "work", "chapter", "ancestor"])
def test_snapshot_symlink(tmp_path, scope):
    """Reject symlinks on every selected directory boundary.

    Args:
        tmp_path: The isolated filesystem root.
        scope: The directory boundary replaced by a symlink.
    """
    original = tmp_path / "Original"
    _files(original, "Library/Work/A/1.png")
    root = original / "Library"
    target = root / "Work/A"
    if scope == "ancestor":
        alias = tmp_path / "Alias"
        alias.symlink_to(original, target_is_directory=True)
        root = alias / "Library"
        target = root / "Work/A"
    else:
        link = {"root": root, "work": root / "Work", "chapter": target}[scope]
        moved = tmp_path / "Moved"
        link.rename(moved)
        link.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ContentError, match="media_source_unavailable"):
        get_handler(LibType.COMIC).snapshot_sources(
            str(root), work_path=root / "Work", targets={target}
        )


@pytest.mark.parametrize("scope", ["", "Work", "Work/A"])
def test_snapshot_unavailable(tmp_path, monkeypatch, scope):
    """Report stat permission errors rather than a stable missing source.

    Args:
        tmp_path: The isolated library directory.
        monkeypatch: The fixture injecting a stat failure.
        scope: The inaccessible directory relative to the library.
    """
    _files(tmp_path, "Work/A/1.png")
    original = Path.stat

    def denied(path, **kwargs):
        """Reject stat calls for the selected inaccessible directory.

        Args:
            path: The path whose attributes are requested.
            **kwargs: Options forwarded to Path.stat for accessible paths.

        Returns:
            The original filesystem attributes when the path is accessible.

        Raises:
            PermissionError: For the selected inaccessible directory.
        """
        if path == tmp_path / scope:
            raise PermissionError(str(path))
        return original(path, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    with pytest.raises(ContentError, match="media_source_unavailable"):
        get_handler(LibType.COMIC).snapshot_sources(
            str(tmp_path), work_path=tmp_path / "Work"
        )


@pytest.mark.parametrize(
    "targets", [set(), {"Other"}, {"Work/.Hidden"}, {"Work/A/Deep"}, {"Work/../Other"}]
)
def test_snapshot_targets(tmp_path, targets):
    """Reject invalid selections before accessing their sources.

    Args:
        tmp_path: The isolated library directory.
        targets: Invalid relative target paths.
    """
    with pytest.raises(ValueError):
        get_handler(LibType.COMIC).snapshot_sources(
            str(tmp_path),
            work_path=tmp_path / "Work",
            targets={tmp_path / target for target in targets},
        )
    with pytest.raises(ValueError):
        get_handler(LibType.NOVEL).snapshot_sources(
            str(tmp_path), work_path=tmp_path / "Work", targets={tmp_path / "Work/A"}
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


@pytest.mark.parametrize(
    ("lib_type", "relative", "forced"),
    [
        (LibType.NOVEL, "Work/Book.TXT", "Work"),
        (LibType.NOVEL, "Work/Book.epub", "Work"),
        (LibType.NOVEL, "Work/metadata.opf", None),
        (LibType.NOVEL, "Work/illustration.png", None),
        (LibType.COMIC, "Work/Chapter/Book.CBZ", "Work/Chapter"),
        (LibType.COMIC, "Work/Chapter/Book.zip", "Work/Chapter"),
        (LibType.COMIC, "Work/Chapter/1.PNG", "Work/Chapter"),
        (LibType.COMIC, "Work/Chapter/COVER.jpg", None),
        (LibType.COMIC, "Work/folder.png", None),
        (LibType.COMIC, "Work/poster.gif", None),
        (LibType.COMIC, "Work/ComicInfo.XML", None),
        (LibType.COMIC, "Work/.hidden/1.png", None),
        (LibType.COMIC, "Work/Chapter/1.png.part", None),
        (LibType.COMIC, "Work/../Outside/1.png", None),
    ],
)
def test_content_events(tmp_path, lib_type, relative, forced):
    """Distinguish body changes from metadata, covers and ignored file events.

    Args:
        tmp_path: The isolated library root.
        lib_type: The reading library type.
        relative: The event path relative to the root.
        forced: The expected forced container, or None for an incremental check.
    """
    handler = get_handler(lib_type)
    assert isinstance(handler, reading.ReadingMediaHandler)
    for event_type in (FileCreatedEvent, FileModifiedEvent, FileDeletedEvent):
        event = event_type(str(tmp_path / relative))
        assert handler.resolve_content_targets(event, base_path=str(tmp_path)) == (
            {tmp_path / "Work": {tmp_path / forced}} if forced else {}
        )


@pytest.mark.parametrize(
    ("src", "dest", "forced"),
    [
        ("Old/cover.png", "New/1.png", {"New"}),
        ("Old/1.png", "New/cover.png", {"Old"}),
        ("Old/1.png", "New/2.png", {"Old", "New"}),
        ("Old/ComicInfo.xml", "New/ComicInfo.xml", set()),
        ("Old/1.png.part", "New/1.png", {"New"}),
        ("../Outside/1.png", "New/1.png", {"New"}),
    ],
)
def test_content_moves(tmp_path, src, dest, forced):
    """Classify both ends of a move independently without changing its facts.

    Args:
        tmp_path: The isolated library root.
        src: The relative source path.
        dest: The relative destination path.
        forced: Work names whose body indexes must be rebuilt.
    """
    event = FileMovedEvent(str(tmp_path / src), str(tmp_path / dest))
    assert get_handler(LibType.COMIC).resolve_content_targets(
        event, base_path=str(tmp_path)
    ) == {tmp_path / work: {tmp_path / work} for work in forced}
    assert (event.src_path, event.dest_path) == (
        str(tmp_path / src),
        str(tmp_path / dest),
    )


def test_content_directories(tmp_path):
    """Force replaced directories while keeping directory modifications incremental.

    Args:
        tmp_path: The isolated library root.
    """
    handler = get_handler(LibType.COMIC)
    path = str(tmp_path / "Work/Chapter")
    for event in (
        DirCreatedEvent(path),
        DirDeletedEvent(path),
        DirMovedEvent(path, str(tmp_path / "Work/Other")),
    ):
        assert handler.resolve_content_targets(
            event, base_path=str(tmp_path)
        ) == handler.resolve_event_targets(event, base_path=str(tmp_path))
    assert (
        handler.resolve_content_targets(DirModifiedEvent(path), base_path=str(tmp_path))
        == {}
    )
    assert (
        handler.resolve_content_targets(
            FileOpenedEvent(path + "/1.png"), base_path=str(tmp_path)
        )
        == {}
    )
    for lib_type, relative in (
        (LibType.NOVEL, "Work/assets"),
        (LibType.COMIC, "Work/Chapter/assets"),
    ):
        handler = get_handler(lib_type)
        assert isinstance(handler, reading.ReadingMediaHandler)
        for event_type in (DirCreatedEvent, DirDeletedEvent):
            event = event_type(str(tmp_path / relative))
            assert handler.resolve_event_targets(event, base_path=str(tmp_path))
            assert handler.resolve_content_targets(event, base_path=str(tmp_path)) == {}


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
