"""Behavioral regressions for review findings; no real TMDB or user library access."""
import errno
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.filetransfer import FileTransfer
from app.media.media import Media
from app.media.meta import MetaInfo
from app.media.meta.metainfo import prepare_title
from app.media.meta.fractional import protect_fractional_episode
from app.media.meta.special import extract_special
from app.media.meta.special_resolver import SpecialResolver
from app.media.meta.extra_transfer import publish_extra
from app.media.tmdbv3api.as_obj import AsObj
from app.utils.exclusive_publish import rename_exclusive
from app.utils.types import MediaType, RmtMode, SyncType


class ReviewRecognitionTest(unittest.TestCase):
    def test_words_precede_numeric_shortcut_and_release_extraction(self):
        pairs = [('1917.mkv', 'Arrival.2016.mkv'), ('1.mkv', '1917.mkv'),
                 ('Show E13.mkv', 'Show E13.5.mkv'), ('Show.mkv', 'Show [OAD].mkv'),
                 ('Movie 2020.mkv', 'Movie 2020 [Directors Cut].mkv')]
        provenance = dict(ignored=['ignore'], replaced=['replace'], offset=['offset'])
        for original, replaced in pairs:
            with self.subTest(original=original), patch('app.media.meta.metainfo.WordsHelper') as words:
                words.return_value.process.return_value = (replaced, [], provenance)
                meta = MetaInfo(original, mtype=MediaType.MOVIE if original in ('1917.mkv', '1.mkv') else None, use_llm=False)
                words.return_value.process.assert_called_once_with(original)
                self.assertEqual(meta.org_string, original)
                self.assertEqual(meta.replaced_words, ['replace'])
                self.assertEqual(meta.ignored_words, ['ignore'])
                self.assertEqual(meta.offset_words, ['offset'])
                if original == '1917.mkv': self.assertEqual(meta.get_name(), 'Arrival')
                if original == '1.mkv': self.assertEqual(meta.get_name(), '1917')
                if 'E13' in original: self.assertEqual(meta.note['fractional_episode']['key'], '13.5')
                if original == 'Show.mkv': self.assertEqual(meta.note['special_episode']['kind'], 'OAD')
                if original.startswith('Movie'): self.assertEqual(meta.cut, 'Directors Cut')

    def test_batch_reuses_extraction_after_user_replacement(self):
        media = Media.__new__(Media)
        media.tmdb = object()
        info = dict(id=42, name='Show', media_type=MediaType.TV, seasons=[dict(season_number=0)])
        ep = dict(episode_number=1, name='Side_Story', overview='Episode 13.5', season_number=0)
        with tempfile.TemporaryDirectory() as root:
            source = str(Path(root, 'Show E13.mkv'))
            Path(source).write_bytes(b'video')
            with patch('app.media.meta.metainfo.WordsHelper') as words, \
                    patch('app.media.meta.metainfo.protect_fractional_episode', wraps=protect_fractional_episode) as fractional, \
                    patch('app.media.meta.metainfo.extract_special', wraps=extract_special) as special, \
                    patch.object(media, 'get_tmdb_tv_season_detail', return_value=dict(episodes=[ep])), \
                    patch.object(media, 'save_rename_cache'):
                words.return_value.process.side_effect = lambda title: (title.replace('E13', 'E13.5'), [], {})
                result = media.get_media_info_on_files([source], tmdb_info=info)[source]
                self.assertFalse(result.skip_reason)
                self.assertEqual((result.begin_season, result.begin_episode), (0, 1))
                self.assertEqual(fractional.call_count, 1)
                self.assertEqual(special.call_count, 1)
                words.return_value.process.assert_called_once()

    def test_prepared_notes_are_not_mutated_by_consumers(self):
        prepared = prepare_title('Show [OVA01].mkv')
        first = MetaInfo('Show [OVA01].mkv', use_llm=False, _prepared=prepared)
        first.note['special_episode']['status'] = 'confirmed'
        second = MetaInfo('Show [OVA01].mkv', use_llm=False, _prepared=prepared)
        self.assertEqual(second.note['special_episode']['status'], 'unconfirmed')

    def test_fractional_title_normalizes_underscores_like_specials(self):
        media = Media.__new__(Media)
        media.tmdb = object()
        info = dict(id=42, media_type=MediaType.TV, seasons=[dict(season_number=0)])
        meta = MetaInfo('Show [13.5] - Side Story.mkv', use_llm=False)
        with patch.object(media, 'get_tmdb_tv_season_detail', return_value=dict(episodes=[
                dict(episode_number=2, name='Side_Story', season_number=0)])):
            self.assertTrue(media._confirm_fractional_episode(meta, info))
            self.assertEqual(meta.begin_episode, 2)

    def test_full_page_is_followed_until_exhausted(self):
        info = dict(id=42, name='One Piece', media_type=MediaType.TV, genres=[dict(id=16)])
        page1 = [info] + [dict(id=i, media_type='person') for i in range(19)]
        media = MagicMock()
        media.get_tmdb_search_page.side_effect = [page1, []]
        media.get_tmdb_info.return_value = info
        self.assertEqual(SpecialResolver(media).search(['One Piece']), [info])
        self.assertEqual([c.kwargs['page'] for c in media.get_tmdb_search_page.call_args_list], [1, 2])
        # A matching work on a later page must still make automatic identity ambiguous.
        other = dict(info, id=43)
        media.get_tmdb_search_page.side_effect = [page1, [other]]
        media.get_tmdb_info.side_effect = lambda mtype, tmdbid, **kw: info if tmdbid == 42 else other
        self.assertEqual(len(SpecialResolver(media).search(['One Piece'])), 2)
        media.get_tmdb_search_page.side_effect = [page1, None]
        with self.assertRaises(ValueError): SpecialResolver(media).search(['One Piece'])
        media.get_tmdb_search_page.side_effect = [page1, page1]
        with self.assertRaises(ValueError): SpecialResolver(media).search(['One Piece'])

    def test_page_number_reaches_sdk_without_mutating_results(self):
        media = Media.__new__(Media)
        media.tmdb, media.search = object(), MagicMock()
        original = AsObj(id=42, media_type='tv', name='Show')
        media.search.multi.return_value = [original]
        self.assertEqual(media.get_tmdb_infos('Show', page=2)[0]['id'], 42)
        media.search.multi.assert_called_once_with({'query': 'Show', 'page': 2})
        self.assertEqual(original.media_type, 'tv')


class ExclusivePublicationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.destination = self.root / 'source.mkv', self.root / 'target.mkv'
        self.source.write_bytes(b'original video')

    @staticmethod
    def copy(source, destination, mode):
        if mode != RmtMode.COPY: raise AssertionError('Expected COPY staging')
        shutil.copyfile(source, destination)
        return 0

    def test_native_exclusive_rename_refuses_existing_destination(self):
        self.destination.write_bytes(b'competing video')
        with self.assertRaises(FileExistsError): rename_exclusive(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), b'competing video')
        self.assertTrue(self.source.exists())

    def test_copy_move_without_hardlinks_and_record_retry(self):
        for mode in (RmtMode.COPY, RmtMode.MOVE):
            with self.subTest(mode=mode):
                self.source.write_bytes(b'original video')
                if self.destination.exists(): self.destination.unlink()
                with patch('app.media.meta.extra_transfer.os.link', side_effect=OSError(errno.EOPNOTSUPP, 'no links')):
                    with self.assertRaises(OSError):
                        publish_extra(str(self.source), str(self.destination), mode, self.copy, lambda: False)
                    self.assertTrue(self.source.exists())
                    self.assertEqual(self.destination.read_bytes(), b'original video')
                    publish_extra(str(self.source), str(self.destination), mode, self.copy, lambda: True)
                    self.assertEqual(self.source.exists(), mode == RmtMode.COPY)
                    self.assertFalse(list(self.root.glob('*.receipt')))

    def test_competitor_between_staging_and_fallback_is_preserved(self):
        def unsupported(*args, **kwargs):
            self.destination.write_bytes(b'competing video')
            raise OSError(errno.EOPNOTSUPP, 'no links')
        with patch('app.media.meta.extra_transfer.os.link', side_effect=unsupported):
            with self.assertRaises(FileExistsError):
                publish_extra(str(self.source), str(self.destination), RmtMode.MOVE, self.copy, lambda: True)
        self.assertEqual(self.destination.read_bytes(), b'competing video')
        self.assertTrue(self.source.exists())

    def test_protected_transfer_cannot_overwrite_late_destination(self):
        transfer = FileTransfer.__new__(FileTransfer)
        def copy_and_compete(source, destination, mode):
            shutil.copyfile(source, destination)
            self.destination.write_bytes(b'competing video')
            return 0
        with patch.object(transfer, '_FileTransfer__transfer_command', side_effect=copy_and_compete):
            with self.assertRaises(FileExistsError):
                transfer._FileTransfer__transfer_file(str(self.source), str(self.destination), RmtMode.COPY, protected=True)
        self.assertEqual(self.destination.read_bytes(), b'competing video')
        self.assertTrue(self.source.exists())

    def test_unconfirmed_source_is_registered_without_transfer(self):
        transfer = FileTransfer.__new__(FileTransfer)
        transfer.progress, transfer.dbhelper, transfer.media = MagicMock(), MagicMock(), MagicMock()
        meta = MetaInfo('Show E13.5.mkv', use_llm=False)
        meta.skip_reason = '需要确认特殊集'
        transfer.media.get_media_info_on_files.return_value = {str(self.source): meta}
        transfer.check_ignore = lambda file_list: (file_list, '')
        ok, _ = transfer.transfer_media(in_from=SyncType.MAN, in_path=str(self.source),
                                       target_dir=str(self.root), rmt_mode=RmtMode.LINK)
        self.assertFalse(ok)
        transfer.dbhelper.insert_transfer_unknown.assert_called_once_with(str(self.source), str(self.root), RmtMode.LINK)
        transfer.dbhelper.insert_transfer_blacklist.assert_not_called()
        self.assertFalse(self.destination.exists())
