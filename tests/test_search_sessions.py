"""Behavior regressions for isolated movie searches and durable Telegram actions."""
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest import TestCase
from unittest.mock import Mock, patch

from app.helper.search_sessions import SearchSessions, pack_media, unpack_media
from app.media.meta import MetaInfo
from app.utils.types import MediaType, SearchType
from app.utils.workload import TaskQueueFull
from web.backend.telegram_search import TelegramSearch, resource_key
from web.backend.telegram_search_view import render_search_message


def movie(title='沙丘', year='2021', tmdbid=438631):
    media = MetaInfo(title, use_llm=False)
    media.set_tmdb_info({'id': tmdbid, 'media_type': MediaType.MOVIE, 'title': title,
        'original_title': 'Dune', 'original_language': 'en', 'release_date': year + '-01-01',
        'genres': [], 'overview': '电影简介'})
    # Artwork is fixed in offline tests; presentation must not query Fanart.
    media.fanart_backdrop = 'https://example.invalid/backdrop.jpg'
    media.fanart_poster = 'https://example.invalid/poster.jpg'
    return media


def torrent(name='Dune.2021.1080p.WEB-DL', url='magnet:?xt=urn:btih:abc', site='Test'):
    media = movie()
    media.org_string = name
    media.enclosure, media.site = url, site
    media.resource_pix = '1080p'
    media.size, media.seeders = 1024 ** 3, 10
    return media


class SearchSessionFixture(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store_type = SearchSessions.__closure__[0].cell_contents
        self.store = self.store_type(self.directory.name)


class SearchSessionsTest(SearchSessionFixture):
    def test_snapshot_round_trip_preserves_movie_and_resource_identity(self):
        media = torrent()
        record, _ = self.store.create(1, -10, '沙丘')
        with self.store.edit(record['id']) as current:
            current.update(state='results', selected=pack_media(media),
                           results={'r1': pack_media(media)})
        loaded = self.store_type(self.directory.name).get(record['id'])
        restored = unpack_media(loaded['results']['r1'])
        self.assertEqual((restored.tmdb_id, restored.type, restored.enclosure),
                         (438631, MediaType.MOVIE, media.enclosure))
        self.assertEqual(restored.tmdb_info['media_type'], MediaType.MOVIE)
        self.assertEqual(restored.get_title_string(), media.get_title_string())
        self.assertEqual(os.stat(os.path.join(self.directory.name, record['id'] + '.json')).st_mode & 0o777, 0o600)

    def test_concurrent_searches_and_redelivery_remain_isolated(self):
        first, created = self.store.create(1, -10, '沙丘', request_id=15)
        again, created_again = self.store.create(1, -10, '沙丘', request_id=15)
        second, _ = self.store.create(1, -10, '星际穿越', request_id=16)
        other, _ = self.store.create(2, -10, '沙丘', request_id=17)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first['id'], again['id'])
        self.assertEqual(len({first['id'], second['id'], other['id']}), 3)
        with self.assertRaises(ValueError):
            self.store.owned(first['id'], 2, -10)
        with self.assertRaises(ValueError):
            self.store.owned(first['id'], 1, -11)
        with self.assertRaises(ValueError):
            self.store.owned(first['id'], 1, -10, epoch=99)
        with self.assertRaises(ValueError):
            self.store.owned(first['id'], 1, -10, channel='web')

    def test_restart_preserves_results_but_does_not_replay_mutations(self):
        record, _ = self.store.create(1, 1, '沙丘')
        with self.store.edit(record['id']) as current:
            current.update(state='searching', results={'r1': pack_media(torrent())},
                           downloads={'r1': {'state': 'submitting', 'tag': 'nts-test'}})
        loaded = self.store_type(self.directory.name).get(record['id'])
        self.assertEqual(loaded['state'], 'interrupted')
        self.assertEqual(loaded['downloads']['r1']['state'], 'unknown')
        self.assertIn('r1', loaded['results'])

    def test_failed_persistence_does_not_publish_claim(self):
        record, _ = self.store.create(1, 1, '沙丘')
        with patch.object(self.store, '_save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError), self.store.edit(record['id']) as current:
                current['downloads']['r1'] = {'state': 'submitting'}
        self.assertEqual(self.store.get(record['id'])['downloads'], {})

    def test_unchanged_edit_preserves_persisted_snapshot(self):
        record, _ = self.store.create(1, 1, '沙丘')
        path = os.path.join(self.directory.name, record['id'] + '.json')
        with open(path, 'rb') as stream:
            before = stream.read()
        with self.store.edit(record['id']):
            pass
        with open(path, 'rb') as stream:
            self.assertEqual(stream.read(), before)
        self.assertEqual(self.store.get(record['id'])['updated_at'], record['updated_at'])

    def test_media_snapshot_does_not_share_mutable_recognition_data(self):
        media = movie()
        media.note['source'] = {'title': '沙丘'}
        snapshot = pack_media(media)
        media.tmdb_info['title'] = '后续识别的片名'
        media.note['source']['title'] = '后续备注'
        self.assertEqual(snapshot['tmdb_info']['title'], '沙丘')
        self.assertEqual(snapshot['note']['source']['title'], '沙丘')


    def test_pending_download_query_is_scoped_ordered_and_returns_detached_data(self):
        ids = []
        for channel, state, checked, tag in (
                ('web', 'accepted', 0, None), ('telegram', 'accepted', 20, 'new'),
                ('telegram', 'unknown', 5, 'old'), ('telegram', 'organized', 0, 'done')):
            record, _ = self.store.create(1, 1, '沙丘', channel=channel)
            with self.store.edit(record['id']) as current:
                current['downloads']['r1'] = {'state': state, 'checked_at': checked, 'tag': tag}
            ids.append(record['id'])
        self.assertEqual(self.store.pending_download_sessions('telegram', limit=1), [ids[2]])
        receipt = self.store.find_download('old')
        receipt['state'] = 'changed by caller'
        self.assertEqual(self.store.get(ids[2])['downloads']['r1']['state'], 'unknown')



class TelegramSearchTest(SearchSessionFixture):
    def setUp(self):
        super().setUp()
        service_type = TelegramSearch.__closure__[0].cell_contents
        self.helper = Mock()
        self.service = service_type(self.store, self.helper)
        self.client = Mock()
        self.client.search_message.return_value = 101
        self.service.client = lambda: self.client
        self.record, _ = self.store.create(1, 1, '沙丘 2021')
        self.sid = self.record['id']

    def ready(self, items=None):
        items = items or [torrent()]
        with self.store.edit(self.sid) as record:
            record.update(state='results', view='results', selected=pack_media(movie()),
                          results={resource_key(item): pack_media(item) for item in items})
        return resource_key(items[0])

    def test_numeric_movie_title_starts_a_search_without_a_reply_target(self):
        self.service.receive('1917', 1, 1, request_id=1917)
        record = self.store.find_message(1, 1, 101)
        self.assertEqual(record['query'], '1917')
        self.assertNotEqual(record['id'], self.sid)
        self.assertEqual(record['state'], 'identifying')
        self.helper.start_thread.assert_called_once()

    def test_old_candidate_button_cannot_select_from_new_search(self):
        with self.store.edit(self.sid) as record:
            record.update(state='candidates', candidates=[pack_media(movie(year='1984', tmdbid=841)), pack_media(movie())])
        newer, _ = self.store.create(1, 1, '别的电影')
        self.service.action(self.sid, 0, 'pick', 1, 1, 1)
        self.assertEqual(self.store.get(self.sid)['selected']['tmdb_id'], 438631)
        self.assertIsNone(self.store.get(newer['id'])['selected'])
        self.assertEqual(self.store.get(self.sid)['epoch'], 1)
        self.service.action(self.sid, 0, 'pick', 0, 1, 1)
        self.assertEqual(self.helper.start_thread.call_count, 1)

    def test_pages_filters_and_details_keep_stable_resource_ids(self):
        items = [torrent(f'Dune.resource.{n}', f'https://example.test/{n}') for n in range(8)]
        rid = self.ready(items)
        record = self.store.get(self.sid)
        text, buttons = render_search_message(record)
        self.assertIn('第 1 页', text)
        self.assertTrue(any(rid in button.get('callback_data', '') for row in buttons for button in row))
        self.service.action(self.sid, 0, 'page', 1, 1, 1)
        self.assertIn('第 2 页', render_search_message(self.store.get(self.sid))[0])
        self.service.action(self.sid, 0, 'detail', rid, 1, 1)
        self.assertIn('Dune.resource.0', render_search_message(self.store.get(self.sid))[0])
        for row in buttons:
            for button in row:
                self.assertLessEqual(len(button['callback_data'].encode()), 64)

    def test_expired_message_offers_new_search_and_never_downloads(self):
        rid = self.ready()
        with self.store.edit(self.sid) as record:
            record['expires_at'] = time.time() - 1
        self.service.action(self.sid, 0, 'download', rid, 1, 1)
        self.helper.start_thread.assert_not_called()
        self.assertIn('搜索已过期', self.client.search_message.call_args.args[1])

    def test_cancel_late_results_cannot_start_auto_download(self):
        self.ready()
        with self.store.edit(self.sid) as record:
            record.update(state='queued', results={})
        downloader = Mock()
        downloader.check_exists_medias.return_value = (False, {}, [])
        def search(*args, **kwargs):
            self.service.action(self.sid, 0, 'cancel', None, 1, 1)
            kwargs['search_context']['on_progress']([torrent()], {'a': {'state': 'done'}})
        with patch('web.backend.telegram_search.Downloader', return_value=downloader), \
                patch('web.backend.telegram_search.Searcher') as searcher:
            searcher.return_value.search_one_media.side_effect = search
            self.service.search(self.sid, 0)
        self.assertEqual(self.store.get(self.sid)['state'], 'canceled')
        self.assertEqual(self.store.get(self.sid)['results'], {})
        downloader.download.assert_not_called()
        downloader.get_download_list.assert_not_called()

    def test_double_click_and_another_session_submit_only_once(self):
        rid = self.ready()
        started, release = threading.Event(), threading.Event()
        downloader = Mock()
        downloader.get_search_task_status.return_value = {}
        downloader.check_exists_medias.return_value = (False, {}, [])
        def submit(*args, **kwargs):
            started.set()
            release.wait(3)
            return True, ''
        downloader.download.side_effect = submit
        with patch('web.backend.telegram_search.Downloader', return_value=downloader), ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.service.download, self.sid, 0, rid)
            try:
                self.assertTrue(started.wait(2))
                second = pool.submit(self.service.download, self.sid, 0, rid)
                second.result(2)
            finally:
                release.set()
            first.result(2)
            another, _ = self.store.create(1, 1, '沙丘 again')
            with self.store.edit(another['id']) as record:
                record.update(state='results', results={rid: pack_media(torrent())})
            self.service.download(another['id'], 0, rid)
        self.assertEqual(downloader.download.call_count, 1)
        self.assertEqual(self.store.get(self.sid)['downloads'][rid]['state'], 'accepted')

    def test_uncertain_submission_is_not_retried_after_restart(self):
        rid = self.ready()
        downloader = Mock()
        downloader.get_search_task_status.return_value = {}
        downloader.check_exists_medias.return_value = (False, {}, [])
        downloader.download.return_value = (None, 'timeout')
        with patch('web.backend.telegram_search.Downloader', return_value=downloader):
            self.service.download(self.sid, 0, rid)
            self.service.store = self.store_type(self.directory.name)
            self.service.download(self.sid, 0, rid)
        self.assertEqual(downloader.download.call_count, 1)
        self.assertEqual(self.service.store.get(self.sid)['downloads'][rid]['state'], 'unknown')

    def test_preferences_persist_per_user_and_do_not_mutate_other_search(self):
        self.ready()
        other, _ = self.store.create(2, 2, '其他')
        self.service.action(self.sid, 0, 'quality', None, 1, 1)
        self.service.action(self.sid, 0, 'save', None, 1, 1)
        new, _ = self.store.create(1, 1, '下一部')
        self.assertEqual(new['preferences']['quality'], '1080p')
        self.assertEqual(self.store.get(other['id'])['preferences']['quality'], '')

    def test_callback_owner_checked_before_dispatch(self):
        self.service.callback({'id': 'query', 'from': {'id': 2},
            'message': {'chat': {'id': 1}}, 'data': f'nts:{self.sid}:0:cancel:0'})
        self.helper.start_thread.assert_not_called()
        self.assertEqual(self.store.get(self.sid)['state'], 'identifying')

    def test_queue_rejection_is_visible_and_can_be_restarted(self):
        self.helper.start_thread.side_effect = TaskQueueFull('busy')
        self.service.enqueue(self.sid, self.service.identify, self.sid, 0)
        self.assertEqual(self.store.get(self.sid)['state'], 'interrupted')
        self.assertIn('任务队列繁忙', self.client.search_message.call_args.args[1])

    def test_callback_redelivery_is_admitted_only_once(self):
        query = {'id': 'same-query', 'from': {'id': 1}, 'message': {'chat': {'id': 1}},
                 'data': f'nts:{self.sid}:0:quality:0'}
        self.service.callback(query)
        self.service.callback(query)
        self.assertEqual(self.helper.start_thread.call_count, 1)

    def test_complete_search_collects_results_without_touching_web_table(self):
        from app.searcher import Searcher
        self.ready()
        with self.store.edit(self.sid) as record:
            record.update(state='queued', results={})
        searcher = Searcher.__new__(Searcher)
        searcher.media, searcher.progress, searcher.dbhelper = Mock(), Mock(), Mock()
        searcher.indexer = Mock()
        item = torrent()
        alternate = torrent(url='magnet:?xt=urn:btih:abc&tr=https://tracker.invalid', site='Other')
        def index_search(**kwargs):
            kwargs['search_context']['on_progress']([item], {'test': {'name': 'Test', 'state': 'done', 'valid': 1}})
            kwargs['search_context']['on_progress']([alternate], {'other': {'name': 'Other', 'state': 'done', 'valid': 1}})
            return [item, alternate]
        searcher.indexer.search_by_keyword.side_effect = index_search
        downloader = Mock()
        downloader.check_exists_medias.return_value = (False, {}, [])
        with patch('web.backend.telegram_search.Downloader', return_value=downloader), \
                patch('web.backend.telegram_search.Searcher', return_value=searcher), \
                patch('web.backend.telegram_search.Config') as config, patch('app.searcher.Config') as search_config:
            config.return_value.get_config.return_value = {'search_auto': False}
            search_config.return_value.get_config.return_value = {}
            self.service.search(self.sid, 0)
        record = self.store.get(self.sid)
        self.assertEqual(record['state'], 'results')
        self.assertEqual(len(record['results']), 1)
        self.assertEqual(len(record['results'][resource_key(item)]['sources']), 2)
        searcher.dbhelper.delete_all_search_torrents.assert_not_called()
        searcher.dbhelper.insert_search_results.assert_not_called()
        downloader.download.assert_not_called()
        downloader.get_search_task_status.return_value = {}
        downloader.download.return_value = (True, '')
        with patch('web.backend.telegram_search.Downloader', return_value=downloader):
            self.service.callback({'id': 'source-choice', 'from': {'id': 1},
                'message': {'chat': {'id': 1}}, 'data': f'nts:{self.sid}:0:source:{resource_key(item)}_1'})
            # Execute the queued callback and download through their real entry
            # points; only the queue and downloader are substituted.
            worker, arguments = self.helper.start_thread.call_args.args
            worker(*arguments)
            worker, arguments = self.helper.start_thread.call_args.args
            worker(*arguments)
        self.assertEqual(downloader.download.call_args.args[0].site, 'Other')

    def test_alias_fallback_uses_selected_identity_and_stops_after_success(self):
        self.ready()
        with self.store.edit(self.sid) as record:
            record.update(state='queued', results={})
        downloader, searcher = Mock(), Mock()
        downloader.check_exists_medias.return_value = (False, {}, [])
        searcher.media.movie.details.return_value = {'alternative_titles': {'titles': [
            {'iso_3166_1': 'US', 'title': 'Dune'},
            {'iso_3166_1': 'US', 'title': 'Dune: Part One'},
            {'iso_3166_1': 'GB', 'title': 'Another title'}]}}
        def fallback(*args, **kwargs):
            item = torrent()
            kwargs['search_context']['on_progress']([item], {})
            return [item]
        searcher.search_medias.side_effect = fallback
        with patch('web.backend.telegram_search.Downloader', return_value=downloader), \
                patch('web.backend.telegram_search.Searcher', return_value=searcher), \
                patch('web.backend.telegram_search.Config') as config:
            config.return_value.get_config.return_value = {'search_auto': False}
            self.service.search(self.sid, 0)
        searcher.media.movie.details.assert_called_once_with(438631, append_to_response='alternative_titles', language='en-US')
        searcher.search_medias.assert_called_once()
        self.assertEqual(searcher.search_medias.call_args.args[0], 'Dune: Part One')
        self.assertEqual(len(self.store.get(self.sid)['results']), 1)

    def test_automatic_download_uses_existing_batch_rules_and_task_tag(self):
        rid = self.ready()
        downloader = Mock()
        downloader.get_search_task_status.return_value = {}
        downloader.check_exists_medias.return_value = (False, {}, [])
        downloader.batch_download.return_value = ([torrent()], {})
        with patch('web.backend.telegram_search.Downloader', return_value=downloader):
            self.service.download(self.sid, 0, rid, automatic=True, no_exists={})
        downloader.batch_download.assert_called_once()
        self.assertTrue(downloader.batch_download.call_args.kwargs['task_tag'].startswith('nts-'))
        self.assertEqual(downloader.batch_download.call_args.kwargs['need_tvs'], {})
        downloader.download.assert_not_called()

    def test_downloader_lookup_failure_never_triggers_submission(self):
        rid = self.ready()
        downloader = Mock()
        downloader.get_search_task_status.return_value = {'lookup_failed': True}
        with patch('web.backend.telegram_search.Downloader', return_value=downloader):
            self.service.download(self.sid, 0, rid)
        downloader.download.assert_not_called()
        self.assertEqual(self.store.get(self.sid)['downloads'][rid]['state'], 'not_submitted')

    def test_confirmed_submission_is_not_downgraded_by_status_query_failure(self):
        rid = self.ready()
        downloader = Mock()
        downloader.get_search_task_status.side_effect = [{}, RuntimeError('status unavailable')]
        downloader.check_exists_medias.return_value = (False, {}, [])
        downloader.download.return_value = (True, '')
        with patch('web.backend.telegram_search.Downloader', return_value=downloader):
            self.service.download(self.sid, 0, rid)
            self.assertEqual(self.store.get(self.sid)['downloads'][rid]['state'], 'accepted')
            with self.assertRaises(RuntimeError):
                self.service.refresh_downloads(self.sid)
        self.assertEqual(self.store.get(self.sid)['downloads'][rid]['state'], 'accepted')
        downloader.download.assert_called_once()

    def test_background_status_only_publishes_lifecycle_changes(self):
        rid = self.ready()
        with self.store.edit(self.sid) as record:
            record['downloads'][rid] = {'state': 'downloading', 'message': '下载中',
                                        'setting': None, 'site': 'Test', 'tag': 'nts-test'}
        with patch('web.backend.telegram_search.Downloader') as downloader:
            downloader.return_value.get_search_task_status.return_value = {
                'task_id': 'abc', 'state': 'downloading', 'message': '下载中 30%'}
            self.service.refresh_downloads(self.sid, background=True)
            self.client.search_message.assert_not_called()
            downloader.return_value.get_search_task_status.return_value = {
                'task_id': 'abc', 'state': 'complete', 'message': '下载完成'}
            self.service.refresh_downloads(self.sid, background=True)
            self.client.search_message.assert_called_once()
        self.assertEqual(self.store.get(self.sid)['downloads'][rid]['state'], 'complete')


class WebSearchIsolationTest(SearchSessionFixture):
    def test_old_web_page_keeps_its_resources_and_other_users_cannot_read_it(self):
        from types import SimpleNamespace
        from flask import Flask, session
        from app.helper import DbHelper
        from web.action import WebAction
        app = Flask(__name__)
        app.secret_key = 'offline-test'
        first, _ = self.store.create('alice', 'alice', '沙丘', channel='web')
        second, _ = self.store.create('alice', 'alice', '新搜索', channel='web')
        with self.store.edit(first['id']) as record:
            record.update(state='results', results={'0': pack_media(torrent())})
        action = WebAction.__new__(WebAction)
        action.dbhelper = DbHelper()
        with app.test_request_context('/search?search_id=' + first['id']), \
                patch('app.helper.search_sessions.SearchSessions', return_value=self.store), \
                patch('web.action.current_user', SimpleNamespace(username='alice')):
            session['search_id'] = second['id']
            result = action.get_search_result()
            self.assertEqual(result['total'], 1)
        with app.test_request_context('/search?search_id=' + first['id']), \
                patch('app.helper.search_sessions.SearchSessions', return_value=self.store), \
                patch('web.action.current_user', SimpleNamespace(username='bob')):
            self.assertEqual(action.get_search_result()['total'], 0)

    def test_web_search_persists_isolated_snapshot_without_deleting_legacy_results(self):
        from web.backend import search_torrents
        item = torrent()
        record, _ = self.store.create('alice', 'alice', '沙丘', channel='web')
        with patch('app.helper.search_sessions.SearchSessions', return_value=self.store), \
                patch.object(search_torrents, 'Searcher') as searcher, \
                patch.object(search_torrents, 'DbHelper') as db, \
                patch.object(search_torrents, 'ProgressHelper'):
            searcher.return_value.search_medias.return_value = [item]
            code, _ = search_torrents.search_medias_for_web('沙丘', ident_flag=False, search_id=record['id'])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.store.get(record['id'])['results']), 1)
        db.return_value.delete_all_search_torrents.assert_not_called()
        db.return_value.insert_search_results.assert_not_called()

    def test_repeated_web_download_uses_original_snapshot_once(self):
        from types import SimpleNamespace
        from flask import Flask
        from web.action import WebAction
        app = Flask(__name__)
        record, _ = self.store.create('alice', 'alice', '沙丘', channel='web')
        with self.store.edit(record['id']) as current:
            current.update(state='results', results={'0': pack_media(torrent())})
        action = WebAction.__new__(WebAction)
        with app.test_request_context('/do'), \
                patch('app.helper.search_sessions.SearchSessions', return_value=self.store), \
                patch('web.action.current_user', SimpleNamespace(username='alice')), \
                patch('web.action.Downloader') as downloader, patch('web.action.Message'):
            downloader.return_value.download.return_value = (True, '')
            for _ in range(2):
                self.assertEqual(action._WebAction__download({'id': record['id'] + ':0'})['retcode'], 0)
            downloader.return_value.download.assert_called_once()
            self.assertEqual(downloader.return_value.download.call_args.args[0].tmdb_id, 438631)



class TelegramTransportTest(TestCase):
    def test_message_edit_is_plain_text_and_retains_returned_message_id(self):
        from app.message.client.telegram import Telegram
        client = Telegram.__new__(Telegram)
        client._telegram_token = 'offline-test'
        reply = Mock()
        reply.json.return_value = {'ok': True, 'result': {'message_id': 12}}
        with patch('app.message.client.telegram.requests.post', return_value=reply) as transport:
            result = client.search_message(-10, '<a>_Dune_', [[{'text': '下载', 'callback_data': 'nts:test'}]], 12)
        self.assertEqual(result, 12)
        self.assertTrue(transport.call_args.args[0].endswith('/editMessageText'))
        payload = transport.call_args.kwargs['json']
        self.assertNotIn('parse_mode', payload)
        self.assertEqual(payload['text'], '<a>_Dune_')
        self.assertEqual(payload['chat_id'], -10)

    def test_webhook_routes_authorized_callbacks_and_rejects_other_users(self):
        import web.main as main
        client = Mock()
        client.get_users.return_value = ['1']
        client.get_admin.return_value = ['1']
        with patch.object(main, 'Message') as messages, patch.object(main, 'SecurityHelper') as security, \
                patch('web.backend.telegram_search.TelegramSearch') as service:
            messages.return_value.get_interactive_client.return_value = {'client': client}
            security.return_value.check_telegram_ip.return_value = True
            for user in (1, 2):
                update = {'callback_query': {'id': str(user), 'from': {'id': user},
                    'message': {'chat': {'id': -10}}, 'data': 'nts:test'}}
                with main.App.test_request_context('/telegram', method='POST', json=update):
                    main.telegram()
            service.return_value.callback.assert_called_once()
            self.assertEqual(service.return_value.callback.call_args.args[0]['from']['id'], 1)
            client.answer_search_callback.assert_called_once()

    def test_numeric_title_leaves_legacy_subscription_mode(self):
        import web.main as main
        from web.backend.search_torrents import SEARCH_MEDIA_TYPE
        client = Mock()
        client.get_users.return_value = ['1']
        update = {'update_id': 1917, 'message': {'text': '1917',
                  'from': {'id': 1}, 'chat': {'id': 1}}}
        with patch.object(main, 'Message') as messages, patch.object(main, 'SecurityHelper') as security, \
                patch.object(main, 'WebAction') as legacy, \
                patch('web.backend.telegram_search.TelegramSearch') as service, \
                patch.dict(SEARCH_MEDIA_TYPE, {1: 'SUBSCRIBE'}), \
                main.App.test_request_context('/telegram', method='POST', json=update):
            messages.return_value.get_interactive_client.return_value = {'client': client}
            security.return_value.check_telegram_ip.return_value = True
            main.telegram()
            legacy.assert_not_called()
            service.return_value.receive.assert_called_once_with('1917', 1, 1, None, 1917, None)
            self.assertEqual(SEARCH_MEDIA_TYPE[1], 'SESSION_SEARCH')
