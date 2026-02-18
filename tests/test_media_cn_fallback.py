# -*- coding: utf-8 -*-

import os
from unittest import TestCase
from unittest.mock import patch

from app.media import Media
from app.media.meta import MetaInfo
from app.utils.types import MediaType


class MediaCnFallbackTest(TestCase):
    _SAMPLE_TITLE = "[LoliHouse] 地狱模式～喜欢速通游戏的玩家在废设定异世界无双～ / Hell Mode - 06 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
    _SAMPLE_CN = "地狱模式～喜欢速通游戏的玩家在废设定异世界无双～"
    _TSDM_TITLE = "[TSDM字幕组] [厄里斯的圣杯][Eris no Seihai][06][HEVC-10bit 1080p AAC][MKV][简繁日内封字幕]エリスの圣杯"
    _TSDM_CN = "厄里斯的圣杯"

    @classmethod
    def setUpClass(cls):
        if not os.environ.get("NASTOOL_CONFIG"):
            root_path = os.path.dirname(os.path.dirname(__file__))
            os.environ["NASTOOL_CONFIG"] = os.path.join(root_path, "config", "config.yaml")

    def setUp(self) -> None:
        self.media = Media()

    def test_extract_cn_fallback_name(self):
        meta_info = MetaInfo(self._SAMPLE_TITLE)
        cn_name = self.media._Media__extract_cn_fallback_name(meta_info)
        self.assertEqual(self._SAMPLE_CN, cn_name)

    def test_get_media_info_cn_fallback_search_success(self):
        mock_tmdb_info = {
            "id": 123456,
            "media_type": MediaType.TV,
            "name": "地狱模式",
            "first_air_date": "2024-01-01",
            "genres": [{"id": 1}]
        }
        with patch.object(self.media, "_Media__search_media_with_name", side_effect=[{}, mock_tmdb_info]) as mock_search:
            media_info = self.media.get_media_info(title=self._SAMPLE_TITLE, cache=False)

        self.assertEqual(123456, media_info.tmdb_id)
        self.assertEqual(self._SAMPLE_CN, media_info.cn_name)
        self.assertEqual(2, mock_search.call_count)
        self.assertEqual("Hell Mode", mock_search.call_args_list[0].kwargs.get("query_name"))
        self.assertEqual(self._SAMPLE_CN, mock_search.call_args_list[1].kwargs.get("query_name"))

    def test_get_media_info_cn_fallback_search_failed(self):
        with patch.object(self.media, "_Media__search_media_with_name", side_effect=[{}, {}]) as mock_search:
            media_info = self.media.get_media_info(title=self._SAMPLE_TITLE, cache=False)

        self.assertEqual(0, media_info.tmdb_id)
        self.assertEqual(self._SAMPLE_CN, media_info.cn_name)
        self.assertEqual(self._SAMPLE_CN, media_info.get_name())
        self.assertEqual(2, mock_search.call_count)

    def test_get_media_info_without_cn_fallback(self):
        title = "Hell Mode - 06 [WebRip 1080p HEVC-10bit AAC]"
        with patch.object(self.media, "_Media__search_media_with_name", side_effect=[{}]) as mock_search:
            media_info = self.media.get_media_info(title=title, cache=False)

        self.assertEqual(1, mock_search.call_count)
        self.assertIsNone(media_info.cn_name)
        self.assertEqual("Hell Mode", media_info.get_name())

    def test_extract_cn_fallback_ignores_noisy_cn_name(self):
        meta_info = MetaInfo(self._TSDM_TITLE)
        meta_info.cn_name = "_[TSDM字幕组]_[厄里斯的圣杯][Eris no Seihai]エリスの圣杯"

        cn_name = self.media._Media__extract_cn_fallback_name(meta_info)
        self.assertEqual(self._TSDM_CN, cn_name)

    def test_get_media_info_tsdm_title_fallback(self):
        with patch.object(self.media, "_Media__search_media_with_name", side_effect=[{}, {}]):
            media_info = self.media.get_media_info(title=self._TSDM_TITLE, cache=False)

        self.assertEqual(0, media_info.tmdb_id)
        self.assertEqual(self._TSDM_CN, media_info.cn_name)
        self.assertEqual(self._TSDM_CN, media_info.get_name())
