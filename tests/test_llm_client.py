# -*- coding: utf-8 -*-

import os
import sys
import threading
import types
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import httpx
from openai import APIStatusError, OpenAI

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
from app.utils.search_cancellation import search_cancellation


class LLMClientTest(TestCase):
    def test_web_and_rss_indexer_searches_retain_configured_sdk_retries(self):
        from app.indexer.indexer import Indexer
        from app.utils.types import IndexerType, SearchType
        from app.utils.workload import BoundedExecutor

        requests = []
        def respond(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(500, json={'error': {'message': 'temporary offline error'}})
            return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok": true}'}}]})
        sdk = OpenAI(api_key='offline-key', base_url='https://llm.invalid/v1', max_retries=1,
                     http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        self.addCleanup(sdk.close)
        client = LLMClient({'base_url': 'https://llm.invalid/v1', 'api_key': 'offline-key',
                            'model': 'offline', 'max_retries': 1})
        client._client = sdk
        indexer_type = Indexer.__closure__[0].cell_contents
        indexer = indexer_type.__new__(indexer_type)
        indexer._client_type = IndexerType.JACKETT
        indexer._client = SimpleNamespace(search=lambda *args: [client.complete_json('system', 'user')])
        indexer.get_indexers = lambda: [SimpleNamespace(id=1, name='offline', pri=0)]
        indexer.progress = Mock()
        indexer._search_executor = BoundedExecutor(1, 2, 'llm-retry-regression')
        self.addCleanup(indexer._search_executor.shutdown)
        for source in (SearchType.WEB, SearchType.RSS):
            with self.subTest(source=source):
                requests.clear()
                self.assertEqual(indexer.search_by_keyword('Dune', {}, in_from=source), [{'ok': True}])
                self.assertEqual(len(requests), 2)

    def test_real_sdk_does_not_retry_request_after_search_is_stopped(self):
        cancel = threading.Event()
        requests = []
        def respond(request):
            requests.append(request)
            if len(requests) == 1:
                cancel.set()
                return httpx.Response(500, json={'error': {'message': 'offline provider failure'}})
            return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok": true}'}}]})
        # Only HTTP transport is replaced; exercise the pinned SDK's real option
        # cloning, error handling and retry behavior without any network calls.
        sdk = OpenAI(api_key='offline-key', base_url='https://llm.invalid/v1', max_retries=2,
                     http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        self.addCleanup(sdk.close)
        client = LLMClient({'base_url': 'https://llm.invalid/v1', 'api_key': 'offline-key', 'model': 'offline'})
        client._client = sdk
        with search_cancellation(cancel, disable_llm_retries=True):
            self.assertEqual(client.complete_text('system', 'user'), '')
        self.assertEqual(len(requests), 1)
        self.assertEqual(sdk.max_retries, 2)
        self.assertEqual(client.complete_json('system', 'another search'), {'ok': True})
        self.assertEqual(len(requests), 2)

    @staticmethod
    def make_sdk(response=None, error=None):
        sdk = Mock()
        if error:
            sdk.chat.completions.create.side_effect = error
        else:
            sdk.chat.completions.create.return_value = response
        return sdk

    def test_api_root_builds_sdk_chat_completion_request(self):
        sdk = self.make_sdk({
            "choices": [{"message": {"content": "{\"ok\": true}"}}]
        })
        with patch("app.utils.llm_client.OpenAI", return_value=sdk) as openai_cls:
            client = LLMClient({
                "base_url": "https://api.example/v1",
                "api_key": "key",
                "model": "gpt-test",
                "timeout": 9,
                "max_retries": 1
            })
            result = client.complete_json("system", "user", max_tokens=128)

        self.assertEqual({"ok": True}, result)
        openai_cls.assert_called_once_with(
            api_key="key",
            base_url="https://api.example/v1/",
            timeout=9,
            max_retries=1
        )
        kwargs = sdk.chat.completions.create.call_args.kwargs
        self.assertEqual("gpt-test", kwargs["model"])
        self.assertEqual(128, kwargs["max_tokens"])
        self.assertEqual("system", kwargs["messages"][0]["content"])

    def test_per_request_timeout_and_retry_override_use_sdk_options(self):
        sdk = self.make_sdk()
        request_sdk = self.make_sdk({
            "choices": [{"message": {"content": "{\"ok\": true}"}}]
        })
        sdk.with_options.return_value = request_sdk
        with patch("app.utils.llm_client.OpenAI", return_value=sdk):
            client = LLMClient({
                "base_url": "https://api.example/v1",
                "api_key": "key",
                "model": "gpt-test",
                "timeout": 20,
                "max_retries": 2
            })
            result = client.complete_json(
                "system",
                "user",
                timeout=1.25,
                max_retries=0
            )

        self.assertEqual({"ok": True}, result)
        sdk.with_options.assert_called_once_with(timeout=1.25, max_retries=0)
        request_sdk.chat.completions.create.assert_called_once()
        sdk.chat.completions.create.assert_not_called()

    def test_full_chat_completion_url_is_converted_for_sdk(self):
        sdk = self.make_sdk({
            "choices": [{"message": {"content": "{\"ok\": true}"}}]
        })
        with patch("app.utils.llm_client.OpenAI", return_value=sdk) as openai_cls:
            client = LLMClient({
                "base_url": "https://openrouter.ai/api/v1/chat/completions?trace=1",
                "api_key": "key",
                "model": "openai/gpt-test"
            })
            result = client.complete_json("system", "user", max_tokens=256)

        self.assertEqual({"ok": True}, result)
        self.assertEqual(
            "https://openrouter.ai/api/v1/",
            openai_cls.call_args.kwargs["base_url"]
        )
        kwargs = sdk.chat.completions.create.call_args.kwargs
        self.assertEqual("system", kwargs["messages"][0]["content"])
        self.assertEqual({"trace": "1"}, kwargs["extra_query"])

    def test_missing_config_is_not_ready(self):
        client = LLMClient({"base_url": "", "api_key": "", "model": ""})
        self.assertFalse(client.is_ready())
        self.assertEqual("", client.complete_text("system", "user"))

    def test_sdk_http_error_is_classified_and_returns_empty_text(self):
        request = httpx.Request("POST", "https://api.example/v1/chat/completions")
        response = httpx.Response(401, request=request)
        error = APIStatusError("invalid api key", response=response, body={"error": "invalid api key"})
        sdk = self.make_sdk(error=error)
        with patch("app.utils.llm_client.OpenAI", return_value=sdk), \
                patch("app.utils.llm_client.log.error") as log_error:
            client = LLMClient({
                "base_url": "https://api.example/v1",
                "api_key": "key",
                "model": "gpt-test"
            })
            self.assertEqual("", client.complete_text("system", "user"))

        message = log_error.call_args.args[0]
        self.assertIn("category=http_status", message)
        self.assertIn("status=401", message)
        self.assertIn("invalid api key", message)

    def test_invalid_json_returns_none(self):
        sdk = self.make_sdk({
            "choices": [{"message": {"content": "not json"}}]
        })
        with patch("app.utils.llm_client.OpenAI", return_value=sdk):
            client = LLMClient({
                "base_url": "https://api.example/v1",
                "api_key": "key",
                "model": "gpt-test"
            })
            self.assertIsNone(client.complete_json("system", "user"))

    def test_status_succeeds_when_sdk_choices_only_contain_reasoning(self):
        sdk = self.make_sdk(SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="", reasoning_content="Okay, the user"),
            finish_reason="length"
        )]))
        with patch("app.utils.llm_client.OpenAI", return_value=sdk), \
                patch("app.utils.llm_client.log.info") as info:
            client = LLMClient({
                "base_url": "https://api.deepseek.com/",
                "api_key": "key",
                "model": "deepseek-v4-flash"
            })
            self.assertTrue(client.get_status())

        kwargs = sdk.chat.completions.create.call_args.kwargs
        self.assertEqual(4, kwargs["max_tokens"])
        self.assertNotIn("extra_body", kwargs)
        message = info.call_args.args[0]
        self.assertIn("reasoning_content_len=14", message)
        self.assertIn("finish_reason=length", message)

    def test_configured_thinking_mode_uses_sdk_extra_body(self):
        sdk = self.make_sdk({
            "choices": [{"message": {"content": "OK"}}]
        })
        with patch("app.utils.llm_client.OpenAI", return_value=sdk):
            client = LLMClient({
                "base_url": "https://api.deepseek.com/",
                "api_key": "key",
                "model": "deepseek-v4-flash",
                "thinking": "disabled"
            })
            self.assertTrue(client.get_status())

        self.assertEqual(
            {"thinking": {"type": "disabled"}},
            sdk.chat.completions.create.call_args.kwargs["extra_body"]
        )

    def test_sdk_extension_parameters_are_forwarded(self):
        sdk = self.make_sdk({
            "choices": [{"message": {"content": "OK"}}]
        })
        with patch("app.utils.llm_client.OpenAI", return_value=sdk):
            client = LLMClient({
                "base_url": "https://api.example/v1",
                "api_key": "key",
                "model": "gpt-test",
                "extra_body": {"vendor_option": True},
                "extra_headers": {"X-App": "nas-tools"},
                "extra_query": {"region": "cn"}
            })
            self.assertTrue(client.get_status())

        kwargs = sdk.chat.completions.create.call_args.kwargs
        self.assertEqual({"vendor_option": True}, kwargs["extra_body"])
        self.assertEqual({"X-App": "nas-tools"}, kwargs["extra_headers"])
        self.assertEqual({"region": "cn"}, kwargs["extra_query"])

    def test_completion_does_not_treat_reasoning_as_final_content(self):
        sdk = self.make_sdk({
            "choices": [{
                "message": {
                    "content": "",
                    "reasoning_content": "private reasoning"
                },
                "finish_reason": "length"
            }]
        })
        with patch("app.utils.llm_client.OpenAI", return_value=sdk), \
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
