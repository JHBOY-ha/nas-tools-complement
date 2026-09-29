"""Offline regressions for preserving complete episode-number expressions."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.media.media import Media
from app.media.meta import MetaInfo
from app.media.meta.fractional import episode_key, release_references
from app.media.meta.special import download_block_reason, extract_special
from app.media.meta.special_resolver import SpecialResolver
from app.utils.types import MediaType


class MultiEpisodeGuardTest(unittest.TestCase):
    def setUp(self):
        self.media = Media.__new__(Media)
        self.media.tmdb = object()
        self.info = {"id": 42, "name": "Show", "media_type": MediaType.TV,
                     "seasons": [{"season_number": 0, "episode_count": 3},
                                 {"season_number": 1, "episode_count": 0}]}
        # Both candidate endpoints exist: existence must not make an ambiguous
        # merged release equivalent to either individual episode.
        self.episodes = [{"id": 100 + number, "show_id": 42, "season_number": 0,
                          "episode_number": number, "name": "Special %s" % number,
                          "overview": "第13.5集 OVA 02" if number == 1 else ""}
                         for number in range(1, 4)]
        self.detail = self.enter_patch(patch.object(
            self.media, "get_tmdb_tv_season_detail", side_effect=lambda tmdbid, season:
            {"episodes": self.episodes if season == 0 else []}))
        self.enter_patch(patch.object(self.media, "save_rename_cache"))
        for target in ("app.media.meta.metainfo.Config", "app.media.media.Config",
                       "app.media.meta.special_resolver.Config"):
            self.enter_patch(patch(target)).return_value.get_config.return_value = {}

    def enter_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def test_fractional_ranges_and_multiple_fields_never_confirm_one_endpoint(self):
        names = ("Show E13.5-E14.5.mkv", "Show [13.5][14.5].mkv",
                 "Show S01E13.5+E14.5.mkv", "Show E13.5 E14.5.mkv",
                 "Show E13.5.E14.5.mkv", "Show [13.5-14.5].mkv",
                 "Show 第13.5至第14.5集.mkv", "Show E13.5-E14.mkv",
                 "Show E13-E14.5.mkv", "Show - 13.5-14.5.mkv",
                 "Show E13.5-E14.1080p.mkv")
        for name in names:
            with self.subTest(name=name):
                meta = MetaInfo(name, use_llm=False)
                note = meta.note["fractional_episode"]
                self.assertIsNone(episode_key(note["raw"]))
                self.assertIsNone(meta.begin_episode)
                self.assertFalse(self.media._confirm_fractional_episode(meta, self.info))
                self.assertTrue(download_block_reason(meta))
        self.detail.assert_not_called()

    def test_ambiguous_fractional_file_stays_skipped_with_bound_download_context(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "Show S01E13.5-E14.5.mkv"
            path.write_bytes(b"both episodes")
            result = self.media.get_media_info_on_files([str(path)], download_context={
                "tmdb_info": self.info, "seasons": [1]})[str(path)]
            self.assertTrue(result.skip_reason)
            self.assertIsNone(result.begin_episode)
            self.assertEqual(b"both episodes", path.read_bytes())
            self.assertNotIn("episode_mapping", result.note)

    def test_range_references_do_not_expose_either_labelled_endpoint(self):
        for evidence in ("Episode 13.5-14.5", "#13.5-14.5",
                         "Season 1 Episode 13.5-14.5", "第一季13.5-14.5",
                         "Episode 13.5 and Episode 14.5", "#13.5 / #14.5",
                         "第13.5至第14.5集"):
            with self.subTest(evidence=evidence):
                self.assertEqual([], release_references(evidence))
        # A separate valid label survives masking of a different range.
        self.assertEqual([("0.5A", None, "#0.5A")], release_references(
            "Episode 13.5-14.5; #0.5A收录于限定版。"))
        self.assertEqual("13.5", release_references("Episode 13.5 - Recap")[0][0])

    def test_single_fractional_labels_and_technical_fields_remain_compatible(self):
        for name in ("Show E13.5 - Recap [1080p].mkv", "Show S01E13.5.1080p.mkv",
                     "Show [13.5].mkv", "Show E13.5-1080p.mkv",
                     "Show E13.5 - 2020.mkv", "Show E13.5 - Recap - 2020.mkv"):
            with self.subTest(name=name):
                meta = MetaInfo(name, use_llm=False)
                self.assertEqual("13.5", meta.note["fractional_episode"]["key"])
                self.assertTrue(self.media._confirm_fractional_episode(meta, self.info))
                self.assertEqual((0, 1), (meta.begin_season, meta.begin_episode))
        for name in ("Show S01E01.10bit.mkv", "Show S01E01.1080p.mkv",
                     "Show - 2022.08.01.mkv", "Show [1920.1080].mkv", "Show [5.1].mkv"):
            with self.subTest(name=name):
                self.assertNotIn("fractional_episode", MetaInfo(name, use_llm=False).note)

    def test_integer_ranges_and_collection_totals_are_not_fractional(self):
        for name, expected in (("Show.S01E01-E03.1080p.mkv", [1, 2, 3]),
                               ("Show.S01E02.12 集全.mkv", [2]),
                               ("Show.S01E02.全12集.mkv", [2])):
            with self.subTest(name=name):
                meta = MetaInfo(name, use_llm=False)
                self.assertNotIn("fractional_episode", meta.note)
                self.assertEqual(expected, meta.get_episode_list())

    def test_continuous_formal_special_lists_keep_the_normal_range_parser(self):
        for name in ("Show S00E01.E02.mkv", "Show S00E01 E02.mkv",
                     "Show S00E01 + E02.mkv", "Show S00E01+E02.mkv",
                     "Show S00E01.E02.E03.mkv", "Show S00E01 E02 E03.mkv",
                     "Show S00E01 + E02 + E03.mkv", "Show S00E01-E03.mkv"):
            with self.subTest(name=name):
                meta = MetaInfo(name, use_llm=False)
                expected = [1, 2, 3] if "E03" in name else [1, 2]
                self.assertEqual(expected, meta.get_episode_list())
                self.assertEqual(0, meta.begin_season)
                self.assertNotIn("special_episode", meta.note)

    def test_formal_single_and_ambiguous_lists_keep_confirmation(self):
        self.assertEqual((0, 1), extract_special("Show S00E01.1080p.mkv")[1]["formal"])
        for name in ("Show S00E01 + E03.mkv", "Show S00E01 S01E02.mkv",
                     "Show [OVA01] S00E01.E02.mkv"):
            with self.subTest(name=name):
                meta = MetaInfo(name, use_llm=False)
                self.assertTrue(meta.note["special_episode"].get("reason"))
                self.assertTrue(SpecialResolver(self.media).resolve(meta, self.info).skip_reason)

    def test_all_special_range_labels_and_unbracketed_ranges_are_protected(self):
        names = ("Show [SPECIAL01-02].mkv", "Show [SP01-02].mkv",
                 "Show [OVA01-02].mkv", "Show [OAD01-02].mkv",
                 "Show - OVA01-OVA02 [1080p].mkv", "Show OVA01-OVA02.mkv",
                 "Show - SPECIAL01-02 [1080p].mkv", "Show OVA1.5.mkv")
        for name in names:
            with self.subTest(name=name):
                meta = MetaInfo(name, use_llm=False)
                self.assertTrue(meta.note["special_episode"].get("reason"))
                self.assertTrue(SpecialResolver(self.media).resolve(meta, self.info).skip_reason)
        self.assertEqual("SPECIAL", extract_special("Show [SPECIAL01-02].mkv")[1]["kind"])
        self.detail.assert_not_called()

    def test_special_range_cannot_become_regular_episode_in_download_context(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "Show [SPECIAL01-02].mkv")
            Path(path).write_bytes(b"special collection")
            result = self.media.get_media_info_on_files([path], download_context={
                "tmdb_info": self.info, "seasons": [1]})[path]
            self.assertTrue(result.skip_reason)
            self.assertTrue(download_block_reason(result))
            self.assertIsNone(result.begin_episode)
            self.assertTrue(os.path.exists(path))

    def test_special_title_words_and_valid_single_labels_are_unchanged(self):
        for name in ("A Special Day 2024.mkv", "The OVA Story.mkv", "Movie [SPIDER].mkv"):
            self.assertIsNone(extract_special(name)[1])
        for name in ("Show [SPECIAL01].mkv", "Show OVA02.mkv", "Show - OVA02 [1080p].mkv"):
            self.assertNotIn("reason", extract_special(name)[1])


if __name__ == "__main__":
    unittest.main()
