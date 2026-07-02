"""Media filename metadata extraction utilities."""

import re

from app.utils.numeral import cn_to_int

# pattern to match common separators: dot, underscore, hyphen, space
_SEPARATOR_PATTERN = re.compile(r"[._\-\s]+")

# pattern to match the leading noise prefix like [SubGroup], (SiteName), 【字幕组】 etc.
_PREFIX_PATTERN = re.compile(r"^[\[\(【][^\]\)】]*[\]\)】]\s*")

# year pattern: a 4-digit year between 1900 and 2099
_YEAR_PATTERN = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")

# season pattern: S01, S01E01, s1, Season 1, 第1季, 第二季 etc.
_SEASON_PATTERN = re.compile(
    r"""
    (?:
        [Ss]eason\s*(\d{1,3})
        | [Ss](\d{1,3})(?:[Ee]\d{1,4}|\b)
        | 第\s*(\d{1,3})\s*季
        | 第\s*([零一二三四五六七八九十百千]+)\s*季
    )
    """,
    re.VERBOSE,
)

# episode pattern: E01, ep1, [01], 第1集, 第一话 etc.
_EPISODE_PATTERN = re.compile(
    r"""
    (?:
        [Ee][Pp]?\.?(\d{1,4})
        | \s-\s0*(\d{1,4})\s*(?:\[|$)
        | \[0*(\d{1,4})(?:\s*-\s*\d{1,4})?\]
        | 第\s*(\d{1,4})\s*[集话話回]
        | 第\s*([零一二三四五六七八九十百千]+)\s*[集话話回]
    )
    """,
    re.VERBOSE,
)

# pattern to match common video tags and everything after them
_VIDEO_TAGS_PATTERN = re.compile(
    r"""
    [\.\s\[\(\-_]   # leading separator
    (?:
        # resolution
        \d{3,4}[pPiI]
        | \d{3,4}x\d{3,4}
        | 4[kK]
        | UHD
        # source
        | BluRay | Blu-Ray | BDRip | DLRip | BDRemux | BD | BDMV
        | WEB-DL | WEBRip | WEBDL | WEB
        | DVDRip | DVD
        | HD(?:TV|CAM)?
        | AMZN | NF | DSNP | HMAX | ATVP | iT
        # encoding
        | [Hh]\.?26[45] | HEVC | AVC | x26[45] | xvid | divx
        | HDR(?:10(?:\+|Plus)?)?| DV | DoVi | SDR
        | 10[Bb]it | 8[Bb]it
        # audio
        | DTS(?:-HD|-MA|-X)? | TrueHD | Atmos | DD\+? | AAC | AC3 | FLAC | MP3 | OPUS
        | [257]\.1
        # language/subtitle markers
        | (?:(?:zh|cn|jp|en|ko|fr|de|es|ru)[-_]?){1,3}(?:sub|dub)?\b
        | (?:CHS|CHT|ENG|JPN|KOR)(?:[._+](?:CHS|CHT|ENG|JPN|KOR))*
        | [Ss]ub(?:bed)?
        # misc
        | PROPER | REPACK | REMUX | EXTENDED | THEATRICAL | DIRECTORS\.CUT
        | [Ss]eason\s*\d+
    )
    [\.\s\]\)\-_]?  # trailing separator
    .*$             # consume the rest
    """,
    re.VERBOSE | re.IGNORECASE,
)


def extract_year(name: str) -> int | None:
    """Extract the release year from a media file name.

    Args:
        name: The raw file name or path stem string.

    Returns:
        The four-digit year as an integer, or None if not found.
    """
    match = _YEAR_PATTERN.search(name)
    if match:
        return int(match.group())
    return None


def extract_season(name: str) -> int | None:
    """Extract the season number from a media file name.

    Args:
        name: The raw file name or path stem string.

    Returns:
        The season number as an integer, or None if not found.
    """
    match = _SEASON_PATTERN.search(name)
    if match:
        value = next(g for g in match.groups() if g is not None)
        return int(value) if value.isdigit() else cn_to_int(value)
    return None


def extract_episode(name: str) -> int | None:
    """Extract the episode number from a media file name.

    Args:
        name: The raw file name or path stem string.

    Returns:
        The episode number as an integer, or None if not found.
    """
    match = _EPISODE_PATTERN.search(name)
    if match:
        value = next(g for g in match.groups() if g is not None)
        return int(value) if value.isdigit() else cn_to_int(value)
    return None


def extract_title(name: str, ext: bool = False) -> str:
    """Extract a clean title string from a media file name.

    Strips common prefix noise (sub-group tags, download-site labels) and
    suffix noise (year, season/episode markers, video quality/codec tags,
    file extension) from the input string.

    Args:
        name: The raw file name or path stem string.
        ext: Whether to remove the file extension before extraction.

    Returns:
        A cleaned title string suitable for media scraping.
    """
    # remove file extension if present
    stem = re.sub(r"\.[a-zA-Z0-9]{2,4}$", "", name) if ext else name

    # strip the leading bracketed prefix (sub-group / site label)
    title = _PREFIX_PATTERN.sub("", stem).strip()

    # remove standalone year token before video tags
    title = re.sub(r"(?<!\d)[\(\[（]?(19|20)\d{2}[\)\]）]?(?!\d)", " ", title)

    # remove trailing technical tags and everything after them
    title = _VIDEO_TAGS_PATTERN.sub("", title)

    # remove season and episode markers
    title = _SEASON_PATTERN.sub(" ", title)
    title = _EPISODE_PATTERN.sub(" ", title)

    # replace common separators with spaces and collapse whitespace
    title = _SEPARATOR_PATTERN.sub(" ", title).strip()

    # unwrap a single surrounding bracket pair left after stripping the prefix
    title = re.sub(r"^[\[\(【](.*?)[\]\)】]$", r"\1", title).strip()

    # fallback to original name if nothing survives
    if not title:
        title = _SEPARATOR_PATTERN.sub(" ", stem).strip()

    return title
