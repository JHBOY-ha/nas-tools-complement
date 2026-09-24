import unittest

from tests.test_metainfo import MetaInfoTest
from tests.test_media_anime_identity import MediaAnimeIdentityTest
from tests.test_media_cn_fallback import MediaCnFallbackTest
from tests.test_meta_llm_parser import LLMMetaParserTest
from tests.test_llm_season_binding import LlmSeasonBindingTest
from tests.test_meta_helper import MetaHelperRandomSampleTest
from tests.test_opensubtitles_api import OpenSubtitlesApiTest

if __name__ == '__main__':
    suite = unittest.TestSuite()
    # 测试名称识别
    suite.addTest(MetaInfoTest('test_metainfo'))
    # 测试中文兜底检索
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(MediaCnFallbackTest))
    # 测试同名作品与动漫身份校验
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(MediaAnimeIdentityTest))
    # 测试LLM识别增强
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(LLMMetaParserTest))
    # 测试LLM季号绑定
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(LlmSeasonBindingTest))
    # 测试TMDB缓存随机采样兼容性
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(MetaHelperRandomSampleTest))
    # OpenSubtitles.com REST API与免费配额保护
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(OpenSubtitlesApiTest))

    # 运行测试
    runner = unittest.TextTestRunner()
    runner.run(suite)
