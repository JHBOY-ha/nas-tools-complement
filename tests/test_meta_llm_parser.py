# -*- coding: utf-8 -*-

import json
import os
import sys
import types
from unittest import TestCase
from unittest.mock import Mock, patch

if "parse" not in sys.modules:
    parse_stub = types.ModuleType("parse")
    parse_stub.parse = lambda *args, **kwargs: None
    sys.modules["parse"] = parse_stub

if "dateparser" not in sys.modules:
    dateparser_stub = types.ModuleType("dateparser")
    dateparser_stub.parse = lambda value: None
    sys.modules["dateparser"] = dateparser_stub

if "cn2an" not in sys.modules:
    cn2an_stub = types.ModuleType("cn2an")
    cn2an_stub.cn2an = lambda value, mode=None: int(value)
    sys.modules["cn2an"] = cn2an_stub

if "bencode" not in sys.modules:
    bencode_stub = types.ModuleType("bencode")
    bencode_stub.bdecode = lambda value: {}
    sys.modules["bencode"] = bencode_stub

if "cacheout" not in sys.modules:
    cacheout_stub = types.ModuleType("cacheout")

    class _Cache:
        def __init__(self, *args, **kwargs):
            self._values = {}

        def get(self, key, default=None):
            return self._values.get(key, default)

        def set(self, key, value, *args, **kwargs):
            self._values[key] = value

        def delete(self, key):
            self._values.pop(key, None)

    class _CacheManager:
        def __init__(self, *args, **kwargs):
            pass

    cacheout_stub.Cache = _Cache
    cacheout_stub.LRUCache = _Cache
    cacheout_stub.CacheManager = _CacheManager
    sys.modules["cacheout"] = cacheout_stub

if "pyquery" not in sys.modules:
    pyquery_stub = types.ModuleType("pyquery")

    class _PyQuery:
        def __init__(self, *args, **kwargs):
            pass

    pyquery_stub.PyQuery = _PyQuery
    sys.modules["pyquery"] = pyquery_stub

if "zhconv" not in sys.modules:
    zhconv_stub = types.ModuleType("zhconv")
    zhconv_stub.convert = lambda value, locale=None: value
    sys.modules["zhconv"] = zhconv_stub

if "undetected_chromedriver" not in sys.modules:
    uc_stub = types.ModuleType("undetected_chromedriver")
    uc_stub.find_chrome_executable = lambda: None
    uc_stub.ChromeOptions = object
    uc_stub.Chrome = object
    sys.modules["undetected_chromedriver"] = uc_stub

if "webdriver_manager.chrome" not in sys.modules:
    webdriver_manager_stub = types.ModuleType("webdriver_manager")
    chrome_stub = types.ModuleType("webdriver_manager.chrome")

    class _ChromeDriverManager:
        def install(self):
            return ""

    chrome_stub.ChromeDriverManager = _ChromeDriverManager
    sys.modules["webdriver_manager"] = webdriver_manager_stub
    sys.modules["webdriver_manager.chrome"] = chrome_stub

if "pyvirtualdisplay" not in sys.modules:
    pyvirtualdisplay_stub = types.ModuleType("pyvirtualdisplay")

    class _Display:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return self

        def stop(self):
            return None

    pyvirtualdisplay_stub.Display = _Display
    sys.modules["pyvirtualdisplay"] = pyvirtualdisplay_stub

from app.media.meta import MetaInfo
from app.media.meta.llm_parser import LLMMetaParser
from app.utils.types import MediaType


class LLMMetaParserTest(TestCase):
    def test_tsdm_search_starts_with_title_not_release_group(self):
        queries = self.parser._LLMMetaParser__build_search_queries(
            "【TSDM字幕组】[Re:从零开始的异世界生活 第4季][14]"
            "[HEVC-10bit 1080p AAC][MKV][简日内封字幕]"
            "[Re Zero kara Hajimeru Isekai Seikatsu 4th Season]")
        self.assertIn("从零开始", queries[0])
        self.assertNotIn("TSDM", queries[0])

    def test_inferred_year_is_distinct_from_explicit_release_year(self):
        for title, inferred in [("Example S01E12", True), ("Example 2026 S01E12", False)]:
            meta = MetaInfo(title, use_llm=False)
            with patch.object(self.parser, "parse", return_value={"year": "2025"}):
                self.parser.merge_into(meta, title)
            self.assertEqual(inferred, meta.note["llm"]["inferred_year"])

    @classmethod
    def setUpClass(cls):
        if not os.environ.get("NASTOOL_CONFIG"):
            root_path = os.path.dirname(os.path.dirname(__file__))
            os.environ["NASTOOL_CONFIG"] = os.path.join(root_path, "config", "config.yaml")

    def setUp(self):
        self.parser = LLMMetaParser()
        self._parser_state = dict(self.parser.__dict__)
        self.addCleanup(self._restore_parser)
        self.parser._enabled = True
        self.parser._mode = "rule_first"
        self.parser._base_url = "https://api.openai.com/v1"
        self.parser._api_key = "test-key"
        self.parser._model = "gpt-4o-mini"
        self.parser._timeout = 20
        self.parser._max_tokens = 1024
        self.parser._thinking = ""
        self.parser._client_config = {}
        self.parser._confidence_threshold = 0.75
        self.parser._client = None
        self.parser._parse_cache = {}

    def _restore_parser(self):
        self.parser.__dict__.clear()
        self.parser.__dict__.update(self._parser_state)

    def test_parse_disabled_should_not_call_client(self):
        self.parser._enabled = False
        with patch.object(self.parser, "_LLMMetaParser__get_client") as mock_client:
            result = self.parser.parse(title="Dune 2021")
        self.assertEqual({}, result)
        mock_client.assert_not_called()

    def test_merge_rule_first_only_fill_missing(self):
        self.parser._mode = "rule_first"
        meta_info = MetaInfo("Dune 2023 1080p", use_llm=False)
        meta_info.year = "2023"
        self.assertIsNone(meta_info.resource_effect)

        llm_result = {
            "type": MediaType.MOVIE,
            "en_name": "Dune",
            "year": "2024",
            "resource_effect": "HDR",
            "confidence": 0.9,
            "field_confidence": {"resource_effect": 0.9}
        }

        with patch.object(self.parser, "parse", return_value=llm_result):
            self.parser.merge_into(meta_info=meta_info, title=meta_info.org_string)

        self.assertEqual("2023", meta_info.year)
        self.assertEqual("HDR", meta_info.resource_effect)
        self.assertTrue(meta_info.note.get("llm", {}).get("applied"))

    def test_verified_candidate_corrects_default_type_but_respects_hint(self):
        self.parser._mode = "rule_first"
        result = {"type": MediaType.ANIME, "tmdb_type": "tv", "tmdb_id": 123,
                  "candidate_verified": True, "confidence": .95}
        for hint, expected in [(None, MediaType.ANIME), (MediaType.MOVIE, MediaType.MOVIE)]:
            meta = MetaInfo("Example 2024", use_llm=False)
            with patch.object(self.parser, "parse", return_value=result):
                self.parser.merge_into(meta, meta.org_string, mtype_hint=hint)
            self.assertEqual(expected, meta.type)

    def test_merge_llm_first_override_existing(self):
        self.parser._mode = "llm_first"
        meta_info = MetaInfo("Dune 2023 1080p", use_llm=False)
        meta_info.year = "2023"
        meta_info.resource_pix = "1080p"

        llm_result = {
            "type": MediaType.MOVIE,
            "year": "2024",
            "resource_pix": "2160p",
            "confidence": 0.95,
            "field_confidence": {"year": 0.95, "resource_pix": 0.95}
        }

        with patch.object(self.parser, "parse", return_value=llm_result):
            self.parser.merge_into(meta_info=meta_info, title=meta_info.org_string)

        self.assertEqual("2024", meta_info.year)
        self.assertEqual("2160p", meta_info.resource_pix)

    def test_merge_hybrid_with_confidence_threshold(self):
        self.parser._mode = "hybrid"
        self.parser._confidence_threshold = 0.8
        meta_info = MetaInfo("Some Show S01E01 1080p", use_llm=False)
        meta_info.resource_pix = "1080p"

        llm_result = {
            "type": MediaType.TV,
            "resource_pix": "2160p",
            "resource_type": "WEB-DL",
            "confidence": 0.7,
            "field_confidence": {
                "resource_pix": 0.6,
                "resource_type": 0.95
            }
        }

        with patch.object(self.parser, "parse", return_value=llm_result):
            self.parser.merge_into(meta_info=meta_info, title=meta_info.org_string)

        self.assertEqual("1080p", meta_info.resource_pix)
        self.assertEqual("WEB-DL", meta_info.resource_type)

    def test_parse_invalid_json_should_fallback(self):
        mock_client = Mock()
        mock_client.complete_text.return_value = "invalid json content"

        with patch.object(self.parser, "_LLMMetaParser__is_client_ready", return_value=True), \
                patch.object(self.parser, "_LLMMetaParser__get_client", return_value=mock_client):
            result = self.parser.parse(title="Dune 2021")

        self.assertEqual({}, result)

    def test_parse_empty_content_should_fallback(self):
        self.parser._parse_cache = {}
        mock_client = Mock()
        mock_client.complete_text.return_value = ""

        with patch.object(self.parser, "_LLMMetaParser__is_client_ready", return_value=True), \
                patch.object(self.parser, "_LLMMetaParser__get_client", return_value=mock_client):
            result = self.parser.parse(title="Dune 2022")

        self.assertEqual({}, result)

    def test_get_status_success(self):
        mock_client = Mock()
        mock_client.get_status.return_value = True

        with patch.object(self.parser, "_LLMMetaParser__is_client_ready", return_value=True), \
                patch.object(self.parser, "_LLMMetaParser__get_client", return_value=mock_client):
            status = self.parser.get_status()

        self.assertTrue(status)

    def test_get_status_failure(self):
        mock_client = Mock()
        mock_client.get_status.side_effect = Exception("boom")

        with patch.object(self.parser, "_LLMMetaParser__is_client_ready", return_value=True), \
                patch.object(self.parser, "_LLMMetaParser__get_client", return_value=mock_client):
            status = self.parser.get_status()

        self.assertFalse(status)

    def test_get_status_uses_temporary_form_config(self):
        temporary_config = {
            "base_url": "https://api.deepseek.com/",
            "api_key": "new-key",
            "model": "deepseek-v4-flash",
            "thinking": "disabled"
        }
        mock_client = Mock()
        mock_client.get_status.return_value = True

        with patch("app.media.meta.llm_parser.LLMClient", return_value=mock_client) as client_cls, \
                patch.object(self.parser, "_LLMMetaParser__get_client") as get_saved_client:
            status = self.parser.get_status(config=temporary_config)

        self.assertTrue(status)
        client_cls.assert_called_once_with(temporary_config)
        get_saved_client.assert_not_called()

    def test_saved_extension_config_reaches_shared_client(self):
        self.parser._client_config = {
            "max_retries": 1,
            "extra_headers": {"X-App": "nas-tools"}
        }

        with patch("app.media.meta.llm_parser.LLMClient") as client_cls:
            self.parser._LLMMetaParser__get_client()

        config = client_cls.call_args.args[0]
        self.assertEqual(1, config["max_retries"])
        self.assertEqual({"X-App": "nas-tools"}, config["extra_headers"])
        self.assertEqual("https://api.openai.com/v1", config["base_url"])

    def test_parse_with_search_context_should_attach_external_candidates(self):
        self.parser._search_context_enable = True
        mock_client = Mock()
        mock_client.complete_text.return_value = "{\"type\":\"movie\",\"tmdb_id\":11,\"tmdb_type\":\"movie\"}"
        candidate_payload = {"tmdb": [{"id": 11, "name": "Dune", "type": "movie"}]}

        with patch.object(self.parser, "_LLMMetaParser__is_client_ready", return_value=True), \
                patch.object(self.parser, "_LLMMetaParser__get_client", return_value=mock_client), \
                patch.object(
                    self.parser,
                    "_LLMMetaParser__build_external_candidates",
                    return_value=(json.dumps(candidate_payload), candidate_payload)
                ):
            result = self.parser.parse(title="Dune 2021", subtitle="", mtype_hint=MediaType.MOVIE)

        call_kwargs = mock_client.complete_text.call_args.kwargs
        user_prompt = call_kwargs.get("user_prompt", "")
        self.assertIn("external_candidates", user_prompt)
        self.assertTrue(result.get("candidate_verified"))

    def test_parse_should_extract_tmdb_id(self):
        result = self.parser._LLMMetaParser__normalize_result({
            "type": "anime",
            "tmdb_id": 226688,
            "tmdb_type": "tv"
        })
        self.assertEqual(226688, result.get("tmdb_id"))
        self.assertEqual("tv", result.get("tmdb_type"))

    def test_merge_should_write_tmdb_id_to_note(self):
        self.parser._enabled = False
        meta_info = MetaInfo("Beyblade X 111", use_llm=False)
        self.parser._enabled = True
        llm_result = {
            "type": MediaType.ANIME,
            "cn_name": "战斗陀螺X",
            "tmdb_id": 226688,
            "tmdb_type": "tv",
            "confidence": 0.9,
            "field_confidence": {}
        }
        with patch.object(self.parser, "parse", return_value=llm_result):
            self.parser.merge_into(meta_info=meta_info, title=meta_info.org_string)
        self.assertEqual(226688, meta_info.note.get("llm", {}).get("tmdb_id"))
        self.assertEqual("tv", meta_info.note.get("llm", {}).get("tmdb_type"))

    def test_build_search_queries_should_strip_episode_and_extension(self):
        queries = self.parser._LLMMetaParser__build_search_queries(
            "[LoliHouse] Yuusha no Kuzu - 10 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
        )

        self.assertTrue(queries)
        self.assertEqual("Yuusha no Kuzu", queries[0])
        self.assertNotIn("Yuusha no Kuzu 10 mkv", queries)

    def test_build_search_queries_should_keep_title_season_number(self):
        queries = self.parser._LLMMetaParser__build_search_queries(
            "Mato Seihei no Slave 2 - 01 [1080p].mkv"
        )

        self.assertTrue(queries)
        self.assertEqual("Mato Seihei no Slave 2", queries[0])

    def test_build_search_queries_should_strip_episode_after_multiple_tail_tags(self):
        queries = self.parser._LLMMetaParser__build_search_queries(
            "[ANi] OVERLORD 第四季 - 04 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
        )

        self.assertTrue(queries)
        self.assertEqual("OVERLORD 第四季", queries[0])
        self.assertNotIn("OVERLORD 第四季 04", queries)

    def test_build_search_queries_should_strip_episode_before_custom_tail_note(self):
        queries = self.parser._LLMMetaParser__build_search_queries(
            "[喵萌奶茶屋&LoliHouse] 金装的薇尔梅 / Kinsou no Vermeil - 01 "
            "[WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
        )

        self.assertTrue(queries)
        self.assertEqual("金装的薇尔梅", queries[0])
        self.assertNotIn("金装的薇尔梅 01", queries)

    def test_build_search_queries_for_fully_bracketed_release_name(self):
        queries = self.parser._LLMMetaParser__build_search_queries(
            "[UHA-WINGS][JoJo's Bizarre Adventure Steel Ball Run][01][1080p HEVC][CHS_JP&CHT_JP].mkv"
        )

        self.assertEqual(["JoJo's Bizarre Adventure Steel Ball Run"], queries)

    def test_build_search_queries_skips_release_group_bracket(self):
        queries = self.parser._LLMMetaParser__build_search_queries(
            "[幻樱字幕组][鬼灭之刃][01][1080p HEVC][简繁内封]"
        )

        self.assertIn("鬼灭之刃", queries)
        self.assertNotIn("幻樱字幕组", queries)

    def test_candidate_payload_serialization_keeps_valid_json_within_budget(self):
        payload = {
            "tmdb": [
                {"id": 45790, "name": "JOJO的奇妙冒险", "type": "tv", "year": "2012",
                 "aliases": ["JoJo alias %d" % index for index in range(20)],
                 "seasons": [{"n": number, "name": "飙马野郎篇%d" % number,
                              "year": "2026", "eps": 12} for number in range(1, 21)]},
                {"id": 226688, "name": "其他候选", "type": "tv",
                 "seasons": [{"n": 1, "name": "第 1 季", "eps": 12}]}
            ],
            "bangumi": [{"id": index, "name_cn": "候选名称" * 20} for index in range(20)]
        }

        text = self.parser._LLMMetaParser__serialize_candidate_payload(payload, budget=900)
        parsed = json.loads(text)

        self.assertTrue(parsed.get("tmdb"))
        self.assertIn("seasons", parsed["tmdb"][0])
        self.assertLessEqual(len(text), 900)

    def test_verify_season_binding_uses_season_name_evidence(self):
        payload = {"tmdb": [{
            "id": 45790, "name": "JOJO的奇妙冒险", "type": "tv",
            "seasons": [
                {"n": 1, "name": "幻影之血和战斗潮流篇", "year": "2012", "eps": 26},
                {"n": 6, "name": "飙马野郎篇", "year": "2026", "eps": 12}
            ]
        }]}
        title = ("[UHA-WINGS][JoJo's Bizarre Adventure Steel Ball Run][01]"
                 "[1080p HEVC][CHS_JP&CHT_JP].mkv")

        verified = self.parser._LLMMetaParser__verify_candidate_binding(
            {"tmdb_id": 45790, "tmdb_type": "tv", "tmdb_season": 6,
             "cn_name": "JoJo的奇妙冒险 飙马野郎"},
            payload, title)

        self.assertTrue(verified.get("season_verified"))
        self.assertEqual(6, verified.get("tmdb_season"))
        self.assertEqual("season_name", verified.get("season_evidence"))
        self.assertEqual("飙马野郎篇", verified.get("tmdb_season_name"))

    def test_verify_season_binding_overrides_wrong_llm_season(self):
        payload = {"tmdb": [{
            "id": 45790, "name": "JOJO的奇妙冒险", "type": "tv",
            "seasons": [
                {"n": 1, "name": "幻影之血和战斗潮流篇", "year": "2012", "eps": 26},
                {"n": 6, "name": "飙马野郎篇", "year": "2026", "eps": 12}
            ]
        }]}
        title = ("[UHA-WINGS][JoJo's Bizarre Adventure Steel Ball Run][01]"
                 "[1080p HEVC][CHS_JP&CHT_JP].mkv")

        verified = self.parser._LLMMetaParser__verify_candidate_binding(
            {"tmdb_id": 45790, "tmdb_type": "tv", "tmdb_season": 1,
             "cn_name": "JoJo的奇妙冒险 飙马野郎"},
            payload, title)

        self.assertTrue(verified.get("season_verified"))
        self.assertEqual(6, verified.get("tmdb_season"))
        self.assertEqual("season_name_override", verified.get("season_evidence"))

    def test_verify_season_binding_rejects_season_outside_candidates(self):
        payload = {"tmdb": [{
            "id": 65942, "name": "Re：从零开始的异世界生活", "type": "tv",
            "seasons": [{"n": 0, "name": "特别篇", "eps": 84},
                        {"n": 1, "name": "第 1 季", "eps": 85}]
        }]}

        verified = self.parser._LLMMetaParser__verify_candidate_binding(
            {"tmdb_id": 65942, "tmdb_type": "tv", "tmdb_season": 4},
            payload, "[Nix-Raws] Re Zero kara Hajimeru Isekai Seikatsu S04E18 [WEB-DL 1080p]")

        self.assertTrue(verified.get("candidate_verified"))
        self.assertFalse(verified.get("season_verified"))

    def test_verify_season_binding_keeps_release_marker_without_evidence(self):
        payload = {"tmdb": [{
            "id": 65942, "name": "Re：从零开始的异世界生活", "type": "tv",
            "seasons": [{"n": 0, "name": "特别篇", "eps": 84},
                        {"n": 1, "name": "第 1 季", "eps": 85}]
        }]}

        verified = self.parser._LLMMetaParser__verify_candidate_binding(
            {"tmdb_id": 65942, "tmdb_type": "tv", "tmdb_season": 1},
            payload, "[Nix-Raws] Re Zero kara Hajimeru Isekai Seikatsu S04E18 [WEB-DL 1080p]")

        self.assertFalse(verified.get("season_verified"))
        self.assertEqual(4, verified.get("release_season"))

    def test_merge_should_write_verified_season_to_note(self):
        meta_info = MetaInfo(
            "[UHA-WINGS][JoJo's Bizarre Adventure Steel Ball Run][01][1080p HEVC].mkv",
            use_llm=False)
        llm_result = {
            "type": MediaType.ANIME,
            "cn_name": "JoJo的奇妙冒险 飙马野郎",
            "tmdb_id": 45790,
            "tmdb_type": "tv",
            "tmdb_season": 6,
            "tmdb_season_name": "飙马野郎篇",
            "tmdb_episode": 1,
            "candidate_verified": True,
            "season_verified": True,
            "season_evidence": "season_name",
            "confidence": 0.9,
            "field_confidence": {}
        }

        with patch.object(self.parser, "parse", return_value=llm_result):
            self.parser.merge_into(meta_info=meta_info, title=meta_info.org_string)

        llm_note = meta_info.note.get("llm", {})
        self.assertTrue(llm_note.get("season_verified"))
        self.assertEqual(6, llm_note.get("tmdb_season"))
        self.assertEqual("飙马野郎篇", llm_note.get("tmdb_season_name"))
        self.assertEqual(1, llm_note.get("tmdb_episode"))

    def test_extract_year_hint_skips_monthly_release_label(self):
        extract = self.parser._LLMMetaParser__extract_year_hint

        self.assertEqual("2019", extract("Some Show (2019) 1080p"))
        self.assertEqual("2024", extract("[Group] Some Show 2024 S02E03 [WEB-DL]"))
        self.assertIsNone(extract("[Group] Some Show 2026年7月番 [1080p]"))
        self.assertIsNone(extract("[Group] Some Show [1080p]", None))

    def test_prioritize_candidates_puts_matching_year_first(self):
        class _Item:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        old_version = _Item(id=11, name="Dune", release_date="1984-12-14")
        new_version = _Item(id=22, name="Dune", release_date="2021-10-22")
        other = _Item(id=33, name="Dune Prophecy", first_air_date="2024-11-17")

        ordered = self.parser._LLMMetaParser__prioritize_raw_by_year(
            [old_version, new_version, other], "2021")

        self.assertEqual([22, 11, 33], [item.id for item in ordered])
        self.assertEqual([old_version, new_version, other],
                         self.parser._LLMMetaParser__prioritize_raw_by_year(
                             [old_version, new_version, other], None))

    def test_tmdb_candidates_prioritize_matching_year(self):
        class _Item:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        old_version = _Item(id=11, name="Dune", release_date="1984-12-14", genre_ids=[878])
        new_version = _Item(id=22, name="Dune", release_date="2021-10-22", genre_ids=[878])
        search = Mock()
        search.movies.return_value = [old_version, new_version]
        config = Mock()
        config.get_config.side_effect = lambda key: (
            {"rmt_tmdbkey": "test-key", "tmdb_domain": "api.tmdb.org"} if key == "app" else {}
        )
        config.get_proxies.return_value = None

        with patch("app.media.meta.llm_parser.Config", return_value=config), \
                patch("app.media.meta.llm_parser.TMDb"), \
                patch("app.media.meta.llm_parser.Search", return_value=search):
            candidates = self.parser._LLMMetaParser__search_tmdb_candidates(
                query="Dune", mtype_hint=MediaType.MOVIE, year="2021")

        self.assertTrue(candidates)
        self.assertEqual(22, candidates[0]["id"])
        self.assertEqual("2021", candidates[0]["year"])

    def test_normalize_result_keeps_pick_reason(self):
        result = self.parser._LLMMetaParser__normalize_result({
            "type": "movie",
            "tmdb_id": 22,
            "tmdb_type": "movie",
            "tmdb_pick_reason": "片名一致且标题年份2021与候选year相符"
        })

        self.assertEqual("片名一致且标题年份2021与候选year相符", result.get("tmdb_pick_reason"))

    def test_candidate_extra_carries_external_ids_and_votes(self):
        detail = {
            "seasons": [{"season_number": 1, "name": "第 1 季",
                         "air_date": "2016-04-04", "episode_count": 25}],
            "alternative_titles": {"results": [{"iso_3166_1": "CN", "title": "Re：从零开始的异世界生活"}]},
            "external_ids": {"imdb_id": "tt5607616", "tvdb_id": 305089},
            "vote_count": 787
        }
        tv = Mock()
        tv.details.return_value = detail

        with patch("app.media.meta.llm_parser.TV", return_value=tv) as tv_cls:
            extra = self.parser._LLMMetaParser__fetch_tv_candidate_extra(65942)

        self.assertEqual("tt5607616", extra.get("imdb"))
        self.assertEqual(305089, extra.get("tvdb"))
        self.assertEqual(787, extra.get("votes"))
        self.assertEqual([{"n": 1, "name": "第 1 季", "year": "2016", "eps": 25}], extra.get("seasons"))
        self.assertEqual("alternative_titles,external_ids",
                         tv_cls.return_value.details.call_args.kwargs.get("append_to_response"))

    def test_duplicate_candidates_without_external_ids_are_dropped(self):
        official = {"id": 65942, "name": "Re：从零开始的异世界生活", "type": "tv",
                    "imdb": "tt5607616", "tvdb": 305089, "votes": 787}
        duplicate = {"id": 336222, "name": "Re：从零开始的异世界生活", "type": "tv"}
        other = {"id": 1234, "name": "别的剧", "type": "tv"}

        kept = self.parser._LLMMetaParser__filter_duplicate_candidates(
            [official, duplicate, other])

        self.assertEqual([65942, 1234], [item["id"] for item in kept])

    def test_duplicate_candidates_with_year_suffix_are_grouped(self):
        official = {"id": 65942, "name": "Re：从零开始的异世界生活", "type": "tv",
                    "imdb": "tt5607616", "votes": 787}
        duplicate = {"id": 336222, "name": "Re：从零开始的异世界生活（2016）", "type": "tv"}
        ova = {"id": 532321, "name": "Re：从零开始的异世界生活 雪之回忆", "type": "tv"}

        kept = self.parser._LLMMetaParser__filter_duplicate_candidates(
            [official, duplicate, ova])

        self.assertEqual([65942, 532321], [item["id"] for item in kept])

    def test_search_candidates_prefer_authoritative_duplicate(self):
        class _Item:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        official = _Item(id=65942, name="Re：从零开始的异世界生活", first_air_date="2016-04-04",
                         media_type="tv", genre_ids=[16])
        duplicate = _Item(id=336222, name="Re：从零开始的异世界生活", first_air_date="2016-04-03",
                          media_type="tv", genre_ids=[16])
        search = Mock()
        search.tv_shows.return_value = [official, duplicate]
        config = Mock()
        config.get_config.side_effect = lambda key: (
            {"rmt_tmdbkey": "test-key", "tmdb_domain": "api.tmdb.org"} if key == "app" else {}
        )
        config.get_proxies.return_value = None
        extras = {65942: {"imdb": "tt5607616", "votes": 787}, 336222: {}}

        with patch("app.media.meta.llm_parser.Config", return_value=config), \
                patch("app.media.meta.llm_parser.TMDb"), \
                patch("app.media.meta.llm_parser.Search", return_value=search), \
                patch.object(self.parser, "_LLMMetaParser__fetch_tv_candidate_extra",
                             side_effect=lambda tmdb_id: extras.get(tmdb_id, {})):
            candidates = self.parser._LLMMetaParser__search_tmdb_candidates(
                query="Re Zero", mtype_hint=MediaType.TV)

        self.assertEqual([65942], [item["id"] for item in candidates])
