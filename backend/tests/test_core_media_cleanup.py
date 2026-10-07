"""Tests for exclusive reading metadata and cover cleanup candidates."""

from xml.sax.saxutils import quoteattr

import pytest

from app.core.media.cleanup import reading_companions
from app.core.media.common import ContentError
from app.core.media.handlers.reading import ReadingSource
from app.models.media import MediaFormat


def _opf(href: str) -> str:
    """Build metadata with one external cover reference.

    Args:
        href: The cover URL stored in the OPF manifest.

    Returns:
        A minimal OPF document with a cover-image resource.
    """
    return (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata/><manifest><item id="cover" properties="cover-image" '
        f'href={quoteattr(href)} media-type="image/png"/></manifest></package>'
    )


@pytest.mark.parametrize(
    "format", [MediaFormat.TXT, MediaFormat.EPUB, MediaFormat.CBZ, MediaFormat.ZIP]
)
def test_reading_companions(tmp_path, format):
    source = ReadingSource(tmp_path / f"Book.{format}", format)
    novel = format in (MediaFormat.TXT, MediaFormat.EPUB)
    names = ["Book.OPF", "Content.opf", "metadata.opf"] if novel else ["ComicInfo.XML"]
    for name in names:
        (tmp_path / name).write_text(
            _opf("art/custom%20cover.png") if novel else "<ComicInfo/>"
        )
    for name in ("COVER.PNG", "folder.jpg", "poster.webp", "README.md", ".keep"):
        (tmp_path / name).write_text("Preserve bytes")
    if novel:
        (tmp_path / "art").mkdir()
        (tmp_path / "art/custom cover.png").write_bytes(b"cover")
        (tmp_path / "unrelated.png").write_bytes(b"extra image")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    candidates = reading_companions(source)
    assert set(candidates) == {
        tmp_path / name for name in names + ["COVER.PNG", "folder.jpg", "poster.webp"]
    } | ({tmp_path / "art/custom cover.png"} if novel else set())
    ordered = list(candidates)
    assert set(ordered[-len(names) :]) == {tmp_path / name for name in names}
    assert all(path.read_bytes() == content for path, content in before.items())


@pytest.mark.parametrize("foreign", ["Other.opf", "Other.xml", "Other.nfo"])
def test_shared_covers(tmp_path, foreign):
    source = ReadingSource(tmp_path / "Book.txt", MediaFormat.TXT)
    for name in ("metadata.opf", foreign):
        (tmp_path / name).write_text(_opf("custom.png"))
    for name in ("cover.png", "custom.png"):
        (tmp_path / name).write_bytes(b"shared")
    assert list(reading_companions(source)) == [tmp_path / "metadata.opf"]


@pytest.mark.parametrize(
    "href",
    [
        "../outside.png",
        "/outside.png",
        "https://example.test/outside.png",
        "//example.test/outside.png",
        "file:///outside.png",
        "art%2f..%2f..%2foutside.png",
    ],
)
def test_cover_reference_boundary(tmp_path, href):
    work = tmp_path / "Work"
    work.mkdir()
    metadata = work / "metadata.opf"
    metadata.write_text(_opf(href))
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"shared")
    assert list(
        reading_companions(ReadingSource(work / "Book.txt", MediaFormat.TXT))
    ) == [metadata]
    assert outside.read_bytes() == b"shared"


@pytest.mark.parametrize("linked", ["file", "parent"])
def test_linked_cover(tmp_path, linked):
    work, outside = tmp_path / "Work", tmp_path / "Outside"
    work.mkdir()
    outside.mkdir()
    (outside / "cover.png").write_bytes(b"shared")
    metadata = work / "metadata.opf"
    if linked == "file":
        (work / "cover.png").symlink_to(outside / "cover.png")
        metadata.write_text(_opf("cover.png"))
    else:
        (work / "art").symlink_to(outside, target_is_directory=True)
        metadata.write_text(_opf("art/cover.png"))
    assert list(
        reading_companions(ReadingSource(work / "Book.txt", MediaFormat.TXT))
    ) == [metadata]
    assert (outside / "cover.png").read_bytes() == b"shared"


@pytest.mark.parametrize(
    ("format", "replacement"),
    [
        (MediaFormat.TXT, "Other.epub"),
        (MediaFormat.EPUB, ".hidden.txt"),
        (MediaFormat.CBZ, "Other.zip"),
        (MediaFormat.ZIP, "1.png"),
        (MediaFormat.CBZ, "Chapter/1.png"),
    ],
)
@pytest.mark.parametrize("linked", [False, True])
def test_replacement_body(tmp_path, format, replacement, linked):
    source = ReadingSource(tmp_path / f"Book.{format}", format)
    path = tmp_path / replacement
    path.parent.mkdir(parents=True, exist_ok=True)
    if linked:
        path.symlink_to(tmp_path / "missing")
    else:
        path.write_bytes(b"replacement")
    with pytest.raises(ContentError, match="content_changed"):
        reading_companions(source)
