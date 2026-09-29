"""Explicit review must stay read-only until confirmed and affect one unchanged file."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.media.media import Media
from app.media.meta import MetaInfo
from app.utils.types import MediaType
from web.backend.special_confirmation import SpecialConfirmation


class SpecialConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, 'Hyouka [11.5].mkv')
        with open(self.path, 'wb') as stream:
            stream.write(b'video')
        self.db, self.media, self.transfer = MagicMock(), MagicMock(), MagicMock()
        self.db.get_unknown_path_by_id.return_value = [SimpleNamespace(PATH=self.path, DEST='/library', MODE=None)]
        self.info = dict(id=42, name='冰菓', media_type=MediaType.TV, seasons=[dict(season_number=0)])
        self.ep = dict(id=123, episode_number=1, season_number=0, name='特别篇', air_date='2013-01-01')
        self.media.get_tmdb_infos.return_value = [self.info]
        self.media.get_tmdb_info.return_value = self.info
        self.media.get_tmdb_tv_season_detail.return_value = dict(episodes=[self.ep])
        self.transfer.transfer_media.return_value = (True, '')
        self.service = SpecialConfirmation(self.db, self.media, self.transfer)
        self.base = dict(flag='unidentification', id=1)

    def request(self, **kwargs):
        return self.service.run(dict(self.base, **kwargs))

    def confirmation(self):
        preview = self.request()
        return dict(stage='confirm', confirmed=True, fingerprint=preview['fingerprint'],
                    type='tv', tmdb_id=42, season=0, episode=1)

    def test_query_and_details_are_read_only_and_have_links(self):
        result = self.request()
        self.assertEqual(result['query'], 'Hyouka')
        self.assertEqual(result['works'][0]['link'], 'https://www.themoviedb.org/tv/42')
        detail = self.request(stage='detail', type='tv', tmdb_id=42)
        self.assertEqual(detail['season'], 0)
        self.assertTrue(detail['episodes'][0]['link'].endswith('/season/0/episode/1'))
        self.transfer.transfer_media.assert_not_called()
        self.db.update_transfer_unknown_state.assert_not_called()

    def test_confirm_rechecks_and_transfers_exact_source(self):
        result = self.request(**self.confirmation())
        self.assertEqual(result['retcode'], 0)
        kwargs = self.transfer.transfer_media.call_args.kwargs
        self.assertEqual(kwargs['in_path'], self.path)
        self.assertEqual(kwargs['tmdb_info']['_file_episode_confirmation']['path'], self.path)
        self.assertNotIn('_file_episode_confirmation', self.info)
        self.db.update_transfer_unknown_state.assert_called_once_with(self.path)

    def test_changed_file_requires_new_preview(self):
        payload = self.confirmation()
        with open(self.path, 'ab') as stream:
            stream.write(b'changed')
        self.assertIn('源文件已变化', self.request(**payload)['retmsg'])
        self.transfer.transfer_media.assert_not_called()

    def test_missing_episode_or_confirmation_cannot_transfer(self):
        for change in [dict(episode=9), dict(confirmed=False), dict(tmdb_id=True), dict(season=-1)]:
            payload = dict(self.confirmation(), **change)
            self.assertEqual(self.request(**payload)['retcode'], 2)
        self.transfer.transfer_media.assert_not_called()

    def test_query_failure_and_transfer_failure_preserve_record(self):
        self.media.get_tmdb_infos.side_effect = RuntimeError('offline')
        self.assertEqual(self.request()['retcode'], 2)
        self.media.get_tmdb_infos.side_effect = None
        self.transfer.transfer_media.return_value = (False, '目标冲突')
        self.assertEqual(self.request(**self.confirmation())['retmsg'], '目标冲突')
        self.db.update_transfer_unknown_state.assert_not_called()

    def test_fractional_confirmation_public_recognition_and_scope(self):
        media = Media.__new__(Media)
        media.tmdb = object()
        info = dict(self.info, _file_episode_confirmation=dict(path=self.path, season=0, episode=1))
        with patch.object(media, 'get_tmdb_tv_season_detail', return_value=dict(episodes=[self.ep])), \
                patch.object(media, 'get_tmdb_season_episodes', return_value=[self.ep]):
            result = media.get_media_info_on_files([self.path], tmdb_info=info)[self.path]
            self.assertFalse(result.skip_reason)
            self.assertEqual(result.begin_season, 0)
            self.assertEqual(result.begin_episode, 1)
            meta = MetaInfo('Other [11.5].mkv', use_llm=False)
            meta.note['fractional_episode']['file_path'] = '/other/file.mkv'
            self.assertFalse(media._confirm_fractional_episode(meta, info))
            meta = MetaInfo('Hyouka [11.5][12.5].mkv', use_llm=False)
            meta.note['fractional_episode']['file_path'] = self.path
            self.assertFalse(media._confirm_fractional_episode(meta, info))

    def test_episode_removed_after_preview_rejects_confirmation(self):
        payload = self.confirmation()
        self.media.get_tmdb_tv_season_detail.return_value = dict(episodes=[])
        self.assertEqual(self.request(**payload)['retcode'], 2)
        self.transfer.transfer_media.assert_not_called()

    def test_source_changed_during_tmdb_lookup_is_rejected(self):
        payload = self.confirmation()
        def change_source(*args, **kwargs):
            with open(self.path, 'ab') as stream:
                stream.write(b'changed during lookup')
            return dict(episodes=[self.ep])
        self.media.get_tmdb_tv_season_detail.side_effect = change_source
        self.assertEqual(self.request(**payload)['retcode'], 2)
        self.transfer.transfer_media.assert_not_called()

    def test_history_uses_recorded_path_and_failed_output_keeps_record(self):
        self.db.get_transfer_path_by_id.return_value = [SimpleNamespace(
            SOURCE_PATH=os.path.dirname(self.path), SOURCE_FILENAME=os.path.basename(self.path),
            DEST='/library', MODE=None)]
        self.base = dict(flag='history', id=2)
        self.assertEqual(self.request(**self.confirmation())['retcode'], 0)
        self.assertEqual(self.transfer.transfer_media.call_args.kwargs['in_path'], self.path)
        self.db.update_transfer_unknown_state.assert_not_called()

    def test_ova_confirmation_uses_existing_manual_episode_controls(self):
        new_path = os.path.join(self.temp.name, 'Show [OVA].mkv')
        os.rename(self.path, new_path)
        self.path = new_path
        self.db.get_unknown_path_by_id.return_value[0].PATH = new_path
        self.assertEqual(self.request(**self.confirmation())['retcode'], 0)
        args = self.transfer.transfer_media.call_args.kwargs
        self.assertNotIn('_file_episode_confirmation', args['tmdb_info'])
        self.assertEqual(args['episode'][0].split_episode('Show [OVA].mkv'), (1, None))
        self.assertEqual(args['season'], 0)

    def test_configured_conflict_is_not_overridden_by_confirmation(self):
        media = Media.__new__(Media)
        media.tmdb = object()
        info = dict(self.info, _file_episode_confirmation=dict(path=self.path, season=0, episode=1))
        meta = MetaInfo('Hyouka [11.5].mkv', use_llm=False)
        meta.note['fractional_episode']['file_path'] = self.path
        with patch('app.media.media.Config') as config:
            config.return_value.get_config.return_value = dict(fractional_episode_mappings=[
                dict(tmdb_id=42, source_episode='11.5', target_season=0, target_episode=2)])
            self.assertFalse(media._confirm_fractional_episode(meta, info))
            self.assertIn('冲突', meta.note['fractional_episode']['reason'])

    def test_confirmed_movie_reaches_real_resolver_without_episode_controls(self):
        source = os.path.join(self.temp.name, 'Show [SP01].mkv')
        os.rename(self.path, source)
        self.path = source
        self.db.get_unknown_path_by_id.return_value[0].PATH = source
        movie = dict(id=42, title='Standalone Story', media_type=MediaType.MOVIE, release_date='2020-01-01')
        self.media.get_tmdb_info.return_value = movie
        real_media = Media.__new__(Media)
        real_media.tmdb = object()
        def transfer(**kwargs):
            result = real_media.get_media_info_on_files([source], tmdb_info=kwargs['tmdb_info'],
                season=kwargs['season'], episode_format=kwargs['episode'][0])[source]
            self.assertFalse(result.skip_reason)
            self.assertEqual(result.type, MediaType.MOVIE)
            self.assertEqual(result.tmdb_id, 42)
            self.assertIsNone(result.begin_episode)
            return True, ''
        self.transfer.transfer_media.side_effect = transfer
        with patch.object(real_media, 'get_tmdb_info', return_value=movie):
            payload = self.confirmation()
            payload.update(type='movie', season=None, episode=None)
            self.assertEqual(self.request(**payload)['retcode'], 0)
