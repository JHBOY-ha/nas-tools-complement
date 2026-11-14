"""Seasonless anime totals must resolve identically in preview and file transfer."""
import tempfile
import unittest
from pathlib import Path

from app.media.meta.recognition import NumberingState
from app.utils import EpisodeFormat
from app.utils.types import MediaType
from tests.recognition_fixture import FakeTmdb, outcome


class AbsoluteEpisodeNumberingTest(unittest.TestCase):
    def setUp(self):
        self.fx = FakeTmdb(self)
        # Confirmed boundary: season 1 ends at 27; release total 28 starts S02E01.
        self.fx.tv(216467, "夜樱家的大作战", original="Yozakura-san Chi no Daisakusen",
                   genres=(16,), year="2024", seasons={0: 28, 1: 27, 2: 24})
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    @staticmethod
    def title(episode):
        return ("[LoliHouse] Yozakura-san Chi no Daisakusen - %s "
                "[WebRip 1080p HEVC-10bit AAC ASSx2].mkv" % episode)

    def file(self, episode, **kwargs):
        path = self.root / self.title(episode)
        path.write_bytes(b"media")
        return self.fx.media.get_media_info_on_files([str(path)], **kwargs).get(str(path))

    def bound(self):
        return self.fx.get_tmdb_info(mtype=MediaType.TV, tmdbid=216467)

    def assertMapped(self, meta, episodes):
        self.assertEqual(dict(id=216467, skip=False, seasons=[2], episodes=episodes), outcome(meta))
        self.assertEqual(NumberingState.VERIFIED, meta._numbering_result.state)
        self.assertEqual(meta._numbering_result.target_of(meta, meta.tmdb_info),
                         meta._numbering_result.target)
        self.assertEqual(episodes, meta.note["absolute_episode_mapping"]["target_episodes"])

    def test_preview_and_cold_warm_bound_files_share_numbering(self):
        for source, target in ((40, 13), (41, 14), (42, 15)):
            self.fx.cache.clear()
            with self.subTest(source=source):
                self.assertMapped(self.fx.media.get_media_info(self.title(source)), [target])
                self.assertMapped(self.file(source), [target])
                self.fx.cache.clear()
                self.assertMapped(self.file(source), [target])
                self.assertMapped(self.fx.media.get_media_info(self.title(source)), [target])
                self.assertMapped(self.file(source, tmdb_info=self.bound()), [target])

    def test_first_season_finale_and_second_season_start(self):
        first = self.fx.media.get_media_info(self.title(27))
        self.assertEqual(dict(id=216467, skip=False, seasons=[1], episodes=[27]), outcome(first))
        self.assertMapped(self.fx.media.get_media_info(self.title(28)), [1])

    def test_verified_llm_season_still_converts_the_total(self):
        self.fx.llm(tmdb_id=216467, tmdb_season=2, season_verified=True,
                    season_evidence="season_name")
        self.assertMapped(self.fx.media.get_media_info(self.title(42)), [15])
        self.assertMapped(self.file(42, tmdb_info=self.bound()), [15])

    def test_verified_llm_episode_remains_authoritative(self):
        self.fx.llm(tmdb_id=216467, tmdb_season=2, tmdb_episode=14, season_verified=True)
        meta = self.fx.media.get_media_info(self.title(42))
        self.assertEqual(dict(id=216467, skip=False, seasons=[2], episodes=[14]), outcome(meta))

    def test_download_task_constrains_the_converted_episode(self):
        context = dict(tmdb_info=self.bound(), seasons=[2], episodes=[15], numbering="tmdb")
        self.assertMapped(self.file(42, download_context=context), [15])
        context["episodes"] = [14]
        result = self.file(42, download_context=context)
        self.assertTrue(result is None or result.skip_reason or not result.tmdb_info)

    def test_range_in_one_season_preserves_every_episode(self):
        self.assertMapped(self.fx.media.get_media_info(self.title("E40-E42")), [13, 14, 15])
        self.assertMapped(self.file("E40-E42", tmdb_info=self.bound()), [13, 14, 15])
        self.fx.llm(tmdb_id=216467, tmdb_season=2, tmdb_episode=13, season_verified=True)
        self.assertMapped(self.fx.media.get_media_info(self.title("E40-E42")), [13, 14, 15])
        self.assertMapped(self.file("E40-E42", tmdb_info=self.bound()), [13, 14, 15])

    def test_llm_range_requires_every_converted_episode(self):
        self.fx.llm(tmdb_id=216467, tmdb_season=2, tmdb_episode=13, season_verified=True)
        self.fx.episodes[(216467, 2)] = [dict(episode_number=13), dict(episode_number=15)]
        for meta in (self.fx.media.get_media_info(self.title("E40-E42")),
                     self.file("E40-E42", tmdb_info=self.bound())):
            self.assertTrue(meta.skip_reason)
            self.assertEqual([40, 41, 42], meta.get_episode_list())
        self.assertEqual({}, self.fx.cache)

    def test_range_across_seasons_is_not_collapsed(self):
        for meta in (self.fx.media.get_media_info(self.title("E27-E28")),
                     self.file("E27-E28", tmdb_info=self.bound())):
            self.assertTrue(meta.skip_reason)
            self.assertEqual([27, 28], meta.get_episode_list())

    def test_unavailable_target_episode_retains_source_and_does_not_cache(self):
        for mode in ("query", "missing_episode"):
            with self.subTest(mode=mode):
                self.fx.cache.clear()
                if mode == "query":
                    self.fx.fail.add(("season", 216467, 2))
                else:
                    self.fx.fail.clear()
                    self.fx.episodes[(216467, 2)] = [dict(episode_number=14)]
                for meta in (self.fx.media.get_media_info(self.title(42)),
                             self.file(42, tmdb_info=self.bound())):
                    self.assertTrue(meta.skip_reason)
                    self.assertEqual(42, meta.begin_episode)
                    self.assertIsNone(meta.begin_season)
                self.assertEqual({}, self.fx.cache)

    def test_missing_prior_season_does_not_invent_an_offset(self):
        self.fx.tv(216467, "夜樱家的大作战", original="Yozakura-san Chi no Daisakusen",
                   genres=(16,), seasons={1: 27, 3: 24})
        meta = self.fx.media.get_media_info(self.title(42))
        self.assertTrue(meta.skip_reason)
        self.assertEqual(42, meta.begin_episode)

    def test_total_beyond_recorded_seasons_remains_unresolved(self):
        meta = self.fx.media.get_media_info(self.title(99))
        self.assertTrue(meta.skip_reason)
        self.assertEqual(99, meta.begin_episode)
        self.assertEqual({}, self.fx.cache)

    def test_manual_targets_and_explicit_seasons_are_preserved(self):
        meta = self.file(42, tmdb_info=self.bound(), season=1,
                         episode_format=EpisodeFormat(None, "42"))
        self.assertEqual(dict(id=216467, skip=False, seasons=[1], episodes=[42]), outcome(meta))
        for title in ("Yozakura-san Chi no Daisakusen S02E15 [1080p].mkv",
                      "Yozakura-san Chi no Daisakusen S01E27 [1080p].mkv"):
            with self.subTest(title=title):
                meta = self.fx.media.get_media_info(title)
                self.assertFalse(meta.skip_reason)
                self.assertNotIn("absolute_episode_mapping", meta.note)

    def test_ordinary_tv_does_not_gain_anime_numbering(self):
        self.fx.tv(52, "Example Show", seasons={1: 27, 2: 24})
        meta = self.fx.media.get_media_info("Example Show - 42 [1080p].mkv")
        self.assertEqual(dict(id=52, skip=False, seasons=[1], episodes=[42]), outcome(meta))


class AbsoluteNumberingPipelineTest(unittest.TestCase):
    def setUp(self):
        self.fx = FakeTmdb(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def show(self, seasons):
        self.fx.cache.clear()
        self.fx.tv(91, "Silver Current", genres=(16,), seasons=seasons)
        return self.fx.get_tmdb_info(mtype=MediaType.TV, tmdbid=91)

    @staticmethod
    def title(episode):
        return "[Group] Silver Current - %s [1080p].mkv" % episode

    def file(self, title, directory="", **kwargs):
        path = self.root / directory / title
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"media")
        return self.fx.media.get_media_info_on_files([str(path)], **kwargs).get(str(path))

    def test_different_catalogues_and_season_boundaries(self):
        cases = [({1: 12, 2: 12}, 13, 2, 1),
                 ({1: 24, 2: 12}, 28, 2, 4),
                 ({1: 12, 2: 13, 3: 12}, 26, 3, 1),
                 ({1: 12, 2: 13, 3: 12}, 37, 3, 12),
                 ({1: 52, 2: 12}, 42, 1, 42),
                 ({0: 99, 1: 12, 2: 12}, 15, 2, 3)]
        for seasons, source, season, episode in cases:
            with self.subTest(seasons=seasons, source=source):
                bound = self.show(seasons)
                title = self.title(source)
                expected = dict(id=91, skip=False, seasons=[season], episodes=[episode])
                for meta in (self.fx.media.get_media_info(title),
                             self.file(title), self.file(title, tmdb_info=bound)):
                    self.assertEqual(expected, outcome(meta))

    def test_directory_season_is_a_hint_for_unmarked_file_totals(self):
        bound = self.show({1: 12, 2: 12})
        for source, episode in ((15, 3), (5, 5)):
            with self.subTest(source=source):
                for options in ({}, {"tmdb_info": bound}):
                    meta = self.file(self.title(source), "Season 2", **options)
                    self.assertEqual(dict(id=91, skip=False, seasons=[2], episodes=[episode]), outcome(meta))

    def test_directory_cannot_authorize_an_episode_in_another_season(self):
        bound = self.show({1: 12, 2: 12, 3: 24})
        for options in ({}, {"tmdb_info": bound}):
            meta = self.file(self.title(15), "Season 1", **options)
            self.assertTrue(meta.skip_reason)
            self.assertEqual((1, 15), (meta.begin_season, meta.begin_episode))
            # A local episode that exists in the directory's season stays local.
            meta = self.file(self.title(15), "Season 3", **options)
            self.assertEqual(dict(id=91, skip=False, seasons=[3], episodes=[15]), outcome(meta))

    def test_provider_can_keep_absolute_episode_numbers_inside_a_season(self):
        bound = self.show({1: 12, 2: 12})
        self.fx.episodes[(91, 2)] = [dict(episode_number=n) for n in range(13, 25)]
        title = self.title(15)
        for meta in (self.fx.media.get_media_info(title),
                     self.file(title, tmdb_info=bound),
                     self.file(title, "Season 2", tmdb_info=bound)):
            self.assertEqual(dict(id=91, skip=False, seasons=[2], episodes=[15]), outcome(meta))

    def test_configured_mapping_precedes_automatic_inference(self):
        bound = self.show({1: 12, 2: 12})
        self.fx.media_config["episode_mappings"] = [
            dict(tmdb_id=91, source_season=1, source_begin=13, source_end=24,
                 target_season=1, offset=-12)]
        title = self.title(15)
        for meta in (self.fx.media.get_media_info(title), self.file(title, tmdb_info=bound)):
            self.assertEqual(dict(id=91, skip=False, seasons=[1], episodes=[3]), outcome(meta))
            self.assertEqual([15], meta.note["episode_mapping"]["source_episodes"])


if __name__ == "__main__":
    unittest.main()
