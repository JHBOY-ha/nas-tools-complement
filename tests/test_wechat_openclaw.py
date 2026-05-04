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
            ok, msg = client.send_msg(title="标题", text="内容", image="https://img", url="https://url")

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
        self.assertIn("https://img", text)

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
