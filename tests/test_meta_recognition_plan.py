"""Regression cases for file-level media recognition and transfer guards."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app.filetransfer import FileTransfer
from app.media.meta import MetaInfo
from app.media.meta._base import MetaBase
from app.media.meta.metainfo import explicit_extra_reason
from app.media.media import Media
from app.utils.types import MediaType, RmtMode, SyncType


class ExtraRecognitionTest(unittest.TestCase):
    def test_explicit_file_tags_only(self):
        for name in ("[Group] Show [NCOP][05].mkv", "Show [NCOP&ED][05].mkv",
                     "Show [ED01].mkv", "Show [PV01].mkv", "Show [SP01].mkv"):
            with self.subTest(name=name):
                self.assertTrue(explicit_extra_reason(name))
        for name in ("The Edited Life 2020.mkv", "Show-SP.mkv",
                     "Show [01].mkv", "Show [01-12+SP]/Show [01].mkv"):
            with self.subTest(name=name):
                self.assertIsNone(explicit_extra_reason(os.path.basename(name)))

    def test_extra_never_reaches_llm_or_tmdb(self):
        with patch("app.media.meta.metainfo.WordsHelper") as words, \
                patch("app.media.meta.metainfo.LLMMetaParser") as llm:
            meta = MetaInfo("[Group] Show [ED01].mkv")
        words.assert_not_called()
        llm.assert_not_called()
        self.assertTrue(meta.skip_reason)
        self.assertIsNone(meta.begin_episode)

        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "Show [SP01].mkv")
            open(path, "wb").close()
            media = Media.__new__(Media)
            media.tmdb = object()
            with patch("app.media.media.MetaInfo") as parser, \
                    patch.object(media, "get_tmdb_info") as tmdb:
                result = media.get_media_info_on_files([path])
            parser.assert_not_called()
            tmdb.assert_not_called()
            self.assertTrue(result[path].skip_reason)
        media = Media.__new__(Media)
        media.tmdb = object()
        with patch("app.media.media.MetaInfo") as parser, \
                patch.object(media, "get_tmdb_info") as tmdb:
            self.assertIsNone(media.get_media_info("Show [NCOP].mkv"))
        parser.assert_not_called()
        tmdb.assert_not_called()

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
                patch.object(media, "get_tmdb_season_episodes", return_value=[
                    {"episode_number": 1, "name": "Bonus Story"}]):
            config.return_value.get_config.return_value = {}
            self.assertFalse(media._confirm_fractional_episode(meta, info))
        self.assertIsNone(meta.begin_episode)

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
            for name in ("Show [PV01].mkv", "Show [01.5].mkv"):
                with self.subTest(name=name):
                    path = os.path.join(root, name)
                    with open(path, "wb") as output:
                        output.write(b"source")
                    meta = MetaBase(name, fileflag=True)
                    meta.skip_reason = "明确附加内容" if "PV" in name else "小数集待确认"
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
                    self.assertTrue(success)
                    move.assert_not_called()
                    with open(path, "rb") as source:
                        self.assertEqual(b"source", source.read())

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


if __name__ == "__main__":
    unittest.main()
