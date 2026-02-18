import unittest

from tests.test_metainfo import MetaInfoTest
from tests.test_media_cn_fallback import MediaCnFallbackTest

if __name__ == '__main__':
    suite = unittest.TestSuite()
    # 测试名称识别
    suite.addTest(MetaInfoTest('test_metainfo'))
    # 测试中文兜底检索
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(MediaCnFallbackTest))

    # 运行测试
    runner = unittest.TextTestRunner()
    runner.run(suite)
