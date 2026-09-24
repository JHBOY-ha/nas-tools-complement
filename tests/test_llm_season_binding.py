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
    _REZERO_INFO = {
        "id": 65942,
        "media_type": MediaType.TV,
        "name": "Re：从零开始的异世界生活",
        "first_air_date": "2016-04-04",
        "genres": [{"id": 16}],
        "seasons": [
            {"season_number": 0, "episode_count": 84},
            {"season_number": 1, "episode_count": 85}
        ]
    }
    _ONE_PIECE_INFO = {
        "id": 37854,
        "media_type": MediaType.TV,
        "name": "海贼王",
        "first_air_date": "1999-10-20",
        "genres": [{"id": 16}],
        "seasons": ([{"season_number": number, "episode_count": 50} for number in range(1, 23)]
                    + [{"season_number": 23, "episode_count": 60}])
    }

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

    def _rezero_meta(self, **overrides):
        meta_info = MetaInfo(
            "[Nix-Raws] Re：从零开始的异世界生活 第四季 S04E18 [CR WEB-DL 1080p AVC AAC]",
            use_llm=False)
        llm_note = {
            "tmdb_id": 65942,
            "tmdb_season": 1,
            "tmdb_season_name": "第 1 季",
            "season_verified": True,
            "season_evidence": "llm_only",
            "release_season": 4
        }
        llm_note.update(overrides)
        meta_info.note = {"llm": llm_note}
        return meta_info

    @staticmethod
    def _no_episode_mapping_rules():
        config = Mock()
        config.get_config.side_effect = lambda key: {"episode_mappings": []} if key == "media" else {}
        return patch("app.media.media.Config", return_value=config)

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

    def test_remapped_season_without_episode_evidence_is_rejected(self):
        meta_info = self._rezero_meta()
        season_detail = {"episodes": [{"episode_number": number} for number in range(1, 86)]}

        with self._no_episode_mapping_rules(), \
                patch.object(self.media, "get_tmdb_tv_season_detail", return_value=season_detail):
            valid = self.media._prepare_media_identity(meta_info, self._REZERO_INFO)

        self.assertFalse(valid)
        self.assertEqual(18, meta_info.begin_episode)

    def test_remapped_season_with_verified_llm_episode_is_kept(self):
        meta_info = self._rezero_meta(tmdb_episode=84)
        season_detail = {"episodes": [{"episode_number": number} for number in range(1, 86)]}

        with self._no_episode_mapping_rules(), \
                patch.object(self.media, "get_tmdb_tv_season_detail", return_value=season_detail):
            valid = self.media._prepare_media_identity(meta_info, self._REZERO_INFO)

        self.assertTrue(valid)
        self.assertEqual(1, meta_info.begin_season)
        self.assertEqual(84, meta_info.begin_episode)

    def test_absolute_episode_conversion_is_applied(self):
        meta_info = MetaInfo("[Group] One Piece S01E1156 [1080p AVC]", use_llm=False)
        meta_info.note = {"llm": {
            "tmdb_id": 37854,
            "tmdb_season": 23,
            "tmdb_season_name": "第 23 季",
            "season_verified": True,
            "season_evidence": "season_name",
            "release_season": 1
        }}
        season_detail = {"episodes": [{"episode_number": number} for number in range(1, 61)]}

        with self._no_episode_mapping_rules(), \
                patch.object(self.media, "get_tmdb_tv_season_detail", return_value=season_detail):
            valid = self.media._prepare_media_identity(meta_info, self._ONE_PIECE_INFO)

        self.assertTrue(valid)
        self.assertEqual(23, meta_info.begin_season)
        self.assertEqual(56, meta_info.begin_episode)
        self.assertEqual([56], meta_info.note["absolute_episode_mapping"]["target_episodes"])

    def test_same_season_needs_no_episode_evidence(self):
        meta_info = MetaInfo("[Group] Some Show S02E05 [1080p AVC]", use_llm=False)
        meta_info.note = {"llm": {
            "tmdb_id": 999,
            "tmdb_season": 2,
            "season_verified": True,
            "season_evidence": "llm_only",
            "release_season": 2
        }}
        info = {"id": 999, "media_type": MediaType.TV, "genres": [{"id": 16}],
                "seasons": [{"season_number": 1, "episode_count": 12},
                            {"season_number": 2, "episode_count": 12}]}
        season_detail = {"episodes": [{"episode_number": number} for number in range(1, 13)]}

        with self._no_episode_mapping_rules(), \
                patch.object(self.media, "get_tmdb_tv_season_detail", return_value=season_detail):
            valid = self.media._prepare_media_identity(meta_info, info)

        self.assertTrue(valid)
        self.assertEqual(2, meta_info.begin_season)
        self.assertEqual(5, meta_info.begin_episode)
