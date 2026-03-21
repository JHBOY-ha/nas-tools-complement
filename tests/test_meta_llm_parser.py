# -*- coding: utf-8 -*-

import os
from unittest import TestCase
from unittest.mock import Mock, patch

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
        self.parser._confidence_threshold = 0.75
        self.parser._client = None

    def test_parse_disabled_should_not_call_client(self):
        self.parser._enabled = False
        with patch.object(self.parser, "_LLMMetaParser__get_client") as mock_client:
            result = self.parser.parse(title="Dune 2021")
        self.assertEqual({}, result)
        mock_client.assert_not_called()

    def test_merge_rule_first_only_fill_missing(self):
        self.parser._mode = "rule_first"
        meta_info = MetaInfo("Dune 2023 1080p")
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
        meta_info = MetaInfo("Dune 2023 1080p")
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
        meta_info = MetaInfo("Some Show S01E01 1080p")
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
        mock_response = Mock()
        mock_choice = Mock()
        mock_choice.message.content = "invalid json content"
        mock_response.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_response

        with patch.object(self.parser, "_LLMMetaParser__is_client_ready", return_value=True), \
                patch.object(self.parser, "_LLMMetaParser__get_client", return_value=mock_client):
            result = self.parser.parse(title="Dune 2021")

        self.assertEqual({}, result)

    def test_get_status_success(self):
        mock_client = Mock()
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_client.chat.completions.create.return_value = mock_response

        with patch.object(self.parser, "_LLMMetaParser__is_client_ready", return_value=True), \
                patch.object(self.parser, "_LLMMetaParser__get_client", return_value=mock_client):
            status = self.parser.get_status()

        self.assertTrue(status)

    def test_get_status_failure(self):
        mock_client = Mock()
        mock_client.chat.completions.create.side_effect = Exception("boom")

        with patch.object(self.parser, "_LLMMetaParser__is_client_ready", return_value=True), \
                patch.object(self.parser, "_LLMMetaParser__get_client", return_value=mock_client):
            status = self.parser.get_status()

        self.assertFalse(status)

    def test_parse_with_search_context_should_attach_external_candidates(self):
        self.parser._search_context_enable = True
        mock_client = Mock()
        mock_response = Mock()
        mock_choice = Mock()
        mock_choice.message.content = "{\"type\":\"movie\"}"
        mock_response.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_response

        with patch.object(self.parser, "_LLMMetaParser__is_client_ready", return_value=True), \
                patch.object(self.parser, "_LLMMetaParser__get_client", return_value=mock_client), \
                patch.object(
                    self.parser,
                    "_LLMMetaParser__build_external_candidates",
                    return_value="{\"tmdb\":[{\"id\":11,\"name\":\"Dune\",\"type\":\"movie\"}]}"
                ):
            self.parser.parse(title="Dune 2021", subtitle="", mtype_hint=MediaType.MOVIE)

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        user_prompt = call_kwargs.get("messages", [{}, {}])[1].get("content", "")
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
        meta_info = MetaInfo("Beyblade X 111")
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
