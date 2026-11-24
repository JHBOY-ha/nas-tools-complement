"""BDMV/DVD movie NFO contracts; run through tests.run_recognition offline."""
import errno
import os
import shutil
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree

from app.media.scraper import Scraper
from app.sync import Sync
from app.utils import PathUtils, exclusive_publish
from app.utils.types import RmtMode
from config import RMT_MEDIAEXT
from tests import test_bluray_transfer


@contextmanager
def nfo_link_failure(error):
    """Fail only NFO publication; preserve real disc transfers and other links."""
    link = os.link
    def fail_nfo(source, target, **kwargs):
        if os.path.basename(source).startswith('.movie-nfo-'):
            raise error
        return link(source, target, **kwargs)
    with patch('app.media.scraper.os.link', side_effect=fail_nfo):
        yield


class DiscMetadataTest(unittest.TestCase):
    enter = test_bluray_transfer.BlurayTransferTest.enter
    run_transfer = test_bluray_transfer.BlurayTransferTest.run_transfer
    assert_complete = test_bluray_transfer.BlurayTransferTest.assert_complete
    assert_unrecorded = test_bluray_transfer.BlurayTransferTest.assert_unrecorded
    downloader = test_bluray_transfer.BlurayTransferTest.downloader

    def setUp(self):
        test_bluray_transfer.BlurayTransferTest.setUp(self)
        self.transfer.scraper = Scraper.__new__(Scraper)
        self.transfer.scraper.media = self.catalog.media
        self.transfer.scraper.douban = MagicMock()
        self.transfer._scraper_flag = True
        self.transfer._scraper_nfo = {'movie': {'basic': True, 'credits': False}}
        self.transfer._scraper_pic = {'movie': {}}

    def nfo(self, folder, content, name='movie.nfo'):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text(content, encoding='utf-8')
        return path

    def assert_movie_nfo(self):
        root = ElementTree.parse(self.destination / 'movie.nfo').getroot()
        self.assertEqual('movie', root.tag)
        self.assertEqual('910001', root.findtext('tmdbid'))
        self.assertEqual('910001', root.findtext("uniqueid[@type='tmdb']"))
        self.assertEqual('Arrival', root.findtext('title'))
        self.assertEqual('2016', root.findtext('year'))

    def test_hardlink_generates_directory_nfo_without_mutating_source(self):
        ok, message = self.run_transfer(RmtMode.LINK)
        self.assertTrue(ok, message)
        self.assert_movie_nfo()
        self.assert_complete(self.destination)
        self.assertFalse((self.source / 'movie.nfo').exists())
        self.assertFalse((self.destination / 'Arrival (2016) - 1080p.nfo').exists())
        ordinary = self.root / 'ordinary.nfo'
        ordinary.write_text('reference permissions')
        self.assertEqual(ordinary.stat().st_mode & 0o777,
                         (self.destination / 'movie.nfo').stat().st_mode & 0o777)
        for relative in self.contents:
            self.assertTrue(os.path.samefile(self.source / relative, self.destination / relative))

    def test_nfo_publication_without_libc_renameat2(self):
        # Model Alpine 3.18's missing libc interface without replacing file I/O.
        with patch.object(exclusive_publish, 'sys', SimpleNamespace(platform='linux')), \
                patch.object(exclusive_publish, 'ctypes', SimpleNamespace(CDLL=lambda *a, **k: SimpleNamespace())):
            for mode in (RmtMode.COPY, RmtMode.LINK, RmtMode.SOFTLINK, RmtMode.MOVE):
                with self.subTest(mode=mode):
                    self.transfer._movie_dir_rmt_format = '{title} ({year})/' + mode.name
                    self.destination = self.library / 'Arrival (2016)' / mode.name
                    ok, message = self.run_transfer(mode)
                    self.assertTrue(ok, message)
                    self.assert_movie_nfo()
                    self.assert_complete(self.destination)
                    self.assertFalse(list(self.destination.glob('.movie-nfo-*')))
                    if mode != RmtMode.MOVE:
                        self.assert_complete(self.source)
        self.assertFalse(self.source.exists())

    def test_nfo_publication_falls_back_when_hardlinks_are_unsupported(self):
        with nfo_link_failure(OSError(errno.EOPNOTSUPP, 'NFO hardlinks unsupported')):
            ok, message = self.run_transfer(RmtMode.MOVE)
        self.assertTrue(ok, message)
        self.assert_movie_nfo()
        self.assert_complete(self.destination)
        self.assertFalse(list(self.destination.glob('.movie-nfo-*')))
        self.assertFalse(self.source.exists())

    def test_nfo_link_io_error_keeps_sources_unrecorded(self):
        with nfo_link_failure(OSError(errno.EIO, 'NFO link I/O error')):
            ok, message = self.run_transfer(RmtMode.MOVE)
        self.assertFalse(ok)
        self.assertIn('NFO link I/O error', message)
        self.assert_complete(self.source)
        self.assert_unrecorded()
        self.assertFalse((self.destination / 'movie.nfo').exists())
        self.assertFalse(list(self.destination.glob('.movie-nfo-*')))

    def test_nfo_publication_does_not_replace_a_concurrent_target(self):
        content = '<movie><tmdbid>999</tmdbid></movie>'
        link = os.link
        def competing_link(source, target, **kwargs):
            if os.path.basename(source).startswith('.movie-nfo-'):
                # An external metadata writer publishes after the initial check.
                self.nfo(self.destination, content)
            return link(source, target, **kwargs)
        with patch('app.media.scraper.os.link', side_effect=competing_link):
            ok, _ = self.run_transfer(RmtMode.MOVE)
        self.assertFalse(ok)
        self.assertEqual(content, (self.destination / 'movie.nfo').read_text())
        self.assert_complete(self.source)
        self.assert_unrecorded()
        self.assertFalse(list(self.destination.glob('.movie-nfo-*')))

    def test_matching_source_nfo_preserves_shared_inode_and_custom_metadata(self):
        content = '<movie><uniqueid type="tmdb">910001</uniqueid><title>Custom title</title><plot>Keep me</plot></movie>'
        source = self.nfo(self.source, content)
        ok, message = self.run_transfer(RmtMode.LINK)
        self.assertTrue(ok, message)
        target = self.destination / 'movie.nfo'
        self.assertTrue(os.path.samefile(source, target))
        self.assertEqual(content, source.read_text())
        self.assertEqual(content, target.read_text())

    def test_matching_destination_nfo_is_never_overwritten(self):
        content = '<movie><tmdbid>910001</tmdbid><title>Keep local title</title></movie>'
        target = self.nfo(self.destination, content)
        inode = target.stat().st_ino
        self.assertTrue(self.run_transfer()[0])
        self.assertEqual(inode, target.stat().st_ino)
        self.assertEqual(content, target.read_text())
        self.assert_complete(self.destination)

    def test_invalid_source_nfos_fail_before_publication_and_keep_source(self):
        for content, reason in (
                ('<movie><tmdbid>999</tmdbid></movie>', '冲突'),
                ('<movie><tmdbid>910001</tmdbid><uniqueid type="tmdb">999</uniqueid></movie>', '冲突'),
                ('<movie><tmdbid>invalid</tmdbid></movie>', '冲突'),
                ('<movie><title>Arrival</title></movie>', '缺少 TMDB ID'),
                ('<movie><tmdbid>910001', '无法解析'),
                ('<tvshow><tmdbid>910001</tmdbid></tvshow>', '不是电影')):
            with self.subTest(content=content):
                source = self.nfo(self.source, content)
                ok, message = self.run_transfer(RmtMode.MOVE)
                self.assertFalse(ok)
                self.assertIn(reason, message)
                self.assertEqual(content, source.read_text())
                self.assert_complete(self.source)
                self.assert_unrecorded()
                self.assertFalse((self.destination / 'BDMV').exists())

    def test_conflicting_target_nfo_blocks_downloader_deletion(self):
        target = self.nfo(self.destination, '<movie><tmdbid>999</tmdbid></movie>')
        downloader, client = self.downloader()
        downloader.transfer()
        client.delete_torrents.assert_not_called()
        self.assert_complete(self.source)
        self.assert_unrecorded()
        self.assertEqual('999', ElementTree.parse(target).getroot().findtext('tmdbid'))
        target.unlink()
        downloader.transfer()
        self.assert_movie_nfo()
        self.assert_complete(self.destination)
        client.delete_torrents.assert_called_once_with(delete_file=True, ids='disc-task')

    def test_legacy_target_nfo_is_checked_before_generating_movie_nfo(self):
        name = 'Arrival (2016) - 1080p.nfo'
        target = self.nfo(self.destination, '<movie><tmdbid>999</tmdbid></movie>', name)
        self.assertFalse(self.run_transfer()[0])
        self.assertFalse((self.destination / 'movie.nfo').exists())
        target.write_text('<movie><tmdbid>910001</tmdbid></movie>')
        self.assertTrue(self.run_transfer()[0])
        self.assert_movie_nfo()
        self.assertTrue(target.exists())

    def test_existing_video_does_not_hide_conflicting_legacy_nfo(self):
        content = '<movie><tmdbid>999</tmdbid></movie>'
        target = self.nfo(self.destination, content, 'Arrival (2016) - 1080p.nfo')
        video = self.destination / 'Arrival (2016) - 1080p.mkv'
        video.write_bytes(b'existing movie')
        ok, message = self.run_transfer(RmtMode.MOVE)
        self.assertFalse(ok)
        self.assertIn('冲突', message)
        self.assertEqual(content, target.read_text())
        self.assertEqual(b'existing movie', video.read_bytes())
        self.assert_complete(self.source)
        self.assert_unrecorded()
        self.assertFalse((self.destination / 'BDMV').exists())
        self.assertFalse((self.destination / 'movie.nfo').exists())
        target.write_text('<movie><tmdbid>910001</tmdbid></movie>')
        ok, message = self.run_transfer(RmtMode.MOVE)
        self.assertTrue(ok, message)
        self.assert_movie_nfo()
        self.assert_complete(self.destination)
        self.assertFalse(self.source.exists())

    def test_legacy_nfo_preserves_dotted_template_stem_without_video(self):
        self.transfer._movie_file_rmt_format = '{title}.{year}.{videoFormat}'
        self.nfo(self.destination, '<movie><tmdbid>999</tmdbid></movie>', 'Arrival.2016.1080p.nfo')
        ok, message = self.run_transfer(RmtMode.MOVE)
        self.assertFalse(ok)
        self.assertIn('冲突', message)
        self.assert_complete(self.source)
        self.assert_unrecorded()
        self.assertFalse((self.destination / 'BDMV').exists())

    def test_identity_only_tmdb_url_is_supported(self):
        source = self.nfo(self.source, 'https://www.themoviedb.org/movie/910001-arrival\n')
        self.assertTrue(self.run_transfer(RmtMode.LINK)[0])
        self.assertTrue(os.path.samefile(source, self.destination / 'movie.nfo'))
        self.assertEqual(source.read_bytes(), (self.destination / 'movie.nfo').read_bytes())

    def test_disabled_scraping_still_checks_existing_metadata(self):
        self.transfer._scraper_flag = False
        self.nfo(self.source, '<movie><tmdbid>999</tmdbid></movie>')
        self.assertFalse(self.run_transfer()[0])
        self.assert_unrecorded()
        (self.source / 'movie.nfo').unlink()
        self.assertTrue(self.run_transfer()[0])
        self.assertFalse((self.destination / 'movie.nfo').exists())

    def test_credits_only_configuration_still_writes_identity(self):
        self.transfer._scraper_nfo = {'movie': {'credits': True}}
        with patch.object(self.catalog.media, 'get_tmdb_directors_actors', return_value=([], [])):
            self.assertTrue(self.run_transfer()[0])
        self.assert_movie_nfo()

    def test_atomic_nfo_failure_keeps_sources_unrecorded_and_retryable(self):
        with nfo_link_failure(OSError(errno.EOPNOTSUPP, 'NFO hardlinks unsupported')), \
                patch('app.utils.exclusive_publish.rename_exclusive', side_effect=OSError('NFO publish failed')):
            ok, message = self.run_transfer(RmtMode.MOVE)
        self.assertFalse(ok)
        self.assertIn('NFO publish failed', message)
        self.assertFalse((self.destination / 'movie.nfo').exists())
        self.assertFalse(list(self.destination.glob('.movie-nfo-*')))
        self.assert_complete(self.source)
        self.assert_unrecorded()
        self.assertTrue(self.run_transfer(RmtMode.MOVE)[0])
        self.assert_movie_nfo()
        self.assertFalse(self.source.exists())

    def test_history_failure_reuses_generated_nfo_on_retry(self):
        with patch.object(self.transfer.dbhelper, 'insert_transfer_history', return_value=False):
            self.assertFalse(self.run_transfer(RmtMode.MOVE)[0])
        self.assert_movie_nfo()
        inode = (self.destination / 'movie.nfo').stat().st_ino
        self.assert_complete(self.source)
        self.assert_unrecorded()
        self.assertTrue(self.run_transfer(RmtMode.MOVE)[0])
        self.assertEqual(inode, (self.destination / 'movie.nfo').stat().st_ino)

    def test_cleanup_receipt_includes_generated_metadata(self):
        rmdir = os.rmdir
        def interrupt(path, *args, **kwargs):
            if str(path).startswith(str(self.source) + os.sep):
                raise PermissionError('cleanup interrupted')
            return rmdir(path, *args, **kwargs)
        with patch('app.filetransfer.os.rmdir', side_effect=interrupt):
            self.assertFalse(self.run_transfer(RmtMode.MOVE)[0])
        target = self.destination / 'movie.nfo'
        original, content = target.stat(), target.read_bytes()
        target.write_text('<movie><tmdbid>999</tmdbid></movie>')
        ok, message = self.run_transfer(RmtMode.MOVE)
        self.assertFalse(ok)
        self.assertIn('目标发生变化', message)
        target.write_bytes(content)
        os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertTrue(self.run_transfer(RmtMode.MOVE)[0])
        self.assert_movie_nfo()


class DvdTransferTest(unittest.TestCase):
    enter = test_bluray_transfer.BlurayTransferTest.enter
    run_transfer = test_bluray_transfer.BlurayTransferTest.run_transfer
    assert_unrecorded = test_bluray_transfer.BlurayTransferTest.assert_unrecorded
    downloader = test_bluray_transfer.BlurayTransferTest.downloader
    assert_movie_nfo = DiscMetadataTest.assert_movie_nfo
    nfo = DiscMetadataTest.nfo

    def setUp(self):
        DiscMetadataTest.setUp(self)
        shutil.rmtree(self.source)
        self.source = self.root / 'Arrival.2016.DVD'
        self.contents = {'VIDEO_TS/VIDEO_TS.IFO': b'index', 'VIDEO_TS/VIDEO_TS.BUP': b'backup',
                         'VIDEO_TS/VTS_01_0.IFO': b'title info', 'VIDEO_TS/VTS_01_1.VOB': b'video',
                         '.disc-info': b'hidden'}
        for relative, content in self.contents.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (self.source / 'AUDIO_TS').mkdir()

    def assert_complete(self, root):
        for relative, content in self.contents.items():
            self.assertEqual(content, (root / relative).read_bytes())
        self.assertTrue((root / 'AUDIO_TS').is_dir())

    def test_root_directory_and_internal_files_normalize_as_one_dvd(self):
        self.assertEqual('dvd', PathUtils.get_disc_type(str(self.source)))
        for relative in ('.', 'VIDEO_TS', 'VIDEO_TS/VIDEO_TS.IFO', 'VIDEO_TS/VIDEO_TS.BUP',
                         'VIDEO_TS/VTS_01_0.IFO', 'VIDEO_TS/VTS_01_1.VOB', 'AUDIO_TS'):
            with self.subTest(relative=relative):
                self.assertEqual(str(self.source), PathUtils.get_disc_dir(str(self.source / relative)))
        self.assertNotIn('.vob', RMT_MEDIAEXT)
        unsupported = self.root / 'isolated.vob'
        unsupported.write_bytes(b'not a DVD')
        self.assertIsNone(PathUtils.get_disc_dir(str(unsupported)))
        self.assertFalse(self.run_transfer(in_path=str(unsupported))[0])

    def test_dvd_hardlinks_with_nfo_and_without_fake_bluray_subtitle_job(self):
        ok, message = self.run_transfer(RmtMode.LINK, files=[str(self.source / relative) for relative in self.contents])
        self.assertTrue(ok, message)
        self.assert_movie_nfo()
        self.assert_complete(self.destination)
        for relative in self.contents:
            self.assertTrue(os.path.samefile(self.source / relative, self.destination / relative))
        self.transfer.threadhelper.start_thread.assert_not_called()
        self.assertFalse(list(self.destination.rglob('*.mkv')))

    def test_dvd_copy_move_and_softlink(self):
        for mode in (RmtMode.COPY, RmtMode.SOFTLINK, RmtMode.MOVE):
            with self.subTest(mode=mode):
                self.transfer._movie_dir_rmt_format = '{title} ({year})/' + mode.name
                self.destination = self.library / 'Arrival (2016)' / mode.name
                ok, message = self.run_transfer(mode)
                self.assertTrue(ok, message)
                self.assert_complete(self.destination)
                self.assert_movie_nfo()
                if mode != RmtMode.MOVE:
                    self.assert_complete(self.source)
        self.assertFalse(self.source.exists())

    def test_dvd_move_keeps_real_tree_when_source_root_is_symlink(self):
        actual = self.source.rename(self.root / 'real-source')
        self.source.symlink_to(actual, target_is_directory=True)
        self.assertFalse(self.run_transfer(RmtMode.MOVE)[0])
        self.assertTrue(self.source.is_symlink())
        self.assert_complete(actual)
        self.assert_unrecorded()

    def test_unrecognized_dvd_move_keeps_disc_together_for_manual_retry(self):
        self.source = self.source.rename(self.root / 'Unidentified.Movie.2017.DVD')
        unknown = self.root / 'unknown'
        unknown.mkdir()
        self.assertFalse(self.run_transfer(RmtMode.MOVE, unknown_dir=str(unknown))[0])
        self.assert_complete(self.source)
        self.assertEqual(list(unknown.iterdir()), [])

    def test_numbered_dvd_and_mixed_sets_do_not_enqueue_bluray_subtitles(self):
        for prefix, mixed in (('DVD', False), ('Disc', True)):
            for mode in (RmtMode.COPY, RmtMode.MOVE):
                with self.subTest(prefix=prefix, mode=mode):
                    pack = self.root / (prefix + '-' + mode.name) / 'Arrival.2016'
                    pack.mkdir(parents=True)
                    shutil.copytree(self.source, pack / (prefix + '1'))
                    second = pack / (prefix + '2')
                    if mixed:
                        (second / 'BDMV/STREAM').mkdir(parents=True)
                        (second / 'BDMV/index.bdmv').write_bytes(b'index')
                        (second / 'BDMV/STREAM/00001.m2ts').write_bytes(b'movie')
                    else:
                        shutil.copytree(self.source, second)
                    self.transfer._movie_dir_rmt_format = '{title} ({year})/' + prefix + '-' + mode.name
                    self.destination = self.library / 'Arrival (2016)' / (prefix + '-' + mode.name)
                    ok, message = self.run_transfer(mode, in_path=str(pack))
                    self.assertTrue(ok, message)
                    self.assert_complete(self.destination / (prefix + '1'))
                    if mixed:
                        stream = self.destination / (prefix + '2') / 'BDMV/STREAM/00001.m2ts'
                        self.assertEqual(b'movie', stream.read_bytes())
                    else:
                        self.assert_complete(self.destination / (prefix + '2'))
                    self.transfer.threadhelper.start_thread.assert_not_called()
                    self.assertEqual(mode != RmtMode.MOVE, pack.exists())

    def test_dvd_nfo_conflicts_are_checked_in_both_supported_names(self):
        for name in ('movie.nfo', 'VIDEO_TS.nfo'):
            with self.subTest(name=name):
                path = self.nfo(self.source, '<movie><tmdbid>999</tmdbid></movie>', name)
                self.assertFalse(self.run_transfer(RmtMode.MOVE)[0])
                self.assert_complete(self.source)
                self.assert_unrecorded()
                path.unlink()
        self.nfo(self.source, '<movie><tmdbid>910001</tmdbid></movie>', 'VIDEO_TS.nfo')
        self.assertTrue(self.run_transfer()[0])
        self.assert_movie_nfo()

    test_atomic_nfo_failure_keeps_sources_unrecorded_and_retryable = DiscMetadataTest.test_atomic_nfo_failure_keeps_sources_unrecorded_and_retryable
    test_cleanup_receipt_includes_generated_metadata = DiscMetadataTest.test_cleanup_receipt_includes_generated_metadata

    def test_interrupted_dvd_move_keeps_ifo_and_resumes(self):
        from app.media.meta.extra_transfer import publish_exclusive
        calls = []
        def interrupt(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 2:
                raise OSError('DVD interrupted')
            return publish_exclusive(*args, **kwargs)
        with patch('app.filetransfer.publish_exclusive', side_effect=interrupt):
            self.assertFalse(self.run_transfer(RmtMode.MOVE)[0])
        self.assert_complete(self.source)
        self.assert_unrecorded()
        self.assertTrue(self.run_transfer(RmtMode.MOVE)[0])
        self.assert_complete(self.destination)
        self.assert_movie_nfo()
        self.assertFalse(self.source.exists())

    def test_move_preserves_unrecognized_dvd_in_collection_on_every_retry(self):
        collection = self.root / 'Collection'
        collection.mkdir()
        self.source = self.source.rename(collection / self.source.name)
        unrecognized = collection / 'Another.Movie' / 'VIDEO_TS' / 'VTS_01_1.VOB'
        unrecognized.parent.mkdir(parents=True)
        unrecognized.write_bytes(b'untransferred movie')
        downloader, client = self.downloader()
        client.get_transfer_task.return_value = [dict(path=str(collection), id='collection', tags=[])]
        for _ in range(2):
            downloader.transfer()
            self.assert_complete(self.destination)
            self.assertEqual(unrecognized.read_bytes(), b'untransferred movie')
            client.delete_torrents.assert_not_called()
            client.set_torrents_status.assert_not_called()

    def test_collection_mixes_dvd_bluray_and_preserves_numbered_dvd_set(self):
        second = self.root / 'Interstellar.2014.BluRay'
        (second / 'BDMV/STREAM').mkdir(parents=True)
        (second / 'BDMV/index.bdmv').write_bytes(b'index')
        (second / 'BDMV/STREAM/00001.m2ts').write_bytes(b'other movie')
        self.catalog.movie(910002, 'Interstellar', year='2014')
        self.assertIsNone(PathUtils.get_disc_dir(str(self.root)))
        self.assertTrue(self.run_transfer(files=[str(self.source / 'VIDEO_TS/VIDEO_TS.IFO'),
                                                 str(second / 'BDMV/index.bdmv')])[0])
        self.assert_complete(self.destination)
        self.assertTrue((self.library / 'Interstellar (2014)/movie.nfo').exists())
        self.transfer.threadhelper.start_thread.assert_called_once()
        subtitle_items = self.transfer.threadhelper.start_thread.call_args.args[1][0]
        self.assertEqual(1, len(subtitle_items))
        self.assertEqual('Interstellar', subtitle_items[0]['title'])
        pack = self.root / 'DVD Set'
        pack.mkdir()
        shutil.copytree(self.source, pack / 'DVD1')
        shutil.copytree(self.source, pack / 'DVD2')
        self.assertEqual(str(pack), PathUtils.get_disc_dir(str(pack / 'DVD1/VIDEO_TS/VTS_01_1.VOB')))
        self.assertIsNone(PathUtils.get_disc_type(str(pack)))

    def test_sync_events_accept_dvd_members_but_ignore_standalone_vob(self):
        sync = Sync()
        self.enter(patch.object(sync, 'filetransfer', self.transfer))
        self.enter(patch.object(sync, 'dbhelper', self.transfer.dbhelper))
        self.enter(patch.object(sync, '_synced_files', []))
        self.enter(patch.object(sync, '_need_sync_paths', {}))
        self.enter(patch.object(sync, 'sync_dir_config', {str(self.root): {
            'target': str(self.library), 'unknown': None, 'syncmod': RmtMode.COPY, 'onlylink': False}}))
        self.transfer._tv_path = self.transfer._anime_path = []
        event = SimpleNamespace(is_directory=False)
        standalone = self.root / 'other.vob'
        standalone.write_bytes(b'not a disc')
        sync.file_change_handler(event, 'created', str(standalone))
        self.assertEqual({}, sync._need_sync_paths)
        for relative in ('VIDEO_TS/VIDEO_TS.IFO', 'VIDEO_TS/VIDEO_TS.BUP', 'VIDEO_TS/VTS_01_1.VOB'):
            sync.file_change_handler(event, 'created', str(self.source / relative))
        self.assertTrue(sync._need_sync_paths)
        sync.transfer_mon_files()
        self.assert_complete(self.destination)
        self.assert_movie_nfo()
