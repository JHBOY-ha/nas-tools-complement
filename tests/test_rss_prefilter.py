# -*- coding: utf-8 -*-

import os
from unittest import TestCase
from unittest.mock import Mock

from app.rss import Rss
from tests.recognition_fixture import FakeTmdb


class RssPrefilterTest(TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("NASTOOL_CONFIG"):
            root_path = os.path.dirname(os.path.dirname(__file__))
            os.environ["NASTOOL_CONFIG"] = os.path.join(root_path, "config", "config.yaml")

    def setUp(self):
        self.fixture = FakeTmdb(self)
        self.fixture.tv(1, "宝可梦 地平线", original="Pocket Monsters", year="2023", genres=(16,), seasons={1: 135})
        self.fixture.tv(2, "日本三国", original="Nippon Sangoku", genres=(16,), seasons={1: 12})
        self.rss = Rss.__new__(Rss)
        self.rss.filter = Mock()
        self.rss.filter.check_torrent_filter.return_value = (True, 0, "")
        self.rss.subscribe = Mock()
        self.rss.subscribe.get_subscribe_tv_episodes.return_value = [66, 69, 70, 73]
        self.rss_info = {
            "id": 1,
            "tmdbid": "1",
            "name": "宝可梦 地平线",
            "rss_sites": ["测试站点"],
            "season": "S01",
            "over_edition": False,
            "current_ep": None,
            "total": 135
        }

    def test_skip_matched_rss_article_when_episode_is_not_lacking(self):
        title = "[jibaketa合成&二次壓制][HOY粵語]寶可夢 地平線 / Pocket Monsters (2023) - 60 [粵語](WEB 1920x1080 x264 AAC)"

        result = self.rss._Rss__should_skip_identified_rss_article(
            media_info=self.fixture.media.get_media_info(title),
            rss_info=self.rss_info
        )

        self.assertTrue(result)

    def test_keep_matched_rss_article_when_episode_is_lacking(self):
        title = "[jibaketa合成&二次壓制][HOY粵語]寶可夢 地平線 / Pocket Monsters (2023) - 73 [粵語](WEB 1920x1080 x264 AAC)"

        result = self.rss._Rss__should_skip_identified_rss_article(
            media_info=self.fixture.media.get_media_info(title),
            rss_info=self.rss_info
        )

        self.assertFalse(result)

    def test_do_not_skip_article_that_only_matches_subscribe_search_keyword(self):
        title = "[LoliHouse] 日本三国 / Nippon Sangoku / 日本三國 - 03 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
        rss_info = dict(self.rss_info)
        rss_info["keyword"] = "日本三国"

        result = self.rss._Rss__should_skip_identified_rss_article(
            media_info=self.fixture.media.get_media_info(title),
            rss_info=rss_info
        )

        self.assertFalse(result)

    def test_fuzzy_rule_can_accept_episode_not_missing_from_exact_rule(self):
        fuzzy = dict(self.rss_info, id=2, tmdbid=None, name="Pocket.*Monsters", fuzzy_match=True)
        self.rss.media = self.fixture.media
        self.rss._sites = [{"name": "测试站点", "rssurl": "offline"}]
        self.rss.subscribe.get_subscribe_movies.return_value = {}
        self.rss.subscribe.get_subscribe_tvs.return_value = {"2": fuzzy, "1": self.rss_info}
        self.rss.dbhelper = Mock()
        self.rss.dbhelper.is_torrent_rssd.return_value = False
        self.rss.parse_rssxml = Mock(return_value=[{"title": "Pocket Monsters (2023) S01E60",
                                                    "enclosure": "offline"}])
        self.rss.downloader = Mock()
        self.rss.downloader.batch_download.return_value = ([], {})

        self.rss.rssdownload()

        selected = self.rss.downloader.batch_download.call_args.args[1]
        self.assertEqual([(2, [60])], [(item.rssid, item.get_episode_list()) for item in selected])
        self.rss.subscribe.get_subscribe_tv_episodes.assert_not_called()

    def test_nonmissing_exact_rule_does_not_hide_a_later_eligible_subscription(self):
        media = self.fixture.media.get_media_info("Pocket Monsters (2023) S01E60")
        media.set_torrent_info(site="测试站点")
        self.rss.subscribe.get_subscribe_tv_episodes.side_effect = lambda rid: [73] if rid == 1 else [60]
        for second in (dict(self.rss_info, id=2, name="Pocket.*Monsters", tmdbid=None, fuzzy_match=True),
                       dict(self.rss_info, id=3, name="Pocket Monsters")):
            with self.subTest(second=second["id"]):
                rules = {"1": self.rss_info, str(second["id"]): second}
                matched, _, selected = self.rss.check_torrent_rss(media, {}, rules, None, None, False, None, None)
                self.assertTrue(matched)
                self.assertEqual(second["id"], selected["id"])
