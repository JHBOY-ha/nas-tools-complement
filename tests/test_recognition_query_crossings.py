"""Offline regressions for identity fallbacks and equivalent provider queries.

Run through ``tests.run_recognition`` so imports use disposable configuration.
Provider-operation counts describe this deterministic workflow, not accuracy or
the number of HTTP calls made by a live TMDB/Bangumi deployment.
"""
import os
import re
import tempfile
import unittest
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlparse
from unittest.mock import Mock, patch

from app.media.media import Media
from app.media.meta.llm_parser import LLMMetaParser
from app.media.meta.recognition import (
    RecognitionPending, cached_recognition_query, query_tmdb, recognition_query_scope,
)
from app.media.tmdbv3api import Movie, Search, TMDb, TV
from app.utils.types import MatchMode, MediaType
from config import Config
from tests.recognition_fixture import FakeTmdb, outcome


class IdentityFallbackCrossingsTest(unittest.TestCase):
    def setUp(self):
        self.fx = FakeTmdb(self)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.parser = LLMMetaParser()
        previous = dict(self.parser.__dict__)
        self.stack.callback(self.restore_parser, previous)
        self.parser._enabled = False
        self.parser._search_context_enable = True
        self.parser._parse_cache = {}
        # Keep the public recognition path and real alias/subject processing;
        # only the raw Bangumi response boundary is replaced in each test.
        self.stack.enter_context(patch("app.media.media.LLMMetaParser", return_value=self.parser))

    def restore_parser(self, previous):
        self.parser.__dict__.clear()
        self.parser.__dict__.update(previous)

    def recognize(self, title, file=False):
        if not file:
            return self.fx.media.get_media_info(title, cache=False)
        with tempfile.TemporaryDirectory(prefix="recognition-query-crossings-") as directory:
            path = Path(directory) / title
            path.write_bytes(b"offline fixture")
            found = self.fx.media.get_media_info_on_files([str(path)])[str(path)]
            self.assertEqual(b"offline fixture", path.read_bytes())
            return found

    def test_keyword_movie_cannot_shadow_tv_episode_or_season_pack(self):
        self.fx.movie(10, "Quiet Harbor", year="2020")
        self.fx.tv(20, "宁静港湾", year="2020", seasons={1: 12})
        self.fx.media._search_keyword = True
        with patch.object(self.parser, "_LLMMetaParser__search_bangumi_candidates", return_value=[
                {"id": 101, "name_cn": "宁静港湾", "name": "静かな港"}]), \
                patch("app.media.media.cacheman", {"tmdb_supply": Mock(get=lambda _: None)}):
            for title in ("Quiet.Harbor.S01E01.mkv", "Quiet.Harbor.S01.Complete.mkv"):
                for file in (False, True):
                    for keyword_is_movie in (False, True):
                        with self.subTest(title=title, file=file, keyword_is_movie=keyword_is_movie), \
                                patch.object(self.fx.media, "_Media__search_engine",
                                             return_value=("Quiet Harbor", keyword_is_movie)):
                            self.fx.cache.clear()
                            found = self.recognize(title, file=file)
                            self.assertEqual(20, found.tmdb_id, outcome(found))
                            self.assertEqual(MediaType.TV, found.type)
                            self.assertEqual([1], found.get_season_list())
                            if "E01" in title or not file:
                                self.assertFalse(found.skip_reason, outcome(found))
                            else:
                                # A single file containing S01 without an episode
                                # stays protected even after its TV identity is known.
                                self.assertTrue(found.skip_reason)

    def test_bangumi_subject_order_cannot_choose_between_distinct_works(self):
        self.fx.tv(10, "琥珀潮汐旧版", year="2010", genres=(16,), language="ja", seasons={1: 12})
        self.fx.tv(20, "琥珀潮汐新版", year="2020", genres=(16,), language="ja", seasons={1: 12})
        subjects = [
            {"id": 101, "name_cn": "琥珀潮汐旧版", "name": "アンバー・タイズ", "year": "2010"},
            {"id": 202, "name_cn": "琥珀潮汐新版", "name": "アンバー・タイズ", "year": "2020"},
        ]
        for reverse in (False, True):
            for file in (False, True):
                with self.subTest(reverse=reverse, file=file), \
                        patch.object(self.parser, "_LLMMetaParser__search_bangumi_candidates",
                                     return_value=list(reversed(subjects)) if reverse else subjects):
                    self.fx.cache.clear()
                    self.fx.media.meta.update_meta_data.reset_mock()
                    found = self.recognize("Amber.Tides.S01E01.mkv", file=file)
                    self.assertFalse(found.tmdb_id, outcome(found))
                    self.assertTrue(found.skip_reason, "Ambiguous subjects must remain pending")
                    self.assertEqual([1], found.get_season_list())
                    self.assertEqual([1], found.get_episode_list())
                    self.assertFalse(self.fx.cache, "Ambiguous identity must not enter the work cache")
                    self.fx.media.meta.update_meta_data.assert_not_called()

    def test_single_bangumi_subject_still_resolves_through_its_alias(self):
        self.fx.tv(10, "琥珀潮汐", year="2010", genres=(16,), language="ja", seasons={1: 12})
        for file in (False, True):
            with self.subTest(file=file), \
                    patch.object(self.parser, "_LLMMetaParser__search_bangumi_candidates", return_value=[
                        {"id": 101, "name_cn": "琥珀潮汐", "name": "アンバー・タイズ", "year": "2010"}]):
                self.fx.cache.clear()
                found = self.recognize("Amber.Tides.S01E01.mkv", file=file)
                self.assertEqual(dict(id=10, skip=False, seasons=[1], episodes=[1]), outcome(found))
                self.assertEqual(MediaType.ANIME, found.type)

    def test_bangumi_unknown_is_not_an_ordinary_miss(self):
        with patch.object(self.parser, "_LLMMetaParser__search_bangumi_candidates",
                          side_effect=RecognitionPending("候选服务暂不可用")):
            for file in (False, True):
                with self.subTest(file=file):
                    found = self.recognize("Amber.Tides.S01E01.mkv", file=file)
                    self.assertTrue(found.skip_reason)
                    self.assertFalse(found.tmdb_info)
                    self.assertFalse(self.fx.cache)

    def test_chinese_fallback_can_expand_previously_unexpanded_aliases(self):
        chinese = "地狱模式～喜欢速通游戏的玩家在废设定异世界无双～"
        self.fx.tv(42, "真实作品", seasons={1: 12})
        self.fx.media_config["name_aliases"] = {"Hell Mode": chinese, chinese: "真实作品"}
        title = ("[LoliHouse] " + chinese + " / Hell Mode - 06 "
                 "[WebRip 1080p HEVC-10bit AAC][简繁内封字幕]")
        with patch.object(self.parser, "_LLMMetaParser__search_bangumi_candidates", return_value=[]):
            result = self.recognize(title)
        self.assertEqual(42, result.tmdb_id)
        self.assertIsNone(result.skip_reason)


class EquivalentProviderQueriesTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "TMDB_API_KEY": "offline", "TMDB_LANGUAGE": "zh-CN",
            "TMDB_CACHE_ENABLED": "True", "TMDB_DEBUG_ENABLED": "False",
            "TMDB_PROXIES": "{}", "TMDB_DOMAIN": "https://tmdb.invalid/3",
        }))
        self.media = Media.__new__(Media)
        self.media.tmdb = TMDb()
        self.media.search, self.media.tv, self.media.movie = Search(), TV(), Movie()
        self.media.tmdb.cache_clear()
        self.stack.callback(self.media.tmdb.cache_clear)
        self.media.meta = Mock()
        self.media.meta.get_meta_data_by_key.return_value = {}
        self.media._rmt_match_mode = MatchMode.NORMAL
        self.media._search_keyword = self.media._search_tmdbweb = False
        self.parser = LLMMetaParser()
        previous = dict(self.parser.__dict__)
        self.stack.callback(self.restore_parser, previous)
        self.parser._enabled = False
        self.parser._mode = "conservative"
        self.parser._search_context_enable = False
        self.parser._search_max_results = 3
        self.parser._parse_cache = {}
        self.media_config = {}
        self.stack.enter_context(patch.object(Config(), "get_config", side_effect=lambda section=None: {
            "app": {"rmt_tmdbkey": "offline", "tmdb_domain": "https://tmdb.invalid/3"},
            "media": self.media_config,
        }.get(section, {})))
        self.tmdb_events = []
        self.bangumi_events = []
        self.bangumi_items = []
        self.http = self.stack.enter_context(patch(
            "app.media.tmdbv3api.tmdb.requests.request", side_effect=self.tmdb_http))
        self.bangumi_http_mock = self.stack.enter_context(patch(
            "app.utils.http_utils.requests.get", side_effect=self.bangumi_http))
        # Wrap the real SDK, including Search instances constructed by the LLM
        # parser. An exact duplicate may hit its HTTP LRU and still waste all
        # recognition work above that cache; record both boundaries explicitly.
        self.sdk_tv = self.stack.enter_context(patch.object(
            Search, "tv_shows", autospec=True, side_effect=Search.tv_shows))

    def restore_parser(self, previous):
        self.parser.__dict__.clear()
        self.parser.__dict__.update(previous)

    def tmdb_http(self, method, url, **_kwargs):
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        self.tmdb_events.append((parsed.path, {key: value for key, value in params.items() if key != "api_key"}))
        return SimpleNamespace(status_code=200, headers={}, json=lambda: {
            "page": 1, "total_pages": 1, "total_results": 0, "results": []})

    def bangumi_http(self, url, **kwargs):
        self.bangumi_events.append((unquote(urlparse(url).path), kwargs["params"]))
        return SimpleNamespace(ok=True, json=lambda: {"list": self.bangumi_items})

    @staticmethod
    def normalized_query(name):
        # Test-side equivalence ignores presentation punctuation and case; all
        # requests below use the same TV type, language, year and first page.
        return re.sub(r"[\W_]+", "", name).casefold()

    def sdk_queries(self):
        return [call.args[1]["query"] for call in self.sdk_tv.call_args_list]

    def assert_queries_once(self, expected):
        sdk_queries = [self.normalized_query(query) for query in self.sdk_queries()]
        http_queries = [self.normalized_query(params["query"][0]) for _, params in self.tmdb_events]
        expected_counts = Counter(self.normalized_query(query) for query in expected)
        evidence = {"sdk_queries": self.sdk_queries(), "tmdb_http": self.tmdb_events,
                    "bangumi_http": self.bangumi_events}
        # Keep the transport assertion separate: with the old exact-query SDK
        # cache it can pass while SDK-side semantic duplication still fails.
        self.assertEqual(expected_counts, Counter(http_queries), evidence)
        self.assertEqual(expected_counts, Counter(sdk_queries), evidence)

    def test_main_configured_rule_llm_and_bangumi_names_are_semantically_unique(self):
        self.parser._mode = "balanced"
        self.parser._search_context_enable = True
        self.media_config["name_aliases"] = {"译名": "ECHO!"}
        self.bangumi_items = [{"id": 1, "name_cn": "译名", "name": "echo"}]
        with patch.object(self.parser, "parse", return_value={"cn_name": "译名", "en_name": "Echo"}):
            found = self.media.get_media_info("Echo S01E01.mkv", mtype=MediaType.TV, cache=False)
        self.assertFalse(found.tmdb_id)
        self.assert_queries_once(["译名", "Echo"])
        self.assertEqual(1, self.bangumi_http_mock.call_count)

    def test_chinese_fallback_does_not_repeat_english_or_same_fuzzy_word(self):
        title = ("[LoliHouse] 地狱模式～喜欢速通游戏的玩家在废设定异世界无双～ / Hell Mode - 06 "
                 "[WebRip 1080p HEVC-10bit AAC][简繁内封字幕]")
        with patch.object(self.parser, "parse", return_value={}):
            found = self.media.get_media_info(title, mtype=MediaType.TV, cache=False)
        self.assertFalse(found.tmdb_id)
        self.assert_queries_once(["Hell Mode", "Hell", "地狱模式～喜欢速通游戏的玩家在废设定异世界无双～"])
        self.assertFalse(self.bangumi_events)

    def test_llm_context_and_main_fallback_reuse_equivalent_tmdb_and_bangumi_queries(self):
        self.parser._enabled = True
        self.parser._search_context_enable = True
        self.parser._base_url = "https://llm.invalid/v1"
        self.parser._api_key = self.parser._model = "offline"
        client = Mock()
        client.complete_text.return_value = "{}"
        with patch.object(self.parser, "_LLMMetaParser__get_client", return_value=client):
            found = self.media.get_media_info("Missing S01E01.mkv", mtype=MediaType.TV, cache=False)
        self.assertFalse(found.tmdb_id)
        client.complete_text.assert_called_once()
        # Explicit TV makes enhanced and main searches equivalent. Their old
        # URLs differed only by omitted/default page=1, so SDK LRU did not help.
        with self.subTest(provider="bangumi"):
            self.assertEqual(1, self.bangumi_http_mock.call_count, self.bangumi_events)
        with self.subTest(provider="tmdb"):
            self.assert_queries_once(["Missing"])

    def test_unknown_query_stops_aliases_and_is_not_cached_as_a_miss(self):
        self.parser._mode = "balanced"
        self.parser._search_context_enable = True
        self.media_config["name_aliases"] = {"译名": "Echo"}
        self.http.side_effect = RuntimeError("offline unavailable")
        with patch.object(self.parser, "parse", return_value={"cn_name": "译名", "en_name": "Echo"}):
            for attempt in (1, 2):
                found = self.media.get_media_info("Echo S01E01.mkv", mtype=MediaType.TV, cache=False)
                self.assertFalse(found.tmdb_id, outcome(found))
                self.assertTrue(found.skip_reason, "An unavailable provider is UNKNOWN, not a miss")
                self.assertEqual(["译名"] * attempt, self.sdk_queries())
                self.assertEqual(attempt, self.http.call_count)
                self.bangumi_http_mock.assert_not_called()
                self.media.meta.update_meta_data.assert_not_called()

    def test_query_scope_shares_default_page_copies_results_and_clears_on_exit(self):
        response = SimpleNamespace(status_code=200, headers={}, json=lambda: {
            "page": 1, "total_pages": 1, "total_results": 1,
            "results": [{"id": 42, "name": "Show", "genres": [{"id": 16}]}],
        })
        self.http.side_effect = [RuntimeError("offline unavailable"), response, response, response]
        with recognition_query_scope():
            with self.assertRaisesRegex(RuntimeError, "offline unavailable"):
                query_tmdb(self.media.search, "tv_shows", {"query": "Show"})
            first = query_tmdb(self.media.search, "tv_shows", {"query": "Show"})
            first[0].name = "changed"
            first[0].genres[0].id = 99
            first.clear()
            with recognition_query_scope():
                again = query_tmdb(self.media.search, "tv_shows", {"query": "Show", "page": 1})
                self.assertEqual("Show", again[0].name)
                self.assertEqual(16, again[0].genres[0].id)
            query_tmdb(self.media.search, "tv_shows", {"query": "Show"})
            self.assertEqual(2, self.http.call_count)
            # The generic boundary permits unavailable None responses, while
            # a genuine empty response may be reused within this scope.
            self.assertIsNone(cached_recognition_query(("optional",), lambda: None))
            self.assertEqual([], cached_recognition_query(("optional",), lambda: []))
            self.assertEqual([], cached_recognition_query(("optional",), lambda: ["unexpected retry"]))

        # Clear only the SDK's independent HTTP LRU so it cannot hide a leaked
        # recognition scope, including one exited by an exception.
        self.media.tmdb.cache_clear()
        with self.assertRaisesRegex(ValueError, "abort recognition"):
            with recognition_query_scope():
                query_tmdb(self.media.search, "tv_shows", {"query": "Show"})
                self.assertEqual(3, self.http.call_count)
                raise ValueError("abort recognition")
        self.media.tmdb.cache_clear()
        with recognition_query_scope():
            query_tmdb(self.media.search, "tv_shows", {"query": "Show"})
        self.assertEqual(4, self.http.call_count)

    def test_real_sdk_query_cache_keeps_type_year_language_and_key_separate(self):
        variations = [
            ("movies", {"query": "Show"}, {}),
            ("tv_shows", {"query": "Show", "first_air_date_year": "2020"}, {}),
            ("tv_shows", {"query": "Show"}, {"TMDB_LANGUAGE": "en-US"}),
            ("tv_shows", {"query": "Show"}, {"TMDB_API_KEY": "offline-other"}),
        ]
        with recognition_query_scope():
            query_tmdb(self.media.search, "tv_shows", {"query": "Show"})
            for expected_requests, (kind, params, environment) in enumerate(variations, start=2):
                with self.subTest(kind=kind, params=params, environment=environment), \
                        patch.dict(os.environ, environment):
                    query_tmdb(self.media.search, kind, params)
                    self.assertEqual(expected_requests, self.http.call_count)
                    query_tmdb(self.media.search, kind, params)
                    self.assertEqual(expected_requests, self.http.call_count)
            query_tmdb(self.media.search, "tv_shows", {"query": "Show"})
            self.assertEqual(5, self.http.call_count)
