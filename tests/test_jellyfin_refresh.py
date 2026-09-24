import unittest
from unittest.mock import Mock, patch

import app.mediaserver.client.jellyfin as jellyfin_module
from app.mediaserver.client.jellyfin import Jellyfin


class FakeResponse:
    """Mimic requests.Response truthiness: 4xx/5xx are falsy."""

    def __init__(self, status_code):
        self.status_code = status_code

    def __bool__(self):
        return 200 <= self.status_code < 400


class JellyfinRefreshTest(unittest.TestCase):
    def client(self):
        with patch.object(Jellyfin, "get_admin_user", return_value={}):
            return Jellyfin(config={"host": "http://jellyfin:8096", "api_key": "TOKEN"})

    def test_item_refresh_sends_authorization_header(self):
        client = self.client()
        seen = {}

        class FakeRequestUtils:
            def __init__(self, **kwargs):
                seen["headers"] = dict(kwargs.get("headers") or {})

            def post_res(self, url, **kwargs):
                seen["url"] = url
                return FakeResponse(204)

        with patch.object(jellyfin_module, "RequestUtils", FakeRequestUtils):
            result = client.refresh_subtitle_target(server_item_id="item-1", parent_server_item_id="parent-1")
        self.assertEqual(result["status"], "refreshed")
        self.assertEqual(result["scope"], "item")
        self.assertIn("/Items/item-1/Refresh", seen["url"])
        self.assertEqual(seen["headers"].get("Authorization"), 'MediaBrowser Token="TOKEN"')

    def test_rejected_refresh_falls_back_to_parent_and_logs_status(self):
        client = self.client()
        requested = []

        class FakeRequestUtils:
            def __init__(self, **kwargs):
                pass

            def post_res(self, url, **kwargs):
                requested.append(url)
                return FakeResponse(401)

        logger = Mock()
        with patch.object(jellyfin_module, "RequestUtils", FakeRequestUtils), \
                patch.object(jellyfin_module, "log", logger):
            result = client.refresh_subtitle_target(server_item_id="item-1", parent_server_item_id="parent-1")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["scope"], "parent")
        self.assertEqual(result["item_id"], "parent-1")
        self.assertEqual(len(requested), 2)
        self.assertTrue(all("/Refresh" in url for url in requested))
        self.assertIn("401", logger.error.call_args[0][0])

    def test_missing_item_id_skips_without_request(self):
        client = self.client()
        with patch.object(jellyfin_module, "RequestUtils") as request_utils:
            result = client.refresh_subtitle_target(media_path="/media/show/S01E01.mkv")
        self.assertEqual(result["status"], "skipped")
        request_utils.assert_not_called()


if __name__ == "__main__":
    unittest.main()
