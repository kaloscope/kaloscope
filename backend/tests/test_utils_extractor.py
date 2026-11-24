"""Unit tests for the media keyword extractor utility."""

import pytest

from app.utils.extractor import (
    extract_episode,
    extract_season,
    extract_title,
    extract_year,
)

YEARS = {
    "1990s": ("Schindler.s.List.1993", 1993),
    "2000s": ("Gladiator.2000.BluRay", 2000),
    "parentheses": ("The Dark Knight (2008)", 2008),
    "brackets": ("Parasite [2019] BluRay", 2019),
    "adjacent_bracket": ("[GM-Team][国漫][仙逆][Renegade Immortal][2023][146]", 2023),
    "underscore": ("流浪地球2_2023_2160p_WEB-DL", 2023),
    "attached": ("Doraemon1979", None),
    "missing": ("Some.Movie.Title.BluRay", None),
    "resolution": ("Movie.1080p", None),
    "dimensions": ("Movie.1920x1080.HEVC", None),
}

SEASONS = {
    "uppercase_s": ("Breaking.Bad.S03E07.1080p", 3),
    "lowercase_s": ("game.of.thrones.s08e06", 8),
    "season_word": ("The Crown Season 4 Complete", 4),
    "season_word_dotted": ("The.Crown.Season.4.Complete", 4),
    "ordinal_word": ("Re Zero kara Hajimeru Isekai Seikatsu 4th Season", 4),
    "english_word": ("Diamond no Ace: Act II Second Season - 12", 2),
    "one_digit": ("[SubGroup] Naruto S1E01", 1),
    "padded": ("Doctor.Who.S10E01.HDR", 10),
    "chinese_marker": ("庆余年 第2季 全集", 2),
    "chinese_number_1": ("某某动漫.第一季.全集", 1),
    "chinese_period_2": ("某某动漫.第二期.全集", 2),
    "chinese_number_21": ("[某某动漫] [第二十一季]", 21),
    "missing": ("Inception.2010.1080p", None),
    "embedded_s": ("MARS01", None),
}

EPISODES = {
    "uppercase_e": ("Breaking.Bad.S03E07.1080p", 7),
    "lowercase_e": ("game.of.thrones.s08e06", 6),
    "ep": ("[HorribleSubs] One Piece - EP950 [720p]", 950),
    "dash": ("[SubGroup] Anime Title - 01 [1080p]", 1),
    "dash_at_end": ("Show - 01", 1),
    "dash_before_dash": ("Show - 02 - 1080p", 2),
    "dash_before_parens": ("Show - 03 (WEB 1080p)", 3),
    "dash_before_end": ("Jujutsu Kaisen S3 - 12 END [WEB]", 12),
    "bracket": ("[SubGroup] Anime S2 [06][1080p].mkv", 6),
    "bracket_total": ("[Show][10 - 总第76][WEB-DL]", 10),
    "traditional_total": ("[Show][09 - 總第75][WEB-DL]", 9),
    "version_2": ("Show [16v2][WebRip]", 16),
    "version_4": ("Show [16v4][WebRip]", 16),
    "version_9": ("Show [16v9][WebRip]", 16),
    "version_10": ("Show [16v10][WebRip]", None),
    "chinese_ji": ("庆余年 第3集", 3),
    "chinese_hua": ("海贼王 第1000話", 1000),
    "chinese_number_1": ("某某动漫 第一季 第一集", 1),
    "chinese_number_21": ("某某动漫.第二季.第二十一集", 21),
    "chinese_number_321": ("【某某动漫】【第三季】【第三百二十一回】", 321),
    "chinese_marker_boundary": (
        "落第贤者的学院无双～第二回转生，S等级作弊魔术师冒险记～ - 01",
        1,
    ),
    "zero": ("Show.S01E00.Special", 0),
    "large": ("Series.S01E1000.mkv", 1000),
    "missing": ("Inception.2010.1080p", None),
    "bracketed_year": ("Movie.Title.[2023]", None),
}

TITLES = {
    # movies
    "movie_basic": ("Inception.2010.1080p.BluRay.x264.mkv", "Inception"),
    "movie_parenthesized_year": ("The Dark Knight (2008) 1080p.mkv", "The Dark Knight"),
    "movie_chinese": ("流浪地球2.2023.2160p.WEB-DL.mkv", "流浪地球2"),
    "movie_bracketed_year": ("Parasite [2019] BluRay 1080p.mp4", "Parasite"),
    "movie_no_extension": ("Gladiator.2000.BluRay.1080p", "Gladiator"),
    "plain_movie_name": ("Dune", "Dune"),
    # tv shows
    "tv_subgroup_prefix": (
        "[HorribleSubs] Attack on Titan - 25 [1080p].mkv",
        "Attack on Titan",
    ),
    "tv_sxxexx": ("Breaking.Bad.S03E07.720p.BluRay.mkv", "Breaking Bad"),
    "tv_season_word": ("The Crown Season 4 Complete 1080p WEB-DL", "The Crown"),
    "tv_ordinal_season": ("Show Title 4th Season - 01 [1080p]", "Show Title"),
    "tv_chinese_episode": ("庆余年 第2季 第03集 WEB-DL 1080P H264", "庆余年"),
    "tv_all_brackets": (
        "[Nekomoe kissaten][Sousou no Frieren][01][1080p][JPSC]",
        "Sousou no Frieren",
    ),
    "tv_bracket_episode_range": (
        "[DMG&SumiSora][Tongari_Boushi_no_Atelier][12-13][1080P][GB][MP4]",
        "Tongari Boushi no Atelier",
    ),
    "tv_bracket_total_episode": (
        "[晚街与灯][Re：从零开始的异世界生活 第四季 / "
        "Re:Zero kara Hajimeru Isekai Seikatsu 4th Season]"
        "[10 - 总第76][WEB-DL Remux][1080P_AVC_AAC][简繁日内封PGS]",
        "Re：从零开始的异世界生活 / Re:Zero kara Hajimeru Isekai Seikatsu",
    ),
    # release tags and separators
    "multiple_separators": ("The.Big.Bang.Theory.S12E24.1080p", "The Big Bang Theory"),
    "codec_tags": (
        "Oppenheimer.2023.1080p.WEB-DL.H265.AAC-SubGroup.mkv",
        "Oppenheimer",
    ),
    "hdr_tags": (
        "Dune.Part.Two.2024.2160p.UHD.BluRay.HDR10.DTS-HD.MA",
        "Dune Part Two",
    ),
    "keeps_tag_prefix": (
        "Some.Webster.Crisis.BDrive.2024.1080p.WEB-DL",
        "Some Webster Crisis BDrive",
    ),
    "iqiyi_source": (
        "花样少年少女 第二季 - 02 [IQIYI WebRip 2160p NVENC AAC]",
        "花样少年少女",
    ),
    "viutv_source": (
        "Re：從零開始的異世界生活 第四季 / "
        "Re：Zero kara Hajimeru Isekai Seikatsu 4th Season - 11 "
        "[ViuTV][WEB-DL][CHT][1080p][AVC AAC]",
        "Re：從零開始的異世界生活 / Re：Zero kara Hajimeru Isekai Seikatsu",
    ),
    "cr_source": ("LV999的村民 - 02 [CR WebRip AI2160p NVENC AAC]", "LV999的村民"),
    "abema_source": (
        "[黒ネズミたち] 左撇子艾倫 / Hidarikiki no Eren - 12 "
        "(ABEMA 1920x1080 AVC AAC MKV)",
        "左撇子艾倫 / Hidarikiki no Eren",
    ),
    "baha_source": (
        "[黒ネズミたち] 黑貓與魔女的教室 / Kuroneko to Majo no Kyoushitsu "
        "- 12 (Baha 1920x1080 AVC AAC MP4)",
        "黑貓與魔女的教室 / Kuroneko to Majo no Kyoushitsu",
    ),
    "b_global_source": (
        "[黒ネズミたち] 海贼王 / One Piece - 1168 (B-Global 3840x2160 HEVC AAC MKV)",
        "海贼王 / One Piece",
    ),
    "b_global_donghua_source": (
        "为喵人生 / Reborn as a Cat - 36 (B-Global Donghua 1920x1080 HEVC AAC MKV)",
        "为喵人生 / Reborn as a Cat",
    ),
    "chinese_subtitle": (
        "LV999的村民 / Lv999 no Murabito - 01 - [繁日内嵌][AVC 8bit 1080P]",
        "LV999的村民 / Lv999 no Murabito",
    ),
    "pre_air_tag": (
        "[Prejudice-Studio] 与你相恋至生命尽头 Kimi ga Shinu made Koi wo Shitai "
        "- 01 [Pre-Air][WebRip 1080P AVC 8bit AAC MKV][简繁日内封][V2]",
        "与你相恋至生命尽头 Kimi ga Shinu made Koi wo Shitai",
    ),
    "language_prefix": (
        "[jibaketa合成&音頻壓制][代理商粵語]咒術迴戰 第三季 / "
        "Jujutsu Kaisen S3 - 12 END [粵日雙語+內封繁體中文字幕]"
        "(WEB 1920x1080 AVC AACx2 SRT Ani-One CHT)",
        "咒術迴戰 / Jujutsu Kaisen",
    ),
    "broadcast_marker": (
        "【喵萌奶茶屋】★04月新番★"
        "[杖與劍的魔劍譚 / Tsue to Tsurugi no Wistoria]"
        "[24][1080p][繁日雙語]",
        "杖與劍的魔劍譚 / Tsue to Tsurugi no Wistoria",
    ),
    "split_brackets": (
        "【TSDM字幕组】[淡岛百景][Awajima Hyakkei][11]"
        "[HEVC-10bit 1080p AAC][MKV][简繁日内封字幕]淡島百景",
        "淡岛百景 Awajima Hyakkei",
    ),
    "episode_title_date": (
        "[丸子家族][櫻桃小丸子第二期(Chibi Maruko-chan II)][1536]"
        "小丸子喜歡媽媽的笑容&中野先生是晴天男?"
        "[2026.06.28][BIG5][1080P][MP4]",
        "櫻桃小丸子 (Chibi Maruko chan II)",
    ),
    "keeps_attached_year": (
        "[哆啦字幕組][哆啦A夢大山版 Doraemon1979][1263][1080P][HDTV][繁日雙語][MP4]",
        "哆啦A夢大山版 Doraemon1979",
    ),
    "versioned_episode": (
        "[绿茶字幕组] 欢迎来到实力至上主义的教室 第四季 / "
        "Youkoso Jitsuryoku Shijou Shugi no Kyoushitsu e S4 "
        "[16v2][WebRip][1080p][简日内嵌]",
        "欢迎来到实力至上主义的教室 / Youkoso Jitsuryoku Shijou Shugi no Kyoushitsu e",
    ),
    "gb_subtitle": ("[Group][Anime Title][10][GB][1080P][MP4]", "Anime Title"),
    "big5_subtitle": ("[Group][Anime Title][10][BIG5][1080P][MP4]", "Anime Title"),
    # episode ranges and collections
    "collection_range": (
        "Raise wa Tanin ga Ii S01 | 01-12 [简繁字幕] BDrip 1080p",
        "Raise wa Tanin ga Ii",
    ),
    "collection_range_specials": (
        "[7³ACG] 龙与虎/Toradora! S01 | 01-25+SPx6 "
        "[简繁字幕] BDrip 1080p x265 OPUS 2.0",
        "龙与虎/Toradora!",
    ),
    "chinese_range": (
        "[Alice Raw][アニメ] 一疊間漫畫咖啡廳日常 "
        "一畳間まんきつ暮らし！ 第01-11話 (3840x2160 x265 AAC)",
        "一疊間漫畫咖啡廳日常 一畳間まんきつ暮らし！",
    ),
    "bracketed_chinese_range": (
        "[LoliHouse] 主播女孩重度依赖 / NEEDY GIRL OVERDOSE "
        "[01-13话][WebRip 1080p HEVC-10bit AAC][简繁内封字幕][Fin]",
        "主播女孩重度依赖 / NEEDY GIRL OVERDOSE",
    ),
    "bracketed_range": (
        "[LoliHouse] 最强的职业不是勇者也不是贤者好像是鉴定士(暂定)的样子？ / "
        "Kanteishi (Kari) [01-12 合集][WebRip 1080p HEVC-10bit AAC]"
        "[无中字][Fin]",
        "最强的职业不是勇者也不是贤者好像是鉴定士(暂定)的样子？ / Kanteishi (Kari)",
    ),
    "wave_range": (
        "[晚街与灯][命运-奇异赝品_Fate strange Fake][合集][00~13]"
        "[BDRip][1080P_HEVC-10bit_FLAC][简繁日双语外挂PGS]",
        "命运 奇异赝品 Fate strange Fake",
    ),
    "wave_range_total": (
        "[晚街与灯][Re：从零开始的异世界生活 第四季丧失篇合集 / "
        "Re:Zero kara Hajimeru Isekai Seikatsu 4th - 01~11]"
        "[总第67~77][WEB-DL Remux][1080P_AVC_AAC][简繁日内封PGS]",
        "Re：从零开始的异世界生活 丧失篇合集 / Re:Zero kara Hajimeru Isekai Seikatsu",
    ),
    "star_range": (
        "六四位元字幕組★哪裡有溫柔對待阿宅的辣妹！？ "
        "Otaku ni Yasashii Gal wa Inai★01~12(完)★1920x1080"
        "★AVC AAC MP4★繁體中文(全修正合集+ass字幕)",
        "哪裡有溫柔對待阿宅的辣妹！？ Otaku ni Yasashii Gal wa Inai",
    ),
    # words that resemble release tags
    "guoman_prefix": (
        "[GM-Team][国漫][仙逆][Renegade Immortal][2023][146][AVC][GB][1080P]",
        "仙逆 Renegade Immortal",
    ),
    "keeps_subtitle_prefix": ("Some.简繁字幕组.2024.1080p.WEB-DL", "Some 简繁字幕组"),
    "keeps_gb_prefix": ("Some.GBStudio.2024.1080p.WEB-DL", "Some GBStudio"),
    "keeps_ko_word": (
        "[jibaketa合成][代理商粵語]【我推的孩子】第三季 / "
        "Oshi no Ko 3rd Season - 11 END "
        "[粵日雙語+內封繁體中文字幕](WEB 1920x1080 AVC AACx2 SRT Ani-One CHT)",
        "【我推的孩子】 / Oshi no Ko",
    ),
    "keeps_de_word": (
        "[黒ネズミたち] 躲在超市後門抽菸的兩人 / "
        "Super no Ura de Yani Suu Futari - 12 "
        "(ABEMA 1920x1080 AVC AAC MKV)",
        "躲在超市後門抽菸的兩人 / Super no Ura de Yani Suu Futari",
    ),
    "chinese_period": (
        "[绿茶字幕组] 关于邻家的天使大人不知不觉把我惯成了废人这件事 "
        "第二期 / Otonari no Tenshi-sama ni Itsunomanika Dame Ningen ni "
        "Sareteita Ken S2 [12][WebRip][1080p][简日内嵌]",
        "关于邻家的天使大人不知不觉把我惯成了废人这件事 / "
        "Otonari no Tenshi sama ni Itsunomanika Dame Ningen ni Sareteita Ken",
    ),
    "keeps_chinese_marker": (
        "[三明治摆烂组&LoliHouse] 落第贤者的学院无双～第二回转生，"
        "S等级作弊魔术师冒险记～ / Rakudai Kenja no Gakuin Musou - 01 "
        "[WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]",
        "落第贤者的学院无双～第二回转生，S等级作弊魔术师冒险记～ / "
        "Rakudai Kenja no Gakuin Musou",
    ),
    "dash_episode": ("Show - 02 - 1080p", "Show"),
    "ep_end_subtitle": (
        "女神「异世界转生想成为什么」我「勇者的肋骨」 - EP12 END "
        "[简／繁] (1080p H.264 AAC DDP SRTx2)",
        "女神「异世界转生想成为什么」我「勇者的肋骨」",
    ),
    "resolution_dimensions": ("Reborn.as.a.Cat.1920x1080.HEVC", "Reborn as a Cat"),
    "parenthesized_dlrip": ("[milky] 真・燐月 (DLrip 1280x720 x264 AAC)", "真・燐月"),
    "preserves_embedded_s_number": ("MARS01", "MARS01"),
    # nothing left after stripping
    "empty_fallback": ("[Group] 2023 1080p S01E01", "[Group] 2023 1080p S01E01"),
    "empty": ("", ""),
}


@pytest.mark.parametrize(("name", "expected"), YEARS.values(), ids=YEARS)
def test_year(name, expected):
    assert extract_year(name) == expected


@pytest.mark.parametrize(("name", "expected"), SEASONS.values(), ids=SEASONS)
def test_season(name, expected):
    assert extract_season(name) == expected


@pytest.mark.parametrize(("name", "expected"), EPISODES.values(), ids=EPISODES)
def test_episode(name, expected):
    assert extract_episode(name) == expected


@pytest.mark.parametrize(("name", "expected"), TITLES.values(), ids=TITLES)
def test_title(name, expected):
    assert extract_title(name) == expected


def test_multi_prefix():
    title = extract_title("[SubGroup][Studio] Some Anime S01E01 [BD 1080p HEVC]")

    assert "some anime" in title.lower()
