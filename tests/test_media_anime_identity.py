# -*- coding: utf-8 -*-

import os
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch

from app.media import Media
from app.media.meta import MetaInfo
from app.media.meta.llm_parser import LLMMetaParser
from app.utils.types import MediaType


FANSUB_RELEASE = "[SAIO-Raws] Hundred 10 [BD 1920x1080 HEVC-10bit OPUS ASSx2].mkv"
PLAIN_TV_RELEASE = "Some Show S01E05 1080p WEB-DL AAC"

# TMDB 上与“Hundred”同名的剧集（无动画分类），LLM 答不出中文名时旧逻辑会绑定到它
NAME_SAKE_TV = {
    "id": 102571,
    "media_type": MediaType.TV,
    "name": "Hundred",
    "first_air_date": "2020-04-25",
    "genres": [{"id": 35}, {"id": 18}, {"id": 10759}],
    "seasons": [{"season_number": 1, "episode_count": 12}],
}
# 正确作品《百武装战记》，Bangumi 候选名可以检索到
ANIME_WORK = {
    "id": 66109,
    "media_type": MediaType.TV,
    "name": "百武装战记",
    "first_air_date": "2016-04-05",
    "genres": [{"id": 16}, {"id": 10759}, {"id": 10765}],
    "seasons": [{"season_number": 1, "episode_count": 12}],
}


class MediaAnimeIdentityTest(TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("NASTOOL_CONFIG"):
            root_path = os.path.dirname(os.path.dirname(__file__))
            os.environ["NASTOOL_CONFIG"] = os.path.join(root_path, "config", "config.yaml")

    def setUp(self):
        self.media = Media()
        self.parser = LLMMetaParser()
        # 这些用例全部桩掉检索调用，不需要真实 TMDB Key
        tmdb_patcher = patch.object(self.media, "tmdb", True)
        tmdb_patcher.start()
        self.addCleanup(tmdb_patcher.stop)
        meta_patcher = patch.object(self.media, "meta", Mock())
        meta_patcher.start()
        self.addCleanup(meta_patcher.stop)
        # 识别缓存会跨用例残留，测试里固定为空且不写入
        cache_patcher = patch.object(self.media, "get_cache_info", return_value={})
        cache_patcher.start()
        self.addCleanup(cache_patcher.stop)
        insert_patcher = patch.object(self.media, "_Media__insert_media_cache")
        insert_patcher.start()
        self.addCleanup(insert_patcher.stop)

    @staticmethod
    def _fake_merge_into(meta_info, title, subtitle=None, mtype_hint=None):
        """模拟 conservative 模式下 LLM 判定为动漫、但没有给出中文名的结果。"""
        note = dict(meta_info.note or {})
        note["llm"] = {
            "enabled": True,
            "mode": "conservative",
            "applied": True,
            "type": MediaType.ANIME,
            "type_hint": mtype_hint.value if mtype_hint else None,
        }
        meta_info.note = note
        return meta_info

    def test_llm_anime_verdict_rejects_same_name_tv(self):
        with patch.object(self.parser, "merge_into", self._fake_merge_into), \
                patch.object(self.media, "_Media__search_media_with_name",
                          return_value=NAME_SAKE_TV), \
                patch.object(self.parser, "get_alias_candidates", return_value=[]):
            media_info = self.media.get_media_info(title=FANSUB_RELEASE, cache=False)

        self.assertEqual(0, media_info.tmdb_id)

    def test_bangumi_alias_rescues_correct_work(self):
        with patch.object(self.parser, "merge_into", self._fake_merge_into), \
                patch.object(self.media, "_Media__search_media_with_name",
                          side_effect=[NAME_SAKE_TV, ANIME_WORK]) as mock_search, \
                patch.object(self.parser, "get_alias_candidates",
                             return_value=["百武装战记"]):
            media_info = self.media.get_media_info(title=FANSUB_RELEASE, cache=False)

        self.assertEqual(66109, media_info.tmdb_id)
        queried = [call.kwargs.get("query_name") for call in mock_search.call_args_list]
        self.assertEqual(["Hundred", "百武装战记"], queried)

    def test_plain_tv_release_keeps_binding_same_name_tv(self):
        with patch.object(self.parser, "merge_into", self._fake_merge_into), \
                patch.object(self.media, "_Media__search_media_with_name",
                          return_value=NAME_SAKE_TV), \
                patch.object(self.parser, "get_alias_candidates", return_value=[]):
            media_info = self.media.get_media_info(title=PLAIN_TV_RELEASE, cache=False)

        self.assertEqual(102571, media_info.tmdb_id)

    def test_file_transfer_path_uses_alias_candidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = os.path.join(temp_dir, FANSUB_RELEASE)
            with open(file_path, "wb") as file_obj:
                file_obj.write(b"")
            with patch.object(self.parser, "merge_into", self._fake_merge_into), \
                    patch.object(self.media, "_Media__search_media_with_name",
                                 side_effect=[NAME_SAKE_TV, ANIME_WORK]) as mock_search, \
                    patch.object(self.parser, "get_alias_candidates",
                                 return_value=["百武装战记"]):
                medias = self.media.get_media_info_on_files([file_path])

        self.assertEqual(1, len(medias))
        media_info = medias.get(file_path)
        self.assertIsNotNone(media_info)
        self.assertEqual(66109, media_info.tmdb_id)
        self.assertEqual(10, media_info.begin_episode)
        queried = [call.kwargs.get("query_name") for call in mock_search.call_args_list]
        self.assertEqual(["Hundred", "百武装战记"], queried)

    def test_poisoned_same_name_cache_is_dropped_and_rebound(self):
        poisoned_cache = {"id": 102571, "type": MediaType.TV, "title": "Hundred"}
        with patch.object(self.media, "get_cache_info", return_value=poisoned_cache), \
                patch.object(self.media, "get_tmdb_info", return_value=NAME_SAKE_TV), \
                patch.object(self.parser, "merge_into", self._fake_merge_into), \
                patch.object(self.media, "_Media__search_media_with_name",
                             side_effect=[NAME_SAKE_TV, ANIME_WORK]) as mock_search, \
                patch.object(self.parser, "get_alias_candidates",
                             return_value=["百武装战记"]):
            media_info = self.media.get_media_info(title=FANSUB_RELEASE)

        self.assertEqual(66109, media_info.tmdb_id)
        self.media.meta.delete_meta_data.assert_called_once()
        queried = [call.kwargs.get("query_name") for call in mock_search.call_args_list]
        self.assertEqual(["Hundred", "百武装战记"], queried)
