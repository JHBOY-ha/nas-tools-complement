"""Constraint matrix for docs/validation/series-recognition-invariants.md.

Each test is one cell: test_c<constraint>_e<entry>_<scenario>. Tests assert the
documented constraint, not incidental behaviour. Naming gaps outside C1-C6 live in
tests/test_recognition_naming_gaps.py. All provider and LLM boundaries are offline.
Run: python3 -m tests.run_recognition tests.test_recognition_invariants
"""
import csv
import tempfile
import unittest
from pathlib import Path

from app.media.meta import MetaInfo
from app.media.meta.special import download_block_reason
from app.utils import EpisodeFormat
from app.utils.types import MediaType
from tests.recognition_fixture import FakeTmdb, outcome

ROOT = Path(__file__).resolve().parents[1]


def rejected(meta):
    """Source retained: missing from results, unidentified, or carrying a reason."""
    return meta is None or not meta.tmdb_info or bool(meta.skip_reason)


class InvariantCase(unittest.TestCase):
    def setUp(self):
        self.fx = FakeTmdb(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def name(self, title, **kwargs):
        return self.fx.media.get_media_info(title, **kwargs)

    def files(self, *relative, **kwargs):
        paths = []
        for item in relative:
            path = self.root / item
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"media")
            paths.append(str(path))
        result = self.fx.media.get_media_info_on_files(paths, **kwargs)
        return [result.get(path) for path in paths]

    def file(self, relative, **kwargs):
        return self.files(relative, **kwargs)[0]

    def show(self, **kwargs):
        options = dict(tmdb_id=42, name="Show", seasons={1: 12, 2: 12})
        options.update(kwargs)
        return self.fx.tv(options.pop("tmdb_id"), options.pop("name"), **options)

    def bound(self, tmdb_id=42):
        return self.fx.get_tmdb_info(mtype=MediaType.TV, tmdbid=tmdb_id)

    def assertRetained(self, meta, seasons, episodes, forbidden=(), cache=None):
        """UNKNOWN keeps the source: a reason, release numbering, no other work, no cache write."""
        self.assertIsNotNone(meta)
        self.assertTrue(meta.skip_reason, outcome(meta))
        self.assertFalse(meta.tmdb_info, outcome(meta))
        self.assertEqual((seasons, episodes), (meta.get_season_list(), meta.get_episode_list()))
        for tmdb_id in forbidden:
            self.assertNotEqual(tmdb_id, meta.tmdb_id)
        self.assertEqual(cache or {}, self.fx.cache)


def mapping(source_season, begin, end, target_season, offset, tmdb_id=42):
    return {"episode_mappings": [dict(tmdb_id=tmdb_id, source_season=source_season, source_begin=begin,
                                      source_end=end, target_season=target_season, offset=offset)]}


class C1ManualNumberingIsNeverRewritten(InvariantCase):
    def test_c1_e6_manual_episode_not_shifted_by_task_absolute_conversion(self):
        self.show()
        context = dict(tmdb_info=self.bound(), seasons=[2], episodes=list(range(1, 13)), numbering="tmdb")
        meta = self.file("Show - 14 [1080p].mkv", tmdb_info=self.bound(),
                         episode_format=EpisodeFormat(None, "14"), download_context=context)
        self.assertTrue(rejected(meta), outcome(meta))

    def test_c1_e6_manual_season_not_rewritten_by_rss_release_season(self):
        self.show(seasons={1: 12, 2: 12, 3: 12, 4: 12})
        context = dict(tmdb_info=self.bound(), seasons=[2], episodes=list(range(1, 13)), numbering="tmdb",
                       source_title="Show S04 - 05 [1080p]")
        meta = self.file("Show - 05 [1080p].mkv", tmdb_info=self.bound(), season=4,
                         episode_format=EpisodeFormat(None, "5"), download_context=context)
        self.assertTrue(rejected(meta), outcome(meta))

    def test_c1_e6_manual_episode_within_task_fills_only_missing_season(self):
        self.show()
        context = dict(tmdb_info=self.bound(), seasons=[2], episodes=list(range(1, 13)), numbering="tmdb")
        meta = self.file("Show - 05 [1080p].mkv", tmdb_info=self.bound(),
                         episode_format=EpisodeFormat(None, "5"), download_context=context)
        self.assertEqual(dict(id=42, skip=False, seasons=[2], episodes=[5]), outcome(meta))

    def test_c1_e5_configured_mapping_not_applied_to_manual_target(self):
        self.fx.media_config.update(mapping(1, 1, 12, 2, 0))
        self.show()
        meta = self.file("Show.S01E03.1080p.mkv", tmdb_info=self.bound(), season=1,
                         episode_format=EpisodeFormat(None, "3"))
        self.assertEqual(dict(id=42, skip=False, seasons=[1], episodes=[3]), outcome(meta))

    def test_c1_e5_llm_season_binding_not_applied_to_manual_target(self):
        self.show(seasons={1: 12, 2: 12, 3: 12})
        self.fx.llm(tmdb_id=42, tmdb_season=2, tmdb_episode=7, season_verified=True)
        meta = self.file("Show.S03E05.1080p.mkv", tmdb_info=self.bound(), season=3,
                         episode_format=EpisodeFormat(None, "5"))
        self.assertEqual(dict(id=42, skip=False, seasons=[3], episodes=[5]), outcome(meta))

    def test_c1_e4_configured_mapping_not_applied_to_manual_target(self):
        self.fx.media_config.update(mapping(1, 1, 12, 2, 0))
        self.show()
        meta = self.file("Show.S01E03.1080p.mkv", season=1, episode_format=EpisodeFormat(None, "3"))
        self.assertEqual(dict(id=42, skip=False, seasons=[1], episodes=[3]), outcome(meta))

    def test_c1_e4_manual_target_same_for_cold_and_warm_cache(self):
        self.show()
        cold = outcome(self.file("a/Show.S01E03.1080p.mkv", season=2, episode_format=EpisodeFormat(None, "3")))
        warm = outcome(self.file("b/Show.S01E03.1080p.mkv", season=2, episode_format=EpisodeFormat(None, "3")))
        self.assertEqual(dict(id=42, skip=False, seasons=[2], episodes=[3]), cold)
        self.assertEqual(cold, warm)

    def test_c1_e5_directory_season_does_not_override_manual_season(self):
        self.show(seasons={1: 12, 2: 12, 3: 12})
        meta = self.file("Show S02/Show - 05.mkv", tmdb_info=self.bound(), season=3,
                         episode_format=EpisodeFormat(None, "5"))
        self.assertEqual(dict(id=42, skip=False, seasons=[3], episodes=[5]), outcome(meta))

    def test_c1_e5_ordinal_manual_season_only(self):
        # Prior review P2: a season-only correction must not require re-entering EP.
        self.show(seasons={1: 12, 2: 12, 3: 12})
        meta = self.file("Show 4th Season 5.mkv", tmdb_info=self.bound(), season=3)
        self.assertEqual(dict(id=42, skip=False, seasons=[3], episodes=[5]), outcome(meta))

    def test_c1_e7_fractional_manual_target_is_verified_not_rewritten(self):
        self.show(seasons={0: 2, 1: 12})
        meta = self.file("Show.S01E13.5.1080p.mkv", tmdb_info=self.bound(), season=0,
                         episode_format=EpisodeFormat(None, "1"))
        self.assertEqual(dict(id=42, skip=False, seasons=[0], episodes=[1]), outcome(meta))

    def test_c1_e7_fractional_manual_target_outside_task_is_rejected_unchanged(self):
        self.show(seasons={0: 2, 1: 12})
        context = dict(tmdb_info=self.bound(), seasons=[1], episodes=list(range(1, 13)), numbering="tmdb")
        meta = self.file("Show.S01E13.5.1080p.mkv", tmdb_info=self.bound(), season=0,
                         episode_format=EpisodeFormat(None, "1"), download_context=context)
        self.assertTrue(rejected(meta), outcome(meta))
        if meta is not None and meta.tmdb_info:
            self.assertEqual(([0], [1]), (meta.get_season_list(), meta.get_episode_list()))

    def test_c1_e7_special_manual_target(self):
        self.show(seasons={0: 3, 1: 12})
        meta = self.file("Show [OVA01].mkv", tmdb_info=self.bound(), season=0,
                         episode_format=EpisodeFormat(None, "2"))
        self.assertEqual(dict(id=42, skip=False, seasons=[0], episodes=[2]), outcome(meta))


class C2UnknownIsNeitherWidenedNorDowngraded(InvariantCase):
    def query_show(self):
        self.fx.tv(2, "Formal Name", aliases=["The Query Show"])

    def test_c2_e1_unrelated_candidate_detail_failure_does_not_block(self):
        self.fx.tv(1, "Totally Different Show", seasons={1: 5})
        self.fx.noise.append((MediaType.TV, 1))
        self.query_show()
        self.fx.fail.add(("detail", 1))
        meta = self.name("The.Query.Show.S01E02.1080p.WEB-DL.mkv", cache=False)
        self.assertEqual(dict(id=2, skip=False, seasons=[1], episodes=[2]), outcome(meta))

    def test_c2_e4_unrelated_candidate_detail_failure_does_not_block(self):
        self.fx.tv(1, "Totally Different Show", seasons={1: 5})
        self.fx.noise.append((MediaType.TV, 1))
        self.query_show()
        self.fx.fail.add(("detail", 1))
        meta = self.file("The.Query.Show.S01E02.1080p.WEB-DL.mkv")
        self.assertEqual(dict(id=2, skip=False, seasons=[1], episodes=[2]), outcome(meta))

    def test_c2_e1_similar_name_candidate_detail_failure_stays_pending(self):
        # A similar name may still be the work (its aliases are unknown).
        self.fx.tv(1, "The Query Show Talk", seasons={1: 5})
        self.query_show()
        self.fx.fail.add(("detail", 1))
        meta = self.name("The.Query.Show.S01E02.1080p.WEB-DL.mkv", cache=False)
        self.assertRetained(meta, [1], [2], forbidden=(2,))

    def test_c2_e4_similar_name_candidate_detail_failure_stays_pending(self):
        self.fx.tv(1, "The Query Show Talk", seasons={1: 5})
        self.query_show()
        self.fx.fail.add(("detail", 1))
        meta = self.file("The.Query.Show.S01E02.1080p.WEB-DL.mkv")
        self.assertRetained(meta, [1], [2], forbidden=(2,))

    def test_c2_e1_movie_match_not_blocked_by_unrelated_tv_detail_failure(self):
        self.fx.movie(10, "The Grand Voyages", year="2001")
        self.fx.tv(11, "Unrelated Talk Show", year="2010")
        self.fx.noise.append((MediaType.TV, 11))
        self.fx.fail.add(("detail", 11))
        meta = self.name("The.Grand.Voyage.1080p.BluRay.mkv", cache=False)
        self.assertEqual(10, (meta.tmdb_info or {}).get("id"), meta.skip_reason)

    def test_c2_e1_movie_with_similar_tv_detail_failure_stays_pending(self):
        self.fx.movie(10, "The Grand Voyages", year="2001")
        self.fx.tv(11, "The Grand Voyage Talk", year="2010")
        self.fx.fail.add(("detail", 11))
        meta = self.name("The.Grand.Voyage.1080p.BluRay.mkv", cache=False)
        self.assertTrue(meta.skip_reason)
        self.assertFalse(meta.tmdb_info)
        self.assertFalse(self.fx.cache)

    def test_c2_e1_detail_reads_are_limited_per_round(self):
        for number in range(1, 13):
            self.fx.tv(100 + number, "Noise %s" % number)
            self.fx.noise.append((MediaType.TV, 100 + number))
        meta = self.name("Quiet.Harbor.S01E01.1080p.mkv", cache=False)
        rounds = len([call for call in self.fx.calls if call[:2] == ("search", MediaType.TV)])
        self.assertFalse(meta.tmdb_info)
        self.assertLessEqual(len(self.fx.detail_calls()), 6 * rounds)

    def test_c2_e1_named_candidate_beyond_limit_stays_pending(self):
        for number in range(1, 8):
            self.fx.tv(100 + number, "Noise %s" % number)
            self.fx.noise.append((MediaType.TV, 100 + number))
        self.fx.tv(200, "Quiet Harbor Tales")
        meta = self.name("Quiet.Harbor.S01E01.1080p.mkv", cache=False)
        self.assertRetained(meta, [1], [1])

    def test_c2_e1_detail_reads_are_reused_within_one_recognition(self):
        self.show()
        meta = self.name("Show.S01E05.1080p.mkv", cache=False)
        self.assertEqual(42, meta.tmdb_id)
        self.assertEqual(1, self.fx.detail_calls().count(("detail", MediaType.TV, 42)))

    def test_c2_e1_matched_candidate_unknown_numbering_does_not_switch(self):
        self.fx.media_config.update(mapping(4, 1, 12, 1, 0))
        self.show(seasons={1: 12})
        self.fx.tv(43, "Show", year="2015", seasons={1: 12, 2: 12, 3: 12, 4: 12})
        self.fx.fail.add(("season", 42, 1))
        meta = self.name("Show.S04E05.1080p.mkv", cache=False)
        self.assertRetained(meta, [4], [5], forbidden=(43,))

    def test_c2_e1_matched_candidate_detail_unavailable_does_not_switch(self):
        self.show()
        self.fx.tv(43, "Other Formal", aliases=["Show"])
        self.fx.fail.add(("detail", 42))
        meta = self.name("Show.S01E05.1080p.mkv", cache=False)
        self.assertRetained(meta, [1], [5], forbidden=(43,))

    def test_c2_e1_query_failure_is_pending_not_miss(self):
        self.show()
        self.fx.fail.add(("search", "Show"))
        meta = self.name("Show.S01E05.1080p.mkv", cache=False)
        self.assertRetained(meta, [1], [5])

    def test_c2_e2_cached_detail_unavailable_keeps_source_without_new_search(self):
        self.show()
        self.fx.tv(43, "Show", year="2015")
        self.fx.warm("[电视剧]Show-None-1", 42)
        cached = dict(self.fx.cache)
        self.fx.fail.add(("detail", 42))
        meta = self.name("Show.S01E05.1080p.mkv")
        self.assertRetained(meta, [1], [5], forbidden=(43,), cache=cached)
        self.assertFalse([call for call in self.fx.calls if call[0] == "search"])

    def test_c2_e2_cached_mapping_unknown_keeps_source_without_new_search(self):
        self.fx.media_config.update(mapping(4, 1, 12, 1, 0))
        self.show(seasons={1: 12})
        self.fx.tv(43, "Show", year="2015", seasons={1: 12, 2: 12, 3: 12, 4: 12})
        self.fx.warm("[电视剧]Show-None-4", 42)
        cached = dict(self.fx.cache)
        self.fx.fail.add(("season", 42, 1))
        meta = self.name("Show.S04E05.1080p.mkv")
        self.assertRetained(meta, [4], [5], forbidden=(43,), cache=cached)
        self.assertFalse([call for call in self.fx.calls if call[0] == "search"])

    def test_c2_e1_fallback_name_not_used_after_unknown_evidence(self):
        self.fx.media_config.update(mapping(1, 1, 12, 2, 0))
        self.fx.tv(42, "某剧", original="某剧", seasons={1: 12, 2: 12})
        self.fx.tv(43, "Some Show", seasons={1: 12})
        self.fx.fail.add(("season", 42, 2))
        meta = self.name("某剧.Some.Show.S01E02.1080p.mkv", cache=False)
        self.assertRetained(meta, [1], [2], forbidden=(43,))

    def test_c2_e3_llm_id_detail_unavailable_does_not_fall_back_to_name(self):
        self.show()
        self.fx.tv(43, "Show", year="2015")
        self.fx.llm(tmdb_id=42, tmdb_type="tv", candidate_verified=True)
        self.fx.fail.add(("detail", 42))
        meta = self.name("Show.S01E05.1080p.mkv", cache=False)
        self.assertRetained(meta, [1], [5], forbidden=(43,))

    def test_c2_e1_llm_season_binding_unavailable_restores_release_numbering(self):
        self.show(seasons={1: 12, 2: 12})
        self.fx.llm(tmdb_id=42, tmdb_season=2, tmdb_episode=5, season_verified=True)
        self.fx.fail.add(("season", 42, 2))
        meta = self.name("Show.S04E05.1080p.mkv", cache=False)
        self.assertRetained(meta, [4], [5])
        self.assertNotIn("season_binding", meta.note)

    def special_show(self):
        self.show(seasons={0: 2, 1: 12}, genres=(16,), language="ja")
        self.fx.episodes[(42, 0)][0]["name"] = "OVA 1"

    def test_c2_e7_special_parent_search_unrelated_detail_failure(self):
        self.special_show()
        self.fx.movie(90, "Show Business", year="1999")
        self.fx.fail.add(("detail", 90))
        meta = self.file("Show [OVA01].mkv")
        self.assertEqual(dict(id=42, skip=False, seasons=[0], episodes=[1]), outcome(meta))

    def test_c2_e7_special_parent_search_named_detail_failure_stays_pending(self):
        self.special_show()
        self.fx.tv(43, "Show", year="2015", genres=(16,), language="ja")
        self.fx.fail.add(("detail", 43))
        meta = self.file("Show [OVA01].mkv")
        self.assertTrue(rejected(meta), outcome(meta))
        self.assertNotEqual(42, meta.tmdb_id if meta and not meta.skip_reason else None)


class C3OnlyDisprovedIdentitySwitchesWork(InvariantCase):
    def ghosts(self):
        self.fx.tv(1, "Ghosts", year="2021", seasons={1: 10, 2: 10, 3: 10, 4: 10})
        self.fx.tv(2, "Ghosts", year="2019", seasons={1: 6, 2: 6, 3: 6, 4: 6, 5: 6})

    def test_c3_e1_missing_newest_season_does_not_switch_work(self):
        self.ghosts()
        meta = self.name("Ghosts.S05E01.1080p.WEB-DL.mkv", cache=False)
        self.assertRetained(meta, [5], [1], forbidden=(2,))

    def test_c3_e1_cold_and_warm_cache_agree(self):
        self.ghosts()
        self.fx.warm("[电视剧]Ghosts-None-5", 1)
        cached = dict(self.fx.cache)
        warm = self.name("Ghosts.S05E01.1080p.WEB-DL.mkv")
        self.assertRetained(warm, [5], [1], forbidden=(2,), cache=cached)
        self.fx.cache.clear()
        cold = self.name("Ghosts.S05E01.1080p.WEB-DL.mkv")
        self.assertRetained(cold, [5], [1], forbidden=(2,))

    def test_c3_e1_same_name_works_without_distinction_stay_pending(self):
        self.ghosts()
        meta = self.name("Ghosts.S01E01.1080p.WEB-DL.mkv", cache=False)
        self.assertRetained(meta, [1], [1], forbidden=(1, 2))

    def test_c3_e1_equally_close_fuzzy_works_stay_pending(self):
        self.fx.tv(1, "Harbor Lights", year="2015")
        self.fx.tv(2, "Harbor Lights", year="2021")
        meta = self.name("Harbor.Light.S01E01.1080p.mkv", cache=False)
        self.assertRetained(meta, [1], [1], forbidden=(1, 2))

    def test_c3_e1_year_conflict_may_switch_work(self):
        self.ghosts()
        meta = self.name("Ghosts.2019.S01E01.1080p.mkv", cache=False)
        self.assertEqual(dict(id=2, skip=False, seasons=[1], episodes=[1]), outcome(meta))

    def test_c3_e1_anime_genre_conflict_may_switch_work(self):
        # A fansub-style name alone parses as TV; the explicit type is the anime evidence.
        self.fx.tv(1, "Show", genres=(18,))
        self.fx.tv(2, "Show", genres=(16,), language="ja")
        meta = self.name("[Lilith-Raws] Show - 01 [Baha][WEB-DL][1080p].mkv", mtype=MediaType.ANIME, cache=False)
        self.assertEqual(2, (meta.tmdb_info or {}).get("id"), meta.skip_reason)

    def test_c3_e3_llm_id_work_missing_season_does_not_fall_back_to_namesake(self):
        self.ghosts()
        self.fx.llm(tmdb_id=1, tmdb_type="tv", candidate_verified=True)
        meta = self.name("Ghosts.S05E01.1080p.WEB-DL.mkv", cache=False)
        self.assertRetained(meta, [5], [1], forbidden=(2,))

    def test_c3_e9_imdb_work_missing_season_is_retained(self):
        self.ghosts()
        meta = MetaInfo("Ghosts.S05E01.1080p.WEB-DL.mkv", use_llm=False)
        self.assertFalse(self.fx.media.prepare_bound_release(meta, self.bound(1)))
        self.assertTrue(meta.skip_reason or not meta.tmdb_info)
        self.assertEqual(([5], [1]), (meta.get_season_list(), meta.get_episode_list()))

    def test_c3_e9_imdb_work_with_season_passes_final_admission(self):
        self.ghosts()
        meta = MetaInfo("Ghosts.S04E02.1080p.WEB-DL.mkv", use_llm=False)
        self.assertTrue(self.fx.media.prepare_bound_release(meta, self.bound(1)))
        self.assertEqual(dict(id=1, skip=False, seasons=[4], episodes=[2]), outcome(meta))
        self.assertIsNone(download_block_reason(meta))

    def test_c3_e7_ordinal_same_name_works_missing_season_do_not_choose(self):
        # The title interpretation is disproved by type, leaving only the
        # question of which same-name "Show" has S04.
        self.fx.movie(3, "Show 4th", year="2015")
        self.fx.tv(1, "Show", year="2021", seasons={1: 12, 2: 12, 3: 12})
        self.fx.tv(2, "Show", year="2010", seasons={1: 12, 2: 12, 3: 12, 4: 12})
        meta = self.file("Show 4th Season 5.mkv")
        self.assertTrue(meta.skip_reason)
        self.assertNotEqual(2, meta.tmdb_id)
        self.assertFalse(self.fx.cache)


class C3UserSampleCyberpunk(InvariantCase):
    """Committed minimal fixture for the user's Cyberpunk Edgerunners S02 sample (no local CSV)."""
    directory = "赛伯朋克边缘行者2Cyberpunk.Edgerunners.S02.1080p.NF.WEB-DL.DUAL.DDP5.1.H.264-VARYG"
    episodes = ("Cyberpunk.Edgerunners.S02E01.Last.Chance.for.a.Slow.Dance.1080p.NF.WEB-DL.DUAL.DDP5.1.H.264-VARYG.mkv",
                "Cyberpunk.Edgerunners.S02E03 021 1080p.NF.WEB-DL.DUAL.DDP5.1.H.264-VARYG.mkv",
                "Cyberpunk.Edgerunners.S02E10.Then.Everyone.Went.Home.1080p.NF.WEB-DL.DUAL.DDP5.1.H.264-VARYG.mkv")

    def catalogue(self, with_s02):
        self.fx.tv(105248, "赛博朋克：边缘行者", original="サイバーパンク エッジランナーズ",
                   aliases=["Cyberpunk: Edgerunners"], year="2022", genres=(16, 10765), language="ja",
                   seasons={1: 10, 2: 10} if with_s02 else {1: 10})

    def recognize(self):
        return self.files(*["%s/%s" % (self.directory, name) for name in self.episodes])

    def test_c3_e4_sample_with_tmdb_s02_is_recognized(self):
        self.catalogue(with_s02=True)
        for meta, episode in zip(self.recognize(), (1, 3, 10)):
            self.assertEqual(dict(id=105248, skip=False, seasons=[2], episodes=[episode]), outcome(meta))

    def test_c3_e4_sample_without_tmdb_s02_keeps_source(self):
        self.catalogue(with_s02=False)
        for meta, episode in zip(self.recognize(), (1, 3, 10)):
            self.assertRetained(meta, [2], [episode])

    def test_c3_e4_sample_without_tmdb_s02_never_switches_to_alias_work(self):
        self.catalogue(with_s02=False)
        self.fx.tv(999, "Edgerunners Live", aliases=["Cyberpunk: Edgerunners"], year="2026", seasons={1: 8, 2: 8})
        for meta, episode in zip(self.recognize(), (1, 3, 10)):
            self.assertRetained(meta, [2], [episode], forbidden=(999,))

    def test_c3_e4_other_formal_name_ties_real_alias_and_stays_pending(self):
        # F6: formal names and aliases are one tier; "both have S02" decides nothing.
        self.catalogue(with_s02=True)
        self.fx.tv(999, "Cyberpunk Edgerunners", year="2026", seasons={1: 8, 2: 8})
        for meta, episode in zip(self.recognize(), (1, 3, 10)):
            self.assertRetained(meta, [2], [episode], forbidden=(999, 105248))

    def test_c3_e1_explicit_year_selects_the_unique_exact_work(self):
        self.catalogue(with_s02=True)
        self.fx.tv(999, "Cyberpunk Edgerunners", year="2026", seasons={1: 8, 2: 8})
        meta = self.name("Cyberpunk.Edgerunners.2022.S02E01.1080p.NF.WEB-DL.mkv", cache=False)
        self.assertEqual(dict(id=105248, skip=False, seasons=[2], episodes=[1]), outcome(meta))


class C4CacheStoresOnlyAdmittedIdentities(InvariantCase):
    def test_c4_e1_release_group_title_is_never_a_cache_key(self):
        self.fx.tv(5, "Lilith-Raws", genres=(16,), language="ja")
        self.name("[Lilith-Raws] [Lilith-Raws][01][1080p].mkv")
        self.assertFalse([key for key in self.fx.cache if "Lilith" in key], self.fx.cache)

    def test_c4_e4_release_group_title_is_never_a_cache_key(self):
        self.fx.tv(5, "Lilith-Raws", genres=(16,), language="ja")
        self.file("[Lilith-Raws] [Lilith-Raws][01][1080p].mkv")
        self.assertFalse([key for key in self.fx.cache if "Lilith" in key], self.fx.cache)

    def test_c4_e1_pending_result_is_not_cached(self):
        self.fx.media_config.update(mapping(4, 1, 12, 1, 0))
        self.show(seasons={1: 12})
        self.fx.fail.add(("season", 42, 1))
        self.name("Show.S04E05.1080p.mkv")
        self.assertFalse(self.fx.cache)

    def test_c4_e5_ordinal_resolution_is_not_cached(self):
        self.show(seasons={1: 12, 2: 12, 3: 12, 4: 12})
        meta = self.file("Show 4th Season 5.mkv", tmdb_info=self.bound())
        self.assertEqual(dict(id=42, skip=False, seasons=[4], episodes=[5]), outcome(meta))
        self.assertFalse(self.fx.cache)

    def test_c4_e2_cache_hit_reverifies_numbering(self):
        self.fx.media_config.update(mapping(4, 1, 19, 1, 66))
        self.show(seasons={1: 85})
        self.fx.warm("[电视剧]Show-None-4", 42)
        meta = self.name("Show.S04E01.1080p.mkv")
        self.assertEqual(dict(id=42, skip=False, seasons=[1], episodes=[67]), outcome(meta))

    def test_c4_e5_task_bound_file_does_not_write_manual_cache(self):
        self.show()
        context = dict(tmdb_info=self.bound(), seasons=[1], episodes=[5], numbering="tmdb")
        self.file("Show.S01E05.1080p.mkv", tmdb_info=self.bound(), download_context=context)
        self.assertFalse(self.fx.cache)


class C5SnapshotMatchesFinalNumbering(InvariantCase):
    def test_c5_e6_task_season_does_not_prove_missing_season(self):
        self.show(seasons={1: 12, 2: 12})
        context = dict(tmdb_info=self.bound(), seasons=[3], episodes=[], numbering="tmdb")
        meta = self.file("Show - 05 [1080p].mkv", tmdb_info=self.bound(), download_context=context)
        self.assertTrue(rejected(meta), outcome(meta))

    def test_c5_e6_manual_episode_task_season_does_not_prove_missing_season(self):
        self.show(seasons={1: 12, 2: 12})
        context = dict(tmdb_info=self.bound(), seasons=[3], episodes=[], numbering="tmdb")
        meta = self.file("Show - 05 [1080p].mkv", tmdb_info=self.bound(),
                         episode_format=EpisodeFormat(None, "5"), download_context=context)
        self.assertTrue(rejected(meta), outcome(meta))

    def test_c5_e6_manual_episode_task_season_verified_by_live_episodes(self):
        # The manifest omits S03, but TMDB's live season lists the manual EP.
        self.show(seasons={1: 12, 2: 12})
        self.fx.episodes[(42, 3)] = [dict(episode_number=n, season_number=3, show_id=42, name=str(n))
                                     for n in range(1, 13)]
        context = dict(tmdb_info=self.bound(), seasons=[3], episodes=[], numbering="tmdb")
        meta = self.file("Show - 05 [1080p].mkv", tmdb_info=self.bound(),
                         episode_format=EpisodeFormat(None, "5"), download_context=context)
        self.assertEqual(dict(id=42, skip=False, seasons=[3], episodes=[5]), outcome(meta))

    def test_c5_e6_task_episode_fill_is_verified(self):
        self.show()
        context = dict(tmdb_info=self.bound(), seasons=[2], episodes=[13], numbering="tmdb")
        meta = self.file("Show.S02.1080p.mkv", tmdb_info=self.bound(), download_context=context)
        self.assertTrue(rejected(meta), outcome(meta))

    def test_c5_e5_configured_mapping_snapshot_matches_final(self):
        self.fx.media_config.update(mapping(4, 1, 12, 1, 0))
        self.show(seasons={1: 12})
        meta = self.file("Show.S04E05.1080p.mkv", tmdb_info=self.bound())
        self.assertEqual(dict(id=42, skip=False, seasons=[1], episodes=[5]), outcome(meta))
        self.assertIsNone(download_block_reason(meta))

    def test_c5_e1_llm_season_binding_is_verified_and_admitted(self):
        self.show(seasons={1: 12, 2: 12})
        self.fx.llm(tmdb_id=42, tmdb_season=2, tmdb_episode=5, season_verified=True)
        meta = self.name("Show.S04E05.1080p.mkv", cache=False)
        self.assertEqual(dict(id=42, skip=False, seasons=[2], episodes=[5]), outcome(meta))
        self.assertIsNone(download_block_reason(meta))


class C6RangesAreNeverNarrowed(InvariantCase):
    def test_c6_e4_multiseason_file_is_rejected(self):
        self.show(seasons={1: 12, 2: 12, 3: 12})
        meta = self.file("Show.S01-S03.E01.1080p.mkv")
        self.assertTrue(rejected(meta), outcome(meta))

    def test_c6_e6_multiseason_file_not_narrowed_by_task(self):
        self.show(seasons={1: 12, 2: 12, 3: 12})
        context = dict(tmdb_info=self.bound(), seasons=[2], episodes=[1], numbering="tmdb")
        meta = self.file("Show.S01-S03.E01.1080p.mkv", tmdb_info=self.bound(), download_context=context)
        self.assertTrue(rejected(meta), outcome(meta))

    def test_c6_e6_chinese_season_range_not_erased_by_rss_fallback(self):
        # Prior review P1.
        self.fx.tv(63639, "The Expanse", seasons={1: 10, 2: 13, 3: 13})
        info = self.fx.get_tmdb_info(mtype=MediaType.TV, tmdbid=63639)
        context = dict(tmdb_info=info, seasons=[2], episodes=[1], numbering="tmdb",
                       source_title="The Expanse 第1季")
        meta = self.file("The Expanse 第1-3季 第01集.mkv", tmdb_info=info, download_context=context)
        self.assertTrue(rejected(meta), outcome(meta))

    def test_c6_e1_llm_single_season_cannot_narrow_season_range(self):
        self.show(seasons={1: 12, 2: 12, 3: 12})
        self.fx.llm(tmdb_id=42, tmdb_season=2, season_verified=True)
        meta = self.name("Show.S01-S03.E01.1080p.mkv", cache=False)
        self.assertTrue(rejected(meta), outcome(meta))
        self.assertEqual([1, 2, 3], meta.get_season_list())

    def test_c6_e1_multi_episode_mapping_requires_every_episode(self):
        self.fx.media_config.update(mapping(4, 1, 19, 1, 66))
        self.show(seasons={1: 68})
        meta = self.name("Show.S04E01-E03.1080p.mkv", cache=False)
        self.assertRetained(meta, [4], [1, 2, 3])

    def test_c6_e5_multi_episode_bound_not_narrowed(self):
        self.show()
        meta = self.file("Show.S01E01-E03.1080p.mkv", tmdb_info=self.bound())
        self.assertEqual(dict(id=42, skip=False, seasons=[1], episodes=[1, 2, 3]), outcome(meta))

    def test_c6_e7_fractional_range_is_pending(self):
        self.show(seasons={0: 2, 1: 14})
        meta = self.file("Show.S01E13.5-E14.5.1080p.mkv", tmdb_info=self.bound())
        self.assertTrue(rejected(meta), outcome(meta))


class PriorReviews(InvariantCase):
    def test_prior_parent_title_wins_over_grandparent(self):
        self.fx.tv(63639, "The Expanse", seasons={1: 10})
        self.fx.tv(119100, "Bocchi the Rock!", genres=(16,), language="ja", seasons={1: 12})
        for relative in ("TV/The Expanse/01.mkv", "Bocchi the Rock (2022)/The Expanse/01.mkv"):
            with self.subTest(relative=relative):
                meta = self.file(relative)
                self.assertEqual(63639, (meta.tmdb_info or {}).get("id") if meta else None)


class UserSamples(InvariantCase):
    """Optional: the full local sample table, when docs/validation is present."""
    expected_csv = ROOT / "docs" / "validation" / "transfer-test-results.csv"

    @unittest.skipUnless(expected_csv.exists(), "sample expectations are local validation data")
    def test_samples_match_user_expectations(self):
        self.fx.tv(105248, "赛博朋克：边缘行者", original="サイバーパンク エッジランナーズ",
                   aliases=["Cyberpunk: Edgerunners"], year="2022", genres=(16, 10765), language="ja",
                   seasons={1: 10, 2: 10})
        self.fx.movie(274, "沉默的羔羊", original="The Silence of the Lambs", year="1991")
        self.fx.movie(530915, "1917", year="2019")
        self.fx.movie(157336, "星际穿越", original="Interstellar", year="2014")
        with open(self.expected_csv, encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        groups = {}
        for row in rows:
            parent, name = row["源路径"].split("/")[-2:]
            groups.setdefault(parent, []).append((row, "%s/%s" % (parent, name)))
        for parent, items in groups.items():
            for (row, _), meta in zip(items, self.files(*[relative for _, relative in items])):
                with self.subTest(file=row["文件名"]):
                    self.assertFalse(rejected(meta), meta and meta.skip_reason)
                    self.assertEqual(int(row["期望TMDB ID"]), meta.tmdb_info.get("id"))
                    if row["期望季"]:
                        self.assertEqual(row["期望季"], meta.get_season_string())
                        self.assertEqual(row["期望集"], meta.get_episode_string())


if __name__ == "__main__":
    unittest.main()
