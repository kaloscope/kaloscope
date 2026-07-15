"""Media filename metadata extraction utilities."""

import re

from app.utils.numeral import cn_to_int

# pattern to match common separators: dot, underscore, hyphen, space, star
_SEPARATOR_PATTERN = re.compile(r"[._\-\s★☆]+")

# pattern to match the leading noise prefix like [SubGroup], (SiteName), 【字幕组】 etc.
_PREFIX_PATTERN = re.compile(r"^[\[\(【][^\]\)】]*[\]\)】]\s*")

# pattern to match a leading metadata prefix after the release group
_LEADING_META_PREFIX_PATTERN = re.compile(
    r"""
    ^(?:
        [\[\(【]
        (?:
            [^\]\)】]*
            (?:粵語|粤语|國語|国语|日語|日语|英語|英语|雙語|双语|無字幕|无字幕|字幕)
            [^\]\)】]*
            | ANi
            | アニメ
            | 国漫
        )
        [\]\)】]\s*
    )+
    """,
    re.VERBOSE,
)

# pattern to strip unbracketed subtitle group prefixes like 字幕組★Title
_LEADING_STAR_PREFIX_PATTERN = re.compile(r"^[^★☆]{1,40}(?:字幕组|字幕組)[^★☆]*[★☆]\s*")

# pattern to match leading seasonal broadcast markers like ★04月新番★
_LEADING_BROADCAST_MARKER_PATTERN = re.compile(r"^[★☆]\s*\d{1,2}\s*月\s*新番\s*[★☆]\s*")

# pattern to merge adjacent bracketed title segments, e.g. [中文][English]
_BRACKET_BOUNDARY_PATTERN = re.compile(r"[\]\)】]\s*[\[\(【]")

# pattern to strip episode titles after a bracketed episode and before an air date
_BRACKETED_EPISODE_TITLE_SUFFIX_PATTERN = re.compile(
    r"""
    \[
    (?!0*(?:19|20)\d{2}\])
    0*\d{1,4}
    \]
    [^\[\]]+
    \[(?:19|20)\d{2}[._-]\d{1,2}[._-]\d{1,2}\]
    .*$
    """,
    re.VERBOSE,
)

# year pattern: a 4-digit year between 1900 and 2099, excluding dimensions
_YEAR_PATTERN = re.compile(
    r"(?:^|(?<=[._\-\s\]\)】]))[\(\[（]?(?P<year>(?:19|20)\d{2})"
    r"(?!\d|[xX]\d{3,4}|[A-Za-z])[\)\]）]?"
)

# season pattern: S01, S01E01, s1, Season 1, 第1季, 第二季, 第二期 etc.
_SEASON_PATTERN = re.compile(
    r"""
    (?:
        [Ss]eason[._\-\s]*(\d{1,3})
        | (?<![A-Za-z0-9])
          (first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)
          [._\-\s]*[Ss]eason\b
        | (?<![A-Za-z0-9])(\d{1,3})(?:[Ss][Tt]|[Nn][Dd]|[Rr][Dd]|[Tt][Hh])
          [._\-\s]*[Ss]eason\b
        | (?<![A-Za-z0-9])[Ss](\d{1,3})(?:[Ee]\d{1,4}|\b)
        | 第\s*(\d{1,3})\s*[季期]
        | 第\s*([零一二三四五六七八九十百千]+)\s*[季期]
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# episode pattern: E01, ep1, [01], 第1集, 第一话 etc.
_EPISODE_PATTERN = re.compile(
    r"""
    (?:
        [Ss]\d{1,3}[Ee]0*(\d{1,4})
        | (?<![A-Za-z0-9])[Ee][Pp]?\.?0*(\d{1,4})(?!\d)
          (?:\s*[Ee][Nn][Dd]\b)?
          (?:\s*[\[\(【][^\]\)】]*[\]\)】])?
        | \s-\s(?!0*(?:19|20)\d{2}\b)0*(\d{1,4})\s*
          (?:[Ee][Nn][Dd]\b\s*)?(?:-|[\[\(]|$)
        | \[(?!(?:19|20)\d{2}\])0*(\d{1,4})
          (?:[Vv][2-9]|\s*-\s*(?:\d{1,4}|[总總]第\s*(?:\d{1,4}|[零一二三四五六七八九十百千]+)))?\]
        | 第\s*(\d{1,4})\s*[集话話回](?=$|[\s\]\)】\[\(【._-])
        | 第\s*([零一二三四五六七八九十百千]+)\s*[集话話回](?=$|[\s\]\)】\[\(【._-])
    )
    """,
    re.VERBOSE,
)

# collection range pattern, e.g. "| 01-12", "[01-12 合集]", "第01-11話"
_COLLECTION_RANGE_PATTERN = re.compile(
    r"""
    (?:
        \s*\|\s*(?!0*(?:19|20)\d{2}\b)0*\d{1,4}\s*[-~～]\s*
        (?!0*(?:19|20)\d{2}\b)0*\d{1,4}(?:\+[Ss][Pp]x\d+)?(?=\s*(?:[\[\(]|$))
        | \s*[★☆]\s*(?!0*(?:19|20)\d{2}\b)0*\d{1,4}\s*[-~～]\s*
          (?!0*(?:19|20)\d{2}\b)0*\d{1,4}
          (?:[\(（]\s*(?:完|[Ee][Nn][Dd]|[Ff]in)\s*[\)）])?(?=\s*[★☆])
        | \s*第\s*0*\d{1,4}\s*[-~～]\s*0*\d{1,4}\s*[集话話回](?=\s*(?:[\[\(]|$))
        | \s*[\[\(【]\s*合集\s*[\]\)】]\s*[\[\(【]\s*
          0*\d{1,4}\s*[~～]\s*0*\d{1,4}\s*[\]\)】]
        | \s*[\[\(【]\s*0*\d{1,4}\s*[-~～]\s*0*\d{1,4}
          (?:\s*[集话話回])?(?:\s*合集)?\s*[\]\)】]
        | \s*[\[\(【]\s*[总總]第\s*0*\d{1,4}\s*[-~～]\s*
          0*\d{1,4}\s*[\]\)】]
        | \s+(?:\d{1,3}(?:[Ss][Tt]|[Nn][Dd]|[Rr][Dd]|[Tt][Hh])\s*)?
          -\s*0*\d{1,4}\s*[~～]\s*0*\d{1,4}(?=\]\s*(?:[\[\(【]|$))
    )
    """,
    re.VERBOSE,
)

# pattern to match common video tags and everything after them
_VIDEO_TAGS_PATTERN = re.compile(
    r"""
    [\.\s\[\(\-_★☆]   # leading separator
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
        | AMZN | NF | DSNP | HMAX | ATVP | IQIYI | ViuTV | ABEMA | Baha
        | B-Global(?:\s+Donghua)?
        | (?-i:CR) | (?-i:iT)
        # encoding
        | [Hh]\.?26[45] | HEVC | AVC | x26[45] | xvid | divx
        | HDR(?:10(?:\+|Plus)?)?| DV | DoVi | SDR
        | 10[Bb]it | 8[Bb]it
        # audio
        | DTS(?:-HD|-MA|-X)? | TrueHD | Atmos | DD\+? | AAC | AC3 | FLAC | MP3 | OPUS
        | [257]\.1
        # language/subtitle markers
        | (?:(?:zh|cn|jp|en|fr|es|ru)[-_]?){1,3}(?:sub|dub)?\b
        | (?:CHS|CHT|ENG|JPN|KOR|GB|BIG5)
          (?:[._+](?:CHS|CHT|ENG|JPN|KOR|GB|BIG5))*
        | [简簡繁](?:[简簡繁])?(?:日|中)?(?:[内內][嵌封](?:字幕)?|字幕)
        | (?:[简簡繁粵粤國国日英中]+(?:語|语)|[简簡繁粵粤國国日英中]+[雙双]語)
          (?:\+(?:[无無]字幕|[内內][嵌封](?:[简簡繁](?:体|體)?(?:中|日)?文?)?字幕))*
        | [内內][嵌封](?:[简簡繁](?:体|體)?(?:中|日)?文?)?字幕
        | [无無]字幕
        | [Ss]ub(?:bed)?
        # misc
        | PROPER | REPACK | REMUX | EXTENDED | THEATRICAL | DIRECTORS\.CUT
        | Pre-Air
        | [Ss]eason\s*\d+
    )
    (?=[\.\s\]\)\-_★☆]|$)  # tag must be complete, not a word prefix
    [\.\s\]\)\-_★☆]?  # trailing separator
    .*$             # consume the rest
    """,
    re.VERBOSE | re.IGNORECASE,
)

# map of English ordinal season words to integers
_ENGLISH_ORDINAL_NUMBERS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
}


def extract_year(name: str) -> int | None:
    """Extract the release year from a media file name.

    Args:
        name: The raw file name or path stem string.

    Returns:
        The four-digit year as an integer, or `None` if not found.
    """
    match = _YEAR_PATTERN.search(name)
    if match:
        return int(match.group("year"))
    return None


def extract_season(name: str) -> int | None:
    """Extract the season number from a media file name.

    Args:
        name: The raw file name or path stem string.

    Returns:
        The season number as an integer, or `None` if not found.
    """
    match = _SEASON_PATTERN.search(name)
    if match:
        value = next(g for g in match.groups() if g is not None)
        if value.isdigit():
            return int(value)
        english_number = _ENGLISH_ORDINAL_NUMBERS.get(value.lower())
        return english_number if english_number is not None else cn_to_int(value)
    return None


def extract_episode(name: str) -> int | None:
    """Extract the episode number from a media file name.

    Args:
        name: The raw file name or path stem string.

    Returns:
        The episode number as an integer, or `None` if not found.
    """
    match = _EPISODE_PATTERN.search(name)
    if match:
        value = next(g for g in match.groups() if g is not None)
        return int(value) if value.isdigit() else cn_to_int(value)
    return None


def extract_title(name: str) -> str:
    """Extract a clean title string from a media file name.

    Strips common prefix noise (sub-group tags, download-site labels) and
    suffix noise (year, season/episode markers, video quality/codec tags,
    etc.) from the input string.

    Args:
        name: The raw file name or path stem string.

    Returns:
        A cleaned title string suitable for media scraping.
    """
    # strip the leading bracketed prefix (sub-group / site label)
    title = _PREFIX_PATTERN.sub("", name).strip()
    title = _LEADING_META_PREFIX_PATTERN.sub("", title).strip()
    title = _LEADING_STAR_PREFIX_PATTERN.sub("", title).strip()
    title = _LEADING_BROADCAST_MARKER_PATTERN.sub("", title).strip()
    title = _BRACKETED_EPISODE_TITLE_SUFFIX_PATTERN.sub("", title).strip()

    # remove standalone year token before video tags
    title = _YEAR_PATTERN.sub(" ", title)

    # remove collection episode ranges before technical tags truncate context
    title = _COLLECTION_RANGE_PATTERN.sub(" ", title)

    # remove trailing technical tags and everything after them
    title = _VIDEO_TAGS_PATTERN.sub("", title)

    # remove any collection episode ranges exposed by technical tag cleanup
    title = _COLLECTION_RANGE_PATTERN.sub(" ", title)

    # remove season and episode markers
    title = _SEASON_PATTERN.sub(" ", title)
    title = _EPISODE_PATTERN.sub(" ", title)

    # replace common separators with spaces and collapse whitespace
    title = _SEPARATOR_PATTERN.sub(" ", title).strip()
    title = _BRACKET_BOUNDARY_PATTERN.sub(" ", title).strip()

    # unwrap a single surrounding bracket pair left after stripping the prefix
    title = re.sub(r"^[\[\(【](.*?)[\]\)】]$", r"\1", title).strip()

    # fallback to original name if nothing survives
    if not title:
        title = _SEPARATOR_PATTERN.sub(" ", name).strip()

    return title
