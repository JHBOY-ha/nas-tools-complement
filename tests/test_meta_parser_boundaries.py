"""Offline regressions for title boundaries and episode evidence precedence."""

import unittest
from unittest.mock import MagicMock, patch

from app.downloader.downloader import Downloader
from app.media.media import Media
from app.media.meta import MetaInfo
from app.media.meta.metaanime import MetaAnime
from app.utils.types import DownloaderType, MediaType


class MetaParserBoundaryTest(unittest.TestCase):
    def test_standalone_bare_episode_ranges(self):
        # Both ordinary TV routing and explicit anime routing must retain the whole pack.
        for title in ("[Group] Anime Name 01-12 [1080p].mkv",
                      "Show Name - 01-12 [1080p].mkv",
                      "Show Name 01-12.mkv",
                      "Show Name - 01-12 [1080p].MKV"):
            for mtype in (None, MediaType.ANIME):
                with self.subTest(title=title, mtype=mtype):
                    meta = MetaInfo(title, mtype=mtype, use_llm=False)
                    self.assertEqual(list(range(1, 13)), meta.get_episode_list())
                    self.assertEqual(12, meta.total_episodes)

    def test_spaced_anime_season_separator_keeps_its_existing_meaning(self):
        # "Title 2 - 05" already means season 2, episode 5, not a range.
        meta = MetaInfo("Show Name 02 - 12 [1080p].mkv", use_llm=False)
        self.assertEqual([2], meta.get_season_list())
        self.assertEqual([12], meta.get_episode_list())

    def test_bare_range_does_not_expand_unrelated_numbers(self):
        for title in ("Show Name 01 12 [1080p].mkv",
                      "Show Name 01-720 [1080p].mkv",
                      "Show Name 01-1080 [1080p].mkv",
                      "Show Name 2001-2012 [1080p].mkv",
                      "Show Name 01-12bit [1080p].mkv",
                      "Show Name 01-12.5 [1080p].mkv"):
            with self.subTest(title=title):
                self.assertLessEqual(len(MetaInfo(title, use_llm=False).get_episode_list()), 2)

    def setUp(self):
        # These cases exercise built-in rules without user words or network-backed LLMs.
        word_patch = patch("app.media.meta.metainfo.WordsHelper")
        words = word_patch.start()
        words.return_value.process.side_effect = lambda title: (title, [], {})
        self.addCleanup(word_patch.stop)
        llm_patch = patch("app.media.meta.metainfo.LLMMetaParser")
        llm = llm_patch.start()
        self.addCleanup(llm_patch.stop)
        llm.return_value.merge_into.side_effect = lambda **kwargs: kwargs["meta_info"]

    def test_pack_descriptions_preserve_single_episode_and_range(self):
        for title, subtitle, episodes in (
                ("Show.S01E02.mkv", "全12集", [2]),
                ("Show.S01E02.全12集.mkv", None, [2]),
                ("Show S01E02 12 集全.mkv", None, [2]),
                ("Show.S01E01-E03.mkv", "全12集", [1, 2, 3]),
                ("某剧 第02集 全12集.mkv", None, [2]),
                ("[Group] Anime Name - 02 [全12集][1080p].mkv", None, [2]),
                ("[Group] Anime Name [02][12集全][1080p].mkv", None, [2])):
            with self.subTest(title=title, subtitle=subtitle):
                meta = MetaInfo(title, subtitle=subtitle, use_llm=False)
                self.assertEqual(episodes, meta.get_episode_list())
                self.assertEqual(len(episodes), meta.total_episodes)
                self.assertEqual(MediaType.TV, meta.type)

    def test_pack_totals_do_not_manufacture_episode_numbers(self):
        # Both token parsing and Chinese subtitle parsing must exclude the count.
        for count in ("全12集", "12集全", "12 集全", "全十二集"):
            for title in ("Show S01 %s 1080p.mkv" % count,
                          "[Group] Anime Name [%s][1080p].mkv" % count):
                with self.subTest(title=title):
                    meta = MetaInfo(title, use_llm=False)
                    self.assertEqual([], meta.get_episode_list())
                    self.assertEqual(0, meta.total_episodes)
                    self.assertEqual(MediaType.TV, meta.type)
            anime = MetaAnime("[Group] Anime Name [%s][1080p].mkv" % count,
                              fileflag=True)
            self.assertEqual([], anime.get_episode_list())

    def test_pack_description_does_not_disable_requested_download_file(self):
        downloader = object.__new__(type(Downloader()))
        client = MagicMock()
        client.get_files.return_value = [
            {"name": "Show.S01E02.全12集.mkv", "index": 0},
            {"name": "Show.S01E03.mkv", "index": 1},
            {"name": "Show.S01E04.mkv", "index": 2},
        ]
        with patch.object(downloader, "_Downloader__get_client", return_value=client):
            selected = downloader.set_files_status("torrent", [2, 3], DownloaderType.QB)
        self.assertEqual({2, 3}, set(selected))
        client.set_files.assert_called_once_with(torrent_hash="torrent", file_ids=[2], priority=0)

    def test_latin_title_digits_are_not_season_or_episode_markers(self):
        for name, year in (("Case39", "2009"), ("Cars24", "2020"), ("Sense80", "2021")):
            for mtype in (None, MediaType.MOVIE):
                with self.subTest(name=name, mtype=mtype):
                    meta = MetaInfo("%s.%s.1080p.mkv" % (name, year),
                                    mtype=mtype, use_llm=False)
                    self.assertEqual(name, meta.get_name())
                    self.assertEqual(year, meta.year)
                    self.assertEqual(MediaType.MOVIE, meta.type)
                    self.assertIsNone(meta.begin_season)
                    self.assertIsNone(meta.begin_episode)
        # The same boundary must be respected when anitopy supplies the title.
        anime = MetaInfo("[Group] Case39 - 02 [1080p].mkv", use_llm=False)
        self.assertEqual("Case39", anime.get_name())
        self.assertEqual([2], anime.get_episode_list())

    def test_movie_title_with_digits_reaches_media_search(self):
        media = Media.__new__(Media)
        media.tmdb = object()
        with patch.object(media, "get_cache_info", return_value={}), \
                patch.object(media, "_Media__search_media_with_name", return_value=None) as search, \
                patch.object(media, "_Media__extract_cn_fallback_name", return_value=None):
            media.get_media_info("Case39.2009.1080p.mkv", mtype=MediaType.MOVIE)
        self.assertEqual("Case39", search.call_args.kwargs["query_name"])
        self.assertEqual("2009", search.call_args.kwargs["meta_info"].year)
        self.assertEqual(MediaType.MOVIE, search.call_args.kwargs["meta_info"].type)

    def test_marker_boundaries_keep_existing_numbering_formats(self):
        for marker, season, episodes in (
                ("E1", None, [1]), ("EP01", None, [1]),
                ("S1E1", 1, [1]), ("S01E02", 1, [2]),
                ("S01EP02", 1, [2]), ("S01E01E02", 1, [1, 2]),
                ("S01E01-E03", 1, [1, 2, 3]), ("E01-03", None, [1, 2, 3]),
                ("S01E01v2", 1, [1])):
            with self.subTest(marker=marker):
                meta = MetaInfo("Case39.%s.mkv" % marker, use_llm=False)
                self.assertEqual("Case39", meta.get_name())
                self.assertEqual(season, meta.begin_season)
                self.assertEqual(episodes, meta.get_episode_list())
        chinese = MetaInfo("中文节目S01E02.mkv", use_llm=False)
        self.assertEqual("中文节目", chinese.get_name())
        self.assertEqual((1, 2), (chinese.begin_season, chinese.begin_episode))

    def test_written_episode_prefix_preserves_title_at_each_width(self):
        for number in ("5", "05", "12", "100", "1000"):
            for title in ("Episode %s - Show Name 1080p.mkv" % number,
                          "Show Name Episode %s 1080p.mkv" % number):
                with self.subTest(title=title):
                    meta = MetaInfo(title, use_llm=False)
                    self.assertEqual("Show Name", meta.get_name())
                    self.assertEqual([int(number)], meta.get_episode_list())
                    self.assertEqual(MediaType.TV, meta.type)
        movie = MetaInfo("Episode of Love 2015.mkv", use_llm=False)
        self.assertEqual("Episode Of Love", movie.get_name())
        self.assertEqual(MediaType.MOVIE, movie.type)

    def test_anime_numeric_titles_keep_name_and_separate_episode(self):
        for title, name in (
                ("[Group] 91 Days - 01 [1080p].mkv", "91 Days"),
                ("91 Days - 01 [1080p].mkv", "91 Days"),
                ("[Group] 86 - 01 [1080p].mkv", "86"),
                ("[Group] 86 [01][1080p].mkv", "86")):
            with self.subTest(title=title):
                meta = MetaInfo(title, use_llm=False)
                self.assertEqual(name, meta.get_name())
                self.assertEqual([1], meta.get_episode_list())
                self.assertEqual(MediaType.TV, meta.type)
        season_pack = MetaInfo("[Group] 86 S01 [1080p].mkv",
                               mtype=MediaType.ANIME, use_llm=False)
        self.assertEqual("86", season_pack.get_name())
        self.assertEqual([1], season_pack.get_season_list())
        self.assertEqual([], season_pack.get_episode_list())

    def test_numeric_title_support_preserves_bare_episode_policy(self):
        # Title preservation must not turn a numeric episode file into a movie.
        for title in ("0001.mkv", "[Group][01][1080p].mkv"):
            meta = MetaInfo(title, use_llm=False)
            self.assertEqual([1], meta.get_episode_list())
            self.assertEqual(MediaType.TV, meta.type)
        numeric_movie = MetaInfo("1917.mkv", mtype=MediaType.MOVIE, use_llm=False)
        self.assertEqual("1917", numeric_movie.get_name())
        self.assertEqual([], numeric_movie.get_episode_list())


if __name__ == "__main__":
    unittest.main()
