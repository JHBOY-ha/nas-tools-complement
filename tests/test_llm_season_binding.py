# -*- coding: utf-8 -*-

import os
from unittest import TestCase
from unittest.mock import Mock, patch

from app.media import Media
from app.media.meta import MetaInfo
from app.utils.types import MediaType


class LlmSeasonBindingTest(TestCase):
    _JOJO_TITLE = ("[UHA-WINGS][JoJo's Bizarre Adventure Steel Ball Run][01]"
                   "[1080p HEVC][CHS_JP&CHT_JP].mkv")
    _JOJO_INFO = {
        "id": 45790,
        "media_type": MediaType.TV,
        "name": "JOJO的奇妙冒险",
        "first_air_date": "2012-10-06",
        "genres": [{"id": 16}],
        "seasons": [
            {"season_number": 1, "episode_count": 26},
            {"season_number": 6, "episode_count": 12}
        ]
    }
    _SEASON_6_EPISODES = {"episodes": [{"episode_number": 1}, {"episode_number": 2}]}

    @classmethod
    def setUpClass(cls):
        if not os.environ.get("NASTOOL_CONFIG"):
            root_path = os.path.dirname(os.path.dirname(__file__))
            os.environ["NASTOOL_CONFIG"] = os.path.join(root_path, "config", "config.yaml")

    def setUp(self):
        self.media = Media()
        tmdb_patcher = patch.object(self.media, "tmdb", True)
        tmdb_patcher.start()
        self.addCleanup(tmdb_patcher.stop)
        meta_patcher = patch.object(self.media, "meta", Mock())
        meta_patcher.start()
        self.addCleanup(meta_patcher.stop)

    def _meta_with_llm_season(self, **overrides):
        meta_info = MetaInfo(self._JOJO_TITLE, use_llm=False)
        llm_note = {
            "tmdb_id": 45790,
            "tmdb_season": 6,
            "tmdb_season_name": "飙马野郎篇",
            "tmdb_episode": 1,
            "season_verified": True,
            "season_evidence": "season_name",
            "release_season": None
        }
        llm_note.update(overrides)
        meta_info.note = {"llm": llm_note}
        return meta_info

    def test_apply_llm_season_sets_verified_season(self):
        meta_info = self._meta_with_llm_season()

        with patch.object(self.media, "get_tmdb_tv_season_detail",
                          return_value=self._SEASON_6_EPISODES):
            applied = self.media._apply_llm_season(meta_info, self._JOJO_INFO)

        self.assertTrue(applied)
        self.assertEqual(6, meta_info.begin_season)
        self.assertEqual(1, meta_info.begin_episode)
        self.assertEqual("S06", meta_info.get_season_string())
        self.assertEqual(6, meta_info.note["season_binding"]["tmdb_season"])

    def test_apply_llm_season_requires_verified_flag(self):
        meta_info = self._meta_with_llm_season(season_verified=False)

        applied = self.media._apply_llm_season(meta_info, self._JOJO_INFO)

        self.assertFalse(applied)
        self.assertNotIn("season_binding", meta_info.note)
        self.assertIsNone(meta_info.begin_season)

    def test_apply_llm_season_rejects_other_work(self):
        meta_info = self._meta_with_llm_season(tmdb_id=999)

        applied = self.media._apply_llm_season(meta_info, self._JOJO_INFO)

        self.assertFalse(applied)
        self.assertNotIn("season_binding", meta_info.note)

    def test_apply_llm_season_needs_tmdb_episode_list(self):
        meta_info = self._meta_with_llm_season()

        with patch.object(self.media, "get_tmdb_tv_season_detail", return_value={}):
            applied = self.media._apply_llm_season(meta_info, self._JOJO_INFO)

        self.assertFalse(applied)
        self.assertIsNone(meta_info.begin_season)

    def test_apply_llm_season_keeps_release_episode_when_llm_episode_invalid(self):
        meta_info = self._meta_with_llm_season(tmdb_episode=99)

        with patch.object(self.media, "get_tmdb_tv_season_detail",
                          return_value={"episodes": [{"episode_number": 1}]}):
            applied = self.media._apply_llm_season(meta_info, self._JOJO_INFO)

        self.assertTrue(applied)
        self.assertEqual(6, meta_info.begin_season)
        self.assertEqual(1, meta_info.begin_episode)

    def test_prepare_media_identity_applies_llm_season(self):
        meta_info = self._meta_with_llm_season()

        with patch.object(self.media, "get_tmdb_tv_season_detail",
                          return_value=self._SEASON_6_EPISODES):
            valid = self.media._prepare_media_identity(meta_info, self._JOJO_INFO)

        self.assertTrue(valid)
        self.assertEqual(6, meta_info.begin_season)

    def test_prepare_media_identity_prefers_episode_mapping_rule(self):
        meta_info = self._meta_with_llm_season()

        def fake_mapping(target_meta, target_info):
            target_meta.begin_season = 1
            note = dict(target_meta.note or {})
            note["episode_mapping"] = {"tmdb_id": target_info.get("id"), "target_season": 1}
            target_meta.note = note

        with patch.object(self.media, "_apply_episode_mapping", side_effect=fake_mapping):
            valid = self.media._prepare_media_identity(meta_info, self._JOJO_INFO)

        self.assertTrue(valid)
        self.assertEqual(1, meta_info.begin_season)
        self.assertNotIn("season_binding", meta_info.note)
