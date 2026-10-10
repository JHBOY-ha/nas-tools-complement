"""出口诊断使用真实 Requests 线路选择，回显服务由离线 adapter 替代。"""
import ast
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests


ROOT = Path(__file__).resolve().parents[1]


def load_helper(relative):
    """只加载无应用启动副作用的工具，避免测试打开真实配置和数据库。"""
    spec = importlib.util.spec_from_file_location(Path(relative).stem, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EchoAdapter(requests.adapters.BaseAdapter):
    def __init__(self):
        self.replies = []
        self.calls = []

    def send(self, request, **kwargs):
        self.calls.append((request.url, kwargs))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response.request = request
        response._content = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
        response._content_consumed = True
        return response

    def close(self):
        pass


class NetworkEgressTest(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(get_proxies=Mock(return_value={}))
        namespace = {"json": json, "Config": lambda: self.config,
                     "normalize_proxies": load_helper("app/utils/security_utils.py").normalize_proxies}
        tree = ast.parse((ROOT / "web/action.py").read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "WebAction")
        # 执行生产方法和固定服务列表，不复制实现或导入整套应用。
        cls.body = [node for node in cls.body if
                    isinstance(node, ast.FunctionDef) and node.name == "__egress_ip_test" or
                    isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id == "EGRESS_IP_URLS" for target in node.targets)]
        exec(compile(ast.Module(body=[cls], type_ignores=[]), "web/action.py", "exec"), namespace)
        self.probe = namespace["WebAction"]._WebAction__egress_ip_test
        self.adapter = EchoAdapter()
        self.session = requests.Session()
        self.session.mount("https://", self.adapter)
        factory = patch("requests.Session", return_value=self.session)
        factory.start()
        self.addCleanup(factory.stop)
        # 即使容器设置了环境代理，也必须能明确测试直连与应用代理。
        environment = patch.dict(os.environ, {"HTTP_PROXY": "http://env.invalid:7890",
                                               "HTTPS_PROXY": "http://env.invalid:7890",
                                               "ALL_PROXY": "socks5://env.invalid:7890"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_direct_ignores_environment_and_client_supplied_url(self):
        self.adapter.replies = [{"ip": "8.8.8.8"}]
        result = self.probe({"mode": "direct", "url": "https://client.invalid"})
        self.assertEqual(result["ip"], "8.8.8.8")
        self.assertTrue(result["res"])
        self.assertFalse(self.session.trust_env)
        self.config.get_proxies.assert_not_called()
        url, kwargs = self.adapter.calls[0]
        self.assertEqual(url, "https://api64.ipify.org/?format=json")
        self.assertFalse(kwargs["proxies"])
        self.assertTrue(kwargs["verify"])
        self.assertEqual(kwargs["timeout"], (3, 5))

    def test_proxy_uses_application_config_and_host_selector(self):
        self.config.get_proxies.return_value = {
            "https": "http://app.invalid:7890", "https://api64.ipify.org": "http://selected.invalid:7891"}
        self.adapter.replies = [{"ip": "1.1.1.1"}]
        self.assertEqual(self.probe({"mode": "proxy"})["ip"], "1.1.1.1")
        url, kwargs = self.adapter.calls[0]
        self.assertEqual(requests.utils.select_proxy(url, kwargs["proxies"]), "http://selected.invalid:7891")
        self.assertFalse(self.session.trust_env)

    def test_legacy_proxy_and_all_selector_are_supported(self):
        for proxy in ("app.invalid:7890", {"all": "socks5h://app.invalid:7890"}):
            with self.subTest(proxy=proxy):
                self.config.get_proxies.return_value = proxy
                self.adapter.replies = [{"ip": "1.1.1.1"}]
                self.assertTrue(self.probe({"mode": "proxy"})["res"])
                url, kwargs = self.adapter.calls[-1]
                self.assertIn("app.invalid:7890", requests.utils.select_proxy(url, kwargs["proxies"]))

    def test_missing_or_inapplicable_proxy_is_skipped_without_direct_fallback(self):
        for proxy in ({}, {"http": "http://app.invalid:7890"}, {"no_proxy": "*"},
                      {"https://api.themoviedb.org": "http://app.invalid:7890"}):
            with self.subTest(proxy=proxy):
                self.config.get_proxies.return_value = proxy
                result = self.probe({"mode": "proxy"})
                self.assertTrue(result["skipped"])
                self.assertFalse(result["res"])
        self.assertEqual(self.adapter.calls, [])

    def test_invalid_proxy_and_mode_do_not_make_requests(self):
        self.config.get_proxies.return_value = "http://user:secret@app.invalid/path"
        result = self.probe({"mode": "proxy"})
        self.assertIn("代理配置无效", result["msg"])
        self.assertNotIn("secret", str(result))
        for data in (None, "direct", {}, {"mode": "other"}, {"mode": []}):
            self.assertFalse(self.probe(data)["res"])
        self.assertEqual(self.adapter.calls, [])

    def test_ipv6_is_validated_and_normalized(self):
        self.adapter.replies = [{"ip": "2001:4860:4860:0000:0000:0000:0000:8888"}]
        self.assertEqual(self.probe({"mode": "direct"})["ip"], "2001:4860:4860::8888")

    def test_dual_stack_failure_falls_back_to_ipv4_on_same_proxy(self):
        self.config.get_proxies.return_value = {"https": "http://app.invalid:7890"}
        self.adapter.replies = [requests.ConnectionError("unavailable"), {"ip": "8.8.8.8"}]
        self.assertTrue(self.probe({"mode": "proxy"})["res"])
        self.assertEqual(self.adapter.calls[1][0], "https://api.ipify.org/?format=json")
        self.assertTrue(all(kwargs["proxies"] for _, kwargs in self.adapter.calls))

    def test_host_only_proxy_never_falls_back_to_an_unproxied_service(self):
        self.config.get_proxies.return_value = {"https://api64.ipify.org": "http://app.invalid:7890"}
        self.adapter.replies = [requests.exceptions.ProxyError("unavailable")]
        self.assertFalse(self.probe({"mode": "proxy"})["res"])
        self.assertEqual(len(self.adapter.calls), 1)

    def test_private_malformed_and_oversized_replies_are_rejected(self):
        for payload in ({"ip": "127.0.0.1"}, {"ip": "192.168.1.1"}, {"ip": "100.64.0.1"},
                        {"ip": "fe80::1"}, {"ip": "2001:4860::8888%eth0"}, {"ip": True},
                        {"ip": "<img src=x>"}, [], {}, b"not json", b"x" * 1025):
            with self.subTest(payload=str(payload)[:60]):
                self.adapter.replies = [payload, payload]
                self.assertFalse(self.probe({"mode": "direct"})["res"])

    def test_network_errors_do_not_expose_authenticated_proxy_urls(self):
        self.config.get_proxies.return_value = "http://user:secret@app.invalid:7890"
        self.adapter.replies = [requests.exceptions.ProxyError("http://user:secret@app.invalid:7890")] * 2
        result = self.probe({"mode": "proxy"})
        self.assertIn("ProxyError", result["msg"])
        self.assertNotIn("secret", str(result))
        self.assertNotIn("app.invalid", str(result))

    def test_service_permission_matches_existing_network_diagnostics(self):
        policy = load_helper("web/backend/action_permissions.py")
        self.assertEqual(policy.ACTION_PERMISSIONS["egress_ip_test"], policy.ACTION_PERMISSIONS["net_test"])


if __name__ == "__main__":
    unittest.main()
