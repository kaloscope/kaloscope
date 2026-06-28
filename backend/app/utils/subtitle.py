"""Subtitle conversion utilities."""

import re
from dataclasses import dataclass

_ASS_TAG_RE = re.compile(r"\{[^{}]*\}")
_ASS_TIME_RE = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})(?:\.(\d{1,3}))?$")
_SRT_TIME_RE = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})$")
_SUBTITLE_BLOCK_RE = re.compile(r"\n{2,}")


@dataclass(frozen=True)
class _Cue:
    """A plain subtitle cue converted to WebVTT."""

    start: str
    end: str
    text: str


def ass_to_vtt(content: str) -> str:
    """Convert ASS/SSA subtitle content to WebVTT.

    Args:
        content: The ASS or SSA subtitle content.

    Returns:
        The converted WebVTT content.
    """
    fields: list[str] = []
    cues: list[_Cue] = []
    in_events = False

    for raw_line in content.splitlines():
        line = raw_line.strip("\ufeff").strip()
        if not line:
            continue

        lower = line.lower()
        if lower == "[events]":
            in_events = True
            continue
        if line.startswith("["):
            in_events = False
            continue
        if not in_events:
            continue

        if lower.startswith("format:"):
            fields = [
                field.strip().lower()
                for field in line.split(":", maxsplit=1)[1].split(",")
            ]
            continue
        if lower.startswith("dialogue:") and fields:
            payload = line.split(":", maxsplit=1)[1].lstrip()
            if cue := _parse_ass_dialogue(payload, fields):
                cues.append(cue)

    return _format_vtt(cues)


def _parse_ass_dialogue(payload: str, fields: list[str]) -> _Cue | None:
    """Parse an ASS/SSA dialogue payload into a plain cue.

    Args:
        payload: The dialogue payload after the ``Dialogue:`` marker.
        fields: The normalized field names from the matching ``Format:`` line.

    Returns:
        The parsed cue, or None if the dialogue cannot be converted.
    """
    parts = payload.split(",", maxsplit=len(fields) - 1)
    if len(parts) != len(fields):
        return None

    data = {field: parts[index].strip() for index, field in enumerate(fields)}
    start = _format_ass_time(data.get("start", ""))
    end = _format_ass_time(data.get("end", ""))
    text = _clean_ass_text(data.get("text", ""))
    if not start or not end or not text:
        return None
    return _Cue(start=start, end=end, text=text)


def _format_ass_time(value: str) -> str | None:
    """Format an ASS/SSA timestamp as a WebVTT timestamp.

    Args:
        value: The ASS/SSA timestamp value.

    Returns:
        The WebVTT timestamp, or None if the value is invalid.
    """
    match = _ASS_TIME_RE.match(value.strip())
    if not match:
        return None

    hours, minutes, seconds, fraction = match.groups()
    milliseconds = int((fraction or "0").ljust(3, "0")[:3])
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}.{milliseconds:03d}"


def _clean_ass_text(text: str) -> str:
    """Strip ASS/SSA styling and normalize line breaks.

    Args:
        text: The ASS/SSA dialogue text.

    Returns:
        The plain subtitle text.
    """
    text = text.replace(r"\N", "\n").replace(r"\n", "\n").replace(r"\h", " ")
    text = _ASS_TAG_RE.sub("", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def srt_to_vtt(content: str) -> str:
    """Convert SubRip subtitle content to WebVTT.

    Args:
        content: The SubRip subtitle content.

    Returns:
        The converted WebVTT content.
    """
    cues: list[_Cue] = []
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()

    for block in _SUBTITLE_BLOCK_RE.split(normalized):
        lines = [line.strip("\ufeff").strip() for line in block.splitlines()]
        if not lines:
            continue

        time_index = 1 if len(lines) > 1 and lines[0].isdigit() else 0
        if cue := _parse_srt_cue(lines, time_index):
            cues.append(cue)

    return _format_vtt(cues)


def _parse_srt_cue(lines: list[str], time_index: int) -> _Cue | None:
    """Parse a SubRip cue block into a plain cue.

    Args:
        lines: The normalized cue block lines.
        time_index: The index of the timing line in the cue block.

    Returns:
        The parsed cue, or None if the block cannot be converted.
    """
    if time_index >= len(lines):
        return None

    timing = lines[time_index]
    if "-->" not in timing:
        return None

    start_value, end_value = [part.strip() for part in timing.split("-->", 1)]
    start = _format_srt_time(start_value)
    end = _format_srt_time(end_value.split(maxsplit=1)[0])
    text = "\n".join(line for line in lines[time_index + 1 :] if line).strip()
    if not start or not end or not text:
        return None
    return _Cue(start=start, end=end, text=text)


def _format_srt_time(value: str) -> str | None:
    """Format a SubRip timestamp as a WebVTT timestamp.

    Args:
        value: The SubRip timestamp value.

    Returns:
        The WebVTT timestamp, or None if the value is invalid.
    """
    match = _SRT_TIME_RE.match(value.strip())
    if not match:
        return None

    hours, minutes, seconds, fraction = match.groups()
    milliseconds = int(fraction.ljust(3, "0")[:3])
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}.{milliseconds:03d}"


def _format_vtt(cues: list[_Cue]) -> str:
    """Format plain cues as WebVTT content.

    Args:
        cues: The cues to write.

    Returns:
        The WebVTT content.
    """
    lines = ["WEBVTT", ""]
    for index, cue in enumerate(cues, start=1):
        lines.extend([str(index), f"{cue.start} --> {cue.end}", cue.text, ""])
    return "\n".join(lines) if cues else "WEBVTT\n\n"
