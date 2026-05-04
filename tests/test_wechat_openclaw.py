# -*- coding: utf-8 -*-

import os
from unittest import TestCase
from unittest.mock import patch


class WeChatOpenClawTest(TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("NASTOOL_CONFIG"):
            root_path = os.path.dirname(os.path.dirname(__file__))
            os.environ["NASTOOL_CONFIG"] = os.path.join(root_path, "config", "config.yaml")

    def test_test_mode_does_not_start_poll_thread(self):
        from app.message.client.wechat_openclaw import WeChatOpenClaw

        with patch.object(WeChatOpenClaw, "_WeChatOpenClaw__start_poll_thread") as start_poll, \
                patch("app.message.client.wechat_openclaw.Config") as config_cls:
            config_cls.return_value.get_temp_path.return_value = "/tmp"

            client = WeChatOpenClaw({
                "bot_token": "token",
                "to_user_id": "user",
                "test": True,
            })

        self.assertTrue(client.match("wechat_openclaw"))
        start_poll.assert_not_called()

    def test_send_msg_builds_text_payload_with_cached_context_token(self):
        from app.message.client.wechat_openclaw import WeChatOpenClaw

        with patch.object(WeChatOpenClaw, "_WeChatOpenClaw__start_poll_thread"), \
                patch("app.message.client.wechat_openclaw.Config") as config_cls:
            config_cls.return_value.get_temp_path.return_value = "/tmp"
            client = WeChatOpenClaw({
                "bot_token": "token",
                "to_user_id": "user-a",
                "test": True,
            })

        client._context_tokens["user-a"] = "ctx-token"

        with patch.object(client, "_WeChatOpenClaw__post_send", return_value=(True, "")) as post_send:
            ok, msg = client.send_msg(title="标题", text="内容", url="https://url")

        self.assertTrue(ok)
        self.assertEqual("", msg)
        payload = post_send.call_args.args[0]
        sent_msg = payload["msg"]
        self.assertEqual("user-a", sent_msg["to_user_id"])
        self.assertEqual("ctx-token", sent_msg["context_token"])
        text = sent_msg["item_list"][0]["text_item"]["text"]
        self.assertIn("标题", text)
        self.assertIn("内容", text)
        self.assertIn("https://url", text)

    def test_send_msg_uploads_image_and_sends_image_item(self):
        from app.message.client.wechat_openclaw import WeChatOpenClaw

        with patch.object(WeChatOpenClaw, "_WeChatOpenClaw__start_poll_thread"), \
                patch("app.message.client.wechat_openclaw.Config") as config_cls:
            config_cls.return_value.get_temp_path.return_value = "/tmp"
            client = WeChatOpenClaw({
                "bot_token": "token",
                "to_user_id": "user-a",
                "test": True,
            })

        client._context_tokens["user-a"] = "ctx-token"
        uploaded = {
            "download_param": "download-param",
            "aeskey": b"0123456789abcdef",
            "ciphertext_size": 32,
        }

        with patch.object(client, "_WeChatOpenClaw__read_image_bytes", return_value=(b"img", "")), \
                patch.object(client, "_WeChatOpenClaw__upload_image_to_cdn", return_value=(uploaded, "")), \
                patch.object(client, "_WeChatOpenClaw__post_send", return_value=(True, "")) as post_send:
            ok, msg = client.send_msg(title="标题", text="内容", image="https://img")

        self.assertTrue(ok)
        self.assertEqual("", msg)
        self.assertEqual(2, post_send.call_count)
        text_payload = post_send.call_args_list[0].args[0]
        image_payload = post_send.call_args_list[1].args[0]
        self.assertEqual(1, text_payload["msg"]["item_list"][0]["type"])
        image_item = image_payload["msg"]["item_list"][0]
        self.assertEqual(2, image_item["type"])
        self.assertEqual("download-param", image_item["image_item"]["media"]["encrypt_query_param"])
        self.assertEqual(
            "MzAzMTMyMzMzNDM1MzYzNzM4Mzk2MTYyNjM2NDY1NjY=",
            image_item["image_item"]["media"]["aes_key"]
        )
        self.assertEqual(32, image_item["image_item"]["mid_size"])

    def test_send_msg_falls_back_to_text_when_image_upload_fails(self):
        from app.message.client.wechat_openclaw import WeChatOpenClaw

        with patch.object(WeChatOpenClaw, "_WeChatOpenClaw__start_poll_thread"), \
                patch("app.message.client.wechat_openclaw.Config") as config_cls:
            config_cls.return_value.get_temp_path.return_value = "/tmp"
            client = WeChatOpenClaw({
                "bot_token": "token",
                "to_user_id": "user-a",
                "test": True,
            })

        with patch.object(client, "_WeChatOpenClaw__read_image_bytes", return_value=(None, "download failed")), \
                patch.object(client, "_WeChatOpenClaw__post_send", return_value=(True, "")) as post_send:
            ok, msg = client.send_msg(title="标题", text="内容", image="https://img")

        self.assertTrue(ok)
        self.assertEqual("", msg)
        self.assertEqual(1, post_send.call_count)
        payload = post_send.call_args.args[0]
        text = payload["msg"]["item_list"][0]["text_item"]["text"]
        self.assertIn("标题", text)
        self.assertIn("内容", text)
        self.assertIn("https://img", text)

    def test_upload_image_to_cdn_encrypts_and_posts_to_encoded_upload_url(self):
        from app.message.client.wechat_openclaw import WeChatOpenClaw

        with patch.object(WeChatOpenClaw, "_WeChatOpenClaw__start_poll_thread"), \
                patch("app.message.client.wechat_openclaw.Config") as config_cls:
            config_cls.return_value.get_temp_path.return_value = "/tmp"
            client = WeChatOpenClaw({
                "bot_token": "token",
                "to_user_id": "user-a",
                "cdn_base_url": "https://cdn.example/c2c",
                "test": True,
            })

        class UploadResponse:
            status_code = 200
            text = ""
            headers = {"x-encrypted-param": "download-param"}

        upload_calls = []

        class FakeRequestUtils:
            def __init__(self, *args, **kwargs):
                pass

            def post_res(self, url, params=None):
                upload_calls.append((url, params))
                return UploadResponse()

        with patch.object(client, "_WeChatOpenClaw__get_upload_url",
                          return_value=({"upload_param": "up/param + token"}, "")), \
                patch("app.message.client.wechat_openclaw.secrets.token_bytes",
                      return_value=b"0123456789abcdef"), \
                patch("app.message.client.wechat_openclaw.secrets.token_hex",
                      return_value="file/key"), \
                patch("app.message.client.wechat_openclaw.RequestUtils", FakeRequestUtils):
            uploaded, err = client._WeChatOpenClaw__upload_image_to_cdn(b"img", "user-a")

        self.assertEqual("", err)
        self.assertEqual("download-param", uploaded["download_param"])
        self.assertEqual(b"0123456789abcdef", uploaded["aeskey"])
        self.assertEqual(16, uploaded["ciphertext_size"])
        self.assertEqual(1, len(upload_calls))
        upload_url, encrypted_body = upload_calls[0]
        self.assertEqual(
            "https://cdn.example/c2c/upload?"
            "encrypted_query_param=up%2Fparam%20%2B%20token&filekey=file%2Fkey",
            upload_url
        )
        self.assertIsInstance(encrypted_body, bytes)
        self.assertNotEqual(b"img", encrypted_body)

    def test_get_updates_only_caches_context_token_when_interactive_enabled(self):
        from app.message.client.wechat_openclaw import WeChatOpenClaw

        with patch.object(WeChatOpenClaw, "_WeChatOpenClaw__start_poll_thread"), \
                patch("app.message.client.wechat_openclaw.Config") as config_cls:
            config_cls.return_value.get_temp_path.return_value = "/tmp"
            client = WeChatOpenClaw({
                "bot_token": "token",
                "to_user_id": "user-a",
                "interactive": 1,
                "test": True,
            })

        res = type("Response", (), {})()
        res.text = "{}"
        res.json = lambda: {
            "ret": 0,
            "get_updates_buf": "buf-new",
            "msgs": [
                {
                    "from_user_id": "user-a",
                    "context_token": "ctx-new",
                    "message_type": 1,
                    "item_list": [
                        {"type": 1, "text_item": {"text": "搜索电影"}}
                    ],
                }
            ],
        }

        with patch.object(client, "_WeChatOpenClaw__post_raw", return_value=res), \
                patch.object(client, "_WeChatOpenClaw__save_state"), \
                patch("web.action.WebAction") as web_action_cls:
            data, ok = client._WeChatOpenClaw__do_get_updates()

        self.assertTrue(ok)
        self.assertEqual(0, data.get("ret"))
        self.assertEqual("ctx-new", client._context_tokens.get("user-a"))
        self.assertEqual("buf-new", client._get_updates_buf)
        web_action_cls.assert_not_called()
