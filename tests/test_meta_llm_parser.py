# -*- coding: utf-8 -*-

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
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("NASTOOL_CONFIG"):
            root_path = os.path.dirname(os.path.dirname(__file__))
            os.environ["NASTOOL_CONFIG"] = os.path.join(root_path, "config", "config.yaml")

    def setUp(self):
        self.parser = LLMMetaParser()
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
        mock_client.complete_text.return_value = "{\"type\":\"movie\"}"

        with patch.object(self.parser, "_LLMMetaParser__is_client_ready", return_value=True), \
                patch.object(self.parser, "_LLMMetaParser__get_client", return_value=mock_client), \
                patch.object(
                    self.parser,
                    "_LLMMetaParser__build_external_candidates",
                    return_value="{\"tmdb\":[{\"id\":11,\"name\":\"Dune\",\"type\":\"movie\"}]}"
                ):
            self.parser.parse(title="Dune 2021", subtitle="", mtype_hint=MediaType.MOVIE)

        call_kwargs = mock_client.complete_text.call_args.kwargs
        user_prompt = call_kwargs.get("user_prompt", "")
        self.assertIn("external_candidates", user_prompt)

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
