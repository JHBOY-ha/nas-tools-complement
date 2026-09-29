"""Deterministic filename scenarios with expectations fixed by release semantics.

The matrix covers rule parsing, not live TMDB identity accuracy. Animation release
names yield TV until provider genres or an explicit ANIME hint supply that type.
``recognition_matrix_counts`` exposes case counts for the standalone test runner.
"""

from collections import Counter
import unittest
from unittest.mock import patch

from app.media.meta import MetaInfo
from app.media.meta.metainfo import is_anime
from app.media.meta.special import download_block_reason
from app.utils.types import MediaType


def _case(case_id, title, name, media_type, year=None, seasons=(), episodes=(),
          mtype=None, subtitle=None, confirmation=None, audio_codec=None, resource_type=None):
    return {"id": case_id, "title": title, "name": name, "media_type": media_type,
            "year": year, "seasons": tuple(seasons), "episodes": tuple(episodes),
            "mtype": mtype, "subtitle": subtitle, "confirmation": confirmation,
            "audio_codec": audio_codec, "resource_type": resource_type}


def _resource_variants(stem):
    # Separators, media suffix case and technical metadata do not change identity.
    return (
        ("space_web", stem + " 1080p WEB-DL H264 AAC2.0.mkv"),
        ("dot_bluray", stem.replace(" ", ".") + ".2160p.BluRay.HEVC.DTS-HD.MA5.1.mp4"),
        ("underscore", stem.replace(" ", "_") + "_720p_WEBRip_x265_10bit_AAC.mkv"),
        ("brackets_lower_ext", stem + " [1080p][HEVC][FLAC].mkv"),
        ("brackets_upper_ext", stem + " [1080p][HEVC][FLAC].MKV"),
    )


MOVIE_CASES = []
for _label, _stem, _name, _year in (
        ("english", "The Matrix 1999", "The Matrix", "1999"),
        ("chinese", "流浪地球 2019", "流浪地球", "2019"),
        ("animation_film", "千与千寻 2001", "千与千寻", "2001"),
        ("word_digit", "Case39 2009", "Case39", "2009"),
        ("leading_number", "2001 A Space Odyssey 1968", "2001 A Space Odyssey", "1968"),
        ("numeric_title_year", "1917 2019", "1917", "2019"),
        ("season_word", "Season of the Witch 2011", "Season of the Witch", "2011"),
        ("season_year", "The Final Season 2007", "The Final Season", "2007"),
        ("episode_word", "Episode of Love 2015", "Episode of Love", "2015"),
        ("no_year", "The Matrix", "The Matrix", None)):
    for _variant, _title in _resource_variants(_stem):
        MOVIE_CASES.append(_case(_label + "/" + _variant, _title, _name,
                                 MediaType.MOVIE, year=_year))
MOVIE_CASES += [
    _case("parenthesized_year", "The Matrix (1999).mkv", "The Matrix",
          MediaType.MOVIE, year="1999"),
    _case("title_only", "The Matrix.mkv", "The Matrix", MediaType.MOVIE),
    _case("chinese_title_only", "流浪地球.mkv", "流浪地球", MediaType.MOVIE),
    _case("hyphenated_year", "Coco - 2017 1080p BluRay.mkv", "Coco",
          MediaType.MOVIE, year="2017"),
    _case("explicit_numeric_lower", "1917.mkv", "1917", MediaType.MOVIE,
          mtype=MediaType.MOVIE),
    _case("explicit_numeric_upper", "1917.MKV", "1917", MediaType.MOVIE,
          mtype=MediaType.MOVIE),
    _case("explicit_numeric_no_suffix", "1917", "1917", MediaType.MOVIE,
          mtype=MediaType.MOVIE),
    _case("explicit_movie_word_digit", "Case39.2009.1080p.mkv", "Case39",
          MediaType.MOVIE, year="2009", mtype=MediaType.MOVIE),
    _case("bracket_audio_source", "Thor Love and Thunder (2022) [1080p] [WEBRip] [5.1]",
          "Thor Love and Thunder", MediaType.MOVIE, year="2022",
          audio_codec="5.1", resource_type="WEBRip"),
]


TV_CASES = []
for _channel in ("2.0", "2.1", "5.1", "7.1"):
    TV_CASES.append(_case("bracket_audio/" + _channel,
                          "Show Name S01E01 [%s].mkv" % _channel,
                          "Show Name", MediaType.TV, seasons=(1,), episodes=(1,),
                          audio_codec=_channel))
TV_CASES.append(_case("codec_bracket_channel", "Show Name S01E01 AAC [5.1].mkv",
                      "Show Name", MediaType.TV, seasons=(1,), episodes=(1,),
                      audio_codec="AAC 5.1"))


for _label, _stem, _name, _year, _seasons, _episodes in (
        ("season_episode", "Breaking Bad S01E02", "Breaking Bad", None, (1,), (2,)),
        ("year_season_episode", "Breaking Bad 2008 S01E02", "Breaking Bad", "2008", (1,), (2,)),
        ("written_markers", "Game of Thrones Season 4 Episode 5", "Game of Thrones", None, (4,), (5,)),
        ("x_marker", "Doctor Who 2005 2x03", "Doctor Who", "2005", (2,), (3,)),
        ("episode_prefix", "Episode 05 - Breaking Bad", "Breaking Bad", None, (1,), (5,)),
        ("chinese_markers", "某剧 第三季 第五集", "某剧", None, (3,), (5,)),
        ("explicit_range", "Show Name S01E01-E03", "Show Name", None, (1,), (1, 2, 3)),
        ("episode_range", "Show Name E01-03", "Show Name", None, (1,), (1, 2, 3)),
        ("season_pack", "Show Name S02", "Show Name", None, (2,), ()),
        ("chinese_season_pack", "某剧 第二季 全十二集", "某剧", None, (2,), ())):
    for _variant, _title in _resource_variants(_stem):
        TV_CASES.append(_case(_label + "/" + _variant, _title, _name, MediaType.TV,
                              year=_year, seasons=_seasons, episodes=_episodes))
TV_CASES += [
    _case("unpadded", "Show Name S1E2.mkv", "Show Name", MediaType.TV,
          seasons=(1,), episodes=(2,)),
    _case("ep_marker", "Show Name S02EP03.mkv", "Show Name", MediaType.TV,
          seasons=(2,), episodes=(3,)),
    _case("joined_episodes", "Show Name S01E01E02.mkv", "Show Name", MediaType.TV,
          seasons=(1,), episodes=(1, 2)),
    _case("special_season_range", "Show Name S00E01-E03.mkv", "Show Name", MediaType.TV,
          seasons=(0,), episodes=(1, 2, 3)),
    _case("chinese_filename_total", "某剧 第2季 第03集 全12集.mkv", "某剧", MediaType.TV,
          seasons=(2,), episodes=(3,)),
    _case("subtitle_total", "Show Name S01E02.mkv", "Show Name", MediaType.TV,
          seasons=(1,), episodes=(2,), subtitle="全12集"),
    _case("dot_total", "Show Name.S01E02.12 集全.mkv", "Show Name", MediaType.TV,
          seasons=(1,), episodes=(2,)),
    _case("episode_only_lower", "01.mkv", "", MediaType.TV, seasons=(1,), episodes=(1,)),
    _case("episode_only_upper", "01.MKV", "", MediaType.TV, seasons=(1,), episodes=(1,)),
    _case("episode_only_mp4", "01.MP4", "", MediaType.TV, seasons=(1,), episodes=(1,)),
    _case("bare_resolution_lower", "Show Name.S01E01-720.mkv", "Show Name", MediaType.TV,
          seasons=(1,), episodes=(1,)),
    _case("bare_resolution_upper", "Show Name.S01E01-720.MKV", "Show Name", MediaType.TV,
          seasons=(1,), episodes=(1,)),
]


ANIME_CASES = []
for _label, _release_name, _name, _season in (
        ("leading_number", "91 Days", "91 Days", 1),
        ("numeric_title", "86", "86", 1),
        ("roman_season", "Youjo Senki II", "Youjo Senki II", 2),
        ("title_x", "Beyblade X", "Beyblade X", 1),
        ("chinese_title", "刀剑神域", "刀剑神域", 1)):
    for _variant, _title in (
            ("dash_episode", "[Group] %s - 01 [1080p][HEVC][AAC].mkv" % _release_name),
            ("bracket_episode", "[Group] %s [01][1080p][HEVC][AAC].mkv" % _release_name),
            ("uppercase_suffix", "[Group] %s - 01 [1080p][HEVC][FLAC].MKV" % _release_name)):
        # Roman II before a dash is explicit supported evidence; a bare title
        # suffix without that separator can also be part of the work's title.
        if _label == "roman_season" and _variant == "bracket_episode":
            continue
        ANIME_CASES.append(_case(_label + "/" + _variant, _title, _name,
                                 MediaType.TV, seasons=(_season,), episodes=(1,)))
        ANIME_CASES.append(_case(_label + "/" + _variant + "/explicit_anime", _title,
                                 _name, MediaType.ANIME, seasons=(_season,), episodes=(1,),
                                 mtype=MediaType.ANIME))
for _ending in ("12 END", "12END", "12 FINAL"):
    ANIME_CASES.append(_case("final/" + _ending,
                             "[DMG][BOCCHI_THE_ROCK!][%s][1080P][GB].mp4" % _ending,
                             "BOCCHI THE ROCK!", MediaType.TV, seasons=(1,), episodes=(12,)))
for _label, _block, _episode in (
        ("separate", "[12 END]", 12), ("compact", "[28END]", 28),
        ("parentheses", "(12 FINAL)", 12), ("cjk_brackets", "【12 FINAL】", 12),
        ("underscore", "[12_END]", 12), ("parentheses_underscore", "(12_FINAL)", 12),
        ("validated_upper_bound", "[1899 END]", 1899)):
    ANIME_CASES.append(_case("completion_block/" + _label,
                             "[Group] Bocchi the Rock! %s [1080p].mkv" % _block,
                             "Bocchi the Rock!", MediaType.TV,
                             seasons=(1,), episodes=(_episode,)))
ANIME_CASES += [
    _case("numeric_season_pack", "[Group] 86 S01 [1080p].mkv", "86", MediaType.ANIME,
          seasons=(1,), mtype=MediaType.ANIME),
    _case("season_episode", "[Group] Anime Name S02E03 [1080p][HEVC][AAC].mkv",
          "Anime Name", MediaType.ANIME, seasons=(2,), episodes=(3,), mtype=MediaType.ANIME),
    _case("release_revision", "[Group] Anime Name - 03v2 [1080p][HEVC][AAC].mkv",
          "Anime Name", MediaType.TV, seasons=(1,), episodes=(3,)),
    _case("english_numeric_word", "[Group] Case39 - 02 [1080p].mkv", "Case39",
          MediaType.TV, seasons=(1,), episodes=(2,)),
    _case("pack_total_single_file", "[Group] Anime Name - 02 [全12集][1080p].mkv",
          "Anime Name", MediaType.TV, seasons=(1,), episodes=(2,)),
    _case("tv_bracket_pack",
          "[xyx98]传颂之物/Utawarerumono/うたわれるもの[BDrip][1920x1080]"
          "[TV 01-26 Fin][hevc-yuv420p10 flac_ac3][ENG PGS]",
          "うたわれるもの", MediaType.TV, seasons=(1,), episodes=tuple(range(1, 27))),
]


UNCONFIRMED_CASES = [
    # Release labels do not prove a formal TMDB integer episode or S00 binding.
    _case("fractional_marker", "Anime Name E13.5.mkv", "Anime Name", MediaType.TV,
          seasons=(1,), confirmation="fractional_episode"),
    _case("fractional_season", "Anime Name S02E13.5.1080p.mkv", "Anime Name", MediaType.TV,
          seasons=(2,), confirmation="fractional_episode"),
    _case("fractional_range", "Anime Name E13.5-E14.5.mkv", "Anime Name", MediaType.TV,
          seasons=(1,), confirmation="fractional_episode"),
    _case("fractional_brackets", "[Group] Anime Name [13.5][1080p].mkv", "Anime Name", MediaType.TV,
          seasons=(1,), confirmation="fractional_episode"),
    _case("fractional_not_channel", "Anime Name [1.5][1080p].mkv", "Anime Name", MediaType.TV,
          seasons=(1,), confirmation="fractional_episode"),
    _case("ova", "[Group] Anime Name [OVA01][1080p].mkv", "Anime Name", None,
          confirmation="special_episode"),
    _case("oad", "[Group] Anime Name [OAD01][1080p].mkv", "Anime Name", None,
          confirmation="special_episode"),
    _case("special_range", "Anime Name [SPECIAL01-02].mkv", "Anime Name", None,
          confirmation="special_episode"),
]


ROUTING_CASES = [
    # Resource dimensions and years are not anime evidence, even inside brackets.
    {"id": "pixel_resolution", "title": "The Matrix 1999 [1080p][HEVC].mkv", "anime": False},
    {"id": "interlaced_resolution", "title": "The Matrix 1999 [1080i][HEVC].mkv", "anime": False},
    {"id": "dimension_resolution", "title": "The Matrix 1999 [1920x1080][HEVC].mkv", "anime": False},
    {"id": "cjk_resolution", "title": "流浪地球 2019【1080P】【HEVC】.mkv", "anime": False},
    {"id": "completion_year", "title": "[Group] Anime Name [2022 END] [1080p].mkv", "anime": False},
    {"id": "completion_resolution", "title": "[Group] Anime Name [1080p END] [1080p].mkv", "anime": False},
    {"id": "completion_partial_word", "title": "[Group] Anime Name [28ENDING] [1080p].mkv", "anime": False},
    {"id": "completion_underscore_partial", "title": "[Group] Anime Name [12_ENDING] [1080p].mkv", "anime": False},
    {"id": "completion_over_bound", "title": "[Group] Anime Name [1900 FINAL] [1080p].mkv", "anime": False},
    {"id": "numeric_episode", "title": "[Group] Anime Name [12][1080p].mkv", "anime": True},
    {"id": "roman_episode", "title": "[Group] Anime Name [III][1080p].mkv", "anime": True},
    {"id": "cjk_roman_episode", "title": "【Group】Anime Name【III】【1080p】.mkv", "anime": True},
    {"id": "revised_episode", "title": "[Group] Anime Name [01v2][1080p].mkv", "anime": True},
    {"id": "tv_range", "title": "[Group] Anime Name [TV 01-26 Fin][1080p].mkv", "anime": True},
    {"id": "tv_year_range", "title": "[Group] Anime Name [TV 2022-2024][1080p].mkv", "anime": False},
]


RECOGNITION_MATRIX = {"movie": MOVIE_CASES, "tv": TV_CASES,
                      "anime": ANIME_CASES, "unconfirmed": UNCONFIRMED_CASES,
                      "routing": ROUTING_CASES}


def recognition_matrix_counts():
    """Return explicit semantic-case counts, including equivalent file variants."""
    return {group: len(cases) for group, cases in RECOGNITION_MATRIX.items()}


class MetaRecognitionMatrixTest(unittest.TestCase):
    def setUp(self):
        # User configuration cannot silently alter these deterministic expectations.
        words_patch = patch("app.media.meta.metainfo.WordsHelper")
        self.addCleanup(words_patch.stop)
        words_patch.start().return_value.process.side_effect = lambda title: (title, [], {})
        config_patch = patch("app.media.meta.metainfo.Config")
        self.addCleanup(config_patch.stop)
        config_patch.start().return_value.get_config.return_value = {"extras": {"enabled": False}}

    def check_group(self, group):
        for case in RECOGNITION_MATRIX[group]:
            with self.subTest(group=group, case=case["id"], title=case["title"]):
                meta = MetaInfo(case["title"], subtitle=case["subtitle"],
                                mtype=case["mtype"], use_llm=False)
                self.assertEqual(case["name"].casefold(), meta.get_name().casefold())
                self.assertEqual(case["year"], meta.year)
                self.assertEqual(case["episodes"], tuple(meta.get_episode_list()))
                if case["media_type"] is not None:
                    self.assertEqual(case["media_type"], meta.type)
                    self.assertEqual(case["seasons"], tuple(meta.get_season_list()))
                if case["audio_codec"] is not None:
                    self.assertEqual(case["audio_codec"], meta.audio_encode)
                if case["resource_type"] is not None:
                    self.assertEqual(case["resource_type"], meta.resource_type)
                if case["confirmation"]:
                    evidence = meta.note.get(case["confirmation"])
                    self.assertIsNotNone(evidence)
                    self.assertEqual("unconfirmed", evidence["status"])
                    self.assertTrue(download_block_reason(meta))
                    self.assertNotIn("episode_mapping", meta.note)
                    if case["confirmation"] == "special_episode":
                        self.assertIsNone(meta.begin_season)
                else:
                    self.assertIsNone(download_block_reason(meta))

    def test_movie_names(self):
        self.check_group("movie")

    def test_tv_names(self):
        self.check_group("tv")

    def test_anime_names(self):
        self.check_group("anime")

    def test_missing_evidence_stays_unconfirmed(self):
        self.check_group("unconfirmed")

    def test_anime_routing_requires_episode_evidence(self):
        for case in ROUTING_CASES:
            with self.subTest(case=case["id"], title=case["title"]):
                self.assertEqual(case["anime"], is_anime(case["title"]))

    def test_case_identifiers_are_unique(self):
        for group, cases in RECOGNITION_MATRIX.items():
            counts = Counter(case["id"] for case in cases)
            self.assertEqual([], [name for name, count in counts.items() if count != 1], group)


if __name__ == "__main__":
    print("Recognition matrix cases:", recognition_matrix_counts())
    unittest.main()
