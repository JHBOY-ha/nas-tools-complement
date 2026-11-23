"""Run with python3 -m tests.run_stability tests.test_douban_covers."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.media.douban import DouBan
from app.media.doubanapi import DoubanWeb
from app.utils import RequestUtils
from app.utils.types import MediaType


class DoubanCoverTests(unittest.TestCase):
    def test_discovery_preserves_provider_urls(self):
        source = DouBan()
        url = "https://img9.doubanio.com/view/photo/s_ratio_poster/public/p42.jpg?sig=a%2Fb&v=2"
        for kind, method, fields in (
                (MediaType.MOVIE, 'movie_recommend', {'cover': {'url': url}}),
                (MediaType.MOVIE, 'movie_recommend', {'cover_url': url}),
                (MediaType.TV, 'tv_recommend', {'pic': {'normal': url}}),
                (MediaType.TV, 'tv_recommend', {'cover': url})):
            with self.subTest(fields=fields), patch.object(source.doubanapi, method, return_value={
                    'items': [dict(id='42', type='movie' if kind == MediaType.MOVIE else 'tv', **fields)]}):
                card, = source.get_douban_disover(kind, 'T', '')
                self.assertEqual(card['id'], 'DB:42')
                self.assertEqual(card['image'], url)

    def test_share_page_keeps_signed_url_even_in_redirect_body(self):
        url = 'https://qnmob3-sign.doubanio.com/view/photo/large/public/p42.jpg?sig=a%2Fb&size=300'
        body = ('<meta property="og:image" content="%s">' % url.replace('&', '&amp;')).encode()
        for status in (200, 302):
            reply = SimpleNamespace(status_code=status, content=body)
            with self.subTest(status=status), patch.object(RequestUtils, 'get_res', return_value=reply) as fetch:
                self.assertEqual(DoubanWeb().subject_preview(42), url)
                fetch.assert_called_once_with('https://m.douban.com/movie/subject/42/', allow_redirects=False)

    def test_missing_failed_or_unrelated_preview(self):
        for reply in (None, SimpleNamespace(status_code=418, content=b''),
                      SimpleNamespace(status_code=302, content=b'<html></html>'),
                      SimpleNamespace(status_code=200, content=b'<meta property="og:image" '
                                      b'content="https://img3.doubanio.com.example.org/view/photo/p42.jpg">')):
            with self.subTest(reply=reply), patch.object(RequestUtils, 'get_res', return_value=reply):
                self.assertIsNone(DoubanWeb().subject_preview(42))

    def test_preview_route_requires_login_and_never_caches_the_signed_redirect(self):
        from web.main import App

        path = '/douban/subject/42/preview'
        url = 'https://qnmob3-sign.doubanio.com/view/photo/large/public/p42.jpg?sig=a%2Fb&v=1'
        client = App.test_client()
        with patch.object(DoubanWeb(), 'subject_preview', return_value=url) as resolve:
            with patch.dict(App.config, LOGIN_DISABLED=False):
                self.assertEqual(client.get(path).status_code, 302)
                resolve.assert_not_called()
            with patch.dict(App.config, LOGIN_DISABLED=True):
                response = client.get(path)
                self.assertEqual((response.status_code, response.location), (302, url))
                self.assertTrue(response.cache_control.no_store)
                resolve.assert_called_once_with(42)
                resolve.return_value = None
                self.assertEqual(client.get(path).status_code, 404)
