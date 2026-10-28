"""Known title-parsing gaps (F7), kept as executable evidence for a separate fix round.

These assertions describe the desired parse and currently fail. The module is
deliberately absent from tests.run_recognition; run it explicitly:
python3 -m tests.run_recognition tests.test_recognition_naming_gaps
"""
import unittest

from app.media.meta import MetaInfo


class NamingGaps(unittest.TestCase):
    def test_unknown_publisher_is_not_the_title(self):
        meta = MetaInfo("[SweetSub][91 Days][01][1080P].mkv", use_llm=False)
        self.assertEqual("91 Days", meta.get_name())

    def test_numeric_bracket_title(self):
        meta = MetaInfo("[Nekomoe kissaten][86][01][1080p].mp4", use_llm=False)
        self.assertEqual(("86", [1]), (meta.get_name(), meta.get_episode_list()))

    def test_chinese_season_range_is_not_a_title(self):
        meta = MetaInfo("The Expanse 第1-3季 第01集.mkv", use_llm=False)
        self.assertEqual((None, "The Expanse"), (meta.cn_name, meta.en_name))

    def test_glued_bilingual_directory(self):
        # The user's sample directory name.
        meta = MetaInfo("赛伯朋克边缘行者2Cyberpunk.Edgerunners.S02.1080p.NF.WEB-DL.DUAL.DDP5.1.H.264-VARYG",
                        use_llm=False)
        self.assertEqual("Cyberpunk Edgerunners", meta.en_name)


if __name__ == "__main__":
    unittest.main()
