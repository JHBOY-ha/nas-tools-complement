"""Numeric EP, scan diagnostics and publication-receipt contracts (offline)."""
import os
import unittest
from unittest.mock import MagicMock, patch

from app.filetransfer import FileTransfer
from app.media.media import Media
from app.media.meta.extra_transfer import has_pending_publication
from app.utils import EpisodeFormat, PathUtils
from app.utils.types import MediaType, RmtMode, SyncType
from tests import test_manual_episode_precedence as precedence
from web.action import WebAction


class EpisodeFormatValidationTest(unittest.TestCase):
    def test_invalid_controls_fail_before_recognition(self):
        for details in ('-1', '4-3', '4,3', '1-2junk', '1,2,3', '1.5', True):
            with self.subTest(details=details), self.assertRaisesRegex(ValueError, '集数'):
                EpisodeFormat(None, details)
        for offset in ('1.5', True, 1.5, 'x'):
            with self.subTest(offset=offset), self.assertRaisesRegex(ValueError, '偏移'):
                EpisodeFormat(None, '3', offset)
        with self.assertRaisesRegex(ValueError, '偏移后'):
            EpisodeFormat(None, '3', -4)

    def test_negative_offsets_and_zero_remain_supported(self):
        for details, offset, expected in [('3', -3, (0, None)), ('3-4', -3, (0, 1)),
                                           ('999', 0, (999, None)), ('0', 0, (0, None))]:
            self.assertEqual(expected, EpisodeFormat(None, details, offset).split_episode('ignored'))
        pattern = EpisodeFormat('Show.E{ep}.mkv', offset=-3)
        self.assertEqual((0, None), pattern.split_episode('Show.E03.mkv'))
        with self.assertRaisesRegex(ValueError, '偏移后'):
            pattern.split_episode('Show.E02.mkv')
        with self.assertRaisesRegex(ValueError, '终止集'):
            EpisodeFormat('Show.E{ep}.mkv').split_episode('Show.E04-03.mkv')

    def test_zero_matching_and_selection_are_consistent(self):
        for details in (None, '0,3'):
            formatter = EpisodeFormat('Show.E{ep}.mkv', details)
            self.assertTrue(formatter.match('Show.E00.mkv'))
            self.assertEqual((0, None), formatter.split_episode('Show.E00.mkv'))
        self.assertFalse(EpisodeFormat('Show.E{ep}.mkv', '1,3').match('Show.E00.mkv'))
        self.assertEqual((None, None), EpisodeFormat(None).split_episode('Show.E00.mkv'))
        # Keep previously accepted whitespace and numeric strings while validating ranges.
        self.assertTrue(EpisodeFormat('Show.E{ep}.mkv', '2, 4').match('Show.E03.mkv'))
        self.assertEqual((3, None), EpisodeFormat(None, '３').split_episode('ignored'))
        selection = EpisodeFormat('Show.E{ep}.mkv', '2,2', offset=1)
        self.assertEqual([False, True, False], [selection.match('Show.E%02d.mkv' % ep) for ep in (1, 2, 3)])
        self.assertEqual((3, None), selection.split_episode('Show.E02.mkv'))


class EpisodeTransferGuardsTest(unittest.TestCase):
    # Reuse isolated source/work fixtures without inheriting and rerunning their tests.
    filename = precedence.ManualEpisodePrecedenceTest.filename
    setUp = precedence.ManualEpisodePrecedenceTest.setUp
    enter = precedence.ManualEpisodePrecedenceTest.enter
    recognize = precedence.ManualEpisodePrecedenceTest.recognize

    def transfer_fixture(self):
        t = FileTransfer.__new__(FileTransfer)
        t.media = self.media
        t.dbhelper, t.progress, t.message, t.threadhelper = [MagicMock() for _ in range(4)]
        t._filesize_cover = t._scraper_flag = t._refresh_mediaserver = False
        t._tv_category_flag = t._anime_category_flag = t._movie_category_flag = False
        t._tv_dir_rmt_format, t._tv_season_rmt_format = '{title} ({year})', 'Season {season}'
        t._tv_file_rmt_format = '{title} - {season_episode}'
        self.enter(patch('app.filetransfer.Subtitle'))
        self.enter(patch.object(Media, 'get_tmdb_en_title', return_value='Cyberpunk Edgerunners'))
        self.enter(patch.object(self.media, 'get_episode_title', return_value='021'))
        self.enter(patch.object(self.media, 'get_tmdb_info', return_value=self.info))
        self.enter(patch.object(t, 'check_ignore', side_effect=lambda file_list: (file_list, '')))
        return t

    def scan(self, t, **kwargs):
        options = dict(in_from=SyncType.MAN, in_path=str(self.root), rmt_mode=RmtMode.LINK)
        options.update(kwargs)
        return t.transfer_media(**options)

    def test_invalid_batch_target_stops_before_any_transfer_or_lookup(self):
        paths = [self.root / 'Show.E03.mkv', self.root / 'Show.E01.mkv']
        for path in paths:
            path.write_bytes(b'source')
        t = self.transfer_fixture()
        formatter = EpisodeFormat('Show.E{ep}.mkv', offset=-2)
        with patch.object(self.media, 'get_media_info_on_files') as lookup:
            # Both an explicit file list and the directory scanner must validate
            # all selected targets before the first file reaches recognition.
            for files in ([str(path) for path in paths], None):
                with self.subTest(directory_scan=files is None):
                    ok, message = self.scan(t, files=files,
                                            episode=(formatter, False), min_filesize=0)
                    self.assertFalse(ok)
                    self.assertIn('Show.E01.mkv', message)
                    self.assertIn('偏移后', message)
            lookup.assert_not_called()
        t.dbhelper.insert_transfer_history.assert_not_called()
        self.assertTrue(all(path.read_bytes() == b'source' for path in paths))
        t.progress.end.assert_called_with('filetransfer')

    def test_direct_recognition_retains_explicit_failure_for_bad_result(self):
        self.path = self.root / 'Show.E03.mkv'
        self.path.write_bytes(b'source')
        meta = self.recognize(EpisodeFormat('Show.E{ep}.mkv', offset=-4))
        self.assertIn('偏移后', meta.skip_reason)
        self.detail.assert_not_called()

    def test_web_rejects_invalid_controls_before_tmdb_and_transfer(self):
        with patch('web.action.Media') as media, patch('web.action.FileTransfer') as transfer:
            for details, offset in [('4-3', None), ('4,3', None), ('3', -4)]:
                ok, message = WebAction._WebAction__manual_transfer(str(self.path), RmtMode.LINK,
                    media_type=MediaType.TV, tmdbid=42, episode_details=details, episode_offset=offset)
                self.assertFalse(ok)
                self.assertIn('集数', message)
            media.assert_not_called()
            transfer.assert_not_called()
            # Movie form leftovers are irrelevant, including invalid TV-only values.
            transfer.return_value.transfer_media.return_value = (True, '')
            self.assertTrue(WebAction._WebAction__manual_transfer(str(self.path), RmtMode.LINK,
                media_type=MediaType.MOVIE, episode_details='4-3', episode_offset=-9)[0])
            formatter = transfer.return_value.transfer_media.call_args.kwargs['episode'][0]
            self.assertEqual((None, None), formatter.split_episode(self.path.name))

    def test_unrecorded_ordinary_episode_remains_a_valid_manual_target(self):
        self.path = self.root / 'Show.S02E03.mkv'
        self.path.write_bytes(b'source')
        meta = self.recognize(EpisodeFormat(None, '999'))
        self.assertIsNone(meta.skip_reason)
        self.assertEqual([999], meta.get_episode_list())
        self.detail.assert_not_called()

    def test_zero_title_lookup_and_directory_scan(self):
        self.path.unlink()
        self.path = self.root / 'Show.E00.mkv'
        self.path.write_bytes(b'source')
        formatter = EpisodeFormat('Show.E{ep}.mkv')
        self.assertEqual([str(self.path)], PathUtils.get_dir_files(str(self.root),
            exts=['.mkv'], episode_format=formatter))
        meta = self.recognize(formatter)
        with patch.object(self.media, 'get_tmdb_season_episodes', return_value=self.episodes) as query:
            self.assertEqual('0', self.media.get_episode_title(meta))
            query.assert_called_once_with(tmdbid=42, season=2)
            meta.begin_episode = None
            query.reset_mock()
            self.assertIsNone(self.media.get_episode_title(meta))
            query.assert_not_called()

    def test_scan_distinguishes_pattern_from_size_rejection(self):
        with self.path.open('wb') as stream:
            stream.truncate(200 * 1024 * 1024)
        t = self.transfer_fixture()
        with patch.object(self.media, 'get_media_info_on_files') as lookup:
            ok, message = self.scan(t, episode=(EpisodeFormat('Other.E{ep}.mkv'), False))
            self.assertFalse(ok)
            self.assertIn('集数定位或筛选范围不匹配 1 个', message)
            self.assertNotIn('150.0M', message)
            self.path.write_bytes(b'small')
            ok, message = self.scan(t)
            self.assertFalse(ok)
            self.assertIn('小于最小文件大小 1 个', message)
            self.assertIn('150.0M', message)
            lookup.assert_not_called()

    def test_scan_mixed_reasons_and_empty_status_keep_existing_contract(self):
        self.path.unlink()
        t = self.transfer_fixture()
        for source in (SyncType.MON, SyncType.MAN, 'downloader'):
            ok, message = self.scan(t, in_from=source)
            self.assertEqual(source == SyncType.MON, ok)
            self.assertIn('未发现文件', message)
        for filename in ('.hidden.mkv', 'notes.txt', 'Show.E04.mkv', 'Show.E02.mkv'):
            (self.root / filename).write_bytes(b'small')
        formatter = EpisodeFormat('Show.E{ep}.mkv', '0,3')
        diagnostics = {}
        self.assertEqual([str(self.root / 'Show.E02.mkv')], PathUtils.get_dir_files(
            str(self.root), exts=['.mkv'], episode_format=formatter, diagnostics=diagnostics))
        self.assertEqual(dict(scanned=4, invalid_path=1, unsupported_extension=1,
                              episode_mismatch=1, below_min_size=0), diagnostics)
        for source in (SyncType.MON, SyncType.MAN, 'downloader'):
            ok, message = self.scan(t, in_from=source, episode=(formatter, False))
            self.assertEqual(source == SyncType.MON, ok)
            for reason in ('无效路径 1 个', '不支持的文件后缀 1 个', '不匹配 1 个', '小于最小文件大小 1 个'):
                self.assertIn(reason, message)

    def test_scan_errors_still_fail_and_special_size_exemption_survives(self):
        t = self.transfer_fixture()
        with patch('app.utils.path_utils.os.scandir', side_effect=PermissionError('access denied')):
            for source in (SyncType.MON, SyncType.MAN, 'downloader'):
                ok, message = self.scan(t, in_from=source)
                self.assertFalse(ok)
                self.assertIn('目录扫描失败', message)
        self.path.unlink()
        special = self.root / 'Show.S00E01.mkv'
        special.write_bytes(b'small special')
        with patch.object(self.media, 'get_media_info_on_files', return_value={}) as lookup:
            self.scan(t)
            self.assertEqual([str(special)], lookup.call_args.args[0])

    def test_scan_validates_episode_ranges_only_for_media_candidates(self):
        self.path.unlink()
        invalid = self.root / 'Show.E04-03.mkv'
        invalid.write_bytes(b'media')
        (self.root / 'Show.E04-03.txt').write_bytes(b'sidecar')
        t = self.transfer_fixture()
        formatter = EpisodeFormat('Show.E{ep}.{ext}')
        with patch.object(self.media, 'get_media_info_on_files') as lookup:
            for source in (SyncType.MON, SyncType.MAN, 'downloader'):
                with self.subTest(source=source):
                    ok, message = self.scan(t, in_from=source, episode=(formatter, False), min_filesize=0)
                    self.assertFalse(ok)
                    self.assertIn('集数参数无效', message)
            invalid.unlink()
            # A sidecar with the same text is a suffix exclusion, not bad media
            # input; the monitor's normal empty-scan result must remain intact.
            ok, message = self.scan(t, in_from=SyncType.MON, episode=(formatter, False))
            self.assertTrue(ok)
            self.assertIn('不支持的文件后缀 1 个', message)
            self.assertNotIn('集数参数无效', message)
            lookup.assert_not_called()

    def test_forced_overwrite_policy_is_unchanged(self):
        paths = [self.root / 'Show.S02E03.mkv', self.root / 'Show.S02E04.mkv']
        paths[0].write_bytes(b'first source')
        paths[1].write_bytes(b'second source')
        t = self.transfer_fixture()
        self.enter(patch.object(t, '_existing_media_files', return_value=[]))
        self.enter(patch.object(t, '_FileTransfer__transfer_subtitles', return_value=0))
        for force, expected in ((False, b'first source'), (True, b'second source')):
            library = self.root / ('forced' if force else 'ordinary')
            library.mkdir()
            self.scan(t, files=[str(path) for path in paths], target_dir=str(library),
                tmdb_info=self.info, season=2, episode=(EpisodeFormat(None, '3'), False), udf_flag=force)
            outputs = list(library.rglob('*.mkv'))
            self.assertEqual(1, len(outputs))
            self.assertEqual(expected, outputs[0].read_bytes())
            self.assertEqual(b'first source', paths[0].read_bytes())
            self.assertEqual(b'second source', paths[1].read_bytes())

    def test_pending_fractional_receipt_recovers_then_completed_history_skips(self):
        # Real receipt lifecycle complements the completed-history assertion in
        # test_fractional_versions; a failed subtitle stage must remain recoverable.
        t = self.transfer_fixture()
        history = []
        self.enter(patch.object(t, '_existing_media_files', side_effect=lambda _: list(history)))
        subtitles = self.enter(patch.object(t, '_FileTransfer__transfer_subtitles', side_effect=[-1, 0]))
        def record(**kwargs):
            history.append((kwargs['out_path'], 'S02E03'))
            return True
        t.dbhelper.insert_transfer_history.side_effect = record
        library = self.root / 'library'
        library.mkdir()
        args = dict(in_from=SyncType.MAN, in_path=str(self.path), files=[str(self.path)],
            target_dir=str(library), rmt_mode=RmtMode.LINK, tmdb_info=self.info, season=2,
            episode=(EpisodeFormat(None, '3'), False))
        self.assertFalse(t.transfer_media(**args)[0])
        destination = history[0][0]
        self.assertTrue(has_pending_publication(str(self.path), destination))
        t.dbhelper.insert_transfer_blacklist.assert_not_called()
        self.assertTrue(t.transfer_media(**args)[0])
        self.assertEqual(2, subtitles.call_count)
        self.assertFalse(has_pending_publication(str(self.path), destination))
        self.assertTrue(os.path.samefile(self.path, destination))
        self.assertTrue(t.transfer_media(**args)[0])
        self.assertEqual(2, subtitles.call_count)
