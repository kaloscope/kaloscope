"""Unit tests for the media keyword extractor utility."""

from app.utils.extractor import (
    extract_episode,
    extract_season,
    extract_title,
    extract_year,
)


class TestExtractYear:
    def test_year_1990s(self):
        assert extract_year("Schindler.s.List.1993") == 1993

    def test_year_2000s(self):
        assert extract_year("Gladiator.2000.BluRay") == 2000

    def test_year_in_parentheses(self):
        assert extract_year("The Dark Knight (2008)") == 2008

    def test_year_in_brackets(self):
        assert extract_year("Parasite [2019] BluRay") == 2019

    def test_year_at_end(self):
        assert extract_year("Avengers.Endgame.2019") == 2019

    def test_year_with_dot_separator(self):
        assert extract_year("Inception.2010.1080p.BluRay.x264") == 2010

    def test_year_in_chinese_filename(self):
        assert extract_year("流浪地球2.2023.2160p.WEB-DL") == 2023

    def test_tv_show_no_year(self):
        assert extract_year("[SubGroup] Attack on Titan S04E01 1080p") is None

    def test_no_year(self):
        assert extract_year("Some.Movie.Title.BluRay") is None

    def test_year_not_confused_by_resolution(self):
        # 1080 should not be treated as a year
        assert extract_year("Movie.1080p") is None


class TestExtractSeason:
    def test_uppercase_s_format(self):
        assert extract_season("Breaking.Bad.S03E07.1080p") == 3

    def test_lowercase_s_format(self):
        assert extract_season("game.of.thrones.s08e06") == 8

    def test_season_word_format(self):
        assert extract_season("The Crown Season 4 Complete") == 4

    def test_season_one_digit(self):
        assert extract_season("[SubGroup] Naruto S1E01") == 1

    def test_season_number_padded(self):
        assert extract_season("Doctor.Who.S10E01.HDR") == 10

    def test_chinese_season_marker(self):
        assert extract_season("庆余年 第2季 全集") == 2

    def test_chinese_season_number_1(self):
        assert extract_season("某某动漫.第一季.全集") == 1

    def test_chinese_season_number_21(self):
        assert extract_season("[某某动漫] [第二十一季]") == 21

    def test_no_season(self):
        assert extract_season("Inception.2010.1080p") is None

    def test_movie_with_year_no_season(self):
        assert extract_season("The.Godfather.1972.BluRay") is None


class TestExtractEpisode:
    def test_uppercase_e_format(self):
        assert extract_episode("Breaking.Bad.S03E07.1080p") == 7

    def test_lowercase_e_format(self):
        assert extract_episode("game.of.thrones.s08e06") == 6

    def test_ep_format(self):
        assert extract_episode("[HorribleSubs] One Piece - EP950 [720p]") == 950

    def test_dash_number_format(self):
        assert extract_episode("[SubGroup] Anime Title - 01 [1080p]") == 1

    def test_bracket_format(self):
        assert extract_episode("[SubGroup] Anime S2 [06][1080p].mkv") == 6

    def test_chinese_episode_marker_ji(self):
        assert extract_episode("庆余年 第3集") == 3

    def test_chinese_episode_marker_hua(self):
        assert extract_episode("海贼王 第1000話") == 1000

    def test_chinese_episode_number_1(self):
        assert extract_episode("某某动漫 第一季 第一集") == 1

    def test_chinese_episode_number_21(self):
        assert extract_episode("某某动漫.第二季.第二十一集") == 21

    def test_chinese_episode_number_321(self):
        assert extract_episode("【某某动漫】【第三季】【第三百二十一回】") == 321

    def test_episode_zero(self):
        assert extract_episode("Show.S01E00.Special") == 0

    def test_large_episode_number(self):
        assert extract_episode("Series.S01E1000.mkv") == 1000

    def test_no_episode(self):
        assert extract_episode("Inception.2010.1080p") is None


class TestExtractTitle:
    # --- Movies ---

    def test_movie_basic(self):
        result = extract_title("Inception.2010.1080p.BluRay.x264.mkv")
        assert result.lower() == "inception"

    def test_movie_with_parenthesized_year(self):
        result = extract_title("The Dark Knight (2008) 1080p.mkv")
        assert "dark knight" in result.lower()

    def test_movie_chinese(self):
        result = extract_title("流浪地球2.2023.2160p.WEB-DL.mkv")
        assert "流浪地球" in result

    def test_movie_with_bracketed_year(self):
        result = extract_title("Parasite [2019] BluRay 1080p.mp4")
        assert "parasite" in result.lower()

    def test_movie_no_extension(self):
        result = extract_title("Gladiator.2000.BluRay.1080p")
        assert "gladiator" in result.lower()

    # --- TV shows ---

    def test_tv_show_with_sub_group_prefix(self):
        result = extract_title("[HorribleSubs] Attack on Titan - 25 [1080p].mkv")
        assert "attack on titan" in result.lower()

    def test_tv_show_sXXeXX_format(self):
        result = extract_title("Breaking.Bad.S03E07.720p.BluRay.mkv")
        assert "breaking bad" in result.lower()

    def test_tv_show_season_word(self):
        result = extract_title("The Crown Season 4 Complete 1080p WEB-DL")
        assert "the crown" in result.lower()

    def test_tv_show_chinese_episode(self):
        result = extract_title("庆余年 第2季 第03集 WEB-DL 1080P H264")
        assert "庆余年" in result

    def test_tv_show_multi_prefix_brackets(self):
        result = extract_title("[SubGroup][Studio] Some Anime S01E01 [BD 1080p HEVC]")
        assert "some anime" in result.lower()

    def test_tv_show_all_brackets_format(self):
        result = extract_title("[Nekomoe kissaten][Sousou no Frieren][01][1080p][JPSC]")
        assert result == "Sousou no Frieren"

    def test_tv_show_all_brackets_episode_range(self):
        result = extract_title(
            "[DMG&SumiSora][Tongari_Boushi_no_Atelier][12-13][1080P][GB][MP4]"
        )
        assert result == "Tongari Boushi no Atelier"

    # --- Edge cases ---

    def test_title_with_multiple_separators(self):
        result = extract_title("The.Big.Bang.Theory.S12E24.1080p")
        assert "big bang theory" in result.lower()

    def test_title_with_codec_info(self):
        result = extract_title("Oppenheimer.2023.1080p.WEB-DL.H265.AAC-SubGroup.mkv")
        assert "oppenheimer" in result.lower()

    def test_title_with_hdr_tags(self):
        result = extract_title("Dune.Part.Two.2024.2160p.UHD.BluRay.HDR10.DTS-HD.MA")
        assert "dune part two" in result.lower()

    def test_title_with_parenthesized_dlrip_tags(self):
        result = extract_title("[milky] 真・燐月 (DLrip 1280x720 x264 AAC)")
        assert result == "真・燐月"

    def test_plain_movie_name(self):
        result = extract_title("Dune")
        assert result.lower() == "dune"

    def test_title_always_returns_string(self):
        assert extract_title("[Group] 2023 1080p S01E01")
        assert isinstance(extract_title(""), str)
