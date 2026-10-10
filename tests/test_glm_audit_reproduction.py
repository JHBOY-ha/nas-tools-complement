"""Offline security regressions for all 15 GLM findings.

Run: python3 -m unittest tests.test_glm_audit_reproduction -v
Assertions now describe hardened behavior, including legitimate legacy inputs.
Production methods are extracted without app startup; network/update boundaries
are blocked or mocked and file operations use disposable temporary directories.
"""
import ast
import base64
from collections import UserDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import datetime
from enum import Enum
from functools import lru_cache, wraps
import hmac
import importlib.util
import io
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from threading import RLock
import time
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from cacheout import Cache
from flask import Flask, g, has_request_context, render_template, request
from flask_login import LoginManager, UserMixin, current_user, login_required
from flask_restx import Api, Resource, reqparse
import jwt
import regex
import requests
import ruamel.yaml
from lxml import etree


ROOT = Path(__file__).resolve().parents[1]
MARKER = "NASTOOL_GLM_AUDIT_MARKER"


def load_support(relative):
    """Import dependency-free helpers without triggering app package startup."""
    spec = importlib.util.spec_from_file_location("_audit_" + Path(relative).stem, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SAFE = load_support("app/utils/security_utils.py")
POLICY = load_support("web/backend/action_permissions.py")
EXCLUSIVE = load_support("app/utils/exclusive_publish.py")


def load_source(relative, names, namespace, class_name=None, keep_decorators=False):
    """Execute actual declarations, omitting application startup and singletons."""
    path = ROOT / relative
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    if class_name:
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
        cls.decorator_list = []
        body = []
        for node in cls.body:
            if isinstance(node, ast.FunctionDef) and node.name in names:
                body.append(node)
            elif isinstance(node, ast.Assign):
                # Preserve real literal constants, including the connection allowlist.
                try:
                    ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    continue
                body.append(node)
        cls.body = body
        body = [cls]
    else:
        body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        if not keep_decorators:
            for node in body:
                node.decorator_list = []
    selected = ast.Module(body=body, type_ignores=[])
    exec(compile(ast.fix_missing_locations(selected), str(path), "exec"), namespace)
    return namespace[class_name] if class_name else namespace


class MediaType(Enum):
    MOVIE = "电影"
    TV = "电视剧"


class AuditSecurityRegressionTest(unittest.TestCase):
    def setUp(self):
        self.network_attempts = []

        def deny_network(*_args, **_kwargs):
            # Track attempts even if production exception handling swallows them.
            self.network_attempts.append(True)
            raise AssertionError("audit must remain offline")

        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo"):
            blocker = patch(target, side_effect=deny_network)
            blocker.start()
            self.addCleanup(blocker.stop)
        for target in ("subprocess.run", "os.system"):
            blocker = patch(target, side_effect=AssertionError("update commands must be mocked"))
            blocker.start()
            self.addCleanup(blocker.stop)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(MARKER, None)
        self.config = MagicMock()
        self.configuration = {
            "security": {"api_key": "offline-audit-key-at-least-32-bytes"},
            "app": {}, "media": {},
        }
        self.config.get_config.side_effect = lambda key=None: (
            self.configuration if key is None else self.configuration.get(key, {}))
        # Exercise the real optional-UA fallback while keeping app startup isolated.
        config_reader = load_source("config.py", {"get_tmdb_web_ua"},
                                    {"default_user_agent": requests.utils.default_user_agent}, "Config")
        self.config.get_tmdb_web_ua.side_effect = lambda: config_reader.get_tmdb_web_ua(self.config)
        # Extracted dispatch methods still need their real admission constants;
        # omitting newer imports would fail before reaching the policy itself.
        from app.helper.action_tasks import COMMAND_TITLES
        from app.utils.workload import TaskQueueFull
        self.ns = {
            "base64": base64, "json": json, "os": os, "shutil": shutil,
            "re": re, "time": time, "deepcopy": deepcopy, "lru_cache": lru_cache,
            "subprocess": subprocess, "importlib": SimpleNamespace(import_module=MagicMock()),
            "ntpath": ntpath, "rename_exclusive": EXCLUSIVE.rename_exclusive,
            "Config": lambda: self.config, "ExceptionUtils": MagicMock(),
            "log": MagicMock(), "MediaType": MediaType, "hmac": hmac,
            "g": g, "has_request_context": has_request_context,
            "generate_password_hash": lambda password: "hashed:" + password,
            "SystemUtils": SimpleNamespace(is_synology=lambda: False),
            "WordsHelper": MagicMock(), "request": request, "wraps": wraps,
            "current_user": current_user, "Sites": MagicMock(),
            "BrushTask": MagicMock(), "StringUtils": MagicMock(),
            "action_allowed": POLICY.action_allowed,
            "ACTION_PERMISSIONS": POLICY.ACTION_PERMISSIONS,
            "COMMAND_TITLES": COMMAND_TITLES, "TaskQueueFull": TaskQueueFull,
            "PAGE_ACTIONS": POLICY.PAGE_ACTIONS,
            "ModuleConf": SimpleNamespace(RMT_MODES={}),
            "RmtMode": SimpleNamespace(COPY=object()),
            **{name: getattr(SAFE, name) for name in (
                "parse_episode_offset", "evaluate_episode_offset", "parse_rule_dict",
                "normalize_proxies", "compile_ignore_pattern")},
        }
        # Keep real naming defaults while mocking the unrelated transfer-mode service.
        for node in ast.parse((ROOT / "config.py").read_text()).body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in {
                    "DEFAULT_MOVIE_FORMAT", "DEFAULT_TV_FORMAT"} for target in node.targets):
                self.ns[node.targets[0].id] = ast.literal_eval(node.value)
        methods = {
            "action", "api_action", "__test_connection", "__user_manager", "update_system",
            "__rename_file", "__import_custom_words", "__delete_history",
            "set_config_value", "__update_config", "__add_brushtask", "__brushtask_detail",
            "__add_or_edit_custom_word", "__restory_backup",
            "__history_delete_plan", "__analyse_import_custom_words_code", "delete_media_file",
        }
        cls = load_source("web/action.py", methods, self.ns, "WebAction")
        self.action = cls()
        self.action.dbhelper = MagicMock()
        self.action.dbhelper.get_config_sync_paths.return_value = []
        # A mock's default truthiness must not masquerade as input validation.
        self.action.dbhelper.is_custom_words_existed.return_value = False
        self.action.delete_media_file = MagicMock(return_value=(True, "deleted"))
        # Use the real dispatcher keys so a renamed/deleted endpoint fails tests.
        tree = ast.parse((ROOT / "web/action.py").read_text())
        cls_node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "WebAction")
        init = next(n for n in cls_node.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
        mapping = next(n.value for n in init.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict))
        self.dispatch_commands = {key.value for key in mapping.keys}
        # Include incremental registrations without weakening the exact policy
        # comparison; task-status commands are installed via _actions.update.
        for node in ast.walk(init):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == 'update' and isinstance(node.func.value, ast.Attribute) \
                    and node.func.value.attr == '_actions' and node.args \
                    and isinstance(node.args[0], ast.Dict):
                self.dispatch_commands.update(key.value for key in node.args[0].keys)
        self.action._actions = {}
        for key, value in zip(mapping.keys, mapping.values):
            if isinstance(value, ast.Attribute) and value.attr in methods:
                attr = "_WebAction" + value.attr if value.attr.startswith("__") else value.attr
                self.action._actions[key.value] = getattr(self.action, attr)
        self.ns["WebAction"] = lambda: self.action
        load_source("web/main.py", {"action_login_check", "do", "check_page_permission"}, self.ns)
        app = Flask(__name__, template_folder=str(ROOT / "web/templates"))
        app.secret_key = "offline-audit-session"
        app.config["TESTING"] = True
        app.before_request(self.ns["check_page_permission"])
        manager = LoginManager(app)

        class TestUser(UserMixin):
            id = "123"
            pris = ""
            username = "audit"

        self.user = TestUser()
        manager.user_loader(lambda user_id: self.user if user_id == self.user.id else None)
        self.ns["User"] = lambda: SimpleNamespace(get_user=lambda name: self.user if name == "audit" else None)
        app.add_url_rule("/do", view_func=self.ns["action_login_check"](self.ns["do"]), methods=["POST"])
        self.app = app
        self.client = app.test_client()
        self.login_as()

    def tearDown(self):
        self.assertEqual(self.network_attempts, [], "unmocked network attempts were blocked")

    def login_as(self, user_id="123", permissions=""):
        self.user.id, self.user.pris = user_id, permissions
        with self.client.session_transaction() as session:
            session["_user_id"] = user_id
            session["_fresh"] = True

    def post(self, cmd, data):
        return self.client.post("/do", data={"cmd": cmd, "data": json.dumps(data)})

    def marker_expression(self, value, result="True"):
        # The adversarial expression only marks this disposable process if a regression executes it.
        return "__import__('os').environ.__setitem__(%r, %r) or %s" % (MARKER, value, result)

    def word_payload(self, offset):
        return {"-1": {"id": -1, "words": {"1": {
            "type": 4, "front": "Show E", "back": r"\.mkv", "regex": 1,
            "season": 1, "offset": offset,
        }}}}

    def import_words(self, offset):
        code = base64.b64encode(json.dumps(self.word_payload(offset)).encode()).decode()
        return self.post("import_custom_words", {"import_code": code, "ids_info": ["-1_1"]})

    def words_helper(self, offset):
        cls = load_source("app/helper/words_helper.py", {"process", "episode_offset"},
                          dict(self.ns, re=regex), "WordsHelper")
        words = cls()
        for attr in ("ignored_words_info", "ignored_words_noregex_info", "replaced_words_info",
                     "replaced_words_noregex_info", "replaced_offset_words_info"):
            setattr(words, attr, [])
        row = self.word_payload(offset)["-1"]["words"]["1"]
        words.offset_words_info = [SimpleNamespace(**{key.upper(): value for key, value in row.items()})]
        return words

    def test_review_share_code_analysis_and_selected_import(self):
        self.login_as(permissions="媒体整理")
        payload = base64.b64encode((json.dumps(self.word_payload("EP+1")) + "@@@@@@note").encode()).decode()
        response = self.post("analyse_import_custom_words_code", {"import_code": payload}).json
        self.assertEqual(response["code"], 0)
        self.assertEqual(response["note_string"], "note")
        self.assertEqual(self.post("import_custom_words", {
            "import_code": payload, "ids_info": ["-1_1"]}).json["code"], 0)

    def test_review_temporary_service_settings_are_used_without_saving(self):
        self.login_as("0")
        self.configuration["qbittorrent"] = {"qbhost": "old-host"}
        response = self.post("update_config", {
            "qbittorrent.qbhost": "new-host", "test": True}).json
        factory = MagicMock()
        factory.return_value.get_status.return_value = True
        self.ns["importlib"].import_module.return_value = SimpleNamespace(Qbittorrent=factory)
        self.assertEqual(self.post("test_connection", {
            "command": "app.downloader.client.qbittorrent|Qbittorrent",
            "config": response["config"]["qbittorrent"]}).json["code"], 0)
        factory.assert_called_once_with(config={"qbhost": "new-host"})
        self.assertEqual(self.configuration["qbittorrent"]["qbhost"], "old-host")
        self.config.save_config.assert_not_called()

    def test_review_invalid_proxy_blocks_requests_but_can_be_repaired(self):
        client, ns = self.tmdb_client()
        client.proxies = {"http": "http://bad host"}
        with self.assertRaisesRegex(RuntimeError, "代理配置无效"):
            client._call("search", "")
        ns["requests"].request.assert_not_called()
        client._session.request.assert_not_called()
        # A second SDK instance must fail closed too, not reuse an old proxy.
        other, _ = self.tmdb_client()
        with self.assertRaisesRegex(RuntimeError, "代理配置无效"):
            other._call("search", "")
        client.proxies = {}
        client.cache = False
        client._call("search", "")

    def test_review_missing_file_and_real_io_failure(self):
        self.login_as(permissions="媒体整理")
        # Exercise the actual helper rather than a canned successful deletion.
        self.action.delete_media_file = type(self.action).delete_media_file
        with tempfile.TemporaryDirectory() as directory:
            row = self.history_row(True)
            row.DEST_PATH, row.DEST_FILENAME = directory, "missing.mkv"
            self.action.dbhelper.get_transfer_path_by_id.return_value = [row]
            self.assertEqual(self.post("delete_history", {"logids": [1], "flag": "del_dest"}).json["retcode"], 0)
            self.action.dbhelper.delete_transfer_log_by_id.assert_called_once_with(1)
            self.action.dbhelper.delete_transfer_log_by_id.reset_mock()
            with patch("os.remove", side_effect=PermissionError("denied")):
                self.assertEqual(self.post("delete_history", {"logids": [1], "flag": "del_dest"}).json["retcode"], 1)
            self.action.dbhelper.delete_transfer_log_by_id.assert_not_called()

    def test_review_file_cleanup_preserves_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Season 01"
            target.mkdir()
            (target / "show.mkv").write_text("media")
            (target / "show.srt").write_text("subtitle")
            self.assertTrue(type(self.action).delete_media_file(str(target), "show.mkv")[0])
            self.assertTrue((target / "show.srt").exists())

    def test_review_anonymous_page_keeps_login_redirect(self):
        self.app.login_manager.login_view = "login"
        self.app.add_url_rule("/login", endpoint="login", view_func=lambda: "login")
        self.app.add_url_rule("/basic", endpoint="basic", view_func=login_required(lambda: "secret"))
        with self.client.session_transaction() as session:
            session.clear()
        response = self.client.get("/basic")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login?next=", response.location)

    def test_review_recommend_normalizes_non_mapping_params(self):
        ns = dict(self.ns, render_template=lambda *args, **kwargs: kwargs,
                  ModuleConf=SimpleNamespace(DISCOVER_FILTER_CONF={}))
        load_source("web/main.py", {"recommend"}, ns)
        for value in ("null", "[]", "false", "1", "bad json"):
            with self.app.test_request_context(query_string={"params": value}):
                self.assertEqual(ns["recommend"]()["Params"], {})

    def test_review_delimiter_only_ignore_filter_is_rejected(self):
        for value in (";", ";;;"):
            with self.assertRaises(ValueError):
                SAFE.compile_ignore_pattern(value)
        self.assertIsNone(SAFE.compile_ignore_pattern(""))

    def test_01_malicious_import_rejected_before_inserts(self):
        self.login_as(permissions="媒体整理")
        self.assertEqual(self.import_words(self.marker_expression("words", "EP+1")).json["code"], 1)
        self.action.dbhelper.insert_custom_word.assert_not_called()
        self.action.dbhelper.insert_custom_word_groups.assert_not_called()
        self.assertNotIn(MARKER, os.environ)

    def test_01_legacy_malicious_offset_cannot_execute(self):
        title, messages, _ = self.words_helper(self.marker_expression("legacy", "EP+1")).process("Show E01.mkv")
        self.assertEqual(title, "Show E01.mkv")
        self.assertTrue(messages)
        self.assertNotIn(MARKER, os.environ)

    def test_01_legitimate_import_and_recognition(self):
        self.login_as(permissions="媒体整理")
        self.action.dbhelper.is_custom_words_existed.return_value = False
        self.assertEqual(self.import_words("EP+1").json["code"], 0)
        self.assertEqual(self.action.dbhelper.insert_custom_word.call_args.kwargs["offset"], "EP+1")
        title, messages, _ = self.words_helper("EP+1").process("Show E01.mkv")
        self.assertEqual((title, messages), ("Show E02.mkv", []))

    def test_01_arithmetic_compatibility_and_resource_bounds(self):
        for expression, episode, expected in (
                ("EP+1", 1, 2), ("EP-1", 1, 0), ("EP*2", 2, 4), ("EP/2", 4, 2),
                ("(EP+2)//2", 4, 3), ("EP+-1", 2, 1)):
            with self.subTest(expression=expression):
                self.assertEqual(SAFE.evaluate_episode_offset(expression, episode), expected)
        for expression in ("EP**999999", "EP/0", "EP/2", "EP-10", "EP.__class__", "[EP][0]",
                           "EP+True", "OTHER+1", "EP+" + "1+" * 100 + "1"):
            with self.subTest(rejected=expression), self.assertRaises(ValueError):
                SAFE.evaluate_episode_offset(expression, 1)

    def test_01_edit_rejects_offset_before_removing_old_word(self):
        self.login_as(permissions="媒体整理")
        for word_type in ("4", 4):
            self.assertEqual(self.post("add_or_edit_custom_word", {
                "id": 4, "type": word_type, "new_offset": self.marker_expression("edit", "EP")
            }).json["code"], 1)
        self.action.dbhelper.delete_custom_word.assert_not_called()

    def test_02_shell_payload_rejected_before_update_commands(self):
        self.login_as("0")
        self.config.get_proxies.return_value = {"http": "http://localhost:8080; printf marker"}
        self.action.restart_server = MagicMock()
        with patch("subprocess.run") as run:
            self.assertEqual(self.post("update_system", {}).json["code"], 1)
        run.assert_not_called()
        self.action.restart_server.assert_not_called()

    def test_02_proxy_arguments_never_use_shell(self):
        self.login_as("0")
        # Shell metacharacters inside a valid credential remain plain URL data.
        proxy = "http://user:p'a;ss$(x)@localhost:8080"
        self.config.get_proxies.return_value = {"http": proxy}
        self.action.restart_server = MagicMock()
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(self.post("update_system", {}).json["code"], 0)
        args = [call.args[0] for call in run.call_args_list]
        self.assertIn(["git", "config", "--global", "http.proxy", proxy], args)
        self.assertIn(["git", "config", "--global", "https.proxy", proxy], args)
        self.assertTrue(all(isinstance(arg, list) and not call.kwargs.get("shell", False)
                            for arg, call in zip(args, run.call_args_list)))
        self.action.restart_server.assert_called_once()

    def test_02_update_failure_stops_restart(self):
        self.login_as("0")
        self.config.get_proxies.return_value = {}
        self.action.restart_server = MagicMock()
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, ["git"])):
            self.assertEqual(self.post("update_system", {}).json["code"], 1)
        self.action.restart_server.assert_not_called()

    def test_02_invalid_config_does_not_mutate_live_settings(self):
        self.login_as("0")
        previous = deepcopy(self.configuration)
        self.assertEqual(self.post("update_config", {
            "app.proxies": "http://localhost' + (" + self.marker_expression("proxy", "''") + ") + '"
        }).json["code"], 1)
        self.config.save_config.assert_not_called()
        self.assertEqual(self.configuration, previous)
        self.assertNotIn(MARKER, os.environ)

    def tmdb_client(self):
        ns = dict(self.ns, requests=MagicMock(), logger=MagicMock(), TMDbException=RuntimeError, urlencode=urlencode)
        cls = load_source("app/media/tmdbv3api/tmdb.py", {
            "proxies", "_validate_response", "_request", "test_connection", "cached_request", "_call"}, ns, "TMDb")
        client = cls()
        client.api_key, client.domain, client.language = "audit", "https://example.invalid/3", "en"
        client.obj_cached, client._remaining, client.debug = True, 40, False
        client._session = MagicMock()
        for transport in (ns["requests"].request, client._session.request):
            transport.return_value.headers = {}
            transport.return_value.status_code = 200
            transport.return_value.json.return_value = {"audit": True}
        return client, ns

    def test_02_tmdb_json_proxy_keeps_lru_and_credentials(self):
        client, ns = self.tmdb_client()
        proxy = {"http": "http://user:p'a;ss@localhost:8080", "https": "socks5h://[::1]:1080"}
        client.proxies = proxy
        self.assertEqual(json.loads(client.proxies), proxy)
        for cached in (True, False):
            client.cache = cached
            self.assertEqual(client._call("/movie/1", ""), {"audit": True})
            transport = ns["requests"].request if cached else client._session.request
            self.assertEqual(transport.call_args.kwargs["proxies"], proxy)
            self.assertTrue(transport.call_args.kwargs["verify"])
        client.cache = True
        client._call("/movie/1", "")
        self.assertEqual(ns["requests"].request.call_count, 1)

    def test_02_legacy_proxy_expression_rejected_in_both_transports(self):
        client, ns = self.tmdb_client()
        os.environ[client.TMDB_PROXIES] = self.marker_expression("legacy-proxy", "{}")
        for cached in (True, False):
            client.cache = cached
            with self.assertRaises(ValueError):
                client._call("/movie/1", "")
        ns["requests"].request.assert_not_called()
        client._session.request.assert_not_called()
        self.assertNotIn(MARKER, os.environ)

    def test_02_proxy_formats_and_empty_reset(self):
        for proxy in ("http://localhost:8080", "https://user:password@[::1]:443", "socks5h://proxy:1080"):
            self.assertEqual(SAFE.normalize_proxies({"http": proxy})["http"], proxy)
        client, _ = self.tmdb_client()
        client.proxies = {"http": "http://localhost:8080"}
        client.proxies = None
        self.assertEqual(json.loads(client.proxies), {})

    def test_02_legacy_yaml_proxies_reach_both_tmdb_transports(self):
        import ruamel.yaml

        # Startup reads YAML directly, bypassing the webpage's URL normalization.
        for label, document, expected in (
                ("bare endpoints", "http: 127.0.0.1:20171\nhttps: 172.17.0.1:20171\n", {
                    "http": "http://127.0.0.1:20171", "https": "http://172.17.0.1:20171"}),
                ("boundary whitespace", 'http: "  http://172.17.0.1:20171  "\n'
                 'https: "  socks5h://localhost:1080  "\n', {
                     "http": "http://172.17.0.1:20171", "https": "socks5h://localhost:1080"}),
                ("disabled HTTP", "http: false\nhttps: http://172.17.0.1:20171\n", {
                    "https": "http://172.17.0.1:20171"})):
            with self.subTest(configuration=label):
                client, ns = self.tmdb_client()
                client.proxies = ruamel.yaml.YAML().load(document)
                for cached in (True, False):
                    client.cache = cached
                    self.assertEqual(client._call("/movie/1", ""), {"audit": True})
                    transport = ns["requests"].request if cached else client._session.request
                    self.assertEqual(transport.call_args.kwargs["proxies"], expected)
                    self.assertTrue(transport.call_args.kwargs["verify"])
                ns["logger"].error.assert_not_called()

    def test_02_disabled_app_proxy_preserves_docker_environment_proxy(self):
        # An unset application proxy must still let Requests inherit Docker's proxy.
        proxy = "http://172.17.0.1:20171"
        with patch.dict(os.environ, {"HTTPS_PROXY": proxy}, clear=True):
            client, ns = self.tmdb_client()
            for disabled in (False, {"http": False, "https": "   "}):
                client.proxies = disabled
                settings = requests.Session().merge_environment_settings(
                    "https://example.invalid/3/movie/1", json.loads(client.proxies),
                    stream=False, verify=True, cert=None)
                self.assertEqual(settings["proxies"]["https"], proxy)
            ns["logger"].error.assert_not_called()

    def test_02_proxy_structures_are_normalized_before_tmdb_requests(self):
        proxy = "http://127.0.0.1:20171"
        both = {"http": proxy, "https": proxy}
        # Legacy configuration can contain a scalar or a non-dict Mapping.
        for label, value, expected in (
                ("address scalar", "127.0.0.1:20171", both),
                ("URL scalar", proxy, both),
                ("JSON mapping scalar", json.dumps(both), both),
                ("literal mapping scalar", repr(both), both),
                ("mapping implementation", UserDict(both), both),
                ("uppercase keys", {"HTTP": proxy, "HTTPS": proxy}, both),
                ("environment keys", {"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy}, both),
                ("all fallback", {"all": proxy}, {"all": proxy}),
                ("specific overrides all", {"ALL_PROXY": proxy, "https": "socks5h://localhost:1080"}, {
                    "all": proxy, "https": "socks5h://localhost:1080"})):
            with self.subTest(configuration=label):
                client, ns = self.tmdb_client()
                client.proxies = value
                for cached in (True, False):
                    client.cache = cached
                    self.assertEqual(client._call("/configuration", ""), {"audit": True})
                    transport = ns["requests"].request if cached else client._session.request
                    self.assertEqual(transport.call_args.kwargs["proxies"], expected)
                ns["logger"].error.assert_not_called()

    def test_02_configuration_returns_canonical_proxy_structures(self):
        cls = load_source("config.py", {"get_proxies"}, {}, "Config")
        config = cls()
        proxy = "http://127.0.0.1:20171"
        for raw, expected in (("127.0.0.1:20171", {"http": proxy, "https": proxy}),
                              (UserDict({"ALL_PROXY": proxy}), {"all": proxy}),
                              ({"HTTP": proxy, "HTTPS": proxy}, {"http": proxy, "https": proxy})):
            app = {"proxies": raw}
            config.get_config = MagicMock(return_value=app)
            self.assertEqual(config.get_proxies(), expected)
            self.assertIs(app["proxies"], raw)
        # Invalid input must still reach TMDb's fail-closed setter for repair.
        raw = [proxy]
        config.get_config.return_value = {"proxies": raw}
        self.assertIs(config.get_proxies(), raw)

    def test_02_basic_settings_display_uses_canonical_proxy(self):
        render = MagicMock()
        self.configuration["app"]["proxies"] = "127.0.0.1:20171"
        namespace = dict(self.ns, render_template=render, WebAction=MagicMock(), SystemConfig=MagicMock())
        load_source("web/main.py", {"basic"}, namespace)
        for field in ("http", "https", "all"):
            self.config.get_proxies.return_value = {field: "http://127.0.0.1:20171"}
            namespace["basic"]()
            self.assertEqual(render.call_args.kwargs["Proxy"], "127.0.0.1:20171")

    def test_02_proxy_form_roundtrip_preserves_advanced_rules(self):
        # The single-address field must not erase rules when unrelated settings save.
        for raw in (
            {"all": "http://proxy:7890", "no_proxy": "localhost",
             "https://www.themoviedb.org": "http://tmdb:7891"},
            {"https://www.themoviedb.org": "http://tmdb:7891"},
            {"http": "http://proxy:7890", "https": "http://other:7891"},
        ):
            cfg = {"app": {"proxies": deepcopy(raw)}}
            shown = (raw.get("http") or raw.get("https") or raw.get("all") or "").replace("http://", "")
            self.action.set_config_value(cfg, "app.proxies", shown)
            self.assertEqual(cfg["app"]["proxies"], raw)
            with self.assertRaisesRegex(ValueError, "高级代理"):
                self.action.set_config_value(cfg, "app.proxies", "changed:7890")
            self.assertEqual(cfg["app"]["proxies"], raw)
        cfg = {"app": {"proxies": {"http": "http://proxy:7890", "https": "http://proxy:7890"}}}
        self.action.set_config_value(cfg, "app.proxies", "new:7891")
        self.assertEqual(cfg["app"]["proxies"], {"http": "http://new:7891", "https": "http://new:7891"})
        self.action.set_config_value(cfg, "app.proxies", "")
        self.assertFalse(any(cfg["app"]["proxies"].values()))
        cfg["app"]["proxies"] = {"invalid": "bad"}
        self.action.set_config_value(cfg, "app.proxies", "")
        self.assertFalse(any(cfg["app"]["proxies"].values()))

    def test_rss_strict_reads_distinguish_errors_from_empty_feeds(self):
        import xml.dom.minidom
        factory = MagicMock()
        namespace = dict(self.ns, RequestUtils=factory, xml=xml,
                         RssTitleUtils=SimpleNamespace(keepfriends_title=lambda title: title))
        rss = load_source("app/rss.py", {"parse_rssxml"}, namespace, "Rss")
        response = SimpleNamespace(text="<rss><channel/></rss>", apparent_encoding="utf-8")
        factory.return_value.get_res.return_value = response
        self.assertEqual(rss.parse_rssxml("https://example.invalid/rss", strict=True), [])
        response.text = "<html><body>login</body></html>"
        self.assertIsNone(rss.parse_rssxml("https://example.invalid/rss", strict=True))
        response.text = "<broken"
        self.assertIsNone(rss.parse_rssxml("https://example.invalid/rss", strict=True))
        self.assertEqual(rss.parse_rssxml("https://example.invalid/rss"), [])
        factory.return_value.get_res.return_value = None
        self.assertIsNone(rss.parse_rssxml("https://example.invalid/rss", strict=True))
        self.assertEqual(rss.parse_rssxml("https://example.invalid/rss"), [])
        # The article-list caller keeps its historical empty-list response.
        checker = load_source("app/rsschecker.py", {"__parse_userrss_result", "get_rss_articles"},
                              dict(self.ns, RequestUtils=factory, etree=etree), "RssChecker")()
        checker.get_userrss_parser = MagicMock(return_value=None)
        checker.get_rsstask_info = MagicMock(return_value={"name": "test"})
        self.assertIsNone(checker._RssChecker__parse_userrss_result({"name": "test"}))
        self.assertEqual(checker.get_rss_articles("1"), [])
        checker.get_userrss_parser.return_value = {"type": "XML", "format": '{"list": "//item"}'}
        response.text = "<rss><channel/></rss>"
        factory.return_value.get_res.return_value = response
        self.assertEqual(checker._RssChecker__parse_userrss_result({"address": "https://example.invalid/rss"}), [])

    def assert_rss_action_status(self, service, brush, expected):
        # Run the actual Web wrapper and terminal classifier together.
        from concurrent.futures import Future
        name = "__run_brushtask" if brush else "__run_userrss"
        namespace = {"BrushTask" if brush else "RssChecker": lambda: service}
        action = load_source("web/action.py", {name}, namespace, "WebAction")
        result = getattr(action, "_WebAction" + name)({"id": "1"})
        cls = load_source("app/helper/action_tasks.py", {"_finished"},
                          {"time": time, "json": json, "copy": SimpleNamespace(deepcopy=deepcopy)}, "ActionTasks")
        tasks = cls()
        tasks._lock = RLock()
        tasks._records = {"test": {"owner": "0", "fingerprint": "test"}}
        tasks._fingerprints = {}
        tasks._futures = {}
        tasks._save = MagicMock()
        future = Future()
        future.set_result(result)
        tasks._finished("test", future)
        self.assertEqual(tasks._records["test"]["status"], expected)
        self.assertTrue(tasks._records["test"]["message"])

    def test_userrss_outcome_reaches_task_center(self):
        service = load_source("app/rsschecker.py", {"check_task_rss"},
                              dict(self.ns, traceback=traceback), "RssChecker")()
        service.get_rsstask_info = MagicMock(return_value=None)
        self.assert_rss_action_status(service, False, "failed")
        service.get_rsstask_info.return_value = {"name": "test"}
        service._RssChecker__parse_userrss_result = MagicMock(return_value=None)
        self.assert_rss_action_status(service, False, "failed")
        service._RssChecker__parse_userrss_result.return_value = []
        self.assert_rss_action_status(service, False, "succeeded")
        service._RssChecker__parse_userrss_result.return_value = [{}]
        service.downloader = MagicMock()
        service.downloader.get_download_list.return_value = []
        self.assert_rss_action_status(service, False, "succeeded")
        # A malformed entry must not become a successful terminal status.
        service._RssChecker__parse_userrss_result.return_value = [None]
        self.assert_rss_action_status(service, False, "failed")

    def test_brush_outcome_reaches_task_center(self):
        rss = MagicMock()
        namespace = dict(self.ns, Rss=rss)
        service = load_source("app/brushtask.py", {"check_task_rss"}, namespace, "BrushTask")()
        service.get_brushtask_info = MagicMock(return_value=None)
        self.assert_rss_action_status(service, True, "failed")
        task = {"name": "test", "rss_url": "https://example.invalid/rss", "rss_rule": {}}
        service.get_brushtask_info.return_value = task
        service.sites = MagicMock()
        service.sites.get_sites.return_value = None
        self.assert_rss_action_status(service, True, "failed")
        service.sites.get_sites.return_value = {"name": "site"}
        task["rss_url"] = ""
        self.assert_rss_action_status(service, True, "failed")
        task["rss_url"] = "https://example.invalid/rss"
        task["free"] = True
        self.assert_rss_action_status(service, True, "failed")
        task["free"] = False
        service.get_downloader_info = MagicMock(return_value=None)
        self.assert_rss_action_status(service, True, "failed")
        service.get_downloader_info.return_value = {"id": "downloader"}
        service._BrushTask__is_allow_new_torrent = MagicMock(return_value=False)
        self.assert_rss_action_status(service, True, "succeeded")
        service._BrushTask__is_allow_new_torrent.return_value = True
        rss.parse_rssxml.return_value = None
        self.assert_rss_action_status(service, True, "failed")
        rss.parse_rssxml.return_value = []
        self.assert_rss_action_status(service, True, "succeeded")
        rss.parse_rssxml.return_value = [{"title": "torrent", "enclosure": "https://example.invalid/torrent"}]
        service._BrushTask__remember_torrent = MagicMock(return_value=True)
        service._BrushTask__check_rss_rule = MagicMock(return_value=True)
        service._BrushTask__download_torrent = MagicMock(return_value=False)
        self.assert_rss_action_status(service, True, "failed")
        service._BrushTask__download_torrent.return_value = True
        self.assert_rss_action_status(service, True, "succeeded")

    def test_02_unsupported_proxy_structures_remain_blocked(self):
        # Supporting legacy data must not execute dict expressions or hide conflicts.
        expression = "{'http': " + self.marker_expression("proxy-shape", "'http://localhost:20171'") + "}"
        for value in (["http://localhost:20171"], True, 123, {"unexpected": "http://localhost:20171"},
                      {"http": "localhost:20171", "HTTP_PROXY": "other-host:20171"},
                      "a" * 65537, {"http": " " * 2049}, expression):
            with self.subTest(configuration_type=type(value).__name__):
                client, ns = self.tmdb_client()
                client.proxies = value
                with self.assertRaisesRegex(RuntimeError, "代理配置无效"):
                    client._call("/configuration", "")
                ns["requests"].request.assert_not_called()
                client._session.request.assert_not_called()
                self.assertNotIn(MARKER, os.environ)

    def test_02_invalid_proxy_diagnostics_identify_field_without_credentials(self):
        client, ns = self.tmdb_client()
        client.proxies = {"https": "http://audit-user:audit-password@localhost:bad-port"}
        with self.assertRaisesRegex(RuntimeError, "代理配置无效"):
            client._call("/movie/1", "")
        args = ns["logger"].error.call_args.args
        message = args[0] % args[1:] if len(args) > 1 else args[0]
        self.assertIn("https", message)
        self.assertNotIn("audit-user", message)
        self.assertNotIn("audit-password", message)
        ns["requests"].request.assert_not_called()
        client._session.request.assert_not_called()

    def test_02_proxy_host_rules_preserve_requests_precedence(self):
        # Expanding all into https would incorrectly override all://host rules.
        proxies = SAFE.normalize_proxies({
            "ALL_PROXY": "localhost:20171", "all://api.themoviedb.org": "localhost:20172",
            "HTTPS://API.TMDB.ORG/": "localhost:20173"})
        self.assertEqual(requests.utils.select_proxy("https://api.themoviedb.org/3", proxies),
                         "http://localhost:20172")
        self.assertEqual(requests.utils.select_proxy("https://api.tmdb.org/3", proxies),
                         "http://localhost:20173")
        self.assertEqual(requests.utils.select_proxy("https://example.invalid", proxies),
                         "http://localhost:20171")
        proxies["https"] = "http://localhost:20174"
        self.assertEqual(requests.utils.select_proxy("https://api.themoviedb.org/3", proxies),
                         "http://localhost:20174")
        self.assertEqual(SAFE.normalize_proxies(proxies), proxies)

    def test_02_no_proxy_metadata_uses_requests_bypass_semantics(self):
        proxy = "http://localhost:20171"
        for key in ("no", "NO_PROXY", "no_proxy"):
            with self.subTest(field=key), patch.dict(os.environ, {"HTTPS_PROXY": proxy}, clear=True):
                proxies = SAFE.normalize_proxies({key: "api.themoviedb.org, localhost"})
                self.assertEqual(proxies, {"no_proxy": "api.themoviedb.org,localhost"})
                session = requests.Session()
                settings = session.merge_environment_settings(
                    "https://api.themoviedb.org/3", dict(proxies), False, True, None)
                self.assertIsNone(requests.utils.select_proxy("https://api.themoviedb.org/3", settings["proxies"]))
                settings = session.merge_environment_settings(
                    "https://example.invalid", dict(proxies), False, True, None)
                self.assertEqual(settings["proxies"]["https"], proxy)

    def test_02_unsupported_proxy_fields_are_identified_without_secrets(self):
        # Field names are useful diagnostics; URL-shaped names must stay redacted.
        for field, visible in (("unexpected_field", True),
                               ("http://audit-user:audit-password@localhost:20171", False)):
            client, ns = self.tmdb_client()
            client.proxies = {field: "http://audit-user:audit-password@localhost:20171"}
            args = ns["logger"].error.call_args.args
            message = args[0] % args[1:]
            if visible:
                self.assertIn(field, message)
            self.assertNotIn("audit-user", message)
            self.assertNotIn("audit-password", message)
            ns["requests"].request.assert_not_called()

    def test_02_http_utils_preserves_complete_requests_proxy_maps(self):
        for proxies in ({"all": "http://localhost:20171"}, {"no_proxy": "example.invalid"},
                        {"https://example.invalid": "http://localhost:20172"}):
            with self.subTest(fields=list(proxies)):
                session = self.response_session()
                self.assertEqual(self.request_utils()(proxies=proxies, session=session).get("https://example.invalid"), "ok")
                self.assertEqual(session.get.call_args.kwargs["proxies"], proxies)

    def test_02_all_proxy_remains_available_to_git_updates(self):
        self.login_as("0")
        proxy = "http://localhost:20171"
        self.config.get_proxies.return_value = {"all": proxy}
        self.action.restart_server = MagicMock()
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(self.post("update_system", {}).json["code"], 0)
        args = [call.args[0] for call in run.call_args_list]
        self.assertIn(["git", "config", "--global", "http.proxy", proxy], args)
        self.assertIn(["git", "config", "--global", "https.proxy", proxy], args)

    def test_02_tmdb_probe_shares_transport_without_changing_running_settings(self):
        client, ns = self.tmdb_client()
        proxies = {"https": "http://localhost:20171", "no_proxy": "localhost"}
        client.proxies = proxies
        ns["requests"].request.return_value.json.return_value = {"images": {}}
        previous = dict(os.environ)
        self.assertTrue(type(client).test_connection("audit", proxies=proxies, domain="api.themoviedb.org"))
        self.assertEqual(dict(os.environ), previous)
        self.assertEqual(type(client).cached_request.cache_info().currsize, 0)
        probe_kwargs = ns["requests"].request.call_args.kwargs
        client.cache = True
        self.assertEqual(client._call("/configuration", ""), {"images": {}})
        self.assertEqual(ns["requests"].request.call_args.kwargs, probe_kwargs)

    def tmdb_network_probe(self):
        client, transport = self.tmdb_client()
        self.configuration["app"]["rmt_tmdbkey"] = "audit"
        self.config.get_proxies.return_value = {"https": "http://localhost:20171"}
        generic = MagicMock()
        namespace = dict(self.ns, TMDb=type(client), TMDbException=RuntimeError, datetime=datetime,
                         urlsplit=urlsplit, RequestUtils=generic)
        action = load_source("web/action.py", {"__net_test"}, namespace, "WebAction")
        return action._WebAction__net_test, client, transport, generic, namespace

    def test_02_web_tmdb_probe_uses_authenticated_endpoint_and_full_elapsed_time(self):
        probe, client, ns, generic, namespace = self.tmdb_network_probe()
        ns["requests"].request.return_value.json.return_value = {"images": {}}
        start = datetime.datetime(2026, 1, 1)
        namespace["datetime"] = SimpleNamespace(datetime=SimpleNamespace(now=MagicMock(
            side_effect=[start, start + datetime.timedelta(seconds=2)] * 2)))
        previous = dict(os.environ)
        for host in ("api.themoviedb.org", "api.tmdb.org"):
            result = probe(host)
            self.assertTrue(result["res"])
            self.assertEqual(result["time"], "2000 毫秒")
            args, kwargs = ns["requests"].request.call_args
            parsed = urlsplit(args[1])
            self.assertEqual((parsed.hostname, parsed.path), (host, "/3/configuration"))
            self.assertEqual(parse_qs(parsed.query)["api_key"], ["audit"])
            self.assertEqual(kwargs["timeout"], client.REQUEST_TIMEOUT)
            self.assertTrue(kwargs["verify"])
            self.assertEqual(kwargs["proxies"], self.config.get_proxies.return_value)
        generic.assert_not_called()
        self.assertEqual(dict(os.environ), previous)
        self.assertEqual(type(client).cached_request.cache_info().currsize, 0)

    def test_02_web_tmdb_probe_reports_auth_and_network_errors_safely(self):
        probe, client, ns, generic, namespace = self.tmdb_network_probe()
        ns["requests"].request.return_value.status_code = 401
        result = probe("api.themoviedb.org")
        self.assertFalse(result["res"])
        self.assertIn("认证", result["msg"])
        self.assertIn("HTTP 401", result["msg"])
        ns["requests"].request.side_effect = requests.ConnectionError("URL with api_key=audit-secret")
        result = probe("api.themoviedb.org")
        self.assertEqual(result["msg"], "ConnectionError")
        self.assertNotIn("audit-secret", str(result))
        self.configuration["app"]["rmt_tmdbkey"] = ""
        ns["requests"].request.reset_mock()
        result = probe("api.themoviedb.org")
        self.assertFalse(result["res"])
        self.assertIn("未配置", result["msg"])
        ns["requests"].request.assert_not_called()
        # Other targets retain the existing lightweight reachability probe.
        generic.return_value.get_res.return_value = SimpleNamespace(ok=True)
        self.assertTrue(probe("example.invalid")["res"])
        generic.assert_called_once_with(timeout=5)

    def test_02_tmdb_website_probe_preserves_http_failures_and_safe_error_types(self):
        probe, client, ns, generic, namespace = self.tmdb_network_probe()
        generic.return_value.get_res.return_value = SimpleNamespace(status_code=403, ok=False)
        result = probe("www.themoviedb.org")
        self.assertFalse(result["res"])
        self.assertEqual(result["http_status"], 403)
        self.assertTrue(result["reachable"])
        self.assertIn("HTTP 403", result["msg"])
        self.assertIn("拒绝访问", result["msg"])
        self.assertEqual(generic.call_args.kwargs["timeout"], client.REQUEST_TIMEOUT)
        self.assertEqual(generic.call_args.kwargs["proxies"], self.config.get_proxies.return_value)
        self.assertTrue(generic.return_value.get_res.call_args.kwargs["raise_errors"])
        for error_type in (requests.exceptions.ReadTimeout, requests.exceptions.ProxyError):
            generic.return_value.get_res.side_effect = error_type("http://audit-user:audit-secret@localhost:20171")
            result = probe("www.themoviedb.org")
            self.assertFalse(result["res"])
            self.assertFalse(result["reachable"])
            self.assertIn(error_type.__name__, result["msg"])
            self.assertNotIn("audit-secret", str(result))
        generic.return_value.get_res.side_effect = None
        generic.return_value.get_res.return_value = SimpleNamespace(status_code=200, ok=True)
        self.assertTrue(probe("www.themoviedb.org")["res"])
        generic.reset_mock()
        self.config.get_proxies.return_value = {"unsupported": "http://localhost:20171"}
        self.assertFalse(probe("www.themoviedb.org")["res"])
        generic.assert_not_called()
        ns["requests"].request.assert_not_called()

    def test_02_tmdb_website_fallback_honors_global_proxy_and_blocks_invalid_settings(self):
        client, _ = self.tmdb_client()
        proxy = {"https": "http://localhost:20171"}
        self.config.get_proxies.return_value = proxy
        factory = MagicMock()
        factory.return_value.get_res.return_value = SimpleNamespace(status_code=200, text="")
        namespace = dict(self.ns, TMDb=type(client), RequestUtils=factory,
                         StringUtils=SimpleNamespace(is_chinese=lambda _: False))
        media = load_source("app/media/media.py", {"__search_tmdb_web", "__search_tmdb_web_cached"},
                            namespace, "Media")()
        self.assertIsNone(media._Media__search_tmdb_web("Example", MediaType.MOVIE))
        factory.assert_called_once_with(headers=requests.utils.default_user_agent(),
                                        proxies=proxy, timeout=client.REQUEST_TIMEOUT)
        self.assertEqual(factory.return_value.get_res.call_args.kwargs["params"], {"query": "Example"})
        factory.reset_mock()
        self.config.get_proxies.return_value = {"unsupported": "http://localhost:20171"}
        self.assertIsNone(media._Media__search_tmdb_web("Different Example", MediaType.MOVIE))
        factory.assert_not_called()

    def test_02_tmdb_website_ua_isolated_from_global_and_site_settings(self):
        # Exercise both entry points through the real HTTP helper, inspecting
        # outgoing headers rather than only the mocked factory arguments.
        old_ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/98.0.4758.102 Safari/537.36")
        self.configuration["app"]["user_agent"] = old_ua
        self.config.get_ua.return_value = old_ua
        probe, client, _, _, namespace = self.tmdb_network_probe()
        before = deepcopy(self.configuration)
        helper = self.request_utils()
        namespace["RequestUtils"] = helper
        media_ns = dict(self.ns, TMDb=type(client), RequestUtils=helper, etree=etree,
                        StringUtils=SimpleNamespace(is_chinese=lambda _: False))
        media = load_source("app/media/media.py", {"__search_tmdb_web", "__search_tmdb_web_cached"},
                            media_ns, "Media")()
        info = {"id": 123, "title": "Example", "media_type": MediaType.MOVIE}
        media.get_tmdb_info = MagicMock(return_value=info)
        response = requests.Response()
        response.status_code = 200
        response._content = b'<a data-id="123" href="/movie/123">Example</a>'
        with patch.object(requests, "get", return_value=response) as transport:
            self.assertTrue(probe("www.themoviedb.org")["res"])
            self.assertEqual(media._Media__search_tmdb_web("Example & Test", MediaType.MOVIE), info)
            for call in transport.call_args_list:
                self.assertEqual(call.kwargs["headers"]["User-Agent"], requests.utils.default_user_agent())
                self.assertEqual(call.kwargs["headers"]["Content-Type"],
                                 "application/x-www-form-urlencoded; charset=UTF-8")
                self.assertEqual(call.kwargs["proxies"], self.config.get_proxies.return_value)
                self.assertEqual(call.kwargs["timeout"], client.REQUEST_TIMEOUT)
                self.assertTrue(call.kwargs["verify"])
            self.assertEqual(transport.call_args.kwargs["params"], {"query": "Example & Test"})
            media.get_tmdb_info.assert_called_once_with(mtype=MediaType.MOVIE, tmdbid="123")
            # Unrelated requests retain the global UA or their explicit site UA.
            helper().get_res("https://example.invalid")
            self.assertEqual(transport.call_args.kwargs["headers"]["User-Agent"], old_ua)
            helper(headers="site-specific-ua").get_res("https://example.invalid")
            self.assertEqual(transport.call_args.kwargs["headers"]["User-Agent"], "site-specific-ua")
        self.assertEqual(self.configuration, before)
        self.config.get_ua.assert_called()

    def test_02_tmdb_website_ua_is_optional_for_existing_configs(self):
        self.assertEqual(self.config.get_tmdb_web_ua(), requests.utils.default_user_agent())
        # YAML null, empty form input and whitespace all retain the existing default.
        for value in (None, "", " \t ", False, 123):
            with self.subTest(value=value):
                self.configuration["app"]["tmdb_web_user_agent"] = value
                self.assertEqual(self.config.get_tmdb_web_ua(), requests.utils.default_user_agent())
        self.configuration["app"]["tmdb_web_user_agent"] = "  Custom TMDB UA/1.0  "
        self.assertEqual(self.config.get_tmdb_web_ua(), "Custom TMDB UA/1.0")

    def test_02_tmdb_website_ua_save_reload_and_cached_retry(self):
        # Persist through the real save endpoint and reload YAML without touching user data.
        config_cls = load_source("config.py", {"get_config", "save_config", "init_config"},
                                 {"os": os, "ruamel": ruamel}, "Config")
        settings = config_cls()
        settings._config = deepcopy(self.configuration)
        settings._config["app"].update(user_agent="Global UA/1.0", rmt_tmdbkey="audit")
        self.config.get_ua.return_value = "Global UA/1.0"
        self.config.get_config.side_effect = settings.get_config
        self.config.save_config.side_effect = settings.save_config
        probe, client, api_transport, _, namespace = self.tmdb_network_probe()
        helper = self.request_utils()
        namespace["RequestUtils"] = helper
        media_ns = dict(self.ns, TMDb=type(client), RequestUtils=helper, etree=etree,
                        StringUtils=SimpleNamespace(is_chinese=lambda _: False))
        media = load_source("app/media/media.py", {"__search_tmdb_web", "__search_tmdb_web_cached"},
                            media_ns, "Media")()
        info = {"id": 123, "title": "Example", "media_type": MediaType.MOVIE}
        media.get_tmdb_info = MagicMock(return_value=info)
        response = requests.Response()
        response.status_code = 403
        response._content = b'<a data-id="123" href="/movie/123">Example</a>'
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(requests, "get", return_value=response) as transport:
            settings._config_path = os.path.join(directory, "config.yaml")
            self.assertFalse(probe("www.themoviedb.org")["res"])
            self.assertIsNone(media._Media__search_tmdb_web("Example", MediaType.MOVIE))
            self.assertEqual(transport.call_count, 2)
            self.assertTrue(all(call.kwargs["headers"]["User-Agent"] == requests.utils.default_user_agent()
                                for call in transport.call_args_list))

            custom_ua = "Custom TMDB UA/1.0"
            self.assertEqual(self.action._WebAction__update_config(
                {"app.tmdb_web_user_agent": custom_ua})["code"], 0)
            settings.init_config()
            self.assertEqual(settings.get_config("app")["tmdb_web_user_agent"], custom_ua)
            response.status_code = 200
            self.assertTrue(probe("www.themoviedb.org")["res"])
            # A new UA retries the same title, including a failure cached before saving.
            self.assertEqual(media._Media__search_tmdb_web("Example", MediaType.MOVIE), info)
            self.assertEqual(transport.call_count, 4)
            self.assertTrue(all(call.kwargs["headers"]["User-Agent"] == custom_ua
                                for call in transport.call_args_list[-2:]))
            self.assertEqual(media._Media__search_tmdb_web("Example", MediaType.MOVIE), info)
            self.assertEqual(transport.call_count, 4)

            # A configured website UA must not leak into API or generic HTTP requests.
            helper().get_res("https://example.invalid")
            self.assertEqual(transport.call_args.kwargs["headers"]["User-Agent"], "Global UA/1.0")
            helper(headers="site-specific-ua").get_res("https://example.invalid")
            self.assertEqual(transport.call_args.kwargs["headers"]["User-Agent"], "site-specific-ua")
            api_transport["requests"].request.return_value.json.return_value = {"images": {}}
            self.assertTrue(probe("api.tmdb.org")["res"])
            api_headers = api_transport["requests"].request.call_args.kwargs.get("headers", {})
            self.assertNotEqual(api_headers.get("User-Agent"), custom_ua)

            self.assertEqual(self.action._WebAction__update_config(
                {"app.tmdb_web_user_agent": ""})["code"], 0)
            settings.init_config()
            self.assertTrue(probe("www.themoviedb.org")["res"])
            self.assertEqual(media._Media__search_tmdb_web("Other Example", MediaType.MOVIE), info)
            self.assertTrue(all(call.kwargs["headers"]["User-Agent"] == requests.utils.default_user_agent()
                                for call in transport.call_args_list[-2:]))

    def test_02_legacy_proxy_normalization_still_blocks_invalid_urls(self):
        # Normalizing legacy endpoints must not permit embedded commands or nodes.
        for value in ("localhost:20171; printf marker", "http://bad host:20171",
                      "http://localhost:70000", "http://localhost:20171/ui",
                      "http://localhost:20171?command=test", "vless://node@localhost:443"):
            with self.subTest(proxy=value):
                client, ns = self.tmdb_client()
                client.proxies = {"http": value}
                for cached in (True, False):
                    client.cache = cached
                    with self.assertRaisesRegex(RuntimeError, "代理配置无效"):
                        client._call("/movie/1", "")
                ns["requests"].request.assert_not_called()
                client._session.request.assert_not_called()

    def test_02_tmdb_http_failure_is_retried_instead_of_cached(self):
        # A transient failure must not outlive recovery of the same URL and proxy.
        for status in (401, 403, 429, 503):
            with self.subTest(status=status):
                client, ns = self.tmdb_client()
                client.cache = True
                client.proxies = {"https": "http://127.0.0.1:20171"}
                failed = SimpleNamespace(headers={}, status_code=status, json=lambda: {
                    "success": False, "status_code": 9})
                recovered = SimpleNamespace(headers={}, status_code=200, json=lambda: {"audit": True})
                ns["requests"].request.side_effect = [failed, recovered]
                with self.assertRaisesRegex(RuntimeError, f"HTTP {status}"):
                    client._call("/configuration", "")
                self.assertEqual(client._call("/configuration", ""), {"audit": True})
                self.assertEqual(client._call("/configuration", ""), {"audit": True})
                self.assertEqual(ns["requests"].request.call_count, 2)

    def test_02_tmdb_invalid_payload_is_not_cached(self):
        for payload in ({"success": False, "status_code": 7}, {"errors": ["bad request"]}, "unavailable", None):
            with self.subTest(payload=payload):
                client, ns = self.tmdb_client()
                client.cache = True
                client.proxies = {}
                failed = SimpleNamespace(headers={}, status_code=200, json=lambda: payload)
                recovered = SimpleNamespace(headers={}, status_code=200, json=lambda: {"audit": True})
                ns["requests"].request.side_effect = [failed, recovered]
                with self.assertRaises(RuntimeError):
                    client._call("/configuration", "")
                self.assertEqual(client._call("/configuration", ""), {"audit": True})
                self.assertEqual(ns["requests"].request.call_count, 2)

        # A 200 HTML error page must not poison the cache either.
        client, ns = self.tmdb_client()
        client.cache = True
        client.proxies = {}
        failed = SimpleNamespace(headers={}, status_code=200, json=MagicMock(side_effect=ValueError("HTML")))
        recovered = SimpleNamespace(headers={}, status_code=200, json=lambda: {"audit": True})
        ns["requests"].request.side_effect = [failed, recovered]
        with self.assertRaisesRegex(RuntimeError, "JSON"):
            client._call("/configuration", "")
        self.assertEqual(client._call("/configuration", ""), {"audit": True})
        self.assertEqual(ns["requests"].request.call_count, 2)

    def test_02_tmdb_configuration_arrays_remain_supported(self):
        # Generic SDK calls must preserve valid array responses such as languages.
        client, ns = self.tmdb_client()
        client.proxies = {}
        languages = [{"iso_639_1": "en", "english_name": "English"}]
        for cached, transport in ((True, ns["requests"].request), (False, client._session.request)):
            client.cache = cached
            transport.return_value.json.return_value = languages
            self.assertEqual(client._call("/configuration/languages", ""), languages)

    def test_02_tmdb_transports_classify_authentication_failure_identically(self):
        client, ns = self.tmdb_client()
        client.proxies = {"https": "http://127.0.0.1:20171"}
        failed = SimpleNamespace(headers={}, status_code=401, json=lambda: {"success": False, "status_code": 7})
        messages = []
        for cached, transport in ((True, ns["requests"].request), (False, client._session.request)):
            client.cache = cached
            transport.return_value = failed
            with self.assertRaises(RuntimeError) as error:
                client._call("/configuration", "")
            messages.append(str(error.exception))
        self.assertEqual(messages[0], messages[1])
        self.assertIn("认证", messages[0])
        self.assertNotIn("api_key=", messages[0])
        self.assertEqual(ns["requests"].request.call_args.kwargs["timeout"],
                         client._session.request.call_args.kwargs["timeout"])
        self.assertEqual(ns["requests"].request.call_args.kwargs["verify"],
                         client._session.request.call_args.kwargs["verify"])

    def test_03_all_dispatch_commands_have_explicit_policy(self):
        self.assertEqual(self.dispatch_commands, set(POLICY.ACTION_PERMISSIONS))
        self.login_as("0")
        self.assertFalse(POLICY.action_allowed(self.user, "future_unlisted_command"))
        reached = MagicMock()
        self.action._actions["future_unlisted_command"] = reached
        with self.app.test_request_context():
            g.api_key_authenticated = True
            self.assertEqual(self.action.action("future_unlisted_command", {})["code"], -1)
        reached.assert_not_called()

    def test_03_permissionless_user_cannot_call_sensitive_commands(self):
        for command in ("user_manager", "update_config", "update_system", "import_custom_words",
                        "test_connection", "rename_file", "delete_history", "restory_backup"):
            with self.subTest(command=command):
                self.assertEqual(self.post(command, {}).json["code"], -1)
        self.action.dbhelper.insert_user.assert_not_called()
        self.config.save_config.assert_not_called()

    def test_03_root_only_policy_and_legitimate_roles(self):
        self.login_as(permissions="系统设置,媒体整理,服务")
        self.assertFalse(POLICY.action_allowed(self.user, "user_manager"))
        self.assertFalse(POLICY.action_allowed(self.user, "update_config"))
        # A file operator cannot enlarge the roots used by rename authorization.
        self.assertFalse(POLICY.action_allowed(self.user, "add_or_edit_sync_path"))
        self.assertFalse(POLICY.action_allowed(self.user, "delete_sync_path"))
        self.assertTrue(POLICY.action_allowed(self.user, "rename_file"))
        self.assertTrue(POLICY.action_allowed(self.user, "name_test"))
        self.assertFalse(POLICY.action_allowed(self.user, "update_site"))
        self.login_as("0")
        self.action.dbhelper.insert_user.return_value = 1
        self.assertEqual(self.post("user_manager", {
            "oper": "add", "name": "audit", "password": "test", "pris": ["系统设置"]
        }).json["code"], 0)
        self.action.dbhelper.insert_user.assert_called_once_with("audit", "hashed:test", "系统设置")

    def test_03_anonymous_user_is_rejected(self):
        self.assertEqual(self.app.test_client().post("/do", data={"cmd": "user_manager"}).json["code"], -1)

    def test_03_rename_rejects_escape_and_outside_roots(self):
        self.login_as(permissions="媒体整理")
        with tempfile.TemporaryDirectory(prefix="glm-rename-") as directory:
            root = Path(directory)
            library = root / "library"
            library.mkdir()
            source = library / "source.txt"
            source.write_text("disposable")
            self.configuration["media"]["movie_path"] = str(library)
            for name in ("../escaped.txt", str(root / "absolute.txt"), "..", r"..\escaped.txt", "C:escaped.txt"):
                with self.subTest(name=name):
                    self.assertEqual(self.post("rename_file", {"path": str(source), "name": name}).json["code"], -1)
                    self.assertTrue(source.exists())
            outside = root / "outside.txt"
            outside.write_text("outside")
            self.assertEqual(self.post("rename_file", {"path": str(outside), "name": "changed.txt"}).json["code"], -1)
            self.assertTrue(outside.exists())

    def test_03_rename_sibling_and_library_file_symlink(self):
        self.login_as(permissions="媒体整理")
        with tempfile.TemporaryDirectory(prefix="glm-rename-") as directory:
            root = Path(directory)
            library = root / "library"
            library.mkdir()
            target_data = root / "download.txt"
            target_data.write_text("data")
            source = library / "linked.txt"
            source.symlink_to(target_data)
            self.configuration["media"]["tv_path"] = [str(library)]
            self.assertEqual(self.post("rename_file", {"path": str(source), "name": "renamed.txt"}).json["code"], 0)
            self.assertTrue((library / "renamed.txt").is_symlink())
            self.assertEqual(target_data.read_text(), "data")
            # A parent symlink to an unconfigured directory must not authorize its children.
            outside_dir = root / "outside"
            outside_dir.mkdir()
            (outside_dir / "source.txt").write_text("outside")
            (library / "escape").symlink_to(outside_dir, target_is_directory=True)
            self.assertEqual(self.post("rename_file", {
                "path": str(library / "escape" / "source.txt"), "name": "renamed.txt"
            }).json["code"], -1)

    def test_03_rename_preserves_existing_destination(self):
        self.login_as(permissions="媒体整理")
        with tempfile.TemporaryDirectory(prefix="glm-rename-") as directory:
            source, target = Path(directory) / "source", Path(directory) / "target"
            source.write_text("source")
            target.write_text("target")
            self.configuration["media"]["movie_path"] = directory
            self.assertEqual(self.post("rename_file", {"path": str(source), "name": "target"}).json["code"], -1)
            self.assertEqual((source.read_text(), target.read_text()), ("source", "target"))

    def test_03_rename_concurrent_destination_is_not_overwritten(self):
        self.login_as(permissions="媒体整理")
        with tempfile.TemporaryDirectory(prefix="glm-rename-race-") as directory:
            source, target = Path(directory) / "source", Path(directory) / "target"
            source.write_text("source")
            self.configuration["media"]["movie_path"] = directory

            def competing_writer(original, destination):
                Path(destination).write_text("concurrent data")
                EXCLUSIVE.rename_exclusive(original, destination)

            with patch.dict(self.ns, rename_exclusive=competing_writer):
                self.assertEqual(self.post("rename_file", {"path": str(source), "name": "target"}).json["code"], -1)
            self.assertEqual((source.read_text(), target.read_text()), ("source", "concurrent data"))

    def test_03_rename_accepts_configured_download_and_sync_roots(self):
        self.login_as(permissions="媒体整理")
        with tempfile.TemporaryDirectory(prefix="glm-rename-config-") as directory:
            source = Path(directory) / "download.txt"
            source.write_text("data")
            self.configuration["downloaddir"] = [{"save_path": "/unmapped", "container_path": directory}]
            self.assertEqual(self.post("rename_file", {"path": str(source), "name": "sync.txt"}).json["code"], 0)
            self.configuration["downloaddir"] = []
            self.action.dbhelper.get_config_sync_paths.return_value = [
                SimpleNamespace(SOURCE=directory, DEST="", UNKNOWN="")]
            self.assertEqual(self.post("rename_file", {
                "path": str(Path(directory) / "sync.txt"), "name": "final.txt"
            }).json["code"], 0)

    def security_namespace(self, cache):
        ns = dict(self.ns, datetime=datetime, jwt=jwt, TokenCache=cache)
        return load_source("web/security.py", {"generate_access_token", "__decode_auth_token",
                                             "identify", "login_required", "require_auth", "require_api_auth",
                                             "api_permission_required", "_cached_token_user", "_authorization_token"}, ns)

    def test_03_jwt_api_cannot_bypass_command_policy(self):
        cache = Cache(maxsize=256, ttl=4 * 3600)
        ns = self.security_namespace(cache)
        token = ns["generate_access_token"]("audit")
        cache.set(token, token)
        protected = ns["login_required"](lambda: self.action.action("user_manager", {"oper": "add"}))
        with self.app.test_request_context(headers={"Authorization": token}):
            self.assertEqual(protected()["code"], -1)
        self.action.dbhelper.insert_user.assert_not_called()

    def test_03_api_key_integrations_remain_authorized(self):
        ns = self.security_namespace(MagicMock())
        self.action.dbhelper.insert_user.return_value = 1
        protected = ns["require_auth"](lambda: self.action.action("user_manager", {
            "oper": "add", "name": "api", "password": "test", "pris": []
        }))
        with self.app.test_request_context(headers={"Authorization": "Bearer " + self.configuration["security"]["api_key"]}):
            self.assertEqual(protected()["code"], 0)

    def test_03_settings_pages_and_backups_require_actual_root(self):
        reached = MagicMock(return_value="sensitive content")
        for endpoint in ("basic", "backup", "upload", "users", "downloader", "indexer", "notification"):
            self.app.add_url_rule("/" + endpoint, endpoint=endpoint, view_func=reached, methods=["GET", "POST"])
        self.login_as(permissions="系统设置")
        for endpoint in ("basic", "backup", "upload", "users", "downloader", "indexer", "notification"):
            self.assertEqual(self.client.get("/" + endpoint).status_code, 403)
        reached.assert_not_called()
        self.login_as("0")
        self.assertEqual(self.client.get("/basic").get_data(as_text=True), "sensitive content")

    def test_03_native_config_views_are_covered_by_page_policy(self):
        tree = ast.parse((ROOT / "web/main.py").read_text())
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and any(
                    isinstance(item, ast.keyword) and item.arg == "Config"
                    and ast.unparse(item.value) == "Config().get_config()" for item in ast.walk(node)):
                self.assertIn(node.name, POLICY.PAGE_ACTIONS)
                self.assertEqual(POLICY.ACTION_PERMISSIONS[POLICY.PAGE_ACTIONS[node.name]], "@admin")

    def test_03_login_returns_scoped_key_to_regular_user(self):
        cache = Cache(maxsize=256, ttl=4 * 3600)
        ns = self.security_namespace(cache)
        ns.update(Resource=object, parser=MagicMock(), user=SimpleNamespace(doc=lambda **_: lambda function: function))
        cls = load_source("web/apiv1.py", {"post"}, ns, "UserLogin")
        login = cls()
        login.parser = SimpleNamespace(parse_args=lambda: {"username": "audit", "password": "disposable"})
        self.user.verify_password = MagicMock(return_value=True)
        with self.app.test_request_context():
            reply = login.post()["data"]
        self.assertEqual(reply["apikey"], reply["token"])
        self.assertNotEqual(reply["apikey"], self.configuration["security"]["api_key"])
        self.assertEqual(cache.get(reply["token"]), reply["token"])
        self.login_as("0")
        with self.app.test_request_context():
            self.assertEqual(login.post()["data"]["apikey"], self.configuration["security"]["api_key"])

    def test_03_rest_scoped_key_preserves_allowed_operations(self):
        self.login_as(permissions="服务")
        cache = Cache(maxsize=256, ttl=4 * 3600)
        ns = self.security_namespace(cache)
        token = ns["generate_access_token"]("audit")
        cache.set(token, token)
        reached = MagicMock(return_value={"code": 0})
        self.action._actions["name_test"] = reached
        resource = ns["require_api_auth"](lambda: self.action.action("name_test", {}))
        with self.app.test_request_context(headers={"Authorization": "Bearer " + token}):
            self.assertEqual(resource()["code"], 0)
        reached.assert_called_once()
        blocked = ns["require_api_auth"](lambda: self.action.action("user_manager", {}))
        with self.app.test_request_context(headers={"Authorization": "Bearer " + token}):
            self.assertEqual(blocked()["code"], -1)
        # Native integration endpoints must not accept a user JWT as a master key.
        with self.app.test_request_context(headers={"Authorization": "Bearer " + token}):
            self.assertEqual(ns["require_auth"](lambda: "native")()["code"], 401)

    def test_03_config_info_api_checks_root_before_reading_configuration(self):
        cache = Cache(maxsize=256, ttl=4 * 3600)
        ns = self.security_namespace(cache)
        ns["ClientResource"] = object
        cls = load_source("web/apiv1.py", {"post"}, ns, "ConfigInfo")
        token = ns["generate_access_token"]("audit")
        cache.set(token, token)
        resource = ns["login_required"](cls.post)
        with self.app.test_request_context(headers={"Authorization": "Bearer " + token}):
            self.assertEqual(resource()["code"], 403)
        self.login_as("0")
        with self.app.test_request_context(headers={"Authorization": token}):
            self.assertEqual(resource()["data"], self.configuration)

    def test_03_direct_rest_methods_cannot_skip_permissions(self):
        for class_name in ("SiteStatistic", "SiteSites", "BrushTaskList", "BrushTaskDownloaderList",
                           "RssParserList", "RssList"):
            with self.subTest(resource=class_name):
                ns = self.security_namespace(MagicMock())
                ns.update(ApiResource=object, ClientResource=object)
                cls = load_source("web/apiv1.py", {"get", "post"}, ns, class_name)
                method = getattr(cls, "get", None) or cls.post
                with self.app.test_request_context():
                    g.api_user = self.user
                    self.assertEqual(method()["code"], 403)

    def test_03_real_rest_dispatch_preserves_authentication_and_permissions(self):
        # Register unchanged Resource classes with real RESTX/reqparse. Only
        # application startup, persistence and credential lookup remain mocked.
        cache = Cache(maxsize=256, ttl=4 * 3600)
        ns = self.security_namespace(cache)
        api = Api(self.app, doc=False)
        ns.update(Resource=Resource, reqparse=reqparse, user=api.namespace("audit-user"),
                  config=api.namespace("audit-config"), site=api.namespace("audit-site"))
        selected = []
        for node in ast.parse((ROOT / "web/apiv1.py").read_text()).body:
            if isinstance(node, ast.ClassDef) and node.name in {
                    "ApiResource", "ClientResource", "UserLogin", "UserManage", "ConfigInfo", "SiteSites"}:
                node.decorator_list = []
                selected.append(node)
        exec(compile(ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[])),
                     str(ROOT / "web/apiv1.py"), "exec"), ns)
        for name, path in (("UserLogin", "/audit-login"), ("UserManage", "/audit-manage"),
                           ("ConfigInfo", "/audit-config"), ("SiteSites", "/audit-sites")):
            api.add_resource(ns[name], path)
        self.user.verify_password = MagicMock(return_value=True)
        reply = self.client.post("/audit-login", data={"username": "audit", "password": "disposable"}).json
        key = reply["data"]["apikey"]
        headers = {"Authorization": "Bearer " + key}
        self.assertNotEqual(key, self.configuration["security"]["api_key"])
        self.assertEqual(self.client.post("/audit-manage", headers=headers,
                                         data={"oper": "add", "name": "other"}).json["code"], -1)
        self.assertEqual(self.client.post("/audit-config", headers=headers).json["code"], 403)
        self.assertEqual(self.client.get("/audit-sites", headers=headers).json["code"], 403)
        self.action.dbhelper.insert_user.assert_not_called()
        self.ns["Sites"].assert_not_called()

    def test_04_recommend_serializes_script_breakout_and_json_values(self):
        ns = dict(self.ns, render_template=render_template, ModuleConf=SimpleNamespace(DISCOVER_FILTER_CONF={}))
        load_source("web/main.py", {"recommend"}, ns)
        self.app.add_url_rule("/recommend", view_func=login_required(ns["recommend"]))
        payload = {"x": "</script><script>window.glmAuditMarker=1</script>", "flag": True, "empty": None}
        response = self.client.get("/recommend", query_string={"params": json.dumps(payload)})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"</script><script>window.glmAuditMarker", response.data)
        serialized = re.search(r"Params = (\{.*\});", response.get_data(as_text=True)).group(1)
        self.assertEqual(json.loads(serialized), payload)

    def test_05_expired_token_is_rejected_even_when_cached(self):
        cache = Cache(maxsize=256, ttl=4 * 3600)
        ns = self.security_namespace(cache)
        token = ns["generate_access_token"]("audit", exp=-1)
        cache.set(token, token)
        target = MagicMock()
        protected = ns["login_required"](target)
        with self.app.test_request_context(headers={"Authorization": token}):
            self.assertEqual(protected()["code"], 403)
        target.assert_not_called()
        self.assertIsNone(cache.get(token))
        self.assertEqual(ns["identify"](token), (False, ""))

    def test_05_valid_token_still_requires_cache_and_current_account(self):
        cache = Cache(maxsize=256, ttl=4 * 3600)
        ns = self.security_namespace(cache)
        token = ns["generate_access_token"]("audit")
        protected = ns["login_required"](lambda: "reached")
        with self.app.test_request_context(headers={"Authorization": token}):
            self.assertEqual(protected()["code"], 403)
            cache.set(token, token)
            self.assertEqual(protected(), "reached")
            self.assertIs(g.api_user, self.user)
        deleted = ns["generate_access_token"]("deleted-user")
        cache.set(deleted, deleted)
        with self.app.test_request_context(headers={"Authorization": deleted}):
            self.assertEqual(protected()["code"], 403)

    def test_05_cached_token_without_expiration_is_rejected(self):
        cache = Cache(maxsize=256, ttl=4 * 3600)
        ns = self.security_namespace(cache)
        token = jwt.encode({"username": "audit", "iat": datetime.datetime.utcnow()},
                           self.configuration["security"]["api_key"], algorithm="HS256")
        cache.set(token, token)
        with self.app.test_request_context(headers={"Authorization": token}):
            self.assertEqual(ns["login_required"](lambda: "reached")()["code"], 403)

    def test_05_api_key_uses_constant_time_comparison_and_handles_empty_header(self):
        ns = self.security_namespace(MagicMock())
        protected = ns["require_auth"](lambda: "reached")
        for header in ("", "   ", "Bearer wrong", "Bearer 非ASCII"):
            with self.subTest(header=header), self.app.test_request_context(headers={"Authorization": header}):
                self.assertEqual(protected()["code"], 401)
        with self.app.test_request_context(headers={"Authorization": "Bearer " + self.configuration["security"]["api_key"]}), \
                patch.object(hmac, "compare_digest", wraps=hmac.compare_digest) as compare:
            self.assertEqual(protected(), "reached")
            compare.assert_called_once()

    def test_06_admin_cannot_execute_arbitrary_connection_inputs(self):
        self.login_as("0")
        for command in (self.marker_expression("connection"), [self.marker_expression("connection")],
                        "os|system", [], None):
            with self.subTest(command=command):
                self.assertEqual(self.post("test_connection", {"command": command}).json["code"], 1)
        self.ns["importlib"].import_module.assert_not_called()
        self.config.init_config.assert_not_called()
        self.assertNotIn(MARKER, os.environ)

    def test_06_valid_legacy_probe_and_temporary_llm_config(self):
        self.login_as("0")
        probe = MagicMock()
        probe.get_status.return_value = True
        module = SimpleNamespace(Qbittorrent=lambda: probe, LLMMetaParser=lambda: probe)
        self.ns["importlib"].import_module.return_value = module
        self.assertEqual(self.post("test_connection", {
            "command": "app.downloader.client.qbittorrent|Qbittorrent"
        }).json["code"], 0)
        probe.get_status.assert_called_once_with()
        probe.get_status.reset_mock()
        temporary = {"base_url": "https://example.invalid", "api_key": "disposable"}
        self.assertEqual(self.post("test_connection", {
            "command": "app.media.meta.llm_parser|LLMMetaParser", "config": temporary
        }).json["code"], 0)
        probe.get_status.assert_called_once_with(config=temporary)

    def test_06_invalid_list_is_rejected_before_any_valid_probe(self):
        self.login_as("0")
        self.assertEqual(self.post("test_connection", {
            "command": ["app.downloader.client.qbittorrent|Qbittorrent", self.marker_expression("last")]
        }).json["code"], 1)
        self.ns["importlib"].import_module.assert_not_called()

    def test_06_allowlist_matches_all_existing_settings_probes(self):
        tree = ast.parse((ROOT / "app/conf/moduleconf.py").read_text())
        commands = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "test_command":
                        commands.add(ast.literal_eval(value))
        commands.add("app.media.meta.llm_parser|LLMMetaParser")
        self.assertEqual(set(self.action.CONNECTION_TESTS), commands)
        for module, name in self.action.CONNECTION_TESTS.values():
            declarations = ast.parse((ROOT / (module.replace(".", "/") + ".py")).read_text())
            self.assertTrue(any(isinstance(node, ast.ClassDef) and node.name == name for node in declarations.body))

    def test_06_brush_expression_rejected_and_legacy_data_supported(self):
        row = MagicMock()
        row.RSS_RULE, row.REMOVE_RULE = self.marker_expression("brush", "{}"), "{}"
        self.action.dbhelper.get_brushtasks.return_value = row
        self.assertEqual(self.action._WebAction__brushtask_detail({"id": 1})["code"], 1)
        self.assertNotIn(MARKER, os.environ)
        row.RSS_RULE, row.REMOVE_RULE = "{'include': 'literal', 'free': None}", '{"time": 24}'
        response = self.action._WebAction__brushtask_detail({"id": 1})
        self.assertEqual(response["task"]["rss_rule"], {"include": "literal", "free": None})
        self.assertEqual(response["task"]["remove_rule"], {"time": 24})

    def test_06_brush_scheduler_skips_invalid_db_rule(self):
        ns = dict(self.ns)
        cls = load_source("app/brushtask.py", {"get_brushtask_info"}, ns, "BrushTask")
        task = cls()
        task.dbhelper = MagicMock()
        for value in (self.marker_expression("scheduler", "{}"), None, "", "[]"):
            # Missing/corrupt rules must not become permissive empty task filters.
            row = SimpleNamespace(ID=1, RSS_RULE=value, REMOVE_RULE="{}")
            task.dbhelper.get_brushtasks.return_value = [row]
            self.assertEqual(task.get_brushtask_info(), [])
        self.assertNotIn(MARKER, os.environ)

    def test_06_json_form_expression_remains_literal_data(self):
        self.login_as(permissions="站点管理")
        expression = self.marker_expression("form", "{}")
        self.assertEqual(self.post("add_brushtask", {"brushtask_include": expression}).json["code"], 0)
        item = self.action.dbhelper.insert_brushtask.call_args.args[1]
        self.assertEqual(SAFE.parse_rule_dict(json.dumps(item["rss_rule"]))["include"], expression)
        self.assertNotIn(MARKER, os.environ)

    def test_06_db_writes_json_and_preserves_literal_values(self):
        # Exercise real insert/update bodies, mocking only persistence/ORM boundaries.
        ns = dict(self.ns, _db=MagicMock(), DbPersist=lambda _: lambda function: function,
                  SITEBRUSHTASK=lambda **fields: SimpleNamespace(**fields))
        cls = load_source("app/helper/db_helper.py", {"insert_brushtask"}, ns, "DbHelper")
        helper = cls()
        helper._db = MagicMock()
        helper._db.query.return_value.filter.return_value.update = MagicMock()
        ns["SITEBRUSHTASK"] = MagicMock(side_effect=lambda **fields: SimpleNamespace(**fields))
        rules = {"include": self.marker_expression("db-write", "{}"), "free": True, "optional": None}
        helper.insert_brushtask(None, {"rss_rule": rules, "remove_rule": {"time": 24}})
        fields = helper._db.insert.call_args.args[0]
        self.assertEqual(json.loads(fields.RSS_RULE), rules)
        self.assertEqual(SAFE.parse_rule_dict(fields.REMOVE_RULE), {"time": 24})
        # The ORM class ID descriptor is a service boundary in the update branch.
        ns["SITEBRUSHTASK"].ID = MagicMock()
        helper.insert_brushtask(1, {"rss_rule": rules, "remove_rule": {"time": 48}})
        updated = helper._db.query.return_value.filter.return_value.update.call_args.args[0]
        self.assertEqual(json.loads(updated["RSS_RULE"]), rules)
        self.assertEqual(SAFE.parse_rule_dict(updated["REMOVE_RULE"]), {"time": 48})
        self.assertNotIn(MARKER, os.environ)

    def history_row(self, complete=False):
        return SimpleNamespace(SOURCE_PATH="/unused", SOURCE_FILENAME="Show.mkv", DEST="/unused",
                               DEST_PATH="/unused/Show" if complete else "",
                               DEST_FILENAME="Show.mkv" if complete else "", TITLE="Show",
                               CATEGORY="", YEAR="2024", SEASON_EPISODE="S01-S02", TYPE=MediaType.TV.value)

    def test_07_ambiguous_fallback_preserves_files_and_history(self):
        self.login_as(permissions="媒体整理")
        self.action.dbhelper.get_transfer_path_by_id.return_value = [self.history_row()]
        for flag in ("del_dest", "del_all"):
            self.assertEqual(self.post("delete_history", {"logids": [42], "flag": flag}).json["retcode"], 1)
        self.action.dbhelper.delete_transfer_log_by_id.assert_not_called()
        self.action.delete_media_file.assert_not_called()

    def test_07_batch_validation_precedes_all_deletions(self):
        self.login_as(permissions="媒体整理")
        self.action.dbhelper.get_transfer_path_by_id.side_effect = [[self.history_row(True)], [self.history_row()]]
        self.assertEqual(self.post("delete_history", {"logids": [1, 2], "flag": "del_all"}).json["retcode"], 1)
        self.action.delete_media_file.assert_not_called()
        self.action.dbhelper.delete_transfer_log_by_id.assert_not_called()

    def test_07_complete_path_deletes_file_before_history(self):
        self.login_as(permissions="媒体整理")
        self.action.dbhelper.get_transfer_path_by_id.return_value = [self.history_row(True)]
        events = []
        self.action.delete_media_file.side_effect = lambda *_: (events.append("file") or True, "deleted")
        self.action.dbhelper.delete_transfer_log_by_id.side_effect = lambda *_: events.append("history")
        self.assertEqual(self.post("delete_history", {"logids": [42], "flag": "del_dest"}).json["retcode"], 0)
        self.assertEqual(events, ["file", "history"])

    def test_07_failed_file_deletion_preserves_history(self):
        self.login_as(permissions="媒体整理")
        self.action.dbhelper.get_transfer_path_by_id.return_value = [self.history_row(True)]
        self.action.delete_media_file.return_value = (False, "failed")
        self.assertEqual(self.post("delete_history", {"logids": [42], "flag": "del_dest"}).json["retcode"], 1)
        self.action.dbhelper.delete_transfer_log_by_id.assert_not_called()

    def legacy_history_directory(self, directory):
        row = self.history_row()
        row.SEASON_EPISODE, row.DEST = "S01", directory
        target = Path(directory) / "Show" / "Season 01"
        target.mkdir(parents=True)
        (target / "episode.mkv").write_text("disposable media")
        meta = SimpleNamespace(get_season_list=lambda: [1], get_episode_string=lambda: "")
        self.ns["MetaInfo"] = MagicMock(return_value=meta)
        self.ns["FileTransfer"] = lambda: SimpleNamespace(get_dest_path_by_info=lambda **_: str(target))
        self.action.dbhelper.get_transfer_path_by_id.return_value = [row]
        return target

    def test_07_unambiguous_legacy_directory_is_still_supported(self):
        self.login_as(permissions="媒体整理")
        with tempfile.TemporaryDirectory(prefix="glm-history-") as directory:
            target = self.legacy_history_directory(directory)
            self.assertEqual(self.post("delete_history", {"logids": [42], "flag": "del_dest"}).json["retcode"], 0)
            self.assertFalse(target.exists())
            self.action.dbhelper.delete_transfer_log_by_id.assert_called_once_with(42)

    def test_07_legacy_directory_failure_preserves_record(self):
        self.login_as(permissions="媒体整理")
        with tempfile.TemporaryDirectory(prefix="glm-history-") as directory:
            target = self.legacy_history_directory(directory)
            with patch("shutil.rmtree", side_effect=PermissionError("disposable failure")):
                self.assertEqual(self.post("delete_history", {"logids": [42], "flag": "del_dest"}).json["retcode"], 1)
            self.assertTrue(target.exists())
            self.action.dbhelper.delete_transfer_log_by_id.assert_not_called()

    def test_07_legacy_target_cannot_escape_destination_root(self):
        self.login_as(permissions="媒体整理")
        with tempfile.TemporaryDirectory(prefix="glm-history-") as directory:
            target = self.legacy_history_directory(directory)
            outside = Path(directory).parent / "outside-Show"
            self.ns["FileTransfer"] = lambda: SimpleNamespace(get_dest_path_by_info=lambda **_: str(outside))
            self.assertEqual(self.post("delete_history", {"logids": [42], "flag": "del_all"}).json["retcode"], 1)
            self.assertTrue(target.exists())
            self.action.delete_media_file.assert_not_called()
            self.action.dbhelper.delete_transfer_log_by_id.assert_not_called()

    def test_08_alias_method_is_unique_and_retains_limit_signature(self):
        tree = ast.parse((ROOT / "app/media/meta/llm_parser.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "LLMMetaParser")
        methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "get_alias_candidates"]
        self.assertEqual(len(methods), 1)
        self.assertEqual([arg.arg for arg in methods[0].args.args], ["self", "title", "subtitle", "limit"])

    def cached_parser(self):
        clock = [1000]
        ns = {"time": SimpleNamespace(time=lambda: clock[0]), "deepcopy": deepcopy}
        cls = load_source("app/media/meta/llm_parser.py",
                          {"__set_cached_parse_result", "__get_cached_parse_result", "__prune_parse_cache"},
                          ns, "LLMMetaParser")
        parser = cls()
        parser._parse_cache = {}
        parser._parse_cache_ttl = 60
        parser._parse_cache_lock = RLock()
        return parser, clock

    def test_09_capacity_and_unread_expiry_are_bounded(self):
        parser, clock = self.cached_parser()
        for number in range(3000):
            parser._LLMMetaParser__set_cached_parse_result(str(number), {"title": "test"})
        self.assertLessEqual(len(parser._parse_cache), 256)
        clock[0] += 61
        self.assertIsNone(parser._LLMMetaParser__get_cached_parse_result("missing"))
        self.assertEqual(parser._parse_cache, {})
        parser._LLMMetaParser__set_cached_parse_result("new", {})
        self.assertEqual(len(parser._parse_cache), 1)

    def test_09_lru_access_and_negative_cache_keep_copy_isolation(self):
        parser, _ = self.cached_parser()
        parser._parse_cache_maxsize = 3
        for key in ("a", "b", "c"):
            parser._LLMMetaParser__set_cached_parse_result(key, {"values": [1]})
        result = parser._LLMMetaParser__get_cached_parse_result("a")
        result["values"].append(2)
        parser._LLMMetaParser__set_cached_parse_result("d", {})
        self.assertIsNone(parser._LLMMetaParser__get_cached_parse_result("b"))
        self.assertEqual(parser._LLMMetaParser__get_cached_parse_result("a"), {"values": [1]})
        self.assertEqual(parser._LLMMetaParser__get_cached_parse_result("d"), {})

    def test_09_concurrent_cache_writes_remain_bounded(self):
        parser, _ = self.cached_parser()

        def worker(prefix):
            for number in range(150):
                key = "%s-%s" % (prefix, number)
                parser._LLMMetaParser__set_cached_parse_result(key, {})
                parser._LLMMetaParser__get_cached_parse_result(key)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(worker, range(4)))
        self.assertLessEqual(len(parser._parse_cache), 256)

    def test_10_invalid_ignore_configuration_is_not_saved(self):
        self.login_as("0")
        for key in ("ignored_paths", "ignored_files"):
            self.assertEqual(self.post("update_config", {"media." + key: "["}).json["code"], 1)
        self.config.save_config.assert_not_called()
        self.assertEqual(self.configuration["media"], {})

    def test_10_invalid_persisted_filters_pause_transfer_without_crash(self):
        cls = load_source("app/filetransfer.py", {"init_config", "check_ignore"}, dict(self.ns), "FileTransfer")
        for key in ("ignored_paths", "ignored_files"):
            self.configuration["media"] = {key: "["}
            transfer = cls()
            transfer.category = MagicMock()
            transfer.init_config()
            self.assertTrue(transfer._ignore_config_error)
            files, reason = transfer.check_ignore(["/library/Show.mkv"])
            self.assertEqual(files, [])
            self.assertIn("配置无效", reason)

    def test_10_valid_filter_and_clear_on_reload(self):
        cls = load_source("app/filetransfer.py", {"init_config", "check_ignore"}, dict(self.ns), "FileTransfer")
        transfer = cls()
        transfer.category = MagicMock()
        self.configuration["media"] = {"ignored_files": r"\[SP\]"}
        transfer.init_config()
        self.assertEqual(transfer.check_ignore(["/library/Show [SP].mkv", "/library/Show.mkv"])[0],
                         ["/library/Show.mkv"])
        self.configuration["media"] = {}
        transfer.init_config()
        self.assertIsNone(transfer._ignored_files)
        self.assertEqual(transfer.check_ignore(["/library/Show [SP].mkv"])[0], ["/library/Show [SP].mkv"])

    def request_utils(self):
        cls = load_source("app/utils/http_utils.py", {"__init__", "get", "get_res"}, dict(self.ns, requests=requests), "RequestUtils")
        return cls

    def test_11_response_diagnostics_opt_in_preserves_default_error_handling(self):
        session = self.response_session()
        session.get.side_effect = requests.exceptions.ReadTimeout("private proxy URL")
        client = self.request_utils()(session=session)
        self.assertIsNone(client.get_res("https://example.invalid"))
        with self.assertRaises(requests.exceptions.ReadTimeout):
            client.get_res("https://example.invalid", raise_errors=True)

    def response_session(self, content=b"ok", encoding=None, headers=None):
        session = MagicMock()
        session.get.return_value.content = content
        session.get.return_value.encoding = encoding
        session.get.return_value.headers = headers or {}
        return session

    def test_11_default_tls_verification_and_private_ca(self):
        for verify in (True, "/tmp/disposable-ca.pem"):
            session = self.response_session()
            kwargs = {} if verify is True else {"verify": verify}
            self.assertEqual(self.request_utils()(session=session, **kwargs).get("https://example.invalid"), "ok")
            self.assertEqual(session.get.call_args.kwargs["verify"], verify)
        session = self.response_session()
        session.get.side_effect = requests.exceptions.SSLError("untrusted")
        self.assertIsNone(self.request_utils()(session=session).get("https://example.invalid"))

    def test_11_openclaw_login_verifies_tls(self):
        ns = {"requests": MagicMock(), "quote": lambda value: value,
              "BOT_TYPE": "3", "QR_POLL_TIMEOUT": 35, "_headers": lambda: {}}
        load_source("app/helper/openclaw_wechat_login.py", {"fetch_qrcode", "poll_status"}, ns)
        ns["fetch_qrcode"]("https://example.invalid")
        ns["poll_status"]("https://example.invalid", "qr")
        self.assertTrue(all(call.kwargs["verify"] is True for call in ns["requests"].get.call_args_list))
        self.assertNotIn("disable_warnings", (ROOT / "app/utils/http_utils.py").read_text())
        self.assertNotIn("disable_warnings", (ROOT / "app/helper/openclaw_wechat_login.py").read_text())

    def test_12_exception_prints_removed(self):
        tree = ast.parse((ROOT / "app/media/media.py").read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and ast.unparse(node) == "print(str(err))"]
        self.assertEqual(calls, [])

    def test_12_web_parse_error_uses_logging_without_stdout(self):
        self.config.get_proxies.return_value = {}
        helper = MagicMock()
        helper.get_res.return_value.status_code = 200
        helper.get_res.return_value.text = "invalid response"
        ns = dict(self.ns, TMDb=type(self.tmdb_client()[0]),
                  StringUtils=SimpleNamespace(is_chinese=lambda _: False), RequestUtils=lambda **_: helper,
                  etree=SimpleNamespace(HTML=MagicMock(side_effect=ValueError("invalid HTML"))))
        cls = load_source("app/media/media.py", {"__search_tmdb_web", "__search_tmdb_web_cached"}, ns, "Media")
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertIsNone(cls()._Media__search_tmdb_web("Example", MediaType.MOVIE))
            self.assertEqual(output.getvalue(), "")
        self.ns["log"].error.assert_called_once()

    def test_13_query_structure_characters_and_spaces_remain_intact(self):
        self.config.get_proxies.return_value = {}
        helper = MagicMock()
        helper.get_res.return_value = None
        ns = dict(self.ns, TMDb=type(self.tmdb_client()[0]),
                  StringUtils=SimpleNamespace(is_chinese=lambda _: False), RequestUtils=lambda **_: helper)
        cls = load_source("app/media/media.py", {"__search_tmdb_web", "__search_tmdb_web_cached"}, ns, "Media")
        for title in ("Love & Peace", "Love Peace", "C++ #1 / 100%", "Quote's Title"):
            cls()._Media__search_tmdb_web(title, MediaType.MOVIE)
            prepared = requests.Request("GET", **helper.get_res.call_args.kwargs).prepare()
            self.assertEqual(parse_qs(urlsplit(prepared.url).query)["query"], [title])

    def test_14_vote_is_numeric_in_python_and_json(self):
        ns = dict(self.ns, TMDB_IMAGE_W500_URL="https://example.invalid/%s")
        cls = load_source("app/media/media.py", {"__dict_tmdbinfos"}, ns, "Media")
        for score in (8.0, None):
            row = cls._Media__dict_tmdbinfos([{"id": 1, "title": "Test", "vote_average": score}], MediaType.MOVIE)[0]
            self.assertIsInstance(row["vote"], (int, float))
            self.assertEqual(json.loads(json.dumps(row))["vote"], score or 0)

    def test_15_invalid_utf8_returns_failure_sentinel(self):
        session = self.response_session(b"\xff\xfe")
        self.assertIsNone(self.request_utils()(session=session).get("https://example.invalid"))

    def test_15_declared_encoding_is_respected_and_bad_codec_is_handled(self):
        session = self.response_session("测试".encode("gb18030"), "gb18030",
                                        {"Content-Type": "text/plain; charset=gb18030"})
        self.assertEqual(self.request_utils()(session=session).get("https://example.invalid"), "测试")
        session = self.response_session(b"ok", "nonexistent-codec",
                                        {"Content-Type": "text/plain; charset=nonexistent-codec"})
        self.assertIsNone(self.request_utils()(session=session).get("https://example.invalid"))

    def test_15_implicit_http_encoding_keeps_utf8_compatibility(self):
        # Requests guesses Latin-1 for text/html without charset; preserve UTF-8.
        session = self.response_session("测试".encode("utf-8"), "ISO-8859-1", {"Content-Type": "text/html"})
        self.assertEqual(self.request_utils()(session=session).get("https://example.invalid"), "测试")


if __name__ == "__main__":
    unittest.main()
