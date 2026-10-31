"""Offline regressions for LLM release evidence and cached work identities.

Run through ``tests.run_recognition`` so configuration and databases are temporary.
Only external LLM/TMDB responses are substituted; JSON normalization, candidate
verification, merge and the public recognition entry points remain real.
"""
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from app.helper.meta_helper import CACHE_EXPIRE_TIMESTAMP_STR, MetaHelper
from app.media.meta import MetaInfo
from app.media.meta.llm_parser import LLMMetaParser
from app.utils.types import MatchMode, MediaType
from tests.recognition_fixture import FakeTmdb


class LlmCrossingsTest(TestCase):
    def setUp(self):
        self.fx = FakeTmdb(self)
        # FakeTmdb mocks media's external alias lookup, but metainfo still uses
        # this real singleton. Restore it so enabled/mode/cache cannot leak.
        self.parser = LLMMetaParser()
        self.parser_state = dict(self.parser.__dict__)
        self.addCleanup(self.restore_parser)
        self.parser._enabled = True
        self.parser._mode = "conservative"
        self.parser._parse_cache = {}

    def restore_parser(self):
        self.parser.__dict__.clear()
        self.parser.__dict__.update(self.parser_state)

    @contextmanager
    def llm_response(self, result, candidates=()):
        client = Mock()
        client.complete_text.return_value = json.dumps(result)
        payload = {"tmdb": list(candidates)}
        with patch.object(self.parser, "_LLMMetaParser__is_client_ready", return_value=True), \
                patch.object(self.parser, "_LLMMetaParser__get_client", return_value=client), \
                patch.object(self.parser, "_LLMMetaParser__build_external_candidates",
                             return_value=(json.dumps(payload), payload)):
            yield client

    def recognize(self, title, entry="title", **options):
        if entry == "title":
            return self.fx.media.get_media_info(title, **options)
        with tempfile.TemporaryDirectory(prefix="llm-crossings-") as directory:
            path = Path(directory) / title
            path.write_bytes(b"offline media fixture")
            result = self.fx.media.get_media_info_on_files([str(path)], **options).get(str(path))
            self.assertEqual(b"offline media fixture", path.read_bytes())
            return result

    def assert_numbering(self, meta, season, episode, end_episode=None):
        self.assertEqual((season, None, 1),
                         (meta.begin_season, meta.end_season, meta.total_seasons))
        self.assertEqual((episode, end_episode, (end_episode or episode) - episode + 1),
                         (meta.begin_episode, meta.end_episode, meta.total_episodes))

    def test_fix03_llm_end_episode_cannot_expand_explicit_single_episode(self):
        self.fx.tv(123, "Example", seasons={1: 12})
        for mode in ("conservative", "balanced"):
            with self.subTest(mode=mode):
                self.parser._mode = mode
                with self.llm_response({"type": "tv", "begin_season": 1,
                                        "begin_episode": 3, "end_episode": 5}):
                    meta = self.recognize("Example S01E03.mkv", cache=False)
                self.assertEqual(123, meta.tmdb_id)
                self.assertIsNone(meta.skip_reason)
                self.assert_numbering(meta, 1, 3)

    def test_fix03_llm_end_season_cannot_expand_explicit_single_season(self):
        self.fx.tv(123, "Example", seasons={1: 12, 2: 12, 3: 12, 4: 12})
        for mode in ("conservative", "balanced"):
            with self.subTest(mode=mode):
                self.parser._mode = mode
                with self.llm_response({"type": "tv", "begin_season": 2, "end_season": 4,
                                        "begin_episode": 3}):
                    meta = self.recognize("Example S02E03.mkv", cache=False)
                self.assertEqual(123, meta.tmdb_id)
                self.assertIsNone(meta.skip_reason)
                self.assert_numbering(meta, 2, 3)

    def test_fix03_missing_rule_numbering_can_still_be_filled(self):
        self.fx.tv(123, "Example", seasons={1: 12, 2: 12})
        title = "Example 1080p.mkv"
        rule = MetaInfo(title, use_llm=False)
        self.assertIsNone(rule.begin_season)
        self.assertIsNone(rule.begin_episode)
        with self.llm_response({"type": "tv", "tmdb_id": 123, "tmdb_type": "tv",
                                "begin_season": 2, "begin_episode": 3, "end_episode": 5},
                               [{"id": 123, "name": "Example", "type": "tv"}]):
            meta = self.recognize(title, cache=False)
        self.assertEqual(123, meta.tmdb_id)
        self.assertIsNone(meta.skip_reason)
        self.assert_numbering(meta, 2, 3, 5)

    def test_llm_season_fill_does_not_relabel_an_existing_release_total(self):
        self.fx.tv(123, "Example Anime", genres=(16,), seasons={1: 12, 2: 12})
        title = "[Group] Example Anime - 15 [1080p].mkv"
        for entry in ("title", "file"):
            with self.subTest(entry=entry):
                self.fx.cache.clear()
                with self.llm_response({"type": "tv", "tmdb_id": 123, "tmdb_type": "tv",
                                        "begin_season": 2},
                                       [{"id": 123, "name": "Example Anime", "type": "tv"}]):
                    meta = self.recognize(title, entry=entry)
                self.assertEqual(123, meta.tmdb_id)
                self.assertIsNone(meta.skip_reason)
                self.assert_numbering(meta, 2, 3)
                self.assertEqual([15], meta.note["absolute_episode_mapping"]["source_episodes"])

    def test_fix03_name_modes_keep_existing_year_numbers_and_resource_fields(self):
        for mode, expected in (("conservative", "Original Show"), ("balanced", "Renamed Show")):
            with self.subTest(mode=mode):
                self.parser._mode = mode
                with self.llm_response({"type": "tv", "en_name": "Renamed Show", "year": "2021",
                                        "begin_season": 2, "begin_episode": 9,
                                        "resource_pix": "2160p"}):
                    meta = MetaInfo("Original.Show.2020.S01E03.1080p.mkv")
                self.assertEqual(expected, meta.en_name)
                self.assertEqual("2020", meta.year)
                self.assertEqual("1080p", meta.resource_pix)
                self.assert_numbering(meta, 1, 3)
                self.assertEqual(["Original Show"], meta.note["llm"]["rule_names"])
                self.assertEqual(["Renamed Show"], meta.note["llm"]["llm_names"])

    def test_fix04_generated_arc_name_cannot_authorize_a_cross_season_mapping(self):
        self.fx.tv(123, "Example", seasons={1: 12, 2: 12, 3: 12})
        candidate = {"id": 123, "name": "Example", "type": "tv", "seasons": [
            {"n": 1, "name": "Dawn Arc"}, {"n": 2, "name": "Winter Arc"},
            {"n": 3, "name": "Night Arc"}]}
        for mode in ("conservative", "balanced"):
            with self.subTest(mode=mode):
                self.parser._mode = mode
                with self.llm_response({"type": "tv", "en_name": "Example Night Arc",
                                        "tmdb_id": 123, "tmdb_type": "tv",
                                        "tmdb_season": 3, "tmdb_episode": 3}, [candidate]):
                    meta = self.recognize("Example S02E03.mkv", cache=False)
                self.assertTrue(meta.note["llm"]["candidate_verified"])
                self.assertFalse(meta.note["llm"].get("season_verified"))
                self.assertNotIn("season_binding", meta.note)
                self.assertEqual(123, meta.tmdb_id)
                self.assertIsNone(meta.skip_reason)
                self.assert_numbering(meta, 2, 3)

    def test_fix04_arc_name_in_source_can_authorize_a_cross_season_mapping(self):
        self.fx.tv(123, "Example", seasons={1: 12, 2: 12, 3: 12})
        candidate = {"id": 123, "name": "Example", "type": "tv", "seasons": [
            {"n": 2, "name": "Winter Arc"}, {"n": 3, "name": "Night Arc"}]}
        with self.llm_response({"type": "tv", "tmdb_id": 123, "tmdb_type": "tv",
                                "tmdb_season": 3, "tmdb_episode": 3}, [candidate]):
            meta = self.recognize("Example S02E03.mkv", subtitle="Night Arc", cache=False)
        self.assertEqual(123, meta.tmdb_id)
        self.assertIsNone(meta.skip_reason)
        self.assertTrue(meta.note["llm"]["season_verified"])
        self.assert_numbering(meta, 3, 3)

    def test_fix04_rule_season_formats_cannot_be_overridden_without_source_evidence(self):
        self.fx.tv(123, "Example", seasons={1: 12, 2: 12, 3: 12})
        candidate = {"id": 123, "name": "Example", "type": "tv",
                     "seasons": [{"n": 2, "name": "Winter Arc"}, {"n": 3, "name": "Night Arc"}]}
        for title in ("Example s02e03.mkv", "Example 2x03.mkv", "Example 第II季 E03.mkv"):
            with self.subTest(title=title), self.llm_response(
                    {"type": "tv", "tmdb_id": 123, "tmdb_type": "tv",
                     "tmdb_season": 3, "tmdb_episode": 3}, [candidate]):
                meta = self.recognize(title, cache=False)
                self.assertIsNone(meta.skip_reason)
                self.assert_numbering(meta, 2, 3)

    def test_fix05_explicit_year_conflict_rejects_llm_id_and_falls_back(self):
        self.fx.tv(10, "Echo Nights", year="2010", seasons={1: 12, 2: 12}, season_years={2: "2011"})
        self.fx.tv(20, "Echo Nights", year="2020", seasons={1: 12, 2: 12})
        self.fx.media._rmt_match_mode = MatchMode.STRICT
        candidates = [{"id": 10, "name": "Echo Nights", "type": "tv", "year": "2010",
                       "seasons": [{"n": 2, "year": "2011", "eps": 12}]}]
        for entry in ("title", "file"):
            with self.subTest(entry=entry):
                self.fx.cache.clear()
                with self.llm_response({"type": "tv", "tmdb_id": 10, "tmdb_type": "tv"}, candidates):
                    meta = self.recognize("Echo.Nights.2020.S02E01.mkv", entry)
                self.assertEqual(20, meta.tmdb_id)
                self.assertIsNone(meta.skip_reason)
                self.assert_numbering(meta, 2, 1)

    def test_fix05_missing_season_year_is_unknown_and_does_not_switch_work(self):
        detail = self.fx.tv(10, "Echo Nights", year="2010", seasons={1: 12, 2: 12})
        detail["seasons"][1]["air_date"] = None
        self.fx.tv(20, "Echo Nights", year="2020", seasons={1: 12, 2: 12})
        candidate = {"id": 10, "name": "Echo Nights", "type": "tv", "year": "2010",
                     "seasons": [{"n": 2, "eps": 12}]}
        for entry in ("title", "file"):
            with self.subTest(entry=entry):
                self.fx.cache.clear()
                with self.llm_response({"type": "tv", "tmdb_id": 10, "tmdb_type": "tv"}, [candidate]):
                    meta = self.recognize("Echo.Nights.2020.S02E01.mkv", entry)
                self.assertFalse(meta.tmdb_info)
                self.assertTrue(meta.skip_reason)
                self.assert_numbering(meta, 2, 1)
                self.assertFalse(self.fx.cache)

    def test_fix05_matching_cache_id_cannot_bypass_explicit_year_validation(self):
        self.fx.tv(10, "Echo Nights", year="2010", seasons={2: 12})
        self.fx.tv(20, "Echo Nights", year="2020", seasons={2: 12})
        title = "Echo.Nights.2020.S02E01.mkv"
        key = self.fx.media._Media__make_cache_key(MetaInfo(title, use_llm=False))
        for entry in ("title", "file"):
            with self.subTest(entry=entry):
                self.fx.cache.clear()
                self.fx.warm(key, 10, title="Echo Nights")
                with self.llm_response({"type": "tv", "tmdb_id": 10, "tmdb_type": "tv"},
                                       [{"id": 10, "type": "tv", "name": "Echo Nights"}]):
                    found = self.recognize(title, entry)
                self.assertEqual(20, found.tmdb_id)
                self.assertIsNone(found.skip_reason)

    def test_fix05_malformed_season_manifest_is_unknown(self):
        detail = self.fx.tv(10, "Example", year="2010", seasons={2: 12})
        for seasons in (None, [None]):
            with self.subTest(seasons=seasons):
                detail["seasons"] = seasons
                with self.llm_response({"type": "tv", "tmdb_id": 10, "tmdb_type": "tv"},
                                       [{"id": 10, "type": "tv", "name": "Example"}]):
                    result = self.recognize("Example.2020.S02E01.mkv", cache=False)
                self.assertTrue(result.skip_reason)
                self.assertFalse(result.tmdb_info)
                self.assertFalse(self.fx.cache)

    def test_fix05_matching_season_year_accepts_an_older_series(self):
        self.fx.tv(10, "Echo Nights", year="2010", seasons={1: 12, 2: 12}, season_years={2: "2020"})
        candidate = {"id": 10, "name": "Echo Nights", "type": "tv", "year": "2010",
                     "seasons": [{"n": 2, "year": "2020", "eps": 12}]}
        for entry in ("title", "file"):
            with self.subTest(entry=entry):
                self.fx.cache.clear()
                with self.llm_response({"type": "tv", "tmdb_id": 10, "tmdb_type": "tv"}, [candidate]):
                    meta = self.recognize("Echo.Nights.2020.S02E01.mkv", entry)
                self.assertEqual(10, meta.tmdb_id)
                self.assertIsNone(meta.skip_reason)
                self.assert_numbering(meta, 2, 1)

    def test_fix05_verified_arc_target_season_owns_year_check(self):
        self.fx.tv(10, "Example", year="2010", seasons={2: 12, 3: 12},
                   season_years={2: "2011", 3: "2020"})
        candidate = {"id": 10, "name": "Example", "type": "tv",
                     "seasons": [{"n": 2, "name": "Winter Arc"}, {"n": 3, "name": "Night Arc"}]}
        with self.llm_response({"type": "tv", "tmdb_id": 10, "tmdb_type": "tv",
                                "tmdb_season": 3, "tmdb_episode": 3}, [candidate]):
            result = self.recognize("Example.2020.S02E03.mkv", subtitle="Night Arc")
        self.assertEqual(10, result.tmdb_id)
        self.assertIsNone(result.skip_reason)
        self.assert_numbering(result, 3, 3)

    def test_fix05_subtitle_year_is_source_evidence_not_inference(self):
        self.fx.tv(10, "Example", year="2010", seasons={1: 12})
        self.fx.tv(20, "Example", year="2020", seasons={1: 12})
        with self.llm_response({"type": "tv", "year": 2010, "tmdb_id": 10, "tmdb_type": "tv"},
                               [{"id": 10, "name": "Example", "type": "tv"}]):
            result = self.recognize("Example S01E01.mkv", subtitle="2020", strict=True)
        self.assertFalse(result.note["llm"]["inferred_year"])
        self.assertEqual(20, result.tmdb_id)
        self.assertIsNone(result.skip_reason)

    def test_fix07_conflicting_cached_id_is_rechecked_or_rejected(self):
        self.fx.tv(1, "Example", seasons={1: 12})
        self.fx.tv(2, "Example", seasons={1: 12})
        title = "Example S01E01.mkv"
        key = self.fx.media._Media__make_cache_key(MetaInfo(title, use_llm=False))
        candidate = {"id": 2, "name": "Example", "type": "tv"}
        for entry in ("title", "file"):
            with self.subTest(entry=entry):
                self.fx.cache.clear()
                with self.llm_response({"type": "tv", "tmdb_id": 2, "tmdb_type": "tv"}, [candidate]):
                    cold = self.recognize(title, entry)
                    self.assertEqual(2, cold.tmdb_id)
                    self.assertIsNone(cold.skip_reason)
                    self.fx.cache.clear()
                    self.fx.warm(key, 1, title="Example")
                    warm = self.recognize(title, entry)
                self.assertTrue(warm.note["llm"]["candidate_verified"])
                # Candidate membership is not absolute proof. Either revalidate
                # the new identity or retain the source with an explicit reason.
                self.assertTrue((warm.tmdb_id == 2 and not warm.skip_reason)
                                or (not warm.tmdb_info and bool(warm.skip_reason)),
                                "a conflicting cache must not silently choose ID %s" % warm.tmdb_id)
                self.assert_numbering(warm, 1, 1)

    def test_fix07_matching_cached_and_llm_ids_still_work_without_name_search(self):
        self.fx.tv(2, "Example", seasons={1: 12})
        title = "Example S01E01.mkv"
        key = self.fx.media._Media__make_cache_key(MetaInfo(title, use_llm=False))
        for entry in ("title", "file"):
            with self.subTest(entry=entry):
                self.fx.cache.clear()
                self.fx.calls.clear()
                self.fx.warm(key, 2, title="Example")
                with self.llm_response({"type": "tv", "tmdb_id": 2, "tmdb_type": "tv"},
                                       [{"id": 2, "name": "Example", "type": "tv"}]):
                    meta = self.recognize(title, entry)
                self.assertEqual(2, meta.tmdb_id)
                self.assertIsNone(meta.skip_reason)
                self.assert_numbering(meta, 1, 1)
                self.assertFalse([call for call in self.fx.calls if call[0] == "search"])


class ExpiredIdentityReadTest(TestCase):
    def test_fix08_expired_identity_is_empty_for_the_first_reader(self):
        helper = MetaHelper()
        saved = dict(helper.__dict__)
        self.addCleanup(self.restore_helper, helper, saved)
        helper._meta_data = {"expired": {"id": 10, CACHE_EXPIRE_TIMESTAMP_STR: 1000}}
        helper._tmdb_cache_expire = True
        with patch("app.helper.meta_helper.time.time", return_value=2000):
            first = helper.get_meta_data_by_key("expired")
            second = helper.get_meta_data_by_key("expired")
        self.assertEqual({}, first)
        self.assertEqual({}, second)
        self.assertNotIn("expired", helper._meta_data)

    @staticmethod
    def restore_helper(helper, state):
        helper.__dict__.clear()
        helper.__dict__.update(state)
