"""Unit tests for text indexing and bounded local content reads."""

import codecs
import io
import json
import os
from pathlib import Path

import pytest

from app.core.media import text
from app.core.media.handlers.base import get_handler
from app.core.media.handlers.reading import ReadingSource
from app.core.media.text import (
    ContentError,
    build_text_index,
    load_text_index,
    read_text_chapter,
)
from app.models.media import LibType, MediaFormat

_SIMPLIFIED = (
    "第一章 山间小路\n清晨的阳光穿过树林，照亮了村庄。少年背着行囊出发，"
    "沿着河流寻找远方的城市。这是一段用于测试中文小说编码的文字，"
    "其中包含人物、时间、地点和日常生活。\n"
)
_TRADITIONAL = (
    "第一章 山間小路\n清晨的陽光穿過樹林，照亮了村莊。少年背著行囊出發，"
    "沿著河流尋找遠方的城市。這是一段用於測試中文小說編碼的文字，"
    "其中包含人物、時間、地點和日常生活。\n"
)


def _source(tmp_path: Path, data: bytes) -> ReadingSource:
    """Discover a real TXT fixture using the reading handler.

    Args:
        tmp_path: The isolated test directory.
        data: The source file bytes.

    Returns:
        The discovered reading source.
    """
    path = tmp_path / "library/Book/book.TXT"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    scan = get_handler(LibType.NOVEL).scan_sources(str(tmp_path / "library"))
    assert not scan.issues
    return scan.sources[0]


def _cached_text(cache: Path) -> str:
    """Reconstruct the normalized body through individual chapter reads.

    Args:
        cache: The completed cache directory.

    Returns:
        All chapter content in reading order.
    """
    index = load_text_index(cache)
    return "".join(
        "\n\n".join(read_text_chapter(cache, chapter.id)) for chapter in index.chapters
    )


@pytest.mark.parametrize(
    ("encoding", "bom"),
    [
        ("utf-8", b""),
        ("utf-8", codecs.BOM_UTF8),
        ("utf-16-le", codecs.BOM_UTF16_LE),
        ("utf-16-be", codecs.BOM_UTF16_BE),
        ("utf-32-le", codecs.BOM_UTF32_LE),
        ("utf-32-be", codecs.BOM_UTF32_BE),
    ],
)
def test_text_encodings(tmp_path, encoding, bom):
    body = "书前说明。\r\n\r\n第一章 初见\r第一段。\n\n第二章 再会\r\n末段😀。"
    raw = bom + body.encode(encoding)
    source = _source(tmp_path, raw)
    before = source.path.stat()
    cache = tmp_path / "cache"
    index = build_text_index(source, cache)

    assert _cached_text(cache) == body.replace("\r\n", "\n").replace("\r", "\n")
    assert [chapter.title for chapter in index.chapters] == [
        None,
        "第一章 初见",
        "第二章 再会",
    ]
    assert load_text_index(cache) == index
    assert index.source_snapshot.size == len(raw)
    assert index.source_snapshot.mtime_ns == before.st_mtime_ns
    assert source.path.read_bytes() == raw
    assert source.path.stat().st_mtime_ns == before.st_mtime_ns
    assert str(tmp_path) not in (cache / "index.json").read_text()


@pytest.mark.parametrize(
    ("encoding", "body"), [("gb18030", _SIMPLIFIED), ("big5", _TRADITIONAL)]
)
def test_legacy_encoding(tmp_path, encoding, body):
    body *= 40
    source = _source(tmp_path, body.encode(encoding))
    cache = tmp_path / "cache"
    build_text_index(source, cache)
    assert _cached_text(cache) == body


def test_late_encoding(tmp_path):
    body = "ASCII preface\n" * 6000 + _SIMPLIFIED * 40
    source = _source(tmp_path, body.encode("gb18030"))
    cache = tmp_path / "cache"
    build_text_index(source, cache)
    assert _cached_text(cache) == body


def test_unheaded_text(tmp_path):
    body = "\n\n没有章节的正文。\n保留单换行。\n\n<script>alert('text')</script>\n\n"
    cache = tmp_path / "cache"
    index = build_text_index(_source(tmp_path, body.encode()), cache)
    assert len(index.chapters) == 1
    assert index.chapters[0].title is None
    assert index.chapters[0].part == 1
    assert _cached_text(cache) == body


def test_text_headings(tmp_path):
    body = "\n  第一百二十三章 起点\n正文\nChapter IV: Return\n正文\n后记\n末段\n"
    index = build_text_index(_source(tmp_path, body.encode()), tmp_path / "cache")
    assert [chapter.title for chapter in index.chapters] == [
        "第一百二十三章 起点",
        "Chapter IV: Return",
        "后记",
    ]
    assert all(chapter.part == 1 for chapter in index.chapters)
    assert _cached_text(tmp_path / "cache") == body


def test_text_sections(tmp_path, monkeypatch):
    monkeypatch.setattr(text, "_SECTION_BYTES", 96)
    body = (
        "第一章 长章\n" + ("😀" * 12 + "\n\n") * 5 + "中" * 100 + "\nChapter 2\nEnding."
    )
    cache = tmp_path / "cache"
    index = build_text_index(_source(tmp_path, body.encode()), cache)
    assert _cached_text(cache) == body
    assert len(index.chapters) > 5
    assert all(chapter.end - chapter.start <= 96 for chapter in index.chapters)
    parts = [
        chapter.part for chapter in index.chapters if chapter.title == "第一章 长章"
    ]
    assert parts == list(range(1, len(parts) + 1))
    assert index.chapters[-1].part == 1
    assert index.chapters[-1].title == "Chapter 2"
    assert "\n\n".join(read_text_chapter(cache, index.chapters[0].id)).endswith("\n\n")


def test_long_line(tmp_path):
    body = "无换行" * 100_000 + "😀"
    cache = tmp_path / "cache"
    index = build_text_index(_source(tmp_path, body.encode()), cache)
    assert len(index.chapters) > 1
    assert _cached_text(cache) == body
    assert all(chapter.title is None for chapter in index.chapters)


@pytest.mark.parametrize("body", [b"", b" \r\n\t\n", codecs.BOM_UTF8, "　\n".encode()])
def test_empty_text(tmp_path, body):
    cache = tmp_path / "cache"
    with pytest.raises(ContentError, match="empty_content"):
        build_text_index(_source(tmp_path, body), cache)
    assert not cache.exists()


@pytest.mark.parametrize(
    "body",
    [
        b"prefix\x00tail",
        b"text\x01",
        codecs.BOM_UTF8 + b"\xff",
        codecs.BOM_UTF16_LE + b"\x61",
    ],
)
def test_invalid_text(tmp_path, body):
    source = _source(tmp_path, body)
    cache = tmp_path / "cache"
    with pytest.raises(ContentError, match="text_decode_failed"):
        build_text_index(source, cache)
    assert not cache.exists()
    assert source.path.read_bytes() == body


def test_uncertain_encoding(tmp_path, monkeypatch):
    class NoMatches:
        def best(self):
            """Return no reliable encoding candidate."""
            return None

    monkeypatch.setattr(text, "from_bytes", lambda *args, **kwargs: NoMatches())
    cache = tmp_path / "cache"
    with pytest.raises(ContentError, match="text_decode_failed"):
        build_text_index(_source(tmp_path, b"\xff\x81\xe1"), cache)
    assert not cache.exists()


def test_ambiguous_encoding(tmp_path):
    body = (
        "Un matin, le garçon traversa la forêt et découvrit une maison abandonnée. "
        * 80
    )
    cache = tmp_path / "cache"
    with pytest.raises(ContentError, match="text_decode_failed"):
        build_text_index(_source(tmp_path, body.encode("cp1252")), cache)
    assert not cache.exists()


def test_bounded_reads():
    class BoundedBytes(io.BytesIO):
        def read(self, size=-1):
            """Reject an unbounded source read.

            Args:
                size: The byte limit; the default is rejected.

            Returns:
                The next bounded source bytes.
            """
            assert 0 <= size <= text._READ_BYTES
            return super().read(size)

    class BoundedText(io.StringIO):
        def readline(self, size=-1):
            """Reject an unbounded line read.

            Args:
                size: The character limit; the default is rejected.

            Returns:
                The next bounded text fragment.
            """
            assert 0 < size <= text._READ_CHARS
            return super().readline(size)

    body = "文本😀" * 40_000
    assert text._text_encoding(BoundedBytes(body.encode())) == "utf-8"
    output = io.BytesIO()
    text._write_text(BoundedText(body), output)
    assert output.getvalue().decode() == body


@pytest.mark.parametrize("limit", ["_MAX_CHAPTERS", "_INDEX_BYTES"])
def test_text_limits(tmp_path, monkeypatch, limit):
    monkeypatch.setattr(text, limit, 1)
    cache = tmp_path / "cache"
    with pytest.raises(ContentError, match="media_limit_exceeded"):
        build_text_index(
            _source(tmp_path, b"Chapter 1\nFirst\nChapter 2\nSecond"), cache
        )
    assert not cache.exists()


def test_preserve_cache(tmp_path):
    source = _source(tmp_path, b"Existing body")
    cache = tmp_path / "cache"
    original = build_text_index(source, cache)
    source.path.write_bytes(codecs.BOM_UTF8 + b"\xff")
    with pytest.raises(FileExistsError):
        build_text_index(source, cache)
    with pytest.raises(ContentError, match="text_decode_failed"):
        build_text_index(source, tmp_path / "failed")
    assert load_text_index(cache) == original
    assert _cached_text(cache) == "Existing body"


def test_cache_write_failure(tmp_path, monkeypatch):
    source = _source(tmp_path, b"Complete body")
    original = tmp_path / "original"
    build_text_index(source, original)
    cache = tmp_path / "failed"
    write_bytes = Path.write_bytes

    def no_space(path, data):
        """Simulate a full disk while saving the new cache index.

        Args:
            path: The target file.
            data: The bytes to write.

        Returns:
            The real write result for other files.

        Raises:
            OSError: When publishing the new index file.
        """
        if path == cache / "index.json":
            raise OSError("no space left on device")
        return write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", no_space)
    with pytest.raises(OSError, match="no space"):
        build_text_index(source, cache)
    assert not cache.exists()
    assert _cached_text(original) == "Complete body"
    assert source.path.read_bytes() == b"Complete body"


@pytest.mark.parametrize("change", ["append", "replace", "delete"])
def test_source_changed(tmp_path, monkeypatch, change):
    source = _source(tmp_path, b"Original content")
    write_text = text._write_text

    def changed(text, output):
        """Mutate the source immediately after its content has been read.

        Args:
            text: The decoded source stream.
            output: The new cache stream.

        Returns:
            The original parser's chapter ranges.
        """
        chapters = write_text(text, output)
        if change == "append":
            with source.path.open("ab") as file:
                file.write(b" appended")
        elif change == "replace":
            before = source.path.stat()
            replacement = source.path.with_suffix(".tmp")
            replacement.write_bytes(b"Different words!")
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            replacement.replace(source.path)
        else:
            source.path.unlink()
        return chapters

    monkeypatch.setattr(text, "_write_text", changed)
    cache = tmp_path / "cache"
    with pytest.raises(ContentError, match="content_changed"):
        build_text_index(source, cache)
    assert not cache.exists()


def test_source_symlink(tmp_path):
    source = _source(tmp_path, b"Source")
    link = source.path.with_name("alias.txt")
    link.symlink_to(source.path)
    with pytest.raises(ContentError, match="media_source_unavailable"):
        build_text_index(ReadingSource(link, MediaFormat.TXT), tmp_path / "cache")
    assert source.path.read_bytes() == b"Source"
    assert not (tmp_path / "cache").exists()


def test_cache_rebuild(tmp_path):
    source = _source(tmp_path, b"Chapter 1\nContent")
    first = build_text_index(source, tmp_path / "first")
    moved = source.path.with_name("renamed.txt")
    source.path.rename(moved)
    second = build_text_index(
        ReadingSource(moved, MediaFormat.TXT), tmp_path / "second"
    )
    assert first.index_version != second.index_version
    assert first.source_snapshot == second.source_snapshot
    assert first.chapters == second.chapters


@pytest.mark.parametrize(
    "damage",
    [
        "missing_index",
        "missing_body",
        "truncated_body",
        "broken_json",
        "schema",
        "range",
        "duplicate",
        "oversize",
        "utf8",
    ],
)
def test_cache_damage(tmp_path, damage):
    cache = tmp_path / "cache"
    index = build_text_index(
        _source(tmp_path, b"Chapter 1\nFirst\nChapter 2\nSecond"), cache
    )
    index_path, body_path = cache / "index.json", cache / "content.txt"
    if damage == "missing_index":
        index_path.unlink()
    elif damage == "missing_body":
        body_path.unlink()
    elif damage == "truncated_body":
        body_path.write_bytes(b"short")
    elif damage == "broken_json":
        index_path.write_text("{")
    elif damage == "oversize":
        index_path.write_bytes(b" " * (text._INDEX_BYTES + 1))
    elif damage == "utf8":
        body_path.write_bytes(b"\xff" * index.text_size)
    else:
        data = json.loads(index_path.read_text())
        if damage == "schema":
            data["schema_version"] = 2
        elif damage == "range":
            data["chapters"][0]["end"] += 1
        else:
            data["chapters"][1]["id"] = data["chapters"][0]["id"]
        index_path.write_text(json.dumps(data))
    with pytest.raises(ContentError, match="content_not_ready"):
        read_text_chapter(cache, index.chapters[0].id)


def test_chapter_membership(tmp_path):
    cache = tmp_path / "cache"
    build_text_index(_source(tmp_path, b"Content"), cache)
    for chapter_id in (
        "unknown",
        "../content.txt",
        str(tmp_path / "library/Book/book.TXT"),
    ):
        with pytest.raises(ContentError, match="not_found"):
            read_text_chapter(cache, chapter_id)


def test_source_format(tmp_path):
    source = _source(tmp_path, b"Content")
    with pytest.raises(ContentError, match="unsupported_media_format"):
        build_text_index(
            ReadingSource(source.path, MediaFormat.EPUB), tmp_path / "cache"
        )
    assert not (tmp_path / "cache").exists()
