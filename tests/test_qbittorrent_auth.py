"""Offline regression tests for the bundled qBittorrent client."""

import importlib.util
import sys
import threading
import types
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "third_party" / "qbittorrent-api"
sys.path.insert(0, str(BUNDLED))
try:
    import qbittorrentapi
finally:
    sys.path.remove(str(BUNDLED))


@contextmanager
def api_server(cookie="QBT_SID_8080", status=204, value="token", expire=False):
    state = types.SimpleNamespace(logins=0, cookies=[], expired=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body="", set_cookie=None):
            self.send_response(code)
            if set_cookie is not None:
                self.send_header("Set-Cookie", set_cookie + "; Path=/; HttpOnly")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_POST(self):
            if self.path != "/api/v2/auth/login":
                self.reply(404)
                return
            data = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            state.logins += 1
            if data.get("password") != ["test-password"]:
                self.reply(200, "Fails.")
                return
            token = value + str(state.logins) if value else ""
            state.token = token
            self.reply(status, "Ok." if status == 200 else "",
                       None if cookie is None else cookie + "=" + token)

        def do_GET(self):
            sent = self.headers.get("Cookie", "")
            state.cookies.append(sent)
            if expire and not state.expired:
                state.expired = True
                self.reply(403)
            elif cookie and cookie + "=" + state.token in sent:
                self.reply(200, "v5.2.3.10")
            else:
                self.reply(403)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        # Prevent environment proxy settings from affecting loopback tests.
        with patch.dict("os.environ", {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
            client = qbittorrentapi.Client(
                host="http://127.0.0.1", port=server.server_port,
                username="test-user", password="test-password",
                REQUESTS_ARGS={"timeout": (2, 2)},
            )
            try:
                yield client, state
            finally:
                client._trigger_session_initialization()
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


class AuthenticationTests(unittest.TestCase):
    def test_bundled_module(self):
        self.assertTrue(Path(qbittorrentapi.__file__).resolve().is_relative_to(BUNDLED))

    def test_old_and_new_cookies_reach_protected_api(self):
        for name, status in (("SID", 200), ("QBT_SID_8080", 204)):
            with self.subTest(name=name), api_server(name, status) as (client, state):
                client.auth_log_in()
                self.assertTrue(client.is_logged_in)
                self.assertEqual(client.app_version(), "v5.2.3.10")
                self.assertEqual(state.logins, 1)
                self.assertEqual(state.cookies, [name + "=token1"])

    def test_invalid_cookies_rejected(self):
        for name, value in ((None, "token"), ("SID", ""), ("QBT_SID_8080", ""),
                            ("other", "token"), ("QBT_SID_", "token"),
                            ("QBT_SID_8080_extra", "token")):
            with self.subTest(name=name, value=value), api_server(name, value=value) as (client, _):
                with self.assertRaises(qbittorrentapi.LoginFailed):
                    client.auth_log_in()
                self.assertFalse(client.is_logged_in)

    def test_wrong_password_rejected(self):
        with api_server() as (client, _):
            with self.assertRaises(qbittorrentapi.LoginFailed):
                client.auth_log_in(username="test-user", password="wrong")
            self.assertFalse(client.is_logged_in)

    def test_403_renews_cookie(self):
        with api_server(expire=True) as (client, state):
            client.auth_log_in()
            self.assertEqual(client.app_version(), "v5.2.3.10")
            self.assertEqual(state.logins, 2)
            self.assertEqual(state.cookies, ["QBT_SID_8080=token1", "QBT_SID_8080=token2"])

    def test_legacy_cookie_precedence_and_empty_fallback(self):
        with api_server() as (client, _):
            client.auth_log_in()
            client._http_session.cookies.set("SID", "legacy")
            self.assertEqual(client._SID, "legacy")
            client._http_session.cookies.set("SID", "")
            self.assertEqual(client._SID, "token1")


def load_wrapper():
    """Load the real wrapper without importing application/database startup."""
    stubs = {}
    for name in ("log", "app.downloader.client._base", "app.utils", "app.utils.types", "config"):
        stubs[name] = types.ModuleType(name)
    stubs["log"].error = Mock()
    stubs["app.downloader.client._base"]._IDownloadClient = object
    stubs["app.utils"].ExceptionUtils = Mock()
    stubs["app.utils"].StringUtils = Mock()
    stubs["app.utils.types"].DownloaderType = types.SimpleNamespace(QB=types.SimpleNamespace(value="qBittorrent"))
    stubs["config"].Config = Mock()
    spec = importlib.util.spec_from_file_location("qbt_wrapper_under_test", ROOT / "app/downloader/client/qbittorrent.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.module = load_wrapper()
        self.client = Mock()
        self.client.app_version.return_value = "v5.2.3.10"
        self.factory = patch.object(qbittorrentapi, "Client", return_value=self.client)
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.config = dict(qbhost="http://127.0.0.1", qbport=8080,
                           qbusername="test-user", qbpassword="test-password")

    def test_success(self):
        wrapper = self.module.Qbittorrent(self.config)
        self.assertIs(wrapper.qbc, self.client)
        self.assertEqual(wrapper.ver, "v5.2.3.10")

    def test_login_failure(self):
        self.client.auth_log_in.side_effect = qbittorrentapi.LoginFailed()
        wrapper = self.module.Qbittorrent(self.config)
        self.assertIsNone(wrapper.qbc)
        self.assertIsNone(wrapper.ver)
        self.assertFalse(wrapper.get_status())
        self.client.app_version.assert_not_called()
        self.module.log.error.assert_called_once()

    def test_reconnect_failure_clears_state(self):
        for method in ("auth_log_in", "app_version"):
            with self.subTest(method=method):
                self.client.auth_log_in.side_effect = None
                self.client.app_version.side_effect = None
                wrapper = self.module.Qbittorrent(self.config)
                getattr(self.client, method).side_effect = (
                    qbittorrentapi.LoginFailed() if method == "auth_log_in" else RuntimeError("version unavailable")
                )
                wrapper.connect()
                self.assertIsNone(wrapper.qbc)
                self.assertIsNone(wrapper.ver)
                self.assertFalse(wrapper.get_status())

    def test_missing_host_clears_state(self):
        wrapper = self.module.Qbittorrent(self.config)
        wrapper.host = None
        wrapper.connect()
        self.assertIsNone(wrapper.qbc)
        self.assertIsNone(wrapper.ver)


if __name__ == "__main__":
    unittest.main()
