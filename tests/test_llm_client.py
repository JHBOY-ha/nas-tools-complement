# -*- coding: utf-8 -*-

import os
import sys
import types
from unittest import TestCase
from unittest.mock import Mock, patch

import requests

if not os.environ.get("NASTOOL_CONFIG"):
    _ROOT_PATH = os.path.dirname(os.path.dirname(__file__))
    os.environ["NASTOOL_CONFIG"] = os.path.join(_ROOT_PATH, "config", "config.yaml")

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

from app.utils.llm_client import LLMClient


class LLMClientTest(TestCase):
    def test_api_root_builds_chat_completion_request(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [
                {"message": {"content": "{\"ok\": true}"}}
            ]
        }
        with patch("app.utils.llm_client.requests.post", return_value=response) as post:
            client = LLMClient({
                "base_url": "https://api.example/v1",
                "api_key": "key",
                "model": "gpt-test",
                "timeout": 9
            })
            result = client.complete_json("system", "user", max_tokens=128)

        self.assertEqual({"ok": True}, result)
        args, kwargs = post.call_args
        self.assertEqual("https://api.example/v1/chat/completions", args[0])
        self.assertEqual("Bearer key", kwargs["headers"]["Authorization"])
        self.assertEqual("gpt-test", kwargs["json"]["model"])
        self.assertEqual(128, kwargs["json"]["max_tokens"])
        self.assertEqual("system", kwargs["json"]["messages"][0]["content"])
        self.assertIs(kwargs["verify"], True)

    def test_full_chat_completion_url_is_used_without_duplicate_path(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [
                {"message": {"content": "{\"ok\": true}"}}
            ]
        }
        with patch("app.utils.llm_client.requests.post", return_value=response) as post:
            client = LLMClient({
                "base_url": "https://openrouter.ai/api/v1/chat/completions?trace=1",
                "api_key": "key",
                "model": "openai/gpt-test"
            })
            result = client.complete_json("system", "user", max_tokens=256)

        self.assertEqual({"ok": True}, result)
        args, kwargs = post.call_args
        self.assertEqual("https://openrouter.ai/api/v1/chat/completions?trace=1", args[0])
        self.assertEqual("Bearer key", kwargs["headers"]["Authorization"])
        self.assertEqual("system", kwargs["json"]["messages"][0]["content"])
        self.assertIs(kwargs["verify"], True)

    def test_missing_config_is_not_ready(self):
        client = LLMClient({"base_url": "", "api_key": "", "model": ""})
        self.assertFalse(client.is_ready())
        self.assertEqual("", client.complete_text("system", "user"))

    def test_http_error_returns_empty_text(self):
        response = requests.Response()
        response.status_code = 401
        response._content = b'{"error":{"message":"invalid api key"}}'
        with patch("app.utils.llm_client.requests.post", return_value=response), \
                patch("app.utils.llm_client.log.warn") as warn:
            client = LLMClient({
                "base_url": "https://api.example/v1",
                "api_key": "key",
                "model": "gpt-test"
            })
            self.assertEqual("", client.complete_text("system", "user"))
        warning = warn.call_args.args[0]
        self.assertIn("status=401", warning)
        self.assertNotIn("status=none", warning)
        self.assertIn("invalid api key", warning)

    def test_invalid_json_returns_none(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [
                {"message": {"content": "not json"}}
            ]
        }
        with patch("app.utils.llm_client.requests.post", return_value=response):
            client = LLMClient({
                "base_url": "https://api.example/v1",
                "api_key": "key",
                "model": "gpt-test"
            })
            self.assertIsNone(client.complete_json("system", "user"))

    def test_status_succeeds_when_choices_only_contain_reasoning(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{
                "message": {
                    "content": "",
                    "reasoning_content": "Okay, the user"
                },
                "finish_reason": "length"
            }]
        }
        with patch("app.utils.llm_client.requests.post", return_value=response) as post, \
                patch("app.utils.llm_client.log.info") as info:
            client = LLMClient({
                "base_url": "https://api.deepseek.com/",
                "api_key": "key",
                "model": "deepseek-v4-flash"
            })
            self.assertTrue(client.get_status())

        self.assertEqual(4, post.call_args.kwargs["json"]["max_tokens"])
        self.assertNotIn("thinking", post.call_args.kwargs["json"])
        message = info.call_args.args[0]
        self.assertIn("reasoning_content_len=14", message)
        self.assertIn("finish_reason=length", message)

    def test_configured_thinking_mode_is_sent_explicitly(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": "OK"}}]
        }
        with patch("app.utils.llm_client.requests.post", return_value=response) as post:
            client = LLMClient({
                "base_url": "https://api.deepseek.com/",
                "api_key": "key",
                "model": "deepseek-v4-flash",
                "thinking": "disabled"
            })
            self.assertTrue(client.get_status())

        self.assertEqual(
            {"type": "disabled"},
            post.call_args.kwargs["json"]["thinking"]
        )

    def test_completion_does_not_treat_reasoning_as_final_content(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{
                "message": {
                    "content": "",
                    "reasoning_content": "private reasoning"
                },
                "finish_reason": "length"
            }]
        }
        with patch("app.utils.llm_client.requests.post", return_value=response), \
                patch("app.utils.llm_client.log.info") as info:
            client = LLMClient({
                "base_url": "https://api.example/v1",
                "api_key": "key",
                "model": "reasoning-model"
            })
            self.assertEqual("", client.complete_text("system", "user"))

        message = info.call_args.args[0]
        self.assertIn("补全请求最终内容为空", message)
        self.assertIn("reasoning_content_len=17", message)
        self.assertNotIn("private reasoning", message)
