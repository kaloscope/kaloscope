"""Unit tests for subtitle conversion utilities."""

from app.utils.subtitle import ass_to_vtt, srt_to_vtt


class TestAssToVtt:
    def test_convert_ass_dialogues_to_webvtt(self):
        content = """
[Script Info]
Title: Example

[V4+ Styles]
Format: Name, Fontname, Fontsize
Style: Default,Arial,20

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,Hello\\NWorld {\\i1}again{\\i0}
Dialogue: 0,0:00:04.20,0:00:05.00,Default,,0,0,0,,Comma, stays
"""

        assert ass_to_vtt(content) == (
            "WEBVTT\n"
            "\n"
            "1\n"
            "00:00:01.000 --> 00:00:03.500\n"
            "Hello\n"
            "World again\n"
            "\n"
            "2\n"
            "00:00:04.200 --> 00:00:05.000\n"
            "Comma, stays\n"
        )

    def test_convert_ssa_dialogues_to_webvtt(self):
        content = """
[Events]
Format: Marked, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: Marked=0,0:00:06.10,0:00:08.00,Default,,0,0,0,,SSA text
"""

        assert ass_to_vtt(content) == (
            "WEBVTT\n\n1\n00:00:06.100 --> 00:00:08.000\nSSA text\n"
        )

    def test_sort_ass_dialogues_by_start_time(self):
        content = """
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:22:15.92,0:22:20.27,ED,,0,0,0,,Ending song
Dialogue: 0,0:00:05.38,0:00:07.90,Default,,0,0,0,,Main dialogue
Dialogue: 0,0:01:53.55,0:02:05.32,OP,,0,0,0,,Opening song
"""

        assert ass_to_vtt(content) == (
            "WEBVTT\n"
            "\n"
            "1\n"
            "00:00:05.380 --> 00:00:07.900\n"
            "Main dialogue\n"
            "\n"
            "2\n"
            "00:01:53.550 --> 00:02:05.320\n"
            "Opening song\n"
            "\n"
            "3\n"
            "00:22:15.920 --> 00:22:20.270\n"
            "Ending song\n"
        )

    def test_empty_when_no_dialogue_is_found(self):
        assert ass_to_vtt("[Script Info]\nTitle: Empty\n") == "WEBVTT\n\n"


class TestSrtToVtt:
    def test_convert_srt_cues_to_webvtt(self):
        content = (
            "\ufeff1\r\n"
            "00:00:01,000 --> 00:00:03,500\r\n"
            "Hello\r\n"
            "World\r\n"
            "\r\n"
            "2\r\n"
            "00:00:04,200 --> 00:00:05,000\r\n"
            "Comma, stays\r\n"
        )

        assert srt_to_vtt(content) == (
            "WEBVTT\n"
            "\n"
            "1\n"
            "00:00:01.000 --> 00:00:03.500\n"
            "Hello\n"
            "World\n"
            "\n"
            "2\n"
            "00:00:04.200 --> 00:00:05.000\n"
            "Comma, stays\n"
        )

    def test_convert_srt_cue_without_index_to_webvtt(self):
        content = "00:00:06,100 --> 00:00:08,000\nSRT text\n"

        assert srt_to_vtt(content) == (
            "WEBVTT\n\n1\n00:00:06.100 --> 00:00:08.000\nSRT text\n"
        )
