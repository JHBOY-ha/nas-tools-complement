# -*- coding: utf-8 -*-

import os
import time
from unittest import TestCase

from app.helper.meta_helper import MetaHelper, CACHE_EXPIRE_TIMESTAMP_STR


class MetaHelperRandomSampleTest(TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("NASTOOL_CONFIG"):
            root_path = os.path.dirname(os.path.dirname(__file__))
            os.environ["NASTOOL_CONFIG"] = os.path.join(root_path, "config", "config.yaml")

    def test_random_sample_accepts_dict_keys_under_python311(self):
        helper = MetaHelper()
        helper._tmdb_cache_expire = False

        now = int(time.time())
        new_meta_data = {
            f"key-{idx}": {
                "id": idx,
                CACHE_EXPIRE_TIMESTAMP_STR: now + 3600
            }
            for idx in range(30)
        }

        # 仅验证不会抛出 TypeError，且不应修改未过期数据
        ret = helper._random_sample(new_meta_data)
        self.assertFalse(ret)
        self.assertEqual(30, len(new_meta_data))
