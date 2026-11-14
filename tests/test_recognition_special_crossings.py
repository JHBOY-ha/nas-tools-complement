"""Offline regressions where fractional, special and work-level evidence meet."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.media.meta import MetaInfo
from app.media.meta.special import download_block_reason
from app.utils import EpisodeFormat
from app.utils.types import MediaType
from tests.recognition_fixture import FakeTmdb


class SpecialCrossingTest(unittest.TestCase):
    def setUp(self):
        self.fx = FakeTmdb(self)
        temporary = tempfile.TemporaryDirectory(prefix="nas-special-crossings-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # FakeTmdb owns provider/config boundaries; preparation reads the same
        # mutable media settings so enabled extras exercise the real parser.
        config = patch("app.media.meta.metainfo.Config").start()
        self.addCleanup(patch.stopall)
        config.return_value.get_config.side_effect = (
            lambda key: copy.deepcopy(self.fx.media_config) if key == "media" else {})
        self.fx.tv(42, "Show", genres=(16,), seasons={0: 2, 1: 0})
        for episode, raw in zip(self.fx.episodes[(42, 0)], ("13.5", "14.5")):
            episode.update(name="Bonus %s" % episode["episode_number"],
                           overview="Episode %s; Season 1 OVA%02d" %
                           (raw, episode["episode_number"]))
        self.info = self.fx.get_tmdb_info(mtype=MediaType.TV, tmdbid=42)

    def recognize_file(self, name, **kwargs):
        path = self.root / name
        path.write_bytes(b"disposable media")
        result = self.fx.media.get_media_info_on_files([str(path)], **kwargs)[str(path)]
        self.assertEqual(b"disposable media", path.read_bytes())
        return result

    def recognize(self, name, entry):
        if entry == "file":
            return self.recognize_file(name, tmdb_info=self.info)
        if entry == "warm_title":
            # A saved work identity cannot prove this release's special target.
            self.fx.warm("[电视剧]Show-None-1", 42, kind=MediaType.ANIME, title="Show")
        return self.fx.media.get_media_info(name)

    def assert_rejected_special(self, result):
        self.assertIsNotNone(result)
        self.assertTrue(result.skip_reason)
        self.assertTrue(download_block_reason(result))
        self.assertNotEqual("confirmed", result.note["special_episode"]["status"])

    def test_special_preserves_numeric_series_title(self):
        for season in (1, 2, 3):
            with self.subTest(season=season):
                name = "Major 2nd Season %s Episode 5 OVA01 [1080p].mkv" % season
                parsed = MetaInfo(name, use_llm=False)
                self.assertEqual("major 2nd", parsed.get_name().casefold())
                self.assertEqual(["major 2nd"],
                                 [value.casefold() for value in parsed.note["special_episode"]["rule_names"]])
                self.assertEqual(season, parsed.note["special_episode"]["source_season"])
                self.assertEqual("1080p", parsed.resource_pix)

    def test_special_search_does_not_replace_sequel_with_original_work(self):
        for tmdb_id, title in ((51, "Major"), (52, "Major 2nd")):
            self.fx.tv(tmdb_id, title, genres=(16,), seasons={0: 1, 2: 5})
            self.fx.episodes[(tmdb_id, 0)][0].update(name="Bonus", overview="Season 2 OVA01")
        result = self.fx.media.get_media_info("Major 2nd Season 2 Episode 5 OVA01 [1080p].mkv")
        self.assertIsNotNone(result)
        self.assertIsNone(result.skip_reason)
        self.assertEqual((52, 0, [1]),
                         (result.tmdb_id, result.begin_season, result.get_episode_list()))

    def test_fraction_does_not_collapse_special_release_ranges(self):
        for kind in ("OVA", "OAD", "SP", "SPECIAL"):
            for entry in ("title", "warm_title", "file"):
                with self.subTest(kind=kind, entry=entry):
                    result = self.recognize("Show S01E13.5 [%s01-02].mkv" % kind, entry)
                    self.assert_rejected_special(result)

    def test_fraction_and_special_number_must_identify_the_same_episode(self):
        for entry in ("title", "file"):
            with self.subTest(entry=entry):
                result = self.recognize("Show S01E13.5 [OVA02].mkv", entry)
                self.assert_rejected_special(result)

    def test_fraction_and_formal_special_number_must_agree(self):
        for entry in ("title", "file"):
            with self.subTest(entry=entry):
                result = self.recognize("Show S00E02 [13.5].mkv", entry)
                self.assert_rejected_special(result)

    def test_fraction_cannot_bypass_configured_special_target(self):
        self.fx.media_config["special_episode_mappings"] = [dict(
            source_type="tv", source_tmdb_id=42, kind="OVA", source_number="1",
            target=dict(media_type="tv", tmdb_id=42, season=0, episode=2))]
        control = self.recognize_file("Show S01 [OVA01].mkv", tmdb_info=self.info)
        self.assertIsNone(control.skip_reason)
        self.assertEqual((0, [2]), (control.begin_season, control.get_episode_list()))
        for entry in ("title", "file"):
            with self.subTest(entry=entry):
                result = self.recognize("Show S01E13.5 [OVA01].mkv", entry)
                self.assert_rejected_special(result)

    def test_matching_fraction_and_special_evidence_remain_accepted(self):
        for entry in ("title", "file"):
            with self.subTest(entry=entry):
                result = self.recognize("Show S01E13.5 [OVA01].mkv", entry)
                self.assertIsNone(result.skip_reason)
                self.assertIsNone(download_block_reason(result))
                self.assertEqual((42, 0, [1]),
                                 (result.tmdb_id, result.begin_season, result.get_episode_list()))
                for key in ("fractional_episode", "special_episode"):
                    self.assertEqual("confirmed", result.note[key]["status"])

    def test_fraction_download_must_obey_saved_special_target_with_empty_episodes(self):
        for name in ("Show S01E14.5.mkv", "Show S01E14.5 [OVA02].mkv"):
            for pack in (False, True):
                with self.subTest(name=name, season_pack=pack):
                    context = dict(tmdb_info=self.info, seasons=[1] if pack else [0],
                                   episodes=[], numbering="tmdb", special_target=dict(
                                       media_type="TV", tmdb_id=42, season=0, episode=1))
                    if pack:
                        context.update(scope="season_pack", release_seasons=[1])
                    result = self.recognize_file(name, download_context=context)
                    self.assertTrue(result.skip_reason)
                    self.assertTrue(download_block_reason(result))
        matching = self.recognize_file("Show S01E13.5 [OVA01].mkv", download_context=dict(
            tmdb_info=self.info, seasons=[0], episodes=[], numbering="tmdb",
            special_target=dict(media_type="TV", tmdb_id=42, season=0, episode=1)))
        self.assertIsNone(matching.skip_reason)
        self.assertEqual((0, [1]), (matching.begin_season, matching.get_episode_list()))

    def test_fraction_subtitle_keeps_conflicting_special_label(self):
        for entry in ("title", "file"):
            with self.subTest(entry=entry):
                result = self.recognize("Show S01E13.5 - OVA02 [1080p].mkv", entry)
                self.assertIn("special_episode", result.note)
                self.assert_rejected_special(result)
                self.assertEqual("1080p", result.resource_pix)

    def test_fraction_subtitle_keeps_enabled_extra_out_of_regular_episodes(self):
        self.fx.media_config["extras"] = dict(enabled=True)
        for kind in ("NCOP", "NCED", "PV", "TRAILER", "INTERVIEW"):
            for entry in ("title", "file"):
                with self.subTest(kind=kind, entry=entry):
                    result = self.recognize("Show S01E13.5 - %s01 [1080p].mkv" % kind, entry)
                    self.assertIn("extra", result.note)
                    self.assertEqual(kind, result.note["extra"]["kind"])
                    self.assertTrue(download_block_reason(result))
                    self.assertEqual("1080p", result.resource_pix)

    def test_bound_automatic_fraction_cannot_teach_ordinary_work_alias(self):
        self.fx.tv(61, "Other Work", seasons={0: 1, 1: 12})
        self.fx.tv(62, "Alias Show", seasons={1: 12})
        self.fx.episodes[(61, 0)][0].update(name="Recap", overview="Season 1 episode 13.5")
        bound = self.fx.get_tmdb_info(mtype=MediaType.TV, tmdbid=61)
        fractional = self.recognize_file("Alias.Show.S01E13.5.mkv", tmdb_info=bound)
        self.assertIsNone(fractional.skip_reason)
        self.assertEqual((61, 0, [1]),
                         (fractional.tmdb_id, fractional.begin_season, fractional.get_episode_list()))
        learned = copy.deepcopy(self.fx.cache)
        warm = self.fx.media.get_media_info("Alias.Show.S01E01.mkv")
        self.fx.cache.clear()
        cold = self.fx.media.get_media_info("Alias.Show.S01E01.mkv", cache=False)
        self.assertIsNone(cold.skip_reason)
        self.assertEqual(62, cold.tmdb_id)
        self.assertIsNone(warm.skip_reason)
        self.assertEqual(cold.tmdb_id, warm.tmdb_id)
        self.assertEqual({}, learned)

    def test_ordinary_manual_binding_still_learns_work_alias(self):
        self.fx.tv(61, "Manual Work", seasons={1: 12})
        bound = self.fx.get_tmdb_info(mtype=MediaType.TV, tmdbid=61)
        manual = self.recognize_file("Local.Alias.S01E01.mkv", tmdb_info=bound)
        self.assertIsNone(manual.skip_reason)
        self.assertTrue(self.fx.cache)
        warm = self.fx.media.get_media_info("Local.Alias.S01E02.mkv")
        self.assertIsNone(warm.skip_reason)
        self.assertEqual((61, 1, [2]), (warm.tmdb_id, warm.begin_season, warm.get_episode_list()))

    def test_explicit_manual_ep_still_overrides_fraction_and_single_special_label(self):
        for name in ("Show S01E13.5.mkv", "Show S01E13.5 [OVA01].mkv"):
            with self.subTest(name=name):
                result = self.recognize_file(name, tmdb_info=self.info, season=0,
                                             episode_format=EpisodeFormat(None, "1-2"))
                self.assertIsNone(result.skip_reason)
                self.assertIsNone(download_block_reason(result))
                self.assertEqual((0, [1, 2]), (result.begin_season, result.get_episode_list()))
                self.assertTrue(result.note["fractional_episode"]["manual"])


if __name__ == "__main__":
    unittest.main()
