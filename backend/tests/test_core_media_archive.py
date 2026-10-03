"""Unit tests for bounded ZIP reads and comic archive indexing."""

import base64
import io
import json
import os
import stat
import struct
import zipfile
from pathlib import Path

import pytest

from app.core.media import archive, image
from app.core.media.common import ContentError
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


def _source(
    tmp_path: Path,
    entries: list[tuple[str | zipfile.ZipInfo, bytes]],
    *,
    format: MediaFormat = MediaFormat.CBZ,
    compression: int = zipfile.ZIP_DEFLATED,
) -> ReadingSource:
    """Create and discover a real comic archive in an isolated container.

    Args:
        tmp_path: The temporary test directory.
        entries: Member names or descriptors with their uncompressed bytes.
        format: The source suffix; defaults to CBZ.
        compression: The method for named entries; defaults to Deflate.

    Returns:
        The archive source discovered by the reading handler.
    """
    path = tmp_path / "library/Comic" / f"comic.{format.value.upper()}"
    path.parent.mkdir(parents=True)
    with zipfile.ZipFile(path, "w", compression=compression) as file:
        for name, data in entries:
            file.writestr(name, data)
    scan = get_handler(LibType.COMIC).scan_sources(str(path.parent.parent))
    assert not scan.issues
    assert len(scan.sources) == 1
    return scan.sources[0]


@pytest.mark.parametrize("format", [MediaFormat.CBZ, MediaFormat.ZIP])
@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_archive_index(tmp_path, format, compression):
    entries = [
        ("包裹/", b""),
        ("包裹/page10.PNG", _PNG),
        ("./包裹//page2.png", _PNG),
        ("包裹/page1.png", _PNG),
        ("包裹/cover.png", _PNG),
        ("包裹/folder.png", _PNG),
        ("__MACOSX/page0.png", b"ignored"),
        ("包裹/.hidden.png", b"ignored"),
        ("包裹/partial.tmp/1.png", b"ignored"),
        ("包裹/2.png.part", b"ignored"),
        ("包裹/nested.zip", b"not opened"),
        (
            "ComicInfo.xml",
            b"<ComicInfo><Pages><Page Image='0' Type='FrontCover'/>"
            b"</Pages></ComicInfo>",
        ),
    ]
    source = _source(tmp_path, entries, format=format, compression=compression)
    original = source.path.read_bytes()
    before = source.path.stat()
    cache = tmp_path / "cache"
    index = build_image_index(source, cache)

    assert index.format == format.value
    assert [page.relative_path for page in index.pages] == [
        "包裹/page1.png",
        "./包裹//page2.png",
        "包裹/page10.PNG",
    ]
    assert index.cover.relative_path == "包裹/cover.png"
    assert index.source_snapshot.size == len(original)
    assert index.source_snapshot.mtime_ns == before.st_mtime_ns
    assert load_image_index(cache) == index
    assert list(cache.iterdir()) == [cache / "index.json"]
    assert str(tmp_path) not in (cache / "index.json").read_text()
    for resource in (*index.pages, index.cover):
        assert resource.crc == zipfile.crc32(_PNG)
        assert resource.mtime_ns is None
        assert read_image_resource(source.path, cache, resource.id) == (
            _PNG,
            "image/png",
        )
    assert source.path.read_bytes() == original
    assert source.path.stat().st_mtime_ns == before.st_mtime_ns
    assert list(source.path.parent.iterdir()) == [source.path]


def test_multiple_folders(tmp_path):
    source = _source(
        tmp_path,
        [
            ("part10/1.png", _PNG),
            ("part2/10.png", _PNG),
            ("part2/2.png", _PNG),
        ],
    )
    index = build_image_index(source, tmp_path / "cache")
    assert [entry.relative_path for entry in index.pages] == [
        "part2/2.png",
        "part2/10.png",
        "part10/1.png",
    ]
    assert index.cover is None
    assert index.cover_id == index.pages[0].id


@pytest.mark.parametrize("fallback", ["folder.png", None])
def test_cover_fallback(tmp_path, fallback):
    entries = [("1.png", _PNG), ("cover.png", b"invalid")]
    if fallback:
        entries.append((fallback, _PNG))
    index = build_image_index(_source(tmp_path, entries), tmp_path / "cache")
    assert len(index.pages) == 1
    assert (index.cover.relative_path if index.cover else None) == fallback


@pytest.mark.parametrize("name", ["cover.png", "nested.zip", "ComicInfo.xml", "1.svg"])
def test_empty_archive(tmp_path, name):
    source = _source(tmp_path, [(name, _PNG)])
    with pytest.raises(ContentError, match="empty_content"):
        build_image_index(source, tmp_path / "cache")
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize(
    "name",
    ["../1.png", "/1.png", "C:/1.png", "C:1.png", r"folder\1.png", "folder/../1.png"],
)
def test_member_path(tmp_path, name):
    source = _source(tmp_path, [("1.png", _PNG), (name, _PNG)])
    with pytest.raises(ContentError, match="invalid_archive"):
        build_image_index(source, tmp_path / "cache")
    assert not (tmp_path / "cache").exists()


def test_null_name(tmp_path):
    source = _source(tmp_path, [("bad-name.png", _PNG)])
    source.path.write_bytes(
        source.path.read_bytes().replace(b"bad-name.png", b"bad\0name.png")
    )
    with pytest.raises(ContentError, match="invalid_archive"):
        build_image_index(source, tmp_path / "cache")


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("1.png", "./1.png"),
        ("dir/1.png", "dir//1.png"),
        ("é.png", "e\u0301.png"),
        ("dir/", "dir"),
    ],
)
def test_duplicate_names(tmp_path, first, second):
    source = _source(tmp_path, [(first, _PNG), (second, _PNG)])
    with pytest.raises(ContentError, match="invalid_archive"):
        build_image_index(source, tmp_path / "cache")


def test_exact_duplicate(tmp_path):
    with pytest.warns(UserWarning, match="Duplicate name"):
        source = _source(tmp_path, [("1.png", _PNG), ("1.png", _PNG)])
    with pytest.raises(ContentError, match="invalid_archive"):
        build_image_index(source, tmp_path / "cache")


@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR])
def test_member_type(tmp_path, mode):
    member = zipfile.ZipInfo("__MACOSX/ignored.png")
    member.create_system = 3
    member.external_attr = (mode | 0o600) << 16
    source = _source(tmp_path, [("1.png", _PNG), (member, b"outside")])
    with pytest.raises(ContentError, match="invalid_archive"):
        build_image_index(source, tmp_path / "cache")


@pytest.mark.parametrize("compression", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_unsupported_compression(tmp_path, compression):
    source = _source(tmp_path, [("1.png", _PNG)], compression=compression)
    with pytest.raises(ContentError, match="unsupported_media_format"):
        build_image_index(source, tmp_path / "cache")


@pytest.mark.parametrize("flag", [1, 32, 64])
def test_unsupported_flags(tmp_path, flag):
    source = _source(tmp_path, [("1.png", _PNG)])
    data = bytearray(source.path.read_bytes())
    for signature, offset in [(b"PK\x03\x04", 6), (b"PK\x01\x02", 8)]:
        position = data.index(signature) + offset
        current = struct.unpack_from("<H", data, position)[0]
        struct.pack_into("<H", data, position, current | flag)
    source.path.write_bytes(data)
    with pytest.raises(ContentError, match="unsupported_media_format"):
        build_image_index(source, tmp_path / "cache")


@pytest.mark.parametrize("damage", ["body", "header", "tail"])
def test_corrupt_archive(tmp_path, damage):
    source = _source(tmp_path, [("1.png", _PNG)], compression=zipfile.ZIP_STORED)
    cache = tmp_path / "cache"
    index = build_image_index(source, cache)
    before = source.path.stat()
    data = bytearray(source.path.read_bytes())
    if damage == "body":
        name_size, extra_size = struct.unpack_from("<HH", data, 26)
        data[30 + name_size + extra_size + 32] ^= 1
    elif damage == "header":
        data[:4] = b"BAD!"
    else:
        data[-22:-18] = b"BAD!"
    source.path.write_bytes(data)
    os.utime(source.path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ContentError, match="invalid_archive"):
        build_image_index(source, tmp_path / "failed")
    with pytest.raises(ContentError, match="invalid_archive"):
        read_image_resource(source.path, cache, index.cover_id)
    assert not (tmp_path / "failed").exists()
    assert load_image_index(cache) == index


@pytest.mark.parametrize("limit", ["members", "expanded", "pages", "image", "index"])
def test_archive_limits(tmp_path, monkeypatch, limit):
    source = _source(tmp_path, [("1.png", _PNG), ("2.png", _PNG)])
    target, constant = {
        "members": (archive, "_MAX_MEMBERS"),
        "expanded": (archive, "_MAX_EXPANDED_BYTES"),
        "pages": (image, "MAX_PAGES"),
        "image": (image, "_IMAGE_BYTES"),
        "index": (image, "INDEX_BYTES"),
    }[limit]
    monkeypatch.setattr(target, constant, 1)
    with pytest.raises(ContentError, match="media_limit_exceeded"):
        build_image_index(source, tmp_path / "cache")
    assert not (tmp_path / "cache").exists()


def test_directory_limit(tmp_path, monkeypatch):
    source = _source(tmp_path, [(f"{number}.png", _PNG) for number in range(50)])
    monkeypatch.setattr(archive, "_DIRECTORY_BYTES", 1024)
    with pytest.raises(ContentError, match="media_limit_exceeded"):
        build_image_index(source, tmp_path / "cache")
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("prefix_bytes", [None, 0, 7, 12, 64 * 1024 + 3])
def test_member_read_bound(tmp_path, monkeypatch, prefix_bytes):
    body = _PNG + b"x" * (archive._READ_BYTES * 2)
    source = _source(tmp_path, [("1.png", body)])
    read = zipfile.ZipExtFile.read
    sizes = []

    def bounded(file, size=-1):
        """Require bounded decompression reads.

        Args:
            file: The member stream.
            size: The read bound; the unbounded default is rejected.

        Returns:
            The next decoded bytes.
        """
        assert 0 < size <= archive._READ_BYTES
        sizes.append(size)
        return read(file, size)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", bounded)
    cache = tmp_path / "cache"
    index = build_image_index(source, cache)
    assert len(sizes) >= 3
    assert read_image_resource(source.path, cache, index.cover_id)[0] == body
    with archive.open_archive(source.path) as (file, _):
        member = file.getinfo("1.png")
        assert (
            archive.read_member(file, member, len(body), prefix_bytes=prefix_bytes)
            == body[:prefix_bytes]
        )


def test_prefix_length(tmp_path):
    source = _source(tmp_path, [("1.png", _PNG)])
    with archive.open_archive(source.path) as (file, _):
        member = file.getinfo("1.png")
        with pytest.raises(ValueError, match="prefix length must be nonnegative"):
            archive.read_member(file, member, len(_PNG), prefix_bytes=-1)


@pytest.mark.parametrize("prefix_bytes", [None, 12])
def test_actual_byte_limit(tmp_path, monkeypatch, prefix_bytes):
    source = _source(tmp_path, [("1.png", _PNG)])
    with archive.open_archive(source.path) as (file, _):
        member = file.getinfo("1.png")
        monkeypatch.setattr(file, "open", lambda info: io.BytesIO(_PNG + b"extra"))
        with pytest.raises(ContentError, match="media_limit_exceeded"):
            archive.read_member(file, member, len(_PNG), prefix_bytes=prefix_bytes)


@pytest.mark.parametrize("phase", ["build", "read"])
@pytest.mark.parametrize("change", ["append", "replace", "delete"])
def test_source_changed(tmp_path, monkeypatch, phase, change):
    source = _source(tmp_path, [("1.png", _PNG)])
    cache = tmp_path / "cache"
    original = build_image_index(source, cache)
    read_member = image.read_member

    def changed(file, member, limit, **kwargs):
        """Mutate the archive after the selected member was decoded.

        Args:
            file: The open archive.
            member: The selected member.
            limit: The uncompressed byte budget.
            kwargs: Remaining options forwarded to the reader.

        Returns:
            The member's original decoded bytes.
        """
        data = read_member(file, member, limit, **kwargs)
        if change == "append":
            with source.path.open("ab") as output:
                output.write(b"appended")
        elif change == "replace":
            before = source.path.stat()
            replacement = source.path.with_suffix(".tmp")
            replacement.write_bytes(source.path.read_bytes())
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            replacement.replace(source.path)
        else:
            source.path.unlink()
        return data

    monkeypatch.setattr(image, "read_member", changed)
    with pytest.raises(ContentError, match="content_changed"):
        if phase == "build":
            build_image_index(source, tmp_path / "failed")
        else:
            read_image_resource(source.path, cache, original.cover_id)
    assert not (tmp_path / "failed").exists()
    assert load_image_index(cache) == original


@pytest.mark.parametrize("change", ["size", "mtime", "member", "crc"])
def test_source_snapshot(tmp_path, change):
    source = _source(tmp_path, [("1.png", _PNG)], compression=zipfile.ZIP_STORED)
    cache = tmp_path / "cache"
    index = build_image_index(source, cache)
    before = source.path.stat()
    if change == "size":
        with source.path.open("ab") as file:
            file.write(b"extra")
    elif change == "mtime":
        os.utime(source.path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
    else:
        with zipfile.ZipFile(source.path, "w") as file:
            name = "2.png" if change == "member" else "1.png"
            data = _PNG if change == "member" else _PNG[:-1] + b"x"
            file.writestr(name, data)
        assert source.path.stat().st_size == before.st_size
        os.utime(source.path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ContentError, match="content_changed"):
        read_image_resource(source.path, cache, index.cover_id)


@pytest.mark.parametrize("kind", ["file", "parent"])
def test_source_symlink(tmp_path, kind):
    source = _source(tmp_path, [("1.png", _PNG)])
    cache = tmp_path / "cache"
    index = build_image_index(source, cache)
    link = tmp_path / "link"
    link.symlink_to(source.path if kind == "file" else source.path.parent)
    path = link if kind == "file" else link / source.path.name
    with pytest.raises(ContentError, match="media_source_unavailable"):
        build_image_index(ReadingSource(path, source.format), tmp_path / "failed")
    with pytest.raises(ContentError, match="media_source_unavailable"):
        read_image_resource(path, cache, index.cover_id)
    assert not (tmp_path / "failed").exists()


def test_archive_relocation(tmp_path):
    source = _source(tmp_path, [("wrap/1.png", _PNG)])
    cache = tmp_path / "cache"
    first = build_image_index(source, cache)
    moved = source.path.with_name("Renamed.cbz")
    source.path.rename(moved)
    assert read_image_resource(moved, cache, first.cover_id)[0] == _PNG
    second = build_image_index(ReadingSource(moved, source.format), tmp_path / "second")
    assert first.pages == second.pages
    assert first.source_snapshot == second.source_snapshot
    assert first.index_version != second.index_version


@pytest.mark.parametrize("damage", ["snapshot", "mtime", "crc", "format", "path"])
def test_index_damage(tmp_path, damage):
    source = _source(tmp_path, [("1.png", _PNG)])
    cache = tmp_path / "cache"
    index = build_image_index(source, cache)
    path = cache / "index.json"
    data = json.loads(path.read_text())
    if damage == "snapshot":
        data["source_snapshot"] = None
    elif damage == "mtime":
        data["pages"][0]["mtime_ns"] = 0
    elif damage == "crc":
        data["pages"][0]["crc"] = None
    elif damage == "format":
        data["format"] = "dir"
    else:
        data["pages"][0]["relative_path"] = "../1.png"
    path.write_text(json.dumps(data))
    with pytest.raises(ContentError, match="content_not_ready"):
        read_image_resource(source.path, cache, index.cover_id)


def test_unknown_resource(tmp_path):
    source = _source(tmp_path, [("wrap/1.png", _PNG)])
    cache = tmp_path / "cache"
    build_image_index(source, cache)
    for identity in ["1.png", "wrap/1.png", "../1.png", str(source.path)]:
        with pytest.raises(ContentError, match="not_found"):
            read_image_resource(source.path, cache, identity)


def test_legacy_directory_index(tmp_path):
    path = tmp_path / "comic"
    path.mkdir()
    (path / "1.png").write_bytes(_PNG)
    cache = tmp_path / "cache"
    index = build_image_index(ReadingSource(path, MediaFormat.DIR), cache)
    data = json.loads((cache / "index.json").read_text())
    del data["source_snapshot"]
    for entry in data["pages"]:
        del entry["crc"]
    (cache / "index.json").write_text(json.dumps(data))
    assert load_image_index(cache) == index
    assert read_image_resource(path, cache, index.cover_id)[0] == _PNG


def test_zip64_member(tmp_path):
    source = _source(tmp_path, [])
    with (
        zipfile.ZipFile(source.path, "w") as file,
        file.open("1.png", "w", force_zip64=True) as member,
    ):
        member.write(_PNG)
    cache = tmp_path / "cache"
    index = build_image_index(source, cache)
    assert read_image_resource(source.path, cache, index.cover_id)[0] == _PNG


@pytest.mark.parametrize("phase", ["build", "read"])
def test_source_unavailable(tmp_path, monkeypatch, phase):
    source = _source(tmp_path, [("1.png", _PNG)])
    cache = tmp_path / "cache"
    index = build_image_index(source, cache)

    class Unreadable(io.FileIO):
        def readinto(self, buffer):
            """Simulate a source I/O failure below the ZIP directory parser.

            Args:
                buffer: The buffer requested by the bounded reader.

            Raises:
                OSError: For every source read.
            """
            raise OSError("source I/O failure")

    monkeypatch.setattr(archive.io, "FileIO", Unreadable)
    with pytest.raises(ContentError, match="media_source_unavailable"):
        if phase == "build":
            build_image_index(source, tmp_path / "failed")
        else:
            read_image_resource(source.path, cache, index.cover_id)
    assert not (tmp_path / "failed").exists()
    assert load_image_index(cache) == index


def test_invalid_image(tmp_path):
    source = _source(tmp_path, [("1.png", b"<html>invalid image</html>")])
    with pytest.raises(ContentError, match="invalid_image"):
        build_image_index(source, tmp_path / "cache")
    assert not (tmp_path / "cache").exists()
