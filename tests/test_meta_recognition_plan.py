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
