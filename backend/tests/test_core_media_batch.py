"""Unit tests for library file classification and batch planning."""

import os
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError

from app.core.config import KaloscopeConfig
from app.core.media import batch
from app.models.media import (
    FileOwner,
    LibType,
    MediaBatch,
    MediaFormat,
    MediaItem,
    MediaLib,
)


def library(root: Path, lib_type: LibType) -> MediaLib:
    return cast(MediaLib, SimpleNamespace(id=1, dir=str(root), lib_type=lib_type))


def item(id: int, path: Path, *, directory: Path | None = None, **fields) -> MediaItem:
    values = {
        "nfo_path": None,
        "danmaku_path": None,
        "poster": None,
        "backdrop": None,
        "format": None,
        "parent_id": None,
        "size": None,
    }
    return cast(
        MediaItem,
        SimpleNamespace(
            id=id,
            path=str(path),
            dir=str(directory or path.parent),
            **(values | fields),
        ),
    )


def write(root: Path, *names: str):
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)


def owners(index: batch.LibraryIndex, **filters) -> dict[str, FileOwner]:
    scan = batch.scan_files(index, **filters)
    return {
        batch.relative(index.root, match.path): match.owner for match in scan.matches
    }


def test_video_owners(tmp_path):
    show = tmp_path / "Show"
    write(
        tmp_path,
        "Show/Show S01E01.mkv",
        "Show/Show S01E01.nfo",
        "Show/Show S01E01.zh.ass",
        "Show/.Show S01E01.json",
        "Show/Show.nfo",
        "Show/poster.jpg",
        "Show/Show S01E02.mkv",
        "Show/Show S01E02.srt",
        "Show/Extras/trailer.mp4",
        "Show/info.url",
        "Show/notes.nfo",
        "Show/.organizing-abc",
        "stray.torrent",
        "old.rmvb",
    )
    index = batch.build_index(
        library(tmp_path, LibType.TV_SHOW),
        [
            item(1, show, directory=show, nfo_path=str(show / "Show.nfo")),
            item(2, show / "Show S01E01.mkv", parent_id=1),
        ],
    )

    assert owners(index) == {
        "Show/.Show S01E01.json": FileOwner.COMPANION,
        "Show/Extras/trailer.mp4": FileOwner.UNINDEXED,
        "Show/Show S01E01.mkv": FileOwner.INDEXED,
        "Show/Show S01E01.nfo": FileOwner.COMPANION,
        "Show/Show S01E01.zh.ass": FileOwner.COMPANION,
        "Show/Show S01E02.mkv": FileOwner.UNINDEXED,
        "Show/Show S01E02.srt": FileOwner.LOOSE,
        "Show/Show.nfo": FileOwner.COMPANION,
        "Show/info.url": FileOwner.LOOSE,
        "Show/notes.nfo": FileOwner.LOOSE,
        "Show/poster.jpg": FileOwner.COMPANION,
        "old.rmvb": FileOwner.UNINDEXED,
        "stray.torrent": FileOwner.LOOSE,
    }


def test_comic_owners(tmp_path):
    write(
        tmp_path,
        "Pages/001.jpg",
        "Pages/002.jpg",
        "Pages/cover.jpg",
        "Pages/ComicInfo.xml",
        "Pages/Thumbs.db",
        "Series/ComicInfo.xml",
        "Series/Vol 1/Vol 1.cbz",
        "Series/Vol 1/readme.txt",
        "Broken/a.cbz",
        "Broken/b.cbz",
        "Broken/page.png",
        "__MACOSX/Pages/001.jpg",
    )
    index = batch.build_index(
        library(tmp_path, LibType.COMIC),
        [
            item(
                1,
                tmp_path / "Pages",
                directory=tmp_path / "Pages",
                format=MediaFormat.DIR,
            ),
            item(2, tmp_path / "Series", directory=tmp_path / "Series"),
            item(
                3,
                tmp_path / "Series/Vol 1/Vol 1.cbz",
                format=MediaFormat.CBZ,
                parent_id=2,
            ),
        ],
    )

    assert owners(index) == {
        "Broken/a.cbz": FileOwner.UNINDEXED,
        "Broken/b.cbz": FileOwner.UNINDEXED,
        "Broken/page.png": FileOwner.UNINDEXED,
        "Pages/001.jpg": FileOwner.PAGE,
        "Pages/002.jpg": FileOwner.PAGE,
        "Pages/ComicInfo.xml": FileOwner.COMPANION,
        "Pages/Thumbs.db": FileOwner.LOOSE,
        "Pages/cover.jpg": FileOwner.COMPANION,
        "Series/ComicInfo.xml": FileOwner.COMPANION,
        "Series/Vol 1/Vol 1.cbz": FileOwner.INDEXED,
        "Series/Vol 1/readme.txt": FileOwner.LOOSE,
        "__MACOSX/Pages/001.jpg": FileOwner.LOOSE,
    }


def test_novel_owners(tmp_path):
    write(
        tmp_path,
        "Book/Book.epub",
        "Book/Book.opf",
        "Book/metadata.opf",
        "Book/art.jpg",
        "Book/source.url",
        "Other/a.txt",
        "Other/b.txt",
        "Other/scan.jpg",
    )
    index = batch.build_index(
        library(tmp_path, LibType.NOVEL),
        [item(1, tmp_path / "Book/Book.epub", format=MediaFormat.EPUB)],
    )

    assert owners(index) == {
        "Book/Book.epub": FileOwner.INDEXED,
        "Book/Book.opf": FileOwner.COMPANION,
        "Book/art.jpg": FileOwner.COMPANION,
        "Book/metadata.opf": FileOwner.COMPANION,
        "Book/source.url": FileOwner.LOOSE,
        "Other/a.txt": FileOwner.UNINDEXED,
        "Other/b.txt": FileOwner.UNINDEXED,
        "Other/scan.jpg": FileOwner.LOOSE,
    }


def test_filters(tmp_path):
    write(tmp_path, "A/one.URL", "A/two.txt", "B/three.url", "B/SAMPLE-clip.txt")
    (tmp_path / "A/big.url").write_bytes(b"x" * 500)
    index = batch.build_index(library(tmp_path, LibType.MOVIE), [])

    assert set(owners(index, extensions=[".url"])) == {
        "A/one.URL",
        "A/big.url",
        "B/three.url",
    }
    assert set(owners(index, directory="A", extensions=[".url"], max_size=100)) == {
        "A/one.URL"
    }
    assert set(owners(index, name="*sample*")) == {"B/SAMPLE-clip.txt"}
    assert set(owners(index, min_size=100)) == {"A/big.url"}


def test_scan_bounds(tmp_path):
    write(tmp_path, *(f"D/{number}.txt" for number in range(20)))
    index = batch.build_index(library(tmp_path, LibType.MOVIE), [])

    assert batch.scan_files(index, limit=5).truncated
    assert not batch.scan_files(index).truncated
    with pytest.raises(ValueError):
        batch.scan_files(index, directory="missing")


def test_links(tmp_path):
    write(tmp_path, "A/keep.txt", "A/free.txt", "Real/inner.txt")
    (tmp_path / "alias.txt").symlink_to(tmp_path / "A/keep.txt")
    (tmp_path / "Linked").symlink_to(tmp_path / "Real", target_is_directory=True)
    index = batch.build_index(library(tmp_path, LibType.MOVIE), [])

    scan = batch.scan_files(index, whole=True)
    entries = {
        entry.src: entry.reason
        for entry in batch.plan_deletion(
            index, scan, [FileOwner.LOOSE], busy_items=set(), busy_paths=set()
        )
    }

    # a directory link is never followed, and its target's files stay protected
    assert entries == {
        "A/free.txt": None,
        "A/keep.txt": "linked",
        "Real/inner.txt": "linked",
        "alias.txt": None,
    }


def test_plan_guards(tmp_path):
    write(tmp_path, "Pages/001.jpg", "Pages/002.jpg", "Film/Film.mkv", "Film/x.url")
    comic = batch.build_index(
        library(tmp_path, LibType.COMIC),
        [
            item(
                1,
                tmp_path / "Pages",
                directory=tmp_path / "Pages",
                format=MediaFormat.DIR,
            )
        ],
    )

    everything = batch.plan_deletion(
        comic,
        batch.scan_files(comic, directory="Pages", whole=True),
        [FileOwner.PAGE],
        busy_items=set(),
        busy_paths=set(),
    )
    one = batch.plan_deletion(
        comic,
        batch.scan_files(comic, directory="Pages", name="001*", whole=True),
        [FileOwner.PAGE],
        busy_items=set(),
        busy_paths=set(),
    )

    assert {entry.reason for entry in everything} == {"empties_item"}
    assert [(entry.kind, entry.status) for entry in one] == [("file", "planned")]

    movie = batch.build_index(
        library(tmp_path, LibType.MOVIE), [item(7, tmp_path / "Film/Film.mkv")]
    )
    scan = batch.scan_files(movie, directory="Film", whole=True)
    planned = batch.plan_deletion(
        movie,
        scan,
        [FileOwner.LOOSE, FileOwner.INDEXED],
        busy_items={7},
        busy_paths={str(tmp_path / "Film/x.url")},
    )

    assert {entry.src: (entry.kind, entry.reason) for entry in planned} == {
        "Film/Film.mkv": ("item", "ingesting"),
        "Film/x.url": ("file", "download_active"),
    }
    excluded = batch.plan_deletion(
        movie, scan, [FileOwner.LOOSE], busy_items=set(), busy_paths=set()
    )
    assert {entry.src: entry.reason for entry in excluded} == {
        "Film/Film.mkv": "owner_excluded",
        "Film/x.url": None,
    }


def test_delete_files(tmp_path, monkeypatch):
    monkeypatch.setattr(
        KaloscopeConfig, "get", lambda: SimpleNamespace(filesystem_trash_mode=False)
    )
    write(tmp_path, "a.txt", "b.txt", "Sub/c.txt")
    states = {name: batch._fingerprint(tmp_path / name) for name in ("a.txt", "b.txt")}
    (tmp_path / "b.txt").write_bytes(b"changed after the review")

    outcomes = batch.delete_files(
        tmp_path,
        [
            (1, "a.txt", states["a.txt"]),
            (2, "b.txt", states["b.txt"]),
            (3, "gone.txt", [0, 0, 0, 0]),
            (4, "../outside.txt", [0, 0, 0, 0]),
        ],
    )

    assert outcomes == {
        1: "done",
        2: "content_changed",
        3: "missing",
        4: "unsafe_path",
    }
    assert not (tmp_path / "a.txt").exists()
    assert (tmp_path / "b.txt").exists()


def test_prune_directories(tmp_path):
    write(tmp_path, "Keep/file.txt")
    for name in ("Empty/Deep", "Indexed"):
        (tmp_path / name).mkdir(parents=True)

    batch.prune_directories(
        tmp_path,
        {tmp_path / "Empty/Deep", tmp_path / "Indexed", tmp_path / "Keep"},
        {str(tmp_path / "Indexed")},
    )

    assert sorted(os.listdir(tmp_path)) == ["Indexed", "Keep"]


@pytest.mark.parametrize(
    "fields",
    [
        {"directory": "/etc"},
        {"directory": "../other"},
        {"item_ids": [1], "extensions": ["mkv"]},
        {"operation": "delete"},
        {"extensions": ["a/b"]},
        {"extensions": ["."]},
        {"min_size": 5, "max_size": 1},
        {"owners": []},
        {"operation": "rename", "name": "*"},
    ],
)
def test_invalid_selector(fields):
    with pytest.raises(ValidationError):
        MediaBatch(library_id=1, **fields)


def test_selector_normalization():
    change = MediaBatch(
        library_id=1,
        directory="Shows/./A",
        extensions=["MKV", ".mkv", "Url"],
        owners=["loose", "indexed", "loose"],
    )

    assert change.directory == "Shows/A"
    assert change.extensions == [".mkv", ".url"]
    assert change.owners == [FileOwner.INDEXED, FileOwner.LOOSE]
    assert MediaBatch(library_id=1, directory=".").directory is None
