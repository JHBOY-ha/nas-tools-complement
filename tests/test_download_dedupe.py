# -*- coding: utf-8 -*-

from types import SimpleNamespace
from unittest import TestCase

from app.downloader.downloader import Downloader
from app.utils.types import MediaType


class DownloadDedupeTest(TestCase):
    @staticmethod
    def _item(tmdb_id, season, episode, title, res_order):
        return SimpleNamespace(
            tmdb_id=tmdb_id,
            type=MediaType.ANIME,
            title=title,
            year="2026",
            res_order=res_order,
            site_order=0,
            seeders=0,
            get_season_list=lambda: [season],
            get_episode_list=lambda: [episode],
            get_title_string=lambda: "%s (2026)" % title,
            get_season_episode_string=lambda: "S%02d E%02d" % (season, episode)
        )

    def test_same_tmdb_episode_keeps_only_highest_priority_release(self):
        downloader = Downloader()
        downloader._download_order = None
        low = self._item(298103, 1, 11, "继母与继姐", 10)
        high = self._item(298103, 1, 11, "继母与继姐", 90)

        result = downloader.get_download_list([low, high])

        self.assertEqual(result, [high])

    def test_different_tmdb_ids_are_not_collapsed_by_same_display_name(self):
        downloader = Downloader()
        downloader._download_order = None
        first = self._item(100, 1, 11, "同名作品", 10)
        second = self._item(200, 1, 11, "同名作品", 10)

        result = downloader.get_download_list([first, second])

        self.assertEqual({item.tmdb_id for item in result}, {100, 200})
