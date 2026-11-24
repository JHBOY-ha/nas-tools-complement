"""Exercise repeated custom RSS polls through the offline recognition runner."""
from unittest import TestCase
from unittest.mock import DEFAULT, Mock, patch

from app.rsschecker import RssChecker
from tests.recognition_fixture import FakeTmdb


class CustomRssRecognitionTest(TestCase):
    def setUp(self):
        fixture = FakeTmdb(self)
        fixture.tv(284580, "Item", year="2026", seasons={1: 11})
        self.checker = RssChecker()
        self.checker._library_matches.clear()
        self.article = {"title": "Item S01E03 1080p", "enclosure": "offline:03"}
        self.downloader = Mock()
        self.downloader.get_download_list.side_effect = list
        self.downloader.download.return_value = (True, "")
        self.recognize = Mock(wraps=fixture.media.get_media_info)
        self.db = Mock()
        self.db.is_userrss_finished.return_value = False
        self.filters = Mock()
        self.filters.check_torrent_filter.return_value = (True, 0, "")
        replacements = patch.multiple(
            self.checker,
            get_rsstask_info=Mock(return_value={"id": 3, "name": "Item", "uses": "D", "recognization": "Y"}),
            _RssChecker__parse_userrss_result=Mock(return_value=[self.article]),
            media=Mock(get_media_info=self.recognize), downloader=self.downloader,
            dbhelper=self.db, filter=self.filters, message=Mock(),
        )
        replacements.start()
        self.addCleanup(replacements.stop)
        self.addCleanup(self.checker._library_matches.clear)

    def poll(self):
        self.assertEqual(0, self.checker.check_task_rss(3)["code"])

    def test_existing_episode_reuses_identity_but_can_be_downloaded_after_removal(self):
        self.downloader.check_exists_medias.side_effect = [(True, {}, []), (True, {}, []), (False, {}, [])]
        self.poll()
        self.poll()
        self.recognize.assert_called_once()
        self.downloader.download.assert_not_called()
        self.db.insert_rss_torrents.assert_not_called()
        self.poll()
        self.assertEqual(3, self.downloader.check_exists_medias.call_count)
        self.downloader.download.assert_called_once()
        selected = self.downloader.download.call_args.kwargs["media_info"]
        self.assertEqual(([3], self.article["enclosure"]), (selected.get_episode_list(), selected.enclosure))

    def test_unconfirmed_library_result_is_retried(self):
        self.downloader.check_exists_medias.side_effect = [(None, {}, []), (True, {}, [])]
        self.poll()
        self.poll()
        self.assertEqual(2, self.recognize.call_count)
        self.downloader.download.assert_not_called()

    def test_failed_recognition_is_retried_on_the_next_poll(self):
        self.recognize.side_effect = [None, DEFAULT]
        self.downloader.check_exists_medias.return_value = (True, {}, [])
        self.poll()
        self.downloader.check_exists_medias.assert_not_called()
        self.poll()
        self.downloader.check_exists_medias.assert_called_once()
        self.downloader.download.assert_not_called()

    def test_changed_title_on_same_link_gets_its_own_recognition(self):
        self.downloader.check_exists_medias.return_value = (True, {}, [])
        self.poll()
        self.article["title"] = "Item S01E08 1080p"
        self.poll()
        self.assertEqual(2, self.recognize.call_count)
        self.assertEqual([8], self.downloader.check_exists_medias.call_args.kwargs["meta_info"].get_episode_list())
