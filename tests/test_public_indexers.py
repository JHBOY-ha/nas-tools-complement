import unittest

from app.indexer import public
from app.indexer.client.builtin import BuiltinIndexer
from app.indexer.providers.common import BaseUrlRefresher, validate_public_url
from config import Config


class PublicIndexerTest(unittest.TestCase):
    def test_expanded_registry_and_selection(self):
        indexers = public.get_indexers(check=False)
        self.assertEqual([item["id"] for item in indexers], ["52bt", "seedhub"])
        self.assertTrue(all(item["public"] and item["builtin"] for item in indexers))

    def test_empty_selection_disables_all_public_sites(self):
        original = Config().get_config("pt").get("public_indexers")
        try:
            Config().get_config("pt")["public_indexers"] = {"enabled": []}
            self.assertEqual(public.get_indexers(check=True), [])
        finally:
            Config().get_config("pt")["public_indexers"] = original

    def test_adapter_result_conversion(self):
        result = {"title": "Movie.2020.1080p", "magnetUrl": "magnet:?xt=urn:btih:" + "a" * 40,
                  "detailsUrl": "https://example.test/movie/", "size": 1024,
                  "seeders": 2, "leechers": 1}
        converted = public._convert("52bt", result)
        self.assertEqual(converted["enclosure"], result["magnetUrl"])
        self.assertEqual(converted["page_url"], result["detailsUrl"])
        self.assertEqual(converted["indexer"], "52bt")

    def test_seedhub_lazy_link_is_scoped_to_indexer(self):
        result = {"title": "Movie", "detailsUrl": "https://example.test/link/"}
        converted = public._convert("seedhub", result)
        self.assertEqual(converted["enclosure"], "public:seedhub:https://example.test/link/")

    def test_builtin_public_flag_does_not_shadow_adapter_module(self):
        indexers = BuiltinIndexer().get_indexers(check=False)
        self.assertIn("52bt", [item["id"] for item in indexers])

    def test_base_refresher_rejects_private_addresses(self):
        pool = BaseUrlRefresher("test", ("https://public.example.test/",))
        self.assertEqual(pool.candidates(), ["https://public.example.test/"])
        with self.assertRaises(Exception):
            validate_public_url("https://127.0.0.1/")
        with self.assertRaises(Exception):
            validate_public_url("https://192.168.1.1/")


if __name__ == "__main__":
    unittest.main()
