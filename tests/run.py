import os
import unittest

# Config 在测试模块导入时初始化；缺少配置时必须显式失败，不能由 quit() 返回成功码。
if __name__ == '__main__' and not os.environ.get('NASTOOL_CONFIG'):
    raise SystemExit('请使用 python3 -m tests.run_recognition 运行隔离测试；常规入口需要 NASTOOL_CONFIG。')

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
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(MetaInfoTest))
    # 季集边界、失败回滚和缓存消费回归也纳入常规入口，避免仅运行旧样例。
    for module in ("tests.test_meta_parser_boundaries", "tests.test_meta_multi_episode_guards",
                   "tests.test_media_recognition_integrity", "tests.test_media_cache_numbering",
                   "tests.test_meta_recognition_matrix", "tests.test_media_identity_matrix"):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName(module))
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
    # 测试失败必须让命令失败，避免自动检查只看退出码而误报通过。
    result = runner.run(suite)
    raise SystemExit(not result.wasSuccessful())
