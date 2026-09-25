# -*- coding: utf-8 -*-

from unittest import TestCase
from unittest.mock import patch

from app.media.meta import MetaInfo
from app.utils.types import MediaType
from tests.cases.meta_cases import meta_cases


class MetaInfoTest(TestCase):
    def test_bocchi_final_episode_is_tv(self):
        # Exercise the full rule parser without LLM or user word substitutions.
        with patch('app.media.meta.metainfo.WordsHelper') as words:
            words.return_value.process.side_effect = lambda title: (title, [], {})
            for block, episode in (('[12 END]', 12), ('[28 END]', 28), ('[28END]', 28)):
                with self.subTest(block=block):
                    meta = MetaInfo('[DMG][BOCCHI_THE_ROCK!]%s[1080P][GB].mp4' % block,
                                    use_llm=False)
                    self.assertEqual('Bocchi The Rock!', meta.en_name)
                    self.assertEqual(episode, meta.begin_episode)
                    self.assertEqual(MediaType.TV, meta.type)

    def setUp(self) -> None:
        pass

    def tearDown(self) -> None:
        pass

    def test_metainfo(self):
        for info in meta_cases:
            if not info.get("title"):
                continue
            meta_info = MetaInfo(title=info.get("title"), subtitle=info.get("subtitle"))
            target = {
                "type": meta_info.type.value,
                "cn_name": meta_info.cn_name or "",
                "en_name": meta_info.en_name or "",
                "year": meta_info.year or "",
                "part": meta_info.part or "",
                "season": meta_info.get_season_string(),
                "episode": meta_info.get_episode_string(),
                "restype": meta_info.get_edtion_string(),
                "pix": meta_info.resource_pix or "",
                "video_codec": meta_info.video_encode or "",
                "audio_codec": meta_info.audio_encode or ""
            }
            self.assertEqual(target, info.get("target"))

    def test_anime_roman_numeral_season_is_preserved(self):
        meta_info = MetaInfo(
            "[LoliHouse] 幼女战记II / Youjo Senki II - 11 "
            "[WebRip 1080p HEVC-10bit AAC]",
            use_llm=False
        )

        self.assertEqual(meta_info.begin_season, 2)
        self.assertEqual(meta_info.begin_episode, 11)

    def test_beyblade_x_is_part_of_title_not_season_ten(self):
        meta = MetaInfo("[jibaketa] Beyblade X - 127 [WEB 1080p AVC AAC]", use_llm=False)
        self.assertIn(meta.begin_season, (None, 1))
        self.assertEqual(127, meta.begin_episode)
        self.assertIn("X", meta.en_name)

    def test_tsdm_bracket_title_keeps_name_season_and_episode(self):
        meta = MetaInfo("【TSDM字幕组】[Re:从零开始的异世界生活 第4季][14]"
                        "[HEVC-10bit 1080p AAC][MKV][简日内封字幕]"
                        "[Re Zero kara Hajimeru Isekai Seikatsu 4th Season]", use_llm=False)
        self.assertIn("从零开始", meta.get_name())
        self.assertEqual(4, meta.begin_season)
        self.assertEqual(14, meta.begin_episode)
