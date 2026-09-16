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

    def test_adjacent_bracket_year(self):
        assert (
            extract_year("[GM-Team][国漫][仙逆][Renegade Immortal][2023][146]") == 2023
        )

    def test_year_at_end(self):
        assert extract_year("Avengers.Endgame.2019") == 2019

    def test_year_dotted(self):
        assert extract_year("Inception.2010.1080p.BluRay.x264") == 2010

    def test_year_underscore(self):
        assert extract_year("流浪地球2_2023_2160p_WEB-DL") == 2023

    def test_attached_year_ignored(self):
        assert extract_year("Doraemon1979") is None

    def test_tv_no_year(self):
        assert extract_year("[SubGroup] Attack on Titan S04E01 1080p") is None

    def test_no_year(self):
        assert extract_year("Some.Movie.Title.BluRay") is None

    def test_resolution_not_year(self):
        # 1080 should not be treated as a year
        assert extract_year("Movie.1080p") is None

    def test_dimensions_not_year(self):
        assert extract_year("Movie.1920x1080.HEVC") is None


class TestExtractSeason:
    def test_uppercase_s(self):
        assert extract_season("Breaking.Bad.S03E07.1080p") == 3

    def test_lowercase_s(self):
        assert extract_season("game.of.thrones.s08e06") == 8

    def test_season_word(self):
        assert extract_season("The Crown Season 4 Complete") == 4

    def test_season_word_dotted(self):
        assert extract_season("The.Crown.Season.4.Complete") == 4

    def test_ordinal_season_word(self):
        assert extract_season("Re Zero kara Hajimeru Isekai Seikatsu 4th Season") == 4

    def test_english_season_word(self):
        assert extract_season("Diamond no Ace: Act II Second Season - 12") == 2

    def test_season_one_digit(self):
        assert extract_season("[SubGroup] Naruto S1E01") == 1

    def test_season_number_padded(self):
        assert extract_season("Doctor.Who.S10E01.HDR") == 10

    def test_chinese_season_marker(self):
        assert extract_season("庆余年 第2季 全集") == 2

    def test_chinese_season_number_1(self):
        assert extract_season("某某动漫.第一季.全集") == 1

    def test_chinese_period_number_2(self):
        assert extract_season("某某动漫.第二期.全集") == 2

    def test_chinese_season_number_21(self):
        assert extract_season("[某某动漫] [第二十一季]") == 21

    def test_no_season(self):
        assert extract_season("Inception.2010.1080p") is None

    def test_embedded_s_not_season(self):
        assert extract_season("MARS01") is None


class TestExtractEpisode:
    def test_uppercase_e(self):
        assert extract_episode("Breaking.Bad.S03E07.1080p") == 7

    def test_lowercase_e(self):
        assert extract_episode("game.of.thrones.s08e06") == 6

    def test_ep(self):
        assert extract_episode("[HorribleSubs] One Piece - EP950 [720p]") == 950

    def test_dash_number(self):
        assert extract_episode("[SubGroup] Anime Title - 01 [1080p]") == 1

    def test_dash_number_at_end(self):
        assert extract_episode("Show - 01") == 1

    def test_dash_number_before_dash(self):
        assert extract_episode("Show - 02 - 1080p") == 2

    def test_dash_number_before_parens(self):
        assert extract_episode("Show - 03 (WEB 1080p)") == 3

    def test_dash_number_before_end(self):
        assert extract_episode("Jujutsu Kaisen S3 - 12 END [WEB]") == 12

    def test_bracket_format(self):
        assert extract_episode("[SubGroup] Anime S2 [06][1080p].mkv") == 6

    def test_bracket_total_episode(self):
        assert extract_episode("[Show][10 - 总第76][WEB-DL]") == 10

    def test_traditional_total_episode(self):
        assert extract_episode("[Show][09 - 總第75][WEB-DL]") == 9

    def test_bracket_version(self):
        assert extract_episode("Show [16v2][WebRip]") == 16
        assert extract_episode("Show [16v4][WebRip]") == 16
        assert extract_episode("Show [16v9][WebRip]") == 16
        assert extract_episode("Show [16v10][WebRip]") is None

    def test_chinese_episode_ji(self):
        assert extract_episode("庆余年 第3集") == 3

    def test_chinese_episode_hua(self):
        assert extract_episode("海贼王 第1000話") == 1000

    def test_chinese_episode_number_1(self):
        assert extract_episode("某某动漫 第一季 第一集") == 1

    def test_chinese_episode_number_21(self):
        assert extract_episode("某某动漫.第二季.第二十一集") == 21

    def test_chinese_episode_number_321(self):
        assert extract_episode("【某某动漫】【第三季】【第三百二十一回】") == 321

    def test_chinese_marker_boundary(self):
        assert (
            extract_episode(
                "落第贤者的学院无双～第二回转生，S等级作弊魔术师冒险记～ - 01"
            )
            == 1
        )

    def test_episode_zero(self):
        assert extract_episode("Show.S01E00.Special") == 0

    def test_large_episode_number(self):
        assert extract_episode("Series.S01E1000.mkv") == 1000

    def test_no_episode(self):
        assert extract_episode("Inception.2010.1080p") is None

    def test_year_not_episode(self):
        assert extract_episode("Movie.2023") is None

    def test_bracketed_year_not_episode(self):
        assert extract_episode("Movie.Title.[2023]") is None


class TestExtractTitle:
    # --- Movies ---

    def test_movie_basic(self):
        result = extract_title("Inception.2010.1080p.BluRay.x264.mkv")
        assert result.lower() == "inception"

    def test_movie_parenthesized_year(self):
        result = extract_title("The Dark Knight (2008) 1080p.mkv")
        assert "dark knight" in result.lower()

    def test_movie_chinese(self):
        result = extract_title("流浪地球2.2023.2160p.WEB-DL.mkv")
        assert "流浪地球" in result

    def test_movie_bracketed_year(self):
        result = extract_title("Parasite [2019] BluRay 1080p.mp4")
        assert "parasite" in result.lower()

    def test_movie_no_extension(self):
        result = extract_title("Gladiator.2000.BluRay.1080p")
        assert "gladiator" in result.lower()

    # --- TV shows ---

    def test_tv_subgroup_prefix(self):
        result = extract_title("[HorribleSubs] Attack on Titan - 25 [1080p].mkv")
        assert "attack on titan" in result.lower()

    def test_tv_sxxexx(self):
        result = extract_title("Breaking.Bad.S03E07.720p.BluRay.mkv")
        assert "breaking bad" in result.lower()

    def test_tv_season_word(self):
        result = extract_title("The Crown Season 4 Complete 1080p WEB-DL")
        assert "the crown" in result.lower()

    def test_tv_ordinal_season(self):
        result = extract_title("Show Title 4th Season - 01 [1080p]")
        assert result == "Show Title"

    def test_tv_chinese_episode(self):
        result = extract_title("庆余年 第2季 第03集 WEB-DL 1080P H264")
        assert "庆余年" in result

    def test_tv_multi_prefix(self):
        result = extract_title("[SubGroup][Studio] Some Anime S01E01 [BD 1080p HEVC]")
        assert "some anime" in result.lower()

    def test_tv_all_brackets(self):
        result = extract_title("[Nekomoe kissaten][Sousou no Frieren][01][1080p][JPSC]")
        assert result == "Sousou no Frieren"

    def test_tv_bracket_episode_range(self):
        result = extract_title(
            "[DMG&SumiSora][Tongari_Boushi_no_Atelier][12-13][1080P][GB][MP4]"
        )
        assert result == "Tongari Boushi no Atelier"

    def test_tv_bracket_total_episode(self):
        result = extract_title(
            "[晚街与灯][Re：从零开始的异世界生活 第四季 / "
            "Re:Zero kara Hajimeru Isekai Seikatsu 4th Season]"
            "[10 - 总第76][WEB-DL Remux][1080P_AVC_AAC][简繁日内封PGS]"
        )
        assert (
            result == "Re：从零开始的异世界生活 / Re:Zero kara Hajimeru Isekai Seikatsu"
        )

    # --- Edge cases ---

    def test_multiple_separators(self):
        result = extract_title("The.Big.Bang.Theory.S12E24.1080p")
        assert "big bang theory" in result.lower()

    def test_codec_tags(self):
        result = extract_title("Oppenheimer.2023.1080p.WEB-DL.H265.AAC-SubGroup.mkv")
        assert "oppenheimer" in result.lower()

    def test_hdr_tags(self):
        result = extract_title("Dune.Part.Two.2024.2160p.UHD.BluRay.HDR10.DTS-HD.MA")
        assert "dune part two" in result.lower()

    def test_keeps_tag_prefix(self):
        result = extract_title("Some.Webster.Crisis.BDrive.2024.1080p.WEB-DL")
        assert result == "Some Webster Crisis BDrive"

    def test_iqiyi_source(self):
        result = extract_title(
            "花样少年少女 第二季 - 02 [IQIYI WebRip 2160p NVENC AAC]"
        )
        assert result == "花样少年少女"

    def test_viutv_source(self):
        result = extract_title(
            "Re：從零開始的異世界生活 第四季 / "
            "Re：Zero kara Hajimeru Isekai Seikatsu 4th Season - 11 "
            "[ViuTV][WEB-DL][CHT][1080p][AVC AAC]"
        )
        assert (
            result
            == "Re：從零開始的異世界生活 / Re：Zero kara Hajimeru Isekai Seikatsu"
        )

    def test_cr_source(self):
        result = extract_title("LV999的村民 - 02 [CR WebRip AI2160p NVENC AAC]")
        assert result == "LV999的村民"

    def test_abema_source(self):
        result = extract_title(
            "[黒ネズミたち] 左撇子艾倫 / Hidarikiki no Eren - 12 "
            "(ABEMA 1920x1080 AVC AAC MKV)"
        )
        assert result == "左撇子艾倫 / Hidarikiki no Eren"

    def test_baha_source(self):
        result = extract_title(
            "[黒ネズミたち] 黑貓與魔女的教室 / Kuroneko to Majo no Kyoushitsu "
            "- 12 (Baha 1920x1080 AVC AAC MP4)"
        )
        assert result == "黑貓與魔女的教室 / Kuroneko to Majo no Kyoushitsu"

    def test_b_global_source(self):
        result = extract_title(
            "[黒ネズミたち] 海贼王 / One Piece - 1168 (B-Global 3840x2160 HEVC AAC MKV)"
        )
        assert result == "海贼王 / One Piece"

    def test_b_global_donghua_source(self):
        result = extract_title(
            "为喵人生 / Reborn as a Cat - 36 (B-Global Donghua 1920x1080 HEVC AAC MKV)"
        )
        assert result == "为喵人生 / Reborn as a Cat"

    def test_chinese_subtitle(self):
        result = extract_title(
            "LV999的村民 / Lv999 no Murabito - 01 - [繁日内嵌][AVC 8bit 1080P]"
        )
        assert result == "LV999的村民 / Lv999 no Murabito"

    def test_pre_air_tag(self):
        result = extract_title(
            "[Prejudice-Studio] 与你相恋至生命尽头 Kimi ga Shinu made Koi wo Shitai "
            "- 01 [Pre-Air][WebRip 1080P AVC 8bit AAC MKV][简繁日内封][V2]"
        )
        assert result == "与你相恋至生命尽头 Kimi ga Shinu made Koi wo Shitai"

    def test_language_prefix(self):
        result = extract_title(
            "[jibaketa合成&音頻壓制][代理商粵語]咒術迴戰 第三季 / "
            "Jujutsu Kaisen S3 - 12 END [粵日雙語+內封繁體中文字幕]"
            "(WEB 1920x1080 AVC AACx2 SRT Ani-One CHT)"
        )
        assert result == "咒術迴戰 / Jujutsu Kaisen"

    def test_broadcast_marker(self):
        result = extract_title(
            "【喵萌奶茶屋】★04月新番★"
            "[杖與劍的魔劍譚 / Tsue to Tsurugi no Wistoria]"
            "[24][1080p][繁日雙語]"
        )
        assert result == "杖與劍的魔劍譚 / Tsue to Tsurugi no Wistoria"

    def test_split_brackets(self):
        result = extract_title(
            "【TSDM字幕组】[淡岛百景][Awajima Hyakkei][11]"
            "[HEVC-10bit 1080p AAC][MKV][简繁日内封字幕]淡島百景"
        )
        assert result == "淡岛百景 Awajima Hyakkei"

    def test_episode_title_date(self):
        result = extract_title(
            "[丸子家族][櫻桃小丸子第二期(Chibi Maruko-chan II)][1536]"
            "小丸子喜歡媽媽的笑容&中野先生是晴天男?"
            "[2026.06.28][BIG5][1080P][MP4]"
        )
        assert result == "櫻桃小丸子 (Chibi Maruko chan II)"

    def test_keeps_attached_year(self):
        result = extract_title(
            "[哆啦字幕組][哆啦A夢大山版 Doraemon1979][1263][1080P][HDTV][繁日雙語][MP4]"
        )
        assert result == "哆啦A夢大山版 Doraemon1979"

    def test_versioned_episode(self):
        result = extract_title(
            "[绿茶字幕组] 欢迎来到实力至上主义的教室 第四季 / "
            "Youkoso Jitsuryoku Shijou Shugi no Kyoushitsu e S4 "
            "[16v2][WebRip][1080p][简日内嵌]"
        )
        assert (
            result == "欢迎来到实力至上主义的教室 / "
            "Youkoso Jitsuryoku Shijou Shugi no Kyoushitsu e"
        )

    def test_gb_subtitle(self):
        result = extract_title("[Group][Anime Title][10][GB][1080P][MP4]")
        assert result == "Anime Title"

    def test_big5_subtitle(self):
        result = extract_title("[Group][Anime Title][10][BIG5][1080P][MP4]")
        assert result == "Anime Title"

    def test_collection_range(self):
        result = extract_title(
            "Raise wa Tanin ga Ii S01 | 01-12 [简繁字幕] BDrip 1080p"
        )
        assert result == "Raise wa Tanin ga Ii"

    def test_collection_range_specials(self):
        result = extract_title(
            "[7³ACG] 龙与虎/Toradora! S01 | 01-25+SPx6 "
            "[简繁字幕] BDrip 1080p x265 OPUS 2.0"
        )
        assert result == "龙与虎/Toradora!"

    def test_chinese_range(self):
        result = extract_title(
            "[Alice Raw][アニメ] 一疊間漫畫咖啡廳日常 "
            "一畳間まんきつ暮らし！ 第01-11話 (3840x2160 x265 AAC)"
        )
        assert result == "一疊間漫畫咖啡廳日常 一畳間まんきつ暮らし！"

    def test_bracketed_chinese_range(self):
        result = extract_title(
            "[LoliHouse] 主播女孩重度依赖 / NEEDY GIRL OVERDOSE "
            "[01-13话][WebRip 1080p HEVC-10bit AAC][简繁内封字幕][Fin]"
        )
        assert result == "主播女孩重度依赖 / NEEDY GIRL OVERDOSE"

    def test_bracketed_range(self):
        result = extract_title(
            "[LoliHouse] 最强的职业不是勇者也不是贤者好像是鉴定士(暂定)的样子？ / "
            "Kanteishi (Kari) [01-12 合集][WebRip 1080p HEVC-10bit AAC]"
            "[无中字][Fin]"
        )
        assert (
            result == "最强的职业不是勇者也不是贤者好像是鉴定士(暂定)的样子？ / "
            "Kanteishi (Kari)"
        )

    def test_wave_range(self):
        result = extract_title(
            "[晚街与灯][命运-奇异赝品_Fate strange Fake][合集][00~13]"
            "[BDRip][1080P_HEVC-10bit_FLAC][简繁日双语外挂PGS]"
        )
        assert result == "命运 奇异赝品 Fate strange Fake"

    def test_wave_range_total(self):
        result = extract_title(
            "[晚街与灯][Re：从零开始的异世界生活 第四季丧失篇合集 / "
            "Re:Zero kara Hajimeru Isekai Seikatsu 4th - 01~11]"
            "[总第67~77][WEB-DL Remux][1080P_AVC_AAC][简繁日内封PGS]"
        )
        assert (
            result == "Re：从零开始的异世界生活 丧失篇合集 / "
            "Re:Zero kara Hajimeru Isekai Seikatsu"
        )

    def test_star_range(self):
        result = extract_title(
            "六四位元字幕組★哪裡有溫柔對待阿宅的辣妹！？ "
            "Otaku ni Yasashii Gal wa Inai★01~12(完)★1920x1080"
            "★AVC AAC MP4★繁體中文(全修正合集+ass字幕)"
        )
        assert result == "哪裡有溫柔對待阿宅的辣妹！？ Otaku ni Yasashii Gal wa Inai"

    def test_guoman_prefix(self):
        result = extract_title(
            "[GM-Team][国漫][仙逆][Renegade Immortal][2023][146][AVC][GB][1080P]"
        )
        assert result == "仙逆 Renegade Immortal"

    def test_keeps_subtitle_prefix(self):
        result = extract_title("Some.简繁字幕组.2024.1080p.WEB-DL")
        assert result == "Some 简繁字幕组"

    def test_keeps_gb_prefix(self):
        result = extract_title("Some.GBStudio.2024.1080p.WEB-DL")
        assert result == "Some GBStudio"

    def test_keeps_ko_word(self):
        result = extract_title(
            "[jibaketa合成][代理商粵語]【我推的孩子】第三季 / "
            "Oshi no Ko 3rd Season - 11 END "
            "[粵日雙語+內封繁體中文字幕](WEB 1920x1080 AVC AACx2 SRT Ani-One CHT)"
        )
        assert result == "【我推的孩子】 / Oshi no Ko"

    def test_keeps_de_word(self):
        result = extract_title(
            "[黒ネズミたち] 躲在超市後門抽菸的兩人 / "
            "Super no Ura de Yani Suu Futari - 12 "
            "(ABEMA 1920x1080 AVC AAC MKV)"
        )
        assert result == "躲在超市後門抽菸的兩人 / Super no Ura de Yani Suu Futari"

    def test_chinese_period(self):
        result = extract_title(
            "[绿茶字幕组] 关于邻家的天使大人不知不觉把我惯成了废人这件事 "
            "第二期 / Otonari no Tenshi-sama ni Itsunomanika Dame Ningen ni "
            "Sareteita Ken S2 [12][WebRip][1080p][简日内嵌]"
        )
        assert (
            result == "关于邻家的天使大人不知不觉把我惯成了废人这件事 / "
            "Otonari no Tenshi sama ni Itsunomanika Dame Ningen ni Sareteita Ken"
        )

    def test_keeps_chinese_marker(self):
        result = extract_title(
            "[三明治摆烂组&LoliHouse] 落第贤者的学院无双～第二回转生，"
            "S等级作弊魔术师冒险记～ / Rakudai Kenja no Gakuin Musou - 01 "
            "[WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]"
        )
        assert (
            result == "落第贤者的学院无双～第二回转生，S等级作弊魔术师冒险记～ / "
            "Rakudai Kenja no Gakuin Musou"
        )

    def test_dash_episode(self):
        result = extract_title("Show - 02 - 1080p")
        assert result == "Show"

    def test_ep_end_subtitle(self):
        result = extract_title(
            "女神「异世界转生想成为什么」我「勇者的肋骨」 - EP12 END "
            "[简／繁] (1080p H.264 AAC DDP SRTx2)"
        )
        assert result == "女神「异世界转生想成为什么」我「勇者的肋骨」"

    def test_resolution_dimensions(self):
        result = extract_title("Reborn.as.a.Cat.1920x1080.HEVC")
        assert result == "Reborn as a Cat"

    def test_parenthesized_dlrip(self):
        result = extract_title("[milky] 真・燐月 (DLrip 1280x720 x264 AAC)")
        assert result == "真・燐月"

    def test_plain_movie_name(self):
        result = extract_title("Dune")
        assert result.lower() == "dune"

    def test_preserves_embedded_s_number(self):
        result = extract_title("MARS01")
        assert result == "MARS01"

    def test_empty_title_fallback(self):
        assert extract_title("[Group] 2023 1080p S01E01") == "[Group] 2023 1080p S01E01"
        assert extract_title("") == ""
