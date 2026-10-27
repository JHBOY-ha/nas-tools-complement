"""Offline regressions for the bundled crawler's HTML link normalization."""
import re
import warnings
from unittest import TestCase

from feapder.network.response import Response


class SpiderResponseTest(TestCase):
    def test_relative_links_preserve_markup_and_ignore_case(self):
        for tag, attribute in (("a", "href"), ("img", "src"),
                               ("link", "href"), ("script", "src")):
            for uppercase in (False, True):
                for quote in ("'", '"'):
                    with self.subTest(tag=tag, uppercase=uppercase, quote=quote):
                        name = tag.upper() if uppercase else tag
                        attr = attribute.upper() if uppercase else attribute
                        prefix = f'<{name}\n class="result" {attr} = {quote}'
                        suffix = f'{quote}></{name}>'
                        response = Response.from_text(
                            prefix + "../detail/42?lang=zh&sort=1" + suffix,
                            url="https://example.test/search/results",
                        )
                        response.make_absolute_links = True
                        # Older Python warns for misplaced flags; Python 3.11+
                        # rejects them. Fail on both runtimes for this regression.
                        # Clear cached patterns so earlier tests cannot hide warnings.
                        re.purge()
                        with warnings.catch_warnings():
                            warnings.simplefilter("error", DeprecationWarning)
                            self.assertEqual(
                                response.text,
                                prefix + "https://example.test/detail/42?lang=zh&sort=1" + suffix,
                            )

    def test_link_kinds_reach_the_selector(self):
        response = Response.from_text(
            '<a href="/detail/42">result</a>'
            '<a href="https://other.test/file.torrent">absolute</a>'
            '<a href="//cdn.test/file.torrent">cdn</a>'
            '<a href="magnet:?xt=urn:btih:abc">magnet</a>',
            url="https://example.test/search/results",
        )
        response.make_absolute_links = True
        self.assertEqual(response.xpath("//a/@href").extract(), [
            "https://example.test/detail/42",
            "https://other.test/file.torrent",
            "https://cdn.test/file.torrent",
            "magnet:?xt=urn:btih:abc",
        ])

    def test_link_normalization_can_be_disabled(self):
        html = '<a href="../detail/42">result</a>'
        response = Response.from_text(html, url="https://example.test/search/results")
        response.make_absolute_links = False
        self.assertEqual(response.text, html)
