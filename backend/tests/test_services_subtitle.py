"""Unit tests for subtitle service helpers."""

import asyncio

import pytest

from app.services.subtitle import SubtitleService


class TestSubtitleService:
    @pytest.mark.parametrize(
        ("label", "language"),
        [
            ("chs", "zh-Hans"),
            ("CHT", "zh-Hant"),
            ("cn", "zh-CN"),
            ("zh", "zh"),
            ("chi", "zh"),
            ("zho", "zh"),
            ("zh-cn", "zh-CN"),
            ("zh_CN", "zh-CN"),
            ("zh-Hans", "zh-Hans"),
            ("zh-tw", "zh-TW"),
            ("zh_Hant", "zh-Hant"),
            ("en", "en"),
            ("eng", "en"),
            ("en-us", "en-US"),
            ("ja", "ja"),
            ("jp", "ja"),
            ("jpn", "ja"),
            ("ja-jp", "ja-JP"),
            ("fr-fr", "fr-FR"),
            ("fre", "fr"),
            ("deu", "de"),
            ("ger", "de"),
            ("ko-kr", "ko-KR"),
            ("pt_BR", "pt-BR"),
            ("spa", "es"),
            ("es-419", "es-419"),
        ],
    )
    def test_standardizes_language_tags(self, label: str, language: str):
        assert SubtitleService._external_track_language(label) == language

    @pytest.mark.parametrize("label", ["subtitle", "C.UTF-8", "zh-普通话"])
    def test_ignores_invalid_language_tags(self, label: str):
        assert SubtitleService._external_track_language(label) is None

    def test_detects_non_utf8_subtitles(self, tmp_path):
        path = tmp_path / "example.ssa"
        dialogues = "\n".join(
            (
                f"Dialogue: Marked=0,0:00:{index:02d}.00,0:00:{index + 1:02d}.00,"
                f"Default,,0,0,0,,这是第{index}句中文字幕，用来测试旧字幕文件。"
            )
            for index in range(1, 20)
        )
        content = (
            "[Events]\n"
            "Format: Marked, Start, End, Style, Name, MarginL, MarginR, "
            f"MarginV, Effect, Text\n{dialogues}\n"
        )
        path.write_bytes(content.encode("gb18030"))

        try:
            result = asyncio.run(SubtitleService.load_external_content(path))
        except UnicodeDecodeError as exc:
            pytest.fail(f"subtitle content should decode automatically: {exc}")

        expected_cues = "\n\n".join(
            (
                f"{index}\n"
                f"00:00:{index:02d}.000 --> 00:00:{index + 1:02d}.000\n"
                f"这是第{index}句中文字幕，用来测试旧字幕文件。"
            )
            for index in range(1, 20)
        )
        assert result == (
            f"WEBVTT\n\n{expected_cues}\n",
            SubtitleService.WEBVTT_CONTENT_TYPE,
        )
