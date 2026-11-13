"""Disc publication/retry contracts; run through python3 -m tests.run_recognition."""
import os
import shutil
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, PropertyMock, patch

from app.downloader.downloader import Downloader
from app.filetransfer import FileTransfer
from app.helper import DbHelper
from app.media.meta.extra_transfer import publish_exclusive
from app.utils.types import DownloaderType, MediaType, RmtMode, SyncType
from app.utils import EpisodeFormat, PathUtils
from app.utils.isolated_io import get_io_pool
from tests.recognition_fixture import FakeTmdb
from tests.test_special_episodes import move_copy_fallback


class BlurayTransferTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='nas-bluray-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.catalog = FakeTmdb(self)
        self.catalog.movie(910001, 'Arrival', year='2016')
        self.source = self.root / 'Arrival.2016.1080p.BluRay'
        self.contents = {
            'BDMV/index.bdmv': b'index',
            'BDMV/STREAM/00001.m2ts': b'movie',
            'BDMV/PLAYLIST/00001.mpls': b'playlist',
            'CERTIFICATE/id.bdmv': b'certificate',
            '.disc-info': b'hidden sidecar',
        }
        for relative, content in self.contents.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (self.source / 'BDMV/AUXDATA').mkdir()
        self.library = self.root / 'library'
        self.library.mkdir()
        self.destination = self.library / 'Arrival (2016)'
        self.transfer = FileTransfer.__new__(FileTransfer)
        self.transfer.media = self.catalog.media
        self.transfer.dbhelper = DbHelper()
        self.transfer.progress, self.transfer.message, self.transfer.threadhelper = [MagicMock() for _ in range(3)]
        self.transfer._filesize_cover = self.transfer._scraper_flag = self.transfer._refresh_mediaserver = False
        self.transfer._movie_category_flag = False
        self.transfer._movie_dir_rmt_format = '{title} ({year})'
        self.transfer._movie_file_rmt_format = '{title} ({year}) - {videoFormat}'
        self.transfer._movie_path = [str(self.library)]
        self.transfer._unknown_path = []
        self.enter(patch('app.filetransfer.Media', return_value=self.catalog.media))
        self.enter(patch.object(self.catalog.media, 'get_tmdb_en_title', return_value='Arrival'))
        self.enter(patch('app.filetransfer.Subtitle'))
        self.enter(patch('app.filetransfer.sleep'))

    def enter(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def run_transfer(self, mode=RmtMode.COPY, **kwargs):
        options = dict(in_from=SyncType.MAN, in_path=str(self.source),
                       target_dir=str(self.library), rmt_mode=mode)
        options.update(kwargs)
        return self.transfer.transfer_media(**options)

    def assert_complete(self, root):
        for relative, content in self.contents.items():
            self.assertEqual(content, (root / relative).read_bytes(), relative)
        self.assertTrue((root / 'BDMV/AUXDATA').is_dir())

    def assert_unrecorded(self):
        self.assertFalse(self.transfer.dbhelper.is_transfer_in_blacklist(str(self.source)))
        rows = self.transfer.dbhelper.get_media_transfer_history(910001, MediaType.MOVIE)
        self.assertFalse([row for row in rows if row.SOURCE_PATH == str(self.source.parent)])

    def downloader(self, mode=RmtMode.MOVE):
        downloader = Downloader()
        client = MagicMock()
        client.get_transfer_task.return_value = [dict(path=str(self.source), id='disc-task', tags=[])]
        self.enter(patch.object(type(downloader), 'default_client', new_callable=PropertyMock, return_value=client))
        self.enter(patch.object(downloader, 'filetransfer', self.transfer))
        self.enter(patch.object(downloader, '_get_download_context', return_value=None))
        self.enter(patch.object(downloader, '_default_client_type', DownloaderType.QB))
        self.enter(patch.object(downloader, '_pt_monitor_only', False))
        self.enter(patch.object(downloader, '_pt_rmt_mode', mode))
        return downloader, client

    def test_empty_existing_directory_is_filled(self):
        self.destination.mkdir()
        ok, message = self.run_transfer()
        self.assertTrue(ok, message)
        self.assert_complete(self.destination)
        self.assert_complete(self.source)

    def test_manual_retry_reuses_identical_files_and_adds_missing_files(self):
        existing = self.destination / 'BDMV/index.bdmv'
        existing.parent.mkdir(parents=True)
        existing.write_bytes(self.contents['BDMV/index.bdmv'])
        inode = existing.stat().st_ino
        ok, message = self.run_transfer(udf_flag=True)
        self.assertTrue(ok, message)
        self.assertEqual(inode, existing.stat().st_ino)
        self.assert_complete(self.destination)

    def test_existing_different_files_fail_even_when_same_size(self):
        existing = self.destination / 'BDMV/STREAM/00001.m2ts'
        existing.parent.mkdir(parents=True)
        for content in (b'bad', b'other'):
            for mode in (RmtMode.COPY, RmtMode.MOVE, RmtMode.LINK):
                with self.subTest(content=content, mode=mode):
                    existing.write_bytes(content)
                    ok, message = self.run_transfer(mode)
                    self.assertFalse(ok)
                    self.assertIn('内容冲突', message)
                    self.assertEqual(content, existing.read_bytes())
                    self.assert_complete(self.source)
                    self.assert_unrecorded()

    def test_failed_copy_keeps_partial_bytes_out_of_final_filename_and_retries(self):
        command = self.transfer._FileTransfer__transfer_command
        calls = []
        def fail_second(file_item, target_file, rmt_mode):
            calls.append(file_item)
            if len(calls) == 2:
                Path(target_file).write_bytes(b'partial')
                return -1
            return command(file_item, target_file, rmt_mode)
        with patch.object(self.transfer, '_FileTransfer__transfer_command', side_effect=fail_second):
            self.assertFalse(self.run_transfer()[0])
        self.assert_complete(self.source)
        self.assert_unrecorded()
        self.assertFalse((self.destination / Path(calls[1]).relative_to(self.source)).exists())
        ok, message = self.run_transfer()
        self.assertTrue(ok, message)
        self.assert_complete(self.destination)

    def test_interrupted_move_retains_whole_source_and_retries_without_recopy(self):
        calls = []
        def fail_second(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 2:
                raise OSError('interrupted disc')
            return publish_exclusive(*args, **kwargs)
        with patch('app.filetransfer.publish_exclusive', side_effect=fail_second):
            self.assertFalse(self.run_transfer(RmtMode.MOVE)[0])
        self.assert_complete(self.source)
        self.assert_unrecorded()
        first = self.destination / Path(calls[0]).relative_to(self.source)
        inode = first.stat().st_ino
        # Same-filesystem MOVE publishes hardlinks while retaining source entries.
        self.assertTrue(os.path.samefile(calls[0], first))
        ok, message = self.run_transfer(RmtMode.MOVE)
        self.assertTrue(ok, message)
        self.assertEqual(inode, first.stat().st_ino)
        self.assert_complete(self.destination)
        self.assertFalse(self.source.exists())

    def test_record_failures_preserve_sources_and_rollback_both_records(self):
        for method in ('insert_transfer_history', 'insert_transfer_blacklist'):
            with self.subTest(method=method):
                with patch.object(self.transfer.dbhelper, method, return_value=False):
                    self.assertFalse(self.run_transfer(RmtMode.MOVE)[0])
                self.assert_complete(self.source)
                self.assert_complete(self.destination)
                self.assert_unrecorded()
        ok, message = self.run_transfer(RmtMode.MOVE)
        self.assertTrue(ok, message)
        self.assertFalse(self.source.exists())
        self.assert_complete(self.destination)

    def test_cross_volume_move_fallback_preserves_sources_until_recorded(self):
        with ExitStack() as stack:
            for relative in self.contents:
                stack.enter_context(move_copy_fallback(self.source / relative))
            with patch.object(self.transfer.dbhelper, 'insert_transfer_history', return_value=False):
                self.assertFalse(self.run_transfer(RmtMode.MOVE)[0])
            self.assert_complete(self.source)
            self.assert_complete(self.destination)
            self.assert_unrecorded()
            for relative in self.contents:
                self.assertFalse(os.path.samefile(self.source / relative, self.destination / relative))
            ok, message = self.run_transfer(RmtMode.MOVE)
        self.assertTrue(ok, message)
        self.assertFalse(self.source.exists())
        self.assert_complete(self.destination)

    def test_same_size_and_timestamp_damage_is_not_hidden_by_compare_cache(self):
        # Keep comparisons in the same persistent worker to exercise its cache.
        with get_io_pool('bulk').session():
            self.assertTrue(self.run_transfer()[0])
            target = self.destination / 'BDMV/STREAM/00001.m2ts'
            original = target.stat()
            target.write_bytes(b'other')
            os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
            ok, message = self.run_transfer(RmtMode.MOVE)
        self.assertFalse(ok)
        self.assertIn('内容冲突', message)
        self.assert_complete(self.source)
        self.assertEqual(b'other', target.read_bytes())

    def test_retry_cleans_receipt_left_after_atomic_publication(self):
        def fail_record(source, target, mode, transfer, record, **kwargs):
            return publish_exclusive(source, target, mode, transfer, lambda: False, **kwargs)
        with patch('app.filetransfer.publish_exclusive', side_effect=fail_record):
            self.assertFalse(self.run_transfer()[0])
        self.assertTrue(list(self.destination.rglob('*.extra-pending')))
        self.assert_complete(self.source)
        self.assert_unrecorded()
        ok, message = self.run_transfer()
        self.assertTrue(ok, message)
        self.assert_complete(self.destination)
        self.assertFalse(list(self.destination.rglob('*.extra-pending*')))

    def test_monitor_does_not_trust_legacy_blacklist_for_partial_disc(self):
        self.transfer.dbhelper.insert_transfer_blacklist(str(self.source))
        self.destination.mkdir()
        ok, message = self.run_transfer(in_from=SyncType.MON)
        self.assertTrue(ok, message)
        self.assert_complete(self.destination)

    def test_hardlink_and_softlink_keep_sources(self):
        for mode in (RmtMode.LINK, RmtMode.SOFTLINK):
            with self.subTest(mode=mode):
                self.transfer._movie_dir_rmt_format = '{title} ({year})/' + mode.name
                ok, message = self.run_transfer(mode)
                self.assertTrue(ok, message)
                target = self.destination / mode.name
                self.assert_complete(target)
                self.assert_complete(self.source)
                for relative in self.contents:
                    self.assertTrue(os.path.samefile(self.source / relative, target / relative))
                    self.assertEqual(mode == RmtMode.SOFTLINK, (target / relative).is_symlink())

    def test_move_rejects_existing_target_symlink_into_source(self):
        target = self.destination / 'BDMV/index.bdmv'
        target.parent.mkdir(parents=True)
        target.symlink_to(self.source / 'BDMV/index.bdmv')
        self.assertFalse(self.run_transfer(RmtMode.MOVE)[0])
        self.assert_complete(self.source)
        self.assert_unrecorded()

    def test_move_rejects_target_subdirectory_linked_into_source(self):
        self.destination.mkdir()
        (self.destination / 'BDMV').symlink_to(self.source / 'BDMV', target_is_directory=True)
        self.assertFalse(self.run_transfer(RmtMode.MOVE)[0])
        self.assert_complete(self.source)
        self.assert_unrecorded()

    def test_interrupted_staging_copy_can_resume(self):
        def interrupt(file_item, target_file, rmt_mode):
            Path(target_file).write_bytes(b'partial staging')
            raise InterruptedError('staging interrupted')
        with patch.object(self.transfer, '_FileTransfer__transfer_command', side_effect=interrupt):
            self.assertFalse(self.run_transfer()[0])
        self.assertTrue(list(self.destination.rglob('*.extra-pending')))
        self.assert_complete(self.source)
        self.assert_unrecorded()
        ok, message = self.run_transfer()
        self.assertTrue(ok, message)
        self.assert_complete(self.destination)
        self.assertFalse(list(self.destination.rglob('*.extra-pending*')))

    def test_move_cleanup_failure_retains_disc_entry_for_retry(self):
        unlink = os.unlink
        removed = []
        def fail_cleanup(path, *args, **kwargs):
            if str(path).startswith(str(self.source) + os.sep):
                if removed:
                    raise PermissionError('cleanup interrupted')
                removed.append(path)
            return unlink(path, *args, **kwargs)
        with patch('app.filetransfer.os.unlink', side_effect=fail_cleanup):
            self.assertFalse(self.run_transfer(RmtMode.MOVE)[0])
        self.assertTrue((self.source / 'BDMV/index.bdmv').is_file())
        self.assert_complete(self.destination)
        ok, message = self.run_transfer(RmtMode.MOVE)
        self.assertTrue(ok, message)
        self.assertFalse(self.source.exists())
        self.assert_complete(self.destination)

    def test_source_changes_during_publication_are_not_acknowledged(self):
        calls = []
        def change_source(*args, **kwargs):
            result = publish_exclusive(*args, **kwargs)
            calls.append(args[0])
            if len(calls) == 1:
                (self.source / 'new-file.txt').write_bytes(b'arrived during transfer')
            return result
        with patch('app.filetransfer.publish_exclusive', side_effect=change_source):
            ok, message = self.run_transfer(RmtMode.MOVE)
        self.assertFalse(ok)
        self.assertIn('转移期间变化', message)
        self.assert_complete(self.source)
        self.assert_unrecorded()
        self.assertTrue(self.run_transfer(RmtMode.MOVE)[0])
        self.assertEqual(b'arrived during transfer', (self.destination / 'new-file.txt').read_bytes())

    def test_scan_error_cannot_be_acknowledged_as_partial_success(self):
        walk = os.walk
        def broken_walk(path, *args, **kwargs):
            if str(path) == str(self.source) and kwargs.get('onerror'):
                kwargs['onerror'](PermissionError('unreadable disc directory'))
            return walk(path, *args, **kwargs)
        with patch('app.filetransfer.os.walk', side_effect=broken_walk):
            ok, message = self.run_transfer(RmtMode.MOVE)
        self.assertFalse(ok)
        self.assertIn('unreadable disc directory', message)
        self.assert_complete(self.source)
        self.assert_unrecorded()

    def test_remote_modes_fail_without_mutating_source(self):
        for mode in (RmtMode.RCLONE, RmtMode.RCLONECOPY, RmtMode.MINIO, RmtMode.MINIOCOPY):
            with self.subTest(mode=mode), patch.object(self.transfer, '_FileTransfer__transfer_command') as command:
                ok, message = self.run_transfer(mode)
                self.assertFalse(ok)
                self.assertIn('远程转移暂不支持完整性校验', message)
                command.assert_not_called()
                self.assert_complete(self.source)
                self.assert_unrecorded()

    def test_downloader_never_deletes_conflicting_or_unverifiable_disc(self):
        existing = self.destination / 'BDMV/index.bdmv'
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b'wrong')
        for mode in (RmtMode.MOVE, RmtMode.RCLONE, RmtMode.MINIO):
            with self.subTest(mode=mode):
                downloader, client = self.downloader(mode)
                downloader.transfer()
                client.delete_torrents.assert_not_called()
                client.set_torrents_status.assert_not_called()
                self.assert_complete(self.source)

    def test_downloader_deletes_only_after_retry_completes_and_records(self):
        downloader, client = self.downloader()
        with patch.object(self.transfer.dbhelper, 'insert_transfer_history', return_value=False):
            downloader.transfer()
        client.delete_torrents.assert_not_called()
        self.assert_complete(self.source)
        self.assert_unrecorded()
        downloader.transfer()
        self.assert_complete(self.destination)
        self.assertTrue(self.transfer.dbhelper.is_transfer_in_blacklist(str(self.source)))
        client.delete_torrents.assert_called_once_with(delete_file=True, ids='disc-task')

    def test_ignored_disc_cannot_authorize_downloader_deletion(self):
        downloader, client = self.downloader()
        with patch.object(self.transfer, 'check_ignore', return_value=([], 'ignored')):
            downloader.transfer()
        client.delete_torrents.assert_not_called()
        self.assert_complete(self.source)
        self.assert_unrecorded()


class BlurayNormalizationTest(unittest.TestCase):
    # Reuse the isolated real-files/database fixture, without rerunning its tests.
    setUp = BlurayTransferTest.setUp
    enter = BlurayTransferTest.enter
    run_transfer = BlurayTransferTest.run_transfer
    assert_complete = BlurayTransferTest.assert_complete
    downloader = BlurayTransferTest.downloader

    def collection(self):
        collection = self.root / 'Collection'
        collection.mkdir()
        moved = collection / self.source.name
        self.source.rename(moved)
        self.source = moved
        second = collection / 'Interstellar.2014.2160p.BluRay'
        shutil.copytree(self.source, second)
        (second / 'BDMV/STREAM/00001.m2ts').write_bytes(b'other movie')
        self.catalog.movie(910002, 'Interstellar', year='2014')
        return collection, second

    def disc_set(self):
        original = self.root / 'disc-staging'
        self.source.rename(original)
        self.source.mkdir()
        original.rename(self.source / 'Disc1')
        shutil.copytree(self.source / 'Disc1', self.source / 'Disc2')
        (self.source / 'notes.nfo').write_text('two discs of one movie')
        return self.source

    def test_named_movies_are_not_a_single_disc(self):
        collection, second = self.collection()
        self.assertIsNone(PathUtils.get_bluray_dir(str(collection)))
        for root in (self.source, second):
            self.assertEqual(str(root), PathUtils.get_bluray_dir(str(root)))
        # Even a wrapper with just one named movie must not acquire its identity.
        shutil.rmtree(second)
        self.assertIsNone(PathUtils.get_bluray_dir(str(collection)))

    def test_collection_scan_returns_roots_without_stream_fragments(self):
        collection, second = self.collection()
        found = PathUtils.get_dir_files(str(collection), exts=['.m2ts'], filesize=10**9,
            episode_format=EpisodeFormat('Show.E{ep}.mkv'), include_bluray=True, raise_errors=True)
        self.assertEqual({str(self.source), str(second)}, set(found))
        self.assertEqual(2, len(found))
        # Other callers retain the ordinary recursive-file contract.
        regular = PathUtils.get_dir_files(str(collection), exts=['.m2ts'])
        self.assertEqual({str(root / 'BDMV/STREAM/00001.m2ts') for root in (self.source, second)}, set(regular))

    def test_collection_transfers_two_movies_to_separate_destinations(self):
        collection, second = self.collection()
        ok, message = self.run_transfer(in_path=str(collection))
        self.assertTrue(ok, message)
        self.assert_complete(self.destination)
        self.assertEqual(b'other movie', (self.library / 'Interstellar (2014)/BDMV/STREAM/00001.m2ts').read_bytes())
        self.assertFalse((self.library / 'Collection').exists())
        for tmdb_id, root in ((910001, self.source), (910002, second)):
            rows = self.transfer.dbhelper.get_media_transfer_history(tmdb_id, MediaType.MOVIE)
            self.assertEqual([root.name], [row.SOURCE_FILENAME for row in rows if row.SOURCE_PATH == str(collection)])

    def test_nested_collection_is_discovered(self):
        collection, second = self.collection()
        genre = collection / 'Science Fiction'
        genre.mkdir()
        nested = genre / second.name
        second.rename(nested)
        ok, message = self.run_transfer(in_path=str(collection))
        self.assertTrue(ok, message)
        self.assert_complete(self.destination)
        self.assertTrue((self.library / 'Interstellar (2014)/BDMV/index.bdmv').is_file())

    def test_all_disc_entry_points_resolve_to_the_same_root(self):
        for relative in ('.', 'BDMV', 'BDMV/STREAM', 'BDMV/index.bdmv',
                         'BDMV/STREAM/00001.m2ts', 'BDMV/PLAYLIST/00001.mpls', 'CERTIFICATE/id.bdmv'):
            with self.subTest(relative=relative):
                self.assertEqual(str(self.source), PathUtils.get_bluray_dir(str(self.source / relative)))

    def test_explicit_mixed_disc_paths_are_deduplicated_before_recognition(self):
        entries = [str(self.source / relative) for relative in ('.', 'BDMV', 'BDMV/STREAM',
            'BDMV/index.bdmv', 'BDMV/STREAM/00001.m2ts', 'BDMV/PLAYLIST/00001.mpls', 'CERTIFICATE/id.bdmv')]
        entries += [str(self.source / 'BDMV/STREAM/../STREAM/00001.m2ts')]
        with patch.object(self.catalog.media, 'get_media_info_on_files',
                          wraps=self.catalog.media.get_media_info_on_files) as recognize:
            ok, message = self.run_transfer(files=entries)
        self.assertTrue(ok, message)
        self.assertEqual([str(self.source)], recognize.call_args.args[0])
        self.assert_complete(self.destination)
        rows = self.transfer.dbhelper.get_media_transfer_history(910001, MediaType.MOVIE)
        self.assertEqual(1, len([row for row in rows if row.SOURCE_PATH == str(self.source.parent)]))

    def test_single_index_file_is_a_supported_transfer_entry(self):
        ok, message = self.run_transfer(in_path=str(self.source / 'BDMV/index.bdmv'))
        self.assertTrue(ok, message)
        self.assert_complete(self.destination)

    def test_explicit_selection_does_not_include_an_unselected_movie(self):
        collection, second = self.collection()
        ok, message = self.run_transfer(RmtMode.MOVE, in_path=str(collection),
            files=[str(self.source / 'BDMV/STREAM/00001.m2ts')])
        self.assertTrue(ok, message)
        self.assertFalse(self.source.exists())
        self.assert_complete(self.destination)
        self.assertTrue((second / 'BDMV/index.bdmv').exists())
        self.assertEqual(b'other movie', (second / 'BDMV/STREAM/00001.m2ts').read_bytes())
        self.assertFalse((self.library / 'Interstellar (2014)').exists())

    def test_explicit_two_disc_list_is_processed_per_movie(self):
        collection, second = self.collection()
        ok, message = self.run_transfer(in_path=str(collection), files=[
            str(self.source / 'BDMV/STREAM/00001.m2ts'), str(second / 'BDMV/index.bdmv'),
            str(self.source / 'BDMV/index.bdmv')])
        self.assertTrue(ok, message)
        self.assert_complete(self.destination)
        self.assertEqual(b'other movie', (self.library / 'Interstellar (2014)/BDMV/STREAM/00001.m2ts').read_bytes())

    def test_disc_and_regular_file_use_their_own_transfer_branches(self):
        movie = self.root / 'Coco.2017.1080p.mkv'
        movie.write_bytes(b'normal movie')
        self.catalog.movie(910003, 'Coco', year='2017')
        ok, message = self.run_transfer(files=[str(self.source / 'BDMV/index.bdmv'), str(movie)])
        self.assertTrue(ok, message)
        self.assert_complete(self.destination)
        normal = self.library / 'Coco (2017)/Coco (2017) - 1080p.mkv'
        self.assertEqual(b'normal movie', normal.read_bytes())
        self.assertFalse((normal.parent / 'BDMV').exists())

    def test_numbered_disc_set_remains_one_item_from_every_entry(self):
        pack = self.disc_set()
        for relative in ('.', 'Disc1', 'Disc2/BDMV', 'Disc1/BDMV/index.bdmv',
                         'Disc2/BDMV/STREAM/00001.m2ts'):
            with self.subTest(relative=relative):
                self.assertEqual(str(pack), PathUtils.get_bluray_dir(str(pack / relative)))
        ok, message = self.run_transfer(files=[str(pack / 'Disc1/BDMV/index.bdmv'),
                                              str(pack / 'Disc2/BDMV/STREAM/00001.m2ts')])
        self.assertTrue(ok, message)
        self.assert_complete(self.destination / 'Disc1')
        self.assert_complete(self.destination / 'Disc2')
        self.assertTrue((self.destination / 'notes.nfo').exists())

    def test_numbered_discs_do_not_absorb_named_movies_or_separate_video_files(self):
        pack = self.disc_set()
        extra = pack / 'Coco.2017.mkv'
        extra.write_bytes(b'other movie')
        self.assertIsNone(PathUtils.get_bluray_dir(str(pack)))
        self.assertEqual(str(pack / 'Disc1'), PathUtils.get_bluray_dir(str(pack / 'Disc1')))
        extra.unlink()
        (pack / 'Interstellar.2014').mkdir()
        self.assertIsNone(PathUtils.get_bluray_dir(str(pack)))

    def test_one_failed_disc_keeps_batch_failed_and_retains_other_disc_identity(self):
        collection, second = self.collection()
        bad_target = self.destination / 'BDMV/index.bdmv'
        bad_target.parent.mkdir(parents=True)
        bad_target.write_bytes(b'conflict')
        ok, message = self.run_transfer(RmtMode.MOVE, in_path=str(collection), files=[
            str(self.source / 'BDMV/index.bdmv'), str(second / 'BDMV/index.bdmv')])
        self.assertFalse(ok)
        self.assertIn('内容冲突', message)
        self.assert_complete(self.source)
        self.assertFalse(second.exists())
        self.assertEqual(b'other movie', (self.library / 'Interstellar (2014)/BDMV/STREAM/00001.m2ts').read_bytes())

    def test_ignored_disc_in_mixed_batch_cannot_authorize_deletion(self):
        collection, second = self.collection()
        with patch.object(self.transfer, 'check_ignore', side_effect=lambda file_list: (
                [path for path in file_list if path != str(second)], 'ignored second disc')):
            ok, message = self.run_transfer(RmtMode.MOVE, in_path=str(collection))
        self.assertFalse(ok)
        self.assertIn('部分蓝光原盘被忽略', message)
        self.assert_complete(self.destination)
        self.assertTrue((second / 'BDMV/index.bdmv').is_file())

    def test_sync_queue_with_root_and_index_list_uses_disc_normalization(self):
        from app.sync import Sync
        sync = Sync()
        entry = str(self.source / 'BDMV/index.bdmv')
        self.enter(patch.object(sync, 'filetransfer', self.transfer))
        self.enter(patch.object(sync, 'dbhelper', self.transfer.dbhelper))
        self.enter(patch.object(sync, '_synced_files', [entry]))
        self.enter(patch.object(sync, '_need_sync_paths', {str(self.source): {
            'files': [entry], 'target': str(self.library), 'syncmod': RmtMode.COPY}}))
        sync.transfer_mon_files()
        self.assert_complete(self.destination)
        self.assertEqual([], sync._synced_files)

    def test_collection_downloader_retries_failed_disc_before_deleting_task(self):
        collection, second = self.collection()
        bad_target = self.destination / 'BDMV/index.bdmv'
        bad_target.parent.mkdir(parents=True)
        bad_target.write_bytes(b'conflict')
        downloader, client = self.downloader()
        client.get_transfer_task.return_value = [dict(path=str(collection), id='disc-task', tags=[])]
        downloader.transfer()
        client.delete_torrents.assert_not_called()
        client.set_torrents_status.assert_not_called()
        self.assert_complete(self.source)
        self.assertFalse(second.exists())
        other_target = self.library / 'Interstellar (2014)/BDMV/STREAM/00001.m2ts'
        inode = other_target.stat().st_ino
        # The conflicting old file needs explicit removal; the successful other
        # movie must remain intact when the downloader retries the same collection.
        bad_target.unlink()
        downloader.transfer()
        self.assert_complete(self.destination)
        self.assertEqual(b'other movie', other_target.read_bytes())
        self.assertEqual(inode, other_target.stat().st_ino)
        client.delete_torrents.assert_called_once_with(delete_file=True, ids='disc-task')

    def test_monitor_preserves_failure_when_only_ignored_disc_and_cached_file_remain(self):
        movie = self.root / 'Coco.2017.1080p.mkv'
        movie.write_bytes(b'cached movie')
        self.transfer.dbhelper.insert_transfer_blacklist(str(movie))
        with patch.object(self.transfer, 'check_ignore', side_effect=lambda file_list: (
                [path for path in file_list if path != str(self.source)], 'ignored disc')):
            ok, message = self.run_transfer(in_from=SyncType.MON,
                files=[str(self.source / 'BDMV/index.bdmv'), str(movie)])
        self.assertFalse(ok)
        self.assertIn('部分蓝光原盘被忽略', message)
        self.assert_complete(self.source)
        self.assertFalse(self.destination.exists())
