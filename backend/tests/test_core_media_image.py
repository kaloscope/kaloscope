"""Unit tests for image indexing and bounded local content reads."""

import base64
import io
import json
import os
from pathlib import Path

import pytest

from app.core.media import image
from app.core.media.common import ContentError
from app.core.media.handlers import reading
from app.core.media.handlers.base import get_handler
from app.core.media.handlers.reading import ReadingSource
from app.core.media.image import (
    build_image_index,
    load_image_index,
    read_image_resource,
)
from app.models.media import LibType, MediaFormat

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4"
    "//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
)
_GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


def _source(tmp_path: Path, files: dict[str, bytes]) -> ReadingSource:
    """Discover a standalone comic in an isolated directory.

    Args:
        tmp_path: The test's temporary directory.
        files: Direct filenames and bytes to create in the comic container.

    Returns:
        The reading source discovered by the existing comic handler.
    """
    directory = tmp_path / "library/Comic"
    directory.mkdir(parents=True)
    for name, data in files.items():
        (directory / name).write_bytes(data)
    scan = get_handler(LibType.COMIC).scan_sources(str(directory.parent))
    assert not scan.issues
    return scan.sources[0]


def test_image_directory(tmp_path):
    files = {
        "page10.PNG": _PNG,
        "page2.jpg": _GIF,
        "page1.png": _PNG,
        "CoVeR.PNG": _PNG,
        "folder.jpg": _GIF,
        "poster.png": _PNG,
        ".hidden.png": b"ignored",
        "ComicInfo.xml": b"<ComicInfo/>",
        "notes.txt": b"notes",
        "transfer.png.part": b"unfinished",
    }
    source = _source(tmp_path, files)
    cache = tmp_path / "cache"
    index = build_image_index(source, cache)

    assert index.cover is not None
    assert [page.relative_path for page in index.pages] == [
        "page1.png",
        "page2.jpg",
        "page10.PNG",
    ]
    assert index.cover.relative_path == "CoVeR.PNG"
    assert index.cover_id == index.cover.id
    assert load_image_index(cache) == index
    assert list(cache.iterdir()) == [cache / "index.json"]
    assert str(tmp_path) not in (cache / "index.json").read_text()
    for page in (*index.pages, index.cover):
        data, mime_type = read_image_resource(source.path, cache, page.id)
        assert data == files[page.relative_path]
        assert mime_type == ("image/gif" if data == _GIF else "image/png")
        assert page.size == len(data)
        assert page.mtime_ns == (source.path / page.relative_path).stat().st_mtime_ns
    assert {path.name: path.read_bytes() for path in source.path.iterdir()} == files


@pytest.mark.parametrize(
    ("header", "mime_type"),
    [
        (b"\xff\xd8\xff\xe0", "image/jpeg"),
        (_PNG, "image/png"),
        (_GIF, "image/gif"),
        (b"GIF87a", "image/gif"),
        (b"RIFF\x10\x00\x00\x00WEBP", "image/webp"),
    ],
)
def test_image_signatures(tmp_path, header, mime_type):
    source = _source(tmp_path, {"1.jpg": header})
    cache = tmp_path / "cache"
    index = build_image_index(source, cache)
    assert index.cover is None
    assert index.cover_id == index.pages[0].id
    assert read_image_resource(source.path, cache, index.cover_id) == (
        header,
        mime_type,
    )


@pytest.mark.parametrize("body", [b"", b"<svg/>", b"<html/>", b"RIFF1234WAVE", b"bad"])
def test_invalid_image(tmp_path, body):
    source = _source(tmp_path, {"1.png": body})
    cache = tmp_path / "cache"
    with pytest.raises(ContentError, match="invalid_image"):
        build_image_index(source, cache)
    assert not cache.exists()
    assert (source.path / "1.png").read_bytes() == body


@pytest.mark.parametrize("fallback", ["folder.png", "poster.png", None])
def test_cover_fallback(tmp_path, fallback):
    files = {"1.png": _PNG, "cover.png": b"broken"}
    if fallback:
        files[fallback] = _GIF
    source = _source(tmp_path, files)
    index = build_image_index(source, tmp_path / "cache")
    assert len(index.pages) == 1
    if fallback:
        assert index.cover is not None
        assert index.cover.relative_path == fallback
    else:
        assert index.cover is None
        assert index.cover_id == index.pages[0].id


def test_rescan_directory(tmp_path):
    source = _source(tmp_path, {"10.png": _PNG})
    (source.path / "2.png").write_bytes(_PNG)
    index = build_image_index(source, tmp_path / "cache")
    assert [page.relative_path for page in index.pages] == ["2.png", "10.png"]


@pytest.mark.parametrize("layout", ["empty", "archive", "mixed"])
def test_changed_layout(tmp_path, layout):
    source = _source(tmp_path, {"1.png": _PNG})
    if layout != "mixed":
        (source.path / "1.png").unlink()
    if layout != "empty":
        (source.path / "comic.cbz").write_bytes(b"archive")
    errors = {
        "empty": "empty_content",
        "archive": "content_changed",
        "mixed": "ambiguous_layout",
    }
    with pytest.raises(ContentError, match=errors[layout]):
        build_image_index(source, tmp_path / "cache")
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("limit", ["pages", "image", "index"])
def test_image_limits(tmp_path, monkeypatch, limit):
    source = _source(tmp_path, {"1.png": _PNG, "2.png": _PNG})
    if limit == "pages":
        monkeypatch.setattr(reading, "MAX_PAGES", 1)
    else:
        monkeypatch.setattr(
            image, "_IMAGE_BYTES" if limit == "image" else "INDEX_BYTES", 1
        )
    with pytest.raises(ContentError, match="media_limit_exceeded"):
        build_image_index(source, tmp_path / "cache")
    assert not (tmp_path / "cache").exists()


def test_resource_limit(tmp_path, monkeypatch):
    source = _source(tmp_path, {"1.png": _PNG, "cover.png": _PNG + b"extra"})
    monkeypatch.setattr(image, "_IMAGE_BYTES", len(_PNG))
    cache = tmp_path / "cache"
    index = build_image_index(source, cache)
    assert index.cover is None
    assert read_image_resource(source.path, cache, index.cover_id)[0] == _PNG
    monkeypatch.setattr(image, "_IMAGE_BYTES", len(_PNG) - 1)
    with pytest.raises(ContentError, match="media_limit_exceeded"):
        read_image_resource(source.path, cache, index.cover_id)


def test_bounded_reads(tmp_path, monkeypatch):
    source = _source(tmp_path, {"1.png": _PNG})
    sizes = []

    class Reader(io.BufferedReader):
        def read(self, size=-1):
            """Record each bounded source read.

            Args:
                size: The byte bound; an unbounded default is rejected.

            Returns:
                The requested source bytes.
            """
            assert 0 < size <= image._IMAGE_BYTES + 1
            sizes.append(size)
            return super().read(size)

    monkeypatch.setattr(os, "fdopen", lambda fd, mode: Reader(io.FileIO(fd, mode)))
    cache = tmp_path / "cache"
    index = build_image_index(source, cache)
    assert sizes == [12]

    def no_scan(*args, **kwargs):
        """Reject directory enumeration during a page read.

        Args:
            args: Any unexpected enumeration arguments.
            kwargs: Any unexpected keyword arguments.

        Raises:
            AssertionError: If resource lookup tries to enumerate the directory.
        """
        raise AssertionError("page reads must use the index")

    monkeypatch.setattr(os, "scandir", no_scan)
    assert read_image_resource(source.path, cache, index.cover_id)[0] == _PNG
    assert sizes == [12, image._IMAGE_BYTES + 1]


@pytest.mark.parametrize("change", ["append", "replace", "delete", "add"])
def test_build_changed(tmp_path, monkeypatch, change):
    source = _source(tmp_path, {"1.png": _PNG})
    read_image = image.read_image_file

    def changed(path, **kwargs):
        """Change the directory after reading the first image header.

        Args:
            path: The image being inspected.
            kwargs: Arguments forwarded to the bounded reader.

        Returns:
            The reader's original bytes and source attributes.
        """
        result = read_image(path, **kwargs)
        if change == "append":
            path.write_bytes(_PNG + b"append")
        elif change == "replace":
            before = path.stat()
            replacement = path.with_suffix(".tmp")
            replacement.write_bytes(_PNG)
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            replacement.replace(path)
        elif change == "delete":
            path.unlink()
        else:
            (source.path / "2.png").write_bytes(_PNG)
        return result

    monkeypatch.setattr(image, "read_image_file", changed)
    with pytest.raises(ContentError, match="content_changed"):
        build_image_index(source, tmp_path / "cache")
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("change", ["append", "touch", "delete", "symlink"])
def test_resource_changed(tmp_path, change):
    source = _source(tmp_path, {"1.png": _PNG})
    cache = tmp_path / "cache"
    index = build_image_index(source, cache)
    path = source.path / "1.png"
    if change == "append":
        path.write_bytes(_PNG + b"append")
    elif change == "touch":
        before = path.stat()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
    else:
        path.unlink()
        if change == "symlink":
            target = tmp_path / "outside.png"
            target.write_bytes(_PNG)
            path.symlink_to(target)
    error = "media_source_unavailable" if change == "symlink" else "content_changed"
    with pytest.raises(ContentError, match=error):
        read_image_resource(source.path, cache, index.cover_id)


def test_directory_symlink(tmp_path):
    source = _source(tmp_path, {"1.png": _PNG})
    cache = tmp_path / "cache"
    index = build_image_index(source, cache)
    link = tmp_path / "link"
    link.symlink_to(source.path, target_is_directory=True)
    with pytest.raises(ContentError, match="media_source_unavailable"):
        build_image_index(ReadingSource(link, MediaFormat.DIR), tmp_path / "failed")
    with pytest.raises(ContentError, match="media_source_unavailable"):
        read_image_resource(link, cache, index.cover_id)
    assert not (tmp_path / "failed").exists()


def test_index_relocation(tmp_path):
    source = _source(tmp_path, {"1.png": _PNG})
    cache = tmp_path / "cache"
    first = build_image_index(source, cache)
    moved = source.path.with_name("Renamed")
    source.path.rename(moved)
    assert read_image_resource(moved, cache, first.cover_id)[0] == _PNG
    second = build_image_index(
        ReadingSource(moved, MediaFormat.DIR), tmp_path / "second"
    )
    assert first.pages == second.pages
    assert first.index_version != second.index_version


@pytest.mark.parametrize(
    "filename",
    [
        "../outside.png",
        "/outside.png",
        "sub/1.png",
        r"sub\1.png",
        "C:1.png",
        "1\0.png",
        ".1.png",
        "ComicInfo.xml",
    ],
)
def test_index_path(tmp_path, filename):
    source = _source(tmp_path, {"1.png": _PNG})
    cache = tmp_path / "cache"
    index = build_image_index(source, cache)
    path = cache / "index.json"
    data = json.loads(path.read_text())
    data["pages"][0]["relative_path"] = filename
    path.write_text(json.dumps(data))
    with pytest.raises(ContentError, match="content_not_ready"):
        read_image_resource(source.path, cache, index.cover_id)


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "json",
        "schema",
        "format",
        "duplicate",
        "filename",
        "order",
        "cover",
        "size",
        "mime",
        "oversize",
    ],
)
def test_index_damage(tmp_path, damage):
    source = _source(tmp_path, {"1.png": _PNG, "2.png": _PNG})
    cache = tmp_path / "cache"
    build_image_index(source, cache)
    path = cache / "index.json"
    data = json.loads(path.read_text())
    if damage == "missing":
        path.unlink()
    elif damage == "json":
        path.write_text("{")
    elif damage == "oversize":
        path.write_bytes(b" " * (image.INDEX_BYTES + 1))
    else:
        if damage == "schema":
            data["schema_version"] = 2
        elif damage == "format":
            data["format"] = "cbz"
        elif damage == "duplicate":
            data["pages"][1]["id"] = data["pages"][0]["id"]
        elif damage == "filename":
            data["pages"][1]["relative_path"] = data["pages"][0]["relative_path"]
        elif damage == "order":
            data["pages"].reverse()
        elif damage == "cover":
            data["pages"][0]["relative_path"] = "cover.png"
        elif damage == "size":
            data["pages"][0]["size"] = image._IMAGE_BYTES + 1
        else:
            data["pages"][0]["mime_type"] = "image/svg+xml"
        path.write_text(json.dumps(data))
    with pytest.raises(ContentError, match="content_not_ready"):
        load_image_index(cache)


def test_resource_membership(tmp_path):
    source = _source(tmp_path, {"1.png": _PNG})
    cache = tmp_path / "cache"
    build_image_index(source, cache)
    for resource_id in ("unknown", "../1.png", "1.png", str(source.path / "1.png")):
        with pytest.raises(ContentError, match="not_found"):
            read_image_resource(source.path, cache, resource_id)


def test_preserve_index(tmp_path):
    source = _source(tmp_path, {"1.png": _PNG})
    cache = tmp_path / "cache"
    first = build_image_index(source, cache)
    (source.path / "1.png").write_bytes(b"broken")
    with pytest.raises(FileExistsError):
        build_image_index(source, cache)
    with pytest.raises(ContentError, match="invalid_image"):
        build_image_index(source, tmp_path / "failed")
    assert load_image_index(cache) == first
    assert not (tmp_path / "failed").exists()


def test_source_format(tmp_path):
    with pytest.raises(ContentError, match="unsupported_media_format"):
        build_image_index(
            ReadingSource(tmp_path / "book.epub", MediaFormat.EPUB), tmp_path / "cache"
        )
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("filename", ["C:1.png", r"sub\1.png"])
def test_unsupported_name(tmp_path, filename):
    source = _source(tmp_path, {filename: _PNG})
    with pytest.raises(ContentError, match="unsupported_media_format"):
        build_image_index(source, tmp_path / "cache")
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("change", ["append", "replace", "delete", "overflow"])
def test_read_changed(tmp_path, monkeypatch, change):
    source = _source(tmp_path, {"1.png": _PNG})
    cache = tmp_path / "cache"
    index = build_image_index(source, cache)
    path = source.path / "1.png"
    if change == "overflow":
        monkeypatch.setattr(image, "_IMAGE_BYTES", len(_PNG))

    class Reader(io.BufferedReader):
        def read(self, size=-1):
            """Change the file after opening it but before reading its bytes.

            Args:
                size: The requested read bound; the default reads remaining bytes.

            Returns:
                The bytes from the already opened file descriptor.
            """
            if change == "replace":
                before = path.stat()
                replacement = path.with_suffix(".tmp")
                replacement.write_bytes(_PNG)
                os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
                replacement.replace(path)
            elif change == "delete":
                path.unlink()
            else:
                path.write_bytes(_PNG + b"extra")
            return super().read(size)

    monkeypatch.setattr(os, "fdopen", lambda fd, mode: Reader(io.FileIO(fd, mode)))
    error = "media_limit_exceeded" if change == "overflow" else "content_changed"
    with pytest.raises(ContentError, match=error):
        read_image_resource(source.path, cache, index.cover_id)


def test_cache_write_failure(tmp_path, monkeypatch):
    source = _source(tmp_path, {"1.png": _PNG})
    original = tmp_path / "original"
    index = build_image_index(source, original)
    cache = tmp_path / "failed"
    write_bytes = Path.write_bytes

    def no_space(path, data):
        """Simulate a failed index write.

        Args:
            path: The target path.
            data: Bytes passed to the write.

        Returns:
            The ordinary write result for other paths.

        Raises:
            OSError: For the new index file.
        """
        if path == cache / "index.json":
            raise OSError("no space left on device")
        return write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", no_space)
    with pytest.raises(OSError, match="no space"):
        build_image_index(source, cache)
    assert not cache.exists()
    assert load_image_index(original) == index
    assert read_image_resource(source.path, original, index.cover_id)[0] == _PNG


def test_source_unavailable(tmp_path, monkeypatch):
    source = _source(tmp_path, {"1.png": _PNG})
    cache = tmp_path / "cache"
    index = build_image_index(source, cache)
    original_stat = Path.stat

    def denied(path, **kwargs):
        """Simulate an unreadable image without making the source disappear.

        Args:
            path: The file being inspected.
            kwargs: Options forwarded to stat.

        Returns:
            The real attributes for other paths.

        Raises:
            PermissionError: For the image source.
        """
        if path == source.path / "1.png":
            raise PermissionError("denied")
        return original_stat(path, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    with pytest.raises(ContentError, match="media_source_unavailable"):
        read_image_resource(source.path, cache, index.cover_id)
    with pytest.raises(PermissionError):
        build_image_index(source, tmp_path / "failed")
    assert not (tmp_path / "failed").exists()
    assert load_image_index(cache) == index


def test_chapter_directory(tmp_path):
    source = _source(tmp_path, {"1.png": _PNG})
    chapter = source.path / "Chapter 2"
    chapter.mkdir()
    (source.path / "1.png").rename(chapter / "1.png")
    scan = get_handler(LibType.COMIC).scan_sources(str(source.path.parent))
    assert not scan.issues
    source = next(entry for entry in scan.sources if entry.format == MediaFormat.DIR)
    assert source.parent_path == chapter.parent
    cache = tmp_path / "cache"
    index = build_image_index(source, cache)
    assert read_image_resource(chapter, cache, index.cover_id)[0] == _PNG

    link = tmp_path / "parent_link"
    link.symlink_to(chapter.parent, target_is_directory=True)
    with pytest.raises(ContentError, match="media_source_unavailable"):
        read_image_resource(link / chapter.name, cache, index.cover_id)
