"""Regression cases for file-level media recognition and transfer guards."""

import os
import re
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app.filetransfer import FileTransfer
from app.media.meta import MetaInfo
from app.media.meta._base import MetaBase
from app.media.media import Media
from app.utils.episode_format import EpisodeFormat
from app.utils.types import MediaType, RmtMode, SyncType


class ExtraRecognitionTest(unittest.TestCase):
    def test_extra_tags_do_not_block_parser_or_llm(self):
        # Extras 默认关闭时仍正常识别；SP 改由特殊集证据确认测试覆盖。
        for tag in ("NCOP", "NCOP&ED", "ED01", "PV01"):
            with self.subTest(tag=tag), \
                    patch("app.media.meta.metainfo.LLMMetaParser") as llm:
                llm.return_value.merge_into.side_effect = lambda **kwargs: kwargs["meta_info"]
                meta = MetaInfo("Show S01E01 [%s].mkv" % tag)
                self.assertIsNone(meta.skip_reason)
                self.assertEqual(1, meta.begin_episode)
                llm.return_value.merge_into.assert_called_once()

    def test_extra_tags_reach_direct_and_file_recognition(self):
        media = Media.__new__(Media)
        media.tmdb = object()
        # 在解析入口停止模拟查询，验证两个调用入口都不再按标签提前返回。
        marker = RuntimeError("parser reached")
        with patch("app.media.media.MetaInfo", side_effect=marker) as parser:
            with self.assertRaisesRegex(RuntimeError, "parser reached"):
                media.get_media_info("Show [NCOP].mkv")
            parser.assert_called_once()
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "Show [SP01].mkv")
            open(path, "wb").close()
            with patch("app.media.media.MetaInfo", side_effect=marker) as parser, \
                    patch("app.media.media.PathUtils.get_bluray_dir", return_value=None):
                media.get_media_info_on_files([path])
            parser.assert_called_once_with("Show [SP01].mkv", use_llm=True)

    def test_transfer_ignore_is_configurable_and_filename_only(self):
        transfer = FileTransfer.__new__(FileTransfer)
        transfer._ignored_paths = ""
        ignored = ["/library/Show [%s].mkv" % tag
                   for tag in ("NCOP", "NCOP&ED", "ED01", "PV01", "SP01")]
        retained = ["/library/RED.mkv", "/library/Show-SP.mkv",
                    "/library/Show [01-12+SP]/Show S01E01.mkv"]
        # 未配置时不隐式过滤；规则仅作为用户可选配置出现在测试和文档中。
        transfer._ignored_files = ""
        self.assertEqual(ignored + retained, transfer.check_ignore(ignored + retained)[0])
        transfer._ignored_files = re.compile(
            r"(?i:[\[【]\s*(?:NCOP|NCED|ED|PV|SP)\d*"
            r"(?:\s*[&+＋]\s*(?:NCOP|NCED|ED|PV|SP)\d*)*\s*[\]】])")
        self.assertEqual(retained, transfer.check_ignore(ignored + retained)[0])

    def test_mixed_collection_directory_does_not_skip_main_episode(self):
        with tempfile.TemporaryDirectory() as root:
            folder = os.path.join(root, "Show [01-12+SP]")
            os.mkdir(folder)
            path = os.path.join(folder, "Show S01E01.mkv")
            open(path, "wb").close()
            media = Media.__new__(Media)
            media.tmdb = object()
            info = {"id": 42, "name": "Show", "media_type": MediaType.TV,
                    "seasons": [{"season_number": 1}]}
            with patch.object(media, "save_rename_cache"):
                parsed = media.get_media_info_on_files(
                    [path], tmdb_info=info, media_type=MediaType.TV)[path]
            self.assertIsNone(parsed.skip_reason)
            self.assertEqual(1, parsed.begin_episode)


class NumericMovieTest(unittest.TestCase):
    def test_movie_slug_uses_numeric_api_id(self):
        media = Media.__new__(Media)
        media.tmdb = MagicMock()
        info = {"id": 530915, "title": "1917", "release_date": "2019-12-25", "genres": []}
        with patch.object(media, "_Media__get_tmdb_movie_detail", return_value=info) as detail:
            result = media.get_tmdb_info(MediaType.MOVIE, "530915-1917")
        detail.assert_called_once_with("530915", None)
        self.assertEqual(530915, result["id"])

    def test_numeric_movie_requires_explicit_identity(self):
        for title in ("1917", "1917.mkv"):
            meta = MetaInfo(title, mtype=MediaType.MOVIE, use_llm=False)
            self.assertEqual("1917", meta.get_name())
            self.assertIsNone(meta.begin_episode)
        # 未绑定电影时仍保留已有纯数字剧集行为。
        self.assertEqual(1, MetaInfo("0001.mkv", use_llm=False).begin_episode)

    def test_bound_numeric_movie_attaches_details(self):
        media = Media.__new__(Media)
        media.tmdb = object()
        info = {"id": 530915, "title": "1917", "release_date": "2019-12-25",
                "media_type": MediaType.MOVIE}
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "1917.mkv")
            open(path, "wb").close()
            with patch.object(media, "save_rename_cache"):
                meta = media.get_media_info_on_files([path], tmdb_info=info)[path]
            self.assertEqual(530915, meta.tmdb_id)
            self.assertEqual(MediaType.MOVIE, meta.type)
            self.assertEqual("2019", meta.year)
            self.assertIsNone(meta.begin_episode)


class RoutingTest(unittest.TestCase):
    def test_movie_year_after_dash_and_anime_episode(self):
        for name, year in (("Knives Out - 2019 1080p BluRay", "2019"),
                           ("Coco - 2017", "2017"),
                           ("Free Guy - 2021 WEB-DL", "2021")):
            with self.subTest(name=name):
                meta = MetaInfo(name, use_llm=False)
                self.assertEqual(MediaType.MOVIE, meta.type)
                self.assertEqual(year, meta.year)
                self.assertEqual(name.split(" - ")[0].title(), meta.en_name)
        anime = MetaInfo("刀剑神域 - 10 [1080p]", use_llm=False)
        self.assertEqual(10, anime.begin_episode)
        self.assertEqual(MediaType.TV, anime.type)

    def test_marker_words_in_movie_titles(self):
        # 普通片名单词不能因与英文季集标记同名而被删除。
        for title, name in (("Season of the Witch 2011.mkv", "Season Of The Witch"),
                            ("The Final Season 2007.mkv", "The Final Season"),
                            ("Episode of Love 2015.mkv", "Episode Of Love")):
            with self.subTest(title=title):
                meta = MetaInfo(title, use_llm=False)
                self.assertEqual(name, meta.en_name)
                self.assertEqual(MediaType.MOVIE, meta.type)
                self.assertIsNone(meta.begin_episode)

    def test_written_season_and_episode(self):
        for name, season, expected_name in (
                ("Game of Thrones Season 4 1080p BluRay", 4, "Game Of Thrones"),
                ("Show Season 2 720p HDTV", 2, "Show")):
            with self.subTest(name=name):
                meta = MetaInfo(name, use_llm=False)
                self.assertEqual(MediaType.TV, meta.type)
                self.assertEqual(season, meta.begin_season)
                self.assertEqual(expected_name, meta.en_name)
        meta = MetaInfo("Episode 5 - Show Name 1080p", use_llm=False)
        self.assertEqual(5, meta.begin_episode)
        self.assertEqual("Show Name", meta.en_name)


class FractionalEpisodeTest(unittest.TestCase):
    def test_fractional_numbers_remain_unconfirmed(self):
        for name, raw in (("[Group] 某某 [01.5][1080p].mkv", "01.5"),
                          ("[Group] 某某 [07.5][1080p].mkv", "07.5"),
                          ("[Group] 某某 [01.25][1080p].mkv", "01.25"),
                          ("Show E01.5.mkv", "01.5"),
                          ("Show EP01.5.mkv", "01.5"),
                          ("Show S01E01.5.mkv", "01.5"),
                          ("Show S02EP01.25.mkv", "01.25"),
                          ("Show - 01.5.mkv", "01.5")):
            with self.subTest(name=name):
                meta = MetaInfo(name, use_llm=False)
                self.assertEqual(raw, meta.note["fractional_episode"]["raw"])
                self.assertIsNone(meta.begin_episode)
                self.assertIsNone(meta.end_episode)
        first = MetaInfo("Show [01.5].mkv", use_llm=False)
        second = MetaInfo("Other E03.mkv", use_llm=False)
        self.assertNotIn("fractional_episode", second.note)
        self.assertIsNot(first.note, second.note)

    def test_codec_decimal_is_not_an_episode(self):
        for name in ("The 355 2022 BluRay 1080p DTS-HD MA5.1 X265.10bit-BeiTai",
                     "Show AAC5.1 1080p", "Show 1920.1080 1080p",
                     "Thor Love and Thunder (2022) [1080p] [WEBRip] [5.1]"):
            with self.subTest(name=name):
                meta = MetaInfo(name, use_llm=False)
                self.assertNotIn("fractional_episode", meta.note)

    def test_confirmed_mapping_needs_matching_episode_evidence(self):
        media = Media.__new__(Media)
        info = {"id": 42, "media_type": MediaType.TV,
                "seasons": [{"season_number": 0}, {"season_number": 1}]}
        meta = MetaInfo("Show [01.5].mkv", use_llm=False)
        rule = {"tmdb_id": 42, "source_episode": "01.5", "target_season": 0,
                "target_episode": 3, "episode_title": "Bonus Story"}
        with patch("app.media.media.Config") as config, \
                patch.object(media, "get_tmdb_season_episodes", return_value=[
                    {"season_number": 0, "episode_number": 3, "name": "Bonus Story"}]) as episodes:
            config.return_value.get_config.return_value = {"fractional_episode_mappings": [rule]}
            self.assertTrue(media._confirm_fractional_episode(meta, info))
        episodes.assert_called_once_with(tmdbid=42, season=0)
        self.assertEqual((0, 3), (meta.begin_season, meta.begin_episode))
        self.assertEqual("Bonus Story", meta.note["episode_mapping"]["evidence"])

        meta = MetaInfo("Show [01.5].mkv", use_llm=False)
        with patch("app.media.media.Config") as config, \
                patch.object(media, "get_tmdb_season_episodes", return_value=[
                    {"season_number": 1, "episode_number": 4, "name": "Wrong Name"}]):
            config.return_value.get_config.return_value = {"fractional_episode_mappings": [
                dict(rule, target_season=1, target_episode=4)]}
            self.assertFalse(media._confirm_fractional_episode(meta, info))
        self.assertIsNone(meta.begin_episode)


    def test_no_candidate_or_ambiguity_is_not_confirmation(self):
        media = Media.__new__(Media)
        meta = MetaInfo("Show [01.5] - Bonus Story.mkv", use_llm=False)
        info = {"id": 42, "media_type": MediaType.TV,
                "seasons": [{"season_number": 0}, {"season_number": 1}]}
        with patch("app.media.media.Config") as config, \
                patch.object(media, "get_tmdb_tv_season_detail", return_value={
                    "episodes": [{"episode_number": 1, "name": "Bonus Story"}]}):
            config.return_value.get_config.return_value = {}
            self.assertFalse(media._confirm_fractional_episode(meta, info))
        self.assertIsNone(meta.begin_episode)

        def incomplete_detail(tmdbid, season):
            return {"episodes": [{"episode_number": 1, "name": "Bonus Story"}]} if season == 0 else {}

        with patch("app.media.media.Config") as config, \
                patch.object(media, "get_tmdb_tv_season_detail", side_effect=incomplete_detail):
            config.return_value.get_config.return_value = {}
            self.assertFalse(media._confirm_fractional_episode(meta, info))
        self.assertIsNone(meta.begin_episode)

        def unique_detail(tmdbid, season):
            return {"episodes": [{"season_number": 0, "episode_number": 2,
                                  "name": "Bonus Story"}]} if season == 0 else {"episodes": []}

        with patch("app.media.media.Config") as config, \
                patch.object(media, "get_tmdb_tv_season_detail", side_effect=unique_detail):
            config.return_value.get_config.return_value = {}
            self.assertTrue(media._confirm_fractional_episode(meta, info))
        self.assertEqual((0, 2), (meta.begin_season, meta.begin_episode))

    def test_query_failure_and_ambiguous_config_leave_episode_unset(self):
        media = Media.__new__(Media)
        info = {"id": 42, "media_type": MediaType.TV,
                "seasons": [{"season_number": 0}]}
        rule = {"tmdb_id": 42, "source_episode": "01.5", "target_season": 0,
                "target_episode": 3, "episode_title": "Bonus Story"}
        with patch("app.media.media.Config") as config, \
                patch.object(media, "get_tmdb_season_episodes", side_effect=RuntimeError("offline")):
            config.return_value.get_config.return_value = {"fractional_episode_mappings": [rule]}
            meta = MetaInfo("Show [01.5].mkv", use_llm=False)
            self.assertFalse(media._confirm_fractional_episode(meta, info))
            self.assertIsNone(meta.begin_episode)
        with patch("app.media.media.Config") as config, \
                patch.object(media, "get_tmdb_season_episodes") as episodes:
            config.return_value.get_config.return_value = {"fractional_episode_mappings": [rule, rule]}
            meta = MetaInfo("Show [01.5].mkv", use_llm=False)
            self.assertFalse(media._confirm_fractional_episode(meta, info))
            episodes.assert_not_called()


class ExplicitEpisodeFormsTest(unittest.TestCase):
    def test_x_separated_season_episode(self):
        meta = MetaInfo("Doctor Who 2005 1x03 1080p", use_llm=False)
        self.assertEqual((1, 3), (meta.begin_season, meta.begin_episode))
        self.assertEqual("Doctor Who", meta.en_name)
        self.assertEqual(MediaType.TV, meta.type)

    def test_total_count_and_roman_season(self):
        meta = MetaInfo("某剧 共24集 1080p", use_llm=False)
        self.assertEqual(MediaType.TV, meta.type)
        self.assertEqual(24, meta.total_episodes)
        self.assertIsNone(meta.begin_episode)
        roman = MetaInfo("某某动漫 第II季 [1080p]", use_llm=False)
        self.assertEqual(2, roman.begin_season)
        self.assertEqual(MediaType.TV, roman.type)
        self.assertIn("某某", roman.get_name())
        movie = MetaInfo("The Matrix Collection 合集 1999", use_llm=False)
        self.assertEqual(MediaType.MOVIE, movie.type)
        beyblade = MetaInfo("[jibaketa] Beyblade X - 127 [WEB 1080p AVC AAC]", use_llm=False)
        self.assertIn("X", beyblade.en_name)
        self.assertNotEqual(10, beyblade.begin_season)


class TransferGuardTest(unittest.TestCase):
    def test_skipped_file_is_not_moved_even_with_unknown_dir(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ("Show [01.5].mkv",):
                with self.subTest(name=name):
                    path = os.path.join(root, name)
                    with open(path, "wb") as output:
                        output.write(b"source")
                    meta = MetaBase(name, fileflag=True)
                    meta.skip_reason = "小数集待确认"
                    transfer = FileTransfer.__new__(FileTransfer)
                    transfer.media = MagicMock()
                    transfer.media.get_media_info_on_files.return_value = {path: meta}
                    transfer.progress = MagicMock()
                    transfer.dbhelper = MagicMock()
                    transfer._default_rmt_mode = RmtMode.COPY
                    with patch.object(transfer, "check_ignore", return_value=([path], "")), \
                            patch("app.filetransfer.PathUtils.get_bluray_dir", return_value=None), \
                            patch.object(transfer, "_FileTransfer__transfer_file") as move:
                        success, _ = transfer.transfer_media(
                            in_from=SyncType.MAN, in_path=path, rmt_mode=RmtMode.COPY,
                            unknown_dir=root)
                    # 下载器只会在成功状态下执行移动模式的删种动作。
                    self.assertFalse(success)
                    move.assert_not_called()
                    with open(path, "rb") as source:
                        self.assertEqual(b"source", source.read())

    def test_fractional_mapping_is_not_overwritten_by_download_context(self):
        media = Media.__new__(Media)
        media.tmdb = object()
        info = {"id": 42, "name": "Show", "media_type": MediaType.TV,
                "seasons": [{"season_number": 0}, {"season_number": 1}]}
        rule = {"tmdb_id": 42, "source_episode": "01.5", "source_season": 1,
                "target_season": 0, "target_episode": 3, "episode_title": "Bonus Story"}
        with tempfile.TemporaryDirectory() as root:
            for filename in ("Show S01E01.5.mkv", "Show [01.5].mkv"):
                path = os.path.join(root, filename)
                open(path, "wb").close()
                with patch("app.media.media.Config") as config, \
                        patch.object(media, "get_tmdb_season_episodes", return_value=[
                            {"season_number": 0, "episode_number": 3, "name": "Bonus Story"}]):
                    selected_rule = dict(rule)
                    if "S01" not in filename:
                        selected_rule.pop("source_season")
                    config.return_value.get_config.return_value = {"fractional_episode_mappings": [selected_rule]}
                    for seasons, episodes, skipped in (([1], [], True), ([0], [4], True),
                                                       ([0], [3], False), ([], [], False)):
                        with self.subTest(filename=filename, seasons=seasons, episodes=episodes):
                            meta = media.get_media_info_on_files(
                                [path], tmdb_info=info, media_type=MediaType.TV,
                                download_context={"seasons": seasons, "episodes": episodes})[path]
                            # 冲突保留跳过结果，防止后续按成功整理处理；一致时保留正式编号。
                            self.assertEqual((0, 3), (meta.begin_season, meta.begin_episode))
                            self.assertEqual(skipped, bool(meta.skip_reason))

    def test_manual_fractional_mapping_and_unconfirmed_skip(self):
        media = Media.__new__(Media)
        media.tmdb = object()
        info = {"id": 42, "name": "Show", "media_type": MediaType.TV,
                "seasons": [{"season_number": 0}, {"season_number": 1}]}
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "Show [01.5].mkv")
            open(path, "wb").close()
            with patch("app.media.media.Config") as config, \
                    patch.object(media, "get_tmdb_season_episodes", return_value=[
                        {"season_number": 1, "episode_number": 7, "name": "The Gap"}]), \
                    patch.object(media, "save_rename_cache"):
                config.return_value.get_config.return_value = {"fractional_episode_mappings": [
                    {"tmdb_id": 42, "source_episode": "01.5", "target_season": 1,
                     "target_episode": 7, "episode_title": "The Gap"}]}
                confirmed = media.get_media_info_on_files(
                    [path], tmdb_info=info, media_type=MediaType.TV)[path]
                self.assertEqual((1, 7), (confirmed.begin_season, confirmed.begin_episode))
                self.assertIsNone(confirmed.skip_reason)
                config.return_value.get_config.return_value = {}
                unconfirmed = media.get_media_info_on_files(
                    [path], tmdb_info=info, media_type=MediaType.TV)[path]
                self.assertTrue(unconfirmed.skip_reason)
                self.assertIsNone(unconfirmed.begin_episode)


class SeasonAndRangeRegressionTest(unittest.TestCase):
    def test_rss_preserves_all_parsed_season_forms(self):
        media = Media.__new__(Media)
        media.tmdb = object()
        info = {"id": 42, "name": "Show", "media_type": MediaType.TV,
                "seasons": [{"season_number": n} for n in (1, 2, 3)]}
        cases = (("Show 2x03.mkv", 2), ("某剧 第三季 第03集.mkv", 3),
                 ("Show 第II季 E03.mkv", 2), ("Show S02E03.mkv", 2),
                 ("[Group] Show II - 03 [1080p].mkv", 2))
        with tempfile.TemporaryDirectory() as root:
            for filename, season in cases:
                path = os.path.join(root, filename)
                open(path, "wb").close()
                for target in (1, season):
                    with self.subTest(filename=filename, target=target):
                        result = media.get_media_info_on_files(
                            [path], tmdb_info=info, media_type=MediaType.TV,
                            download_context={"seasons": [target], "episodes": [3]})
                        if target != season:
                            self.assertNotIn(path, result)
                        else:
                            self.assertEqual((season, 3),
                                             (result[path].begin_season, result[path].begin_episode))
            # 真正没有季标记时，仍允许下载任务补充季号。
            path = os.path.join(root, "Show E03.mkv")
            open(path, "wb").close()
            result = media.get_media_info_on_files(
                [path], tmdb_info=info, media_type=MediaType.TV,
                download_context={"seasons": [2], "episodes": [3]})
            self.assertEqual(2, result[path].begin_season)

    def test_empty_manual_format_runs_verified_mapping(self):
        media = Media.__new__(Media)
        media.tmdb = object()
        info = {"id": 42, "name": "Show", "media_type": MediaType.TV,
                "seasons": [{"season_number": 1}, {"season_number": 4}]}
        rule = {"tmdb_id": 42, "source_season": 4, "source_begin": 1,
                "source_end": 19, "target_season": 1, "offset": 66}
        with tempfile.TemporaryDirectory() as root, \
                patch("app.media.media.Config") as config, \
                patch.object(media, "get_tmdb_tv_season_detail") as detail, \
                patch.object(media, "save_rename_cache"):
            path = os.path.join(root, "Show S04E01.mkv")
            open(path, "wb").close()
            config.return_value.get_config.return_value = {"episode_mappings": [rule]}
            detail.return_value = {"episodes": [{"episode_number": 67}]}
            result = media.get_media_info_on_files(
                [path], tmdb_info=info, media_type=MediaType.TV,
                season="", episode_format=EpisodeFormat(None))[path]
            self.assertEqual((1, 67), (result.begin_season, result.begin_episode))
            # TMDB 不存在目标集时，空规则不能绕过映射验证。
            detail.return_value = {"episodes": []}
            self.assertNotIn(path, media.get_media_info_on_files(
                [path], tmdb_info=info, media_type=MediaType.TV,
                episode_format=EpisodeFormat(None)))
            # 用户实际指定集号时，继续尊重手动覆盖。
            result = media.get_media_info_on_files(
                [path], tmdb_info=info, media_type=MediaType.TV,
                episode_format=EpisodeFormat(None, "8"))[path]
            self.assertEqual((4, 8), (result.begin_season, result.begin_episode))

    def test_explicit_multi_episode_ranges_survive_file_parsing(self):
        for filename in ("Show S01E01-E03.mkv", "Show S01E01-03.mkv",
                         "Show E01-E04.mkv", "Show S01EP01-EP04.mkv"):
            with self.subTest(filename=filename):
                meta = MetaInfo(filename, use_llm=False)
                end = 3 if "03" in filename else 4
                self.assertEqual(list(range(1, end + 1)), meta.get_episode_list())
                self.assertEqual(end, meta.total_episodes)
        # 不把无区间标记的资源数字放大成数百集。
        meta = MetaInfo("Show S01E01 1080.mkv", use_llm=False)
        self.assertEqual([1], meta.get_episode_list())


if __name__ == "__main__":
    unittest.main()
