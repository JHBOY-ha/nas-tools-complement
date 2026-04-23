# -*- coding: utf-8 -*-

import os
from unittest import TestCase
from unittest.mock import Mock

from app.rss import Rss


class RssPrefilterTest(TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("NASTOOL_CONFIG"):
            root_path = os.path.dirname(os.path.dirname(__file__))
            os.environ["NASTOOL_CONFIG"] = os.path.join(root_path, "config", "config.yaml")

    def setUp(self):
        self.rss = Rss.__new__(Rss)
        self.rss.subscribe = Mock()
        self.rss.subscribe.get_subscribe_tv_episodes.return_value = [66, 69, 70, 73]
        self.rss_info = {
            "id": 1,
            "name": "宝可梦 地平线",
            "rss_sites": ["测试站点"],
            "season": "S01",
            "over_edition": False,
            "current_ep": None,
            "total": 135
        }

    def test_skip_matched_rss_article_when_episode_is_not_lacking(self):
        title = "[jibaketa合成&二次壓制][HOY粵語]寶可夢 地平線 / Pocket Monsters (2023) - 60 [粵語](WEB 1920x1080 x264 AAC)"

        result = self.rss._Rss__should_skip_rss_article_before_identify(
            title=title,
            site_name="测试站点",
            rss_tvs={"1": self.rss_info}
        )

        self.assertTrue(result)

    def test_keep_matched_rss_article_when_episode_is_lacking(self):
        title = "[jibaketa合成&二次壓制][HOY粵語]寶可夢 地平線 / Pocket Monsters (2023) - 73 [粵語](WEB 1920x1080 x264 AAC)"

        result = self.rss._Rss__should_skip_rss_article_before_identify(
            title=title,
            site_name="测试站点",
            rss_tvs={"1": self.rss_info}
        )

        self.assertFalse(result)

    def test_do_not_skip_article_that_only_matches_subscribe_search_keyword(self):
        title = "[LoliHouse] 日本三国 / Nippon Sangoku / 日本三國 - 03 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
        rss_info = dict(self.rss_info)
        rss_info["keyword"] = "日本三国"

        result = self.rss._Rss__should_skip_rss_article_before_identify(
            title=title,
            site_name="测试站点",
            rss_tvs={"1": rss_info}
        )

        self.assertFalse(result)
