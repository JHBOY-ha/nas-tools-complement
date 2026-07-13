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
    def test_openai_provider_builds_chat_completion_request(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [
                {"message": {"content": "{\"ok\": true}"}}
            ]
        }
        with patch("app.utils.llm_client.requests.post", return_value=response) as post:
            client = LLMClient({
                "provider": "openai",
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

    def test_anthropic_provider_builds_messages_request(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "content": [
                {"type": "text", "text": "[{\"id\": 1, \"text\": \"你好\"}]"}
            ]
        }
        with patch("app.utils.llm_client.requests.post", return_value=response) as post:
            client = LLMClient({
                "provider": "anthropic",
                "base_url": "https://api.anthropic.com/v1",
                "api_key": "key",
                "model": "claude-test",
                "anthropic_version": "2023-06-01"
            })
            result = client.complete_json("system", "user", max_tokens=256)

        self.assertEqual([{"id": 1, "text": "你好"}], result)
        args, kwargs = post.call_args
        self.assertEqual("https://api.anthropic.com/v1/messages", args[0])
        self.assertEqual("key", kwargs["headers"]["x-api-key"])
        self.assertEqual("2023-06-01", kwargs["headers"]["anthropic-version"])
        self.assertEqual("system", kwargs["json"]["system"])
        self.assertEqual("user", kwargs["json"]["messages"][0]["content"])
        self.assertNotIn("temperature", kwargs["json"])

    def test_missing_config_is_not_ready(self):
        client = LLMClient({"provider": "openai", "base_url": "", "api_key": "", "model": ""})
        self.assertFalse(client.is_ready())
        self.assertEqual("", client.complete_text("system", "user"))

    def test_http_error_returns_empty_text(self):
        response = requests.Response()
        response.status_code = 401
        response._content = b'{"error":{"message":"invalid api key"}}'
        with patch("app.utils.llm_client.requests.post", return_value=response), \
                patch("app.utils.llm_client.log.warn") as warn:
            client = LLMClient({
                "provider": "openai",
                "base_url": "https://api.example/v1",
                "api_key": "key",
                "model": "gpt-test"
            })
            self.assertEqual("", client.complete_text("system", "user"))
        warning = warn.call_args.args[0]
        self.assertIn("status=401", warning)
        self.assertNotIn("status=none", warning)
        self.assertIn("invalid api key", warning)

    def test_anthropic_http_error_keeps_real_status(self):
        response = requests.Response()
        response.status_code = 429
        response._content = b'{"error":{"message":"rate limited"}}'
        with patch("app.utils.llm_client.requests.post", return_value=response), \
                patch("app.utils.llm_client.log.warn") as warn:
            client = LLMClient({
                "provider": "anthropic",
                "base_url": "https://api.anthropic.com/v1",
                "api_key": "key",
                "model": "claude-test"
            })
            self.assertEqual("", client.complete_text("system", "user"))
        warning = warn.call_args.args[0]
        self.assertIn("status=429", warning)
        self.assertNotIn("status=none", warning)

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
                "provider": "openai",
                "base_url": "https://api.example/v1",
                "api_key": "key",
                "model": "gpt-test"
            })
            self.assertIsNone(client.complete_json("system", "user"))
