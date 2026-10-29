"""Cour/part numbering and ordinary episode collision regressions, offline."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app.filetransfer import FileTransfer
from app.media.media import Media
from app.media.meta import MetaInfo
from app.media.meta.llm_parser import LLMMetaParser
from app.media.meta.metavideo import MetaVideo
from app.media.meta.title_utils import strip_anime_part_markers
from app.utils.types import MediaType, RmtMode, SyncType


class CourEpisodeRecognitionTest(unittest.TestCase):
    def setUp(self):
        words = patch('app.media.meta.metainfo.WordsHelper')
        self.addCleanup(words.stop)
        words.start().return_value.process.side_effect = lambda title: (title, [], {})

    @staticmethod
    def filename(number, marker='Cour 3'):
        return ('[CheeseAni] Dr.STONE SCIENCE FUTURE %s [%s]'
                '[BDRip 1080p HEVC OPUSx2 SRT][简繁内封].mkv') % (marker, number)

    def test_all_thirteen_files_keep_their_actual_episode(self):
        for number in range(25, 38):
            title = self.filename(number)
            with self.subTest(number=number):
                meta = MetaInfo(title, use_llm=False)
                self.assertEqual([number], meta.get_episode_list())
                self.assertIsNone(meta.begin_season)
                self.assertEqual(title, meta.org_string)
                self.assertEqual('CheeseAni', meta.resource_team)

    def test_part_labels_keep_separate_single_and_range_episode_evidence(self):
        for marker in ('Cour 3', 'Cour.III', 'Part 3', 'Part3', 'Part III',
                       '第3部分', '第3クール', '3rd Cour'):
            for number, expected in (('37', [37]), ('25-37', list(range(25, 38)))):
                with self.subTest(marker=marker, number=number):
                    meta = MetaInfo(self.filename(number, marker), use_llm=False)
                    self.assertEqual(expected, meta.get_episode_list())

    def test_release_part_without_episode_cannot_manufacture_one(self):
        for marker in ('Cour 3', 'Part 3', 'Part3', '第3部分', '第3クール', '3rd Cour'):
            title = '[CheeseAni] Dr.STONE SCIENCE FUTURE %s [BDRip 1080p HEVC].mkv' % marker
            with self.subTest(marker=marker):
                self.assertEqual([], MetaInfo(title, mtype=MediaType.ANIME, use_llm=False).get_episode_list())

    def test_part_token_boundaries_preserve_unrelated_title_words(self):
        for token in ('Party 3', 'Courthouse 3', 'Part 3D', 'Cour 3.5', 'Part 1999'):
            title = '[Group] Show %s [37][1080p].mkv' % token
            with self.subTest(token=token):
                self.assertEqual(title, strip_anime_part_markers(title))

    def test_part_movie_without_episode_evidence_keeps_the_video_parser(self):
        title = '[Group] Example Movie Part 2 (2024) [BDRip 1080p HEVC].mkv'
        self.assertIsInstance(MetaInfo(title, mtype=MediaType.MOVIE, use_llm=False), MetaVideo)

    def test_part_with_explicit_season_and_versioned_episode(self):
        for episode in ('[37v2]', 'E37', 'EP37'):
            title = '[Group] Show S04 Part 3 %s [BDRip 1080p HEVC].mkv' % episode
            with self.subTest(episode=episode):
                meta = MetaInfo(title, use_llm=False)
                self.assertEqual((4, 37), (meta.begin_season, meta.begin_episode))

    def test_real_rss_pack_keeps_season_four_and_complete_range(self):
        title = ('[芝士动物朋友] 石纪元 科学与未来 第3部分 / Dr.STONE SCIENCE FUTURE Cour 3 / '
                 'Dr.STONE SCIENCE FUTURE 第3クール [25-37][BDRip 1080p HEVC OPUS SRT]'
                 '[简繁内封]【CatchPlay官译】（新石纪 第四季 S4 Part 3）')
        meta = MetaInfo(title, use_llm=False)
        self.assertEqual([4], meta.get_season_list())
        self.assertEqual(list(range(25, 38)), meta.get_episode_list())

    def test_both_llm_modes_start_from_the_correct_pack_range(self):
        for mode in ('conservative', 'balanced'):
            parser = object.__new__(type(LLMMetaParser()))
            parser._enabled, parser._mode = True, mode
            title = self.filename('25-37')
            meta = MetaInfo(title, use_llm=False)
            with self.subTest(mode=mode), patch.object(parser, 'parse', return_value={
                    'begin_episode': 25, 'end_episode': 37}):
                parser.merge_into(meta, title)
                self.assertEqual(list(range(25, 38)), meta.get_episode_list())

    def test_bound_download_uses_file_episode_and_task_season_without_llm(self):
        media = Media.__new__(Media)
        media.tmdb = object()
        info = {'id': 86031, 'name': '石纪元', 'media_type': MediaType.TV,
                'genre_ids': [16], 'seasons': [{'season_number': 4, 'episode_count': 37}]}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / self.filename(37)
            path.touch()
            with patch.object(media, 'save_rename_cache'), patch(
                    'app.media.meta.metainfo.LLMMetaParser') as llm:
                meta = media.get_media_info_on_files([str(path)], download_context={
                    'tmdb_info': info, 'seasons': [4], 'episodes': [37],
                    'numbering': 'tmdb'})[str(path)]
            self.assertIsNone(meta.skip_reason)
            self.assertEqual((4, 37), (meta.begin_season, meta.begin_episode))
            llm.return_value.merge_into.assert_not_called()
            llm.return_value.parse.assert_not_called()


class OrdinaryEpisodeCollisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = self.root / 'library'
        self.library.mkdir()
        self.transfer = FileTransfer.__new__(FileTransfer)
        self.info = {'id': 86031, 'name': '石纪元', 'media_type': MediaType.TV,
                     'genre_ids': [16], 'seasons': [{'season_number': 4}]}

    def batch(self, episodes=(3, 3)):
        medias = {}
        for index, episode in enumerate(episodes):
            path = self.root / ('source%d.mkv' % index)
            path.write_bytes(('source%d' % index).encode())
            meta = MetaInfo('Show S04E%02d.mkv' % episode, use_llm=False)
            meta.set_tmdb_info(self.info)
            medias[str(path)] = meta
        return medias

    def destination(self, root, meta):
        path = self.library / ('Show S04E%02d' % meta.begin_episode)
        return True, str(self.library), False, str(path)

    def preflight(self, medias):
        with patch.object(self.transfer, '_FileTransfer__is_media_exists', side_effect=self.destination):
            return self.transfer._check_fractional_destinations(medias, str(self.library), RmtMode.LINK)

    def test_all_different_sources_in_collision_group_are_retained(self):
        medias = self.batch()
        self.preflight(medias)
        self.assertTrue(all('冲突' in (meta.skip_reason or '') for meta in medias.values()))
        self.assertEqual([], list(self.library.iterdir()))
        self.assertTrue(all(Path(path).exists() for path in medias))

    def test_unrelated_episodes_and_separate_named_versions_still_pass(self):
        medias = self.batch((3, 3, 4))
        metas = list(medias.values())
        metas[0].resource_pix, metas[1].resource_pix = '1080p', '2160p'
        def target(root, meta):
            return True, root, False, str(self.library / ('E%s-%s' % (meta.begin_episode, meta.resource_pix)))
        with patch.object(self.transfer, '_FileTransfer__is_media_exists', side_effect=target):
            self.transfer._check_fractional_destinations(medias, str(self.library), RmtMode.LINK)
        self.assertTrue(all(not m.skip_reason for m in medias.values()))

    def test_one_collision_does_not_block_an_unrelated_episode(self):
        medias = self.batch((3, 3, 4))
        self.preflight(medias)
        self.assertTrue(all(m.skip_reason for m in list(medias.values())[:2]))
        self.assertIsNone(list(medias.values())[2].skip_reason)

    def test_same_inode_aliases_do_not_trigger_a_false_collision(self):
        medias = self.batch()
        first, second = map(Path, medias)
        second.unlink()
        os.link(first, second)
        self.preflight(medias)
        self.assertTrue(all(not m.skip_reason for m in medias.values()))

    def test_path_resolution_error_retains_only_the_affected_source(self):
        medias = self.batch((3, 4))
        def target(root, meta):
            if meta.begin_episode == 3:
                raise OSError('unavailable destination')
            return self.destination(root, meta)
        with patch.object(self.transfer, '_FileTransfer__is_media_exists', side_effect=target):
            self.transfer._check_fractional_destinations(medias, str(self.library), RmtMode.LINK)
        self.assertTrue(list(medias.values())[0].skip_reason)
        self.assertIsNone(list(medias.values())[1].skip_reason)

    def test_transfer_entry_refuses_collision_before_any_publication(self):
        medias = self.batch()
        transfer = self.transfer
        transfer.progress = MagicMock()
        transfer.dbhelper = MagicMock()
        transfer.media = MagicMock()
        transfer.message = MagicMock()
        transfer._ignore_config_error = ''
        transfer.media.get_media_info_on_files.return_value = medias
        with patch.object(transfer, 'check_ignore', return_value=(list(medias), '')), \
                patch.object(transfer, '_FileTransfer__is_media_exists', side_effect=self.destination), \
                patch.object(transfer, '_FileTransfer__transfer_command') as publish:
            success, message = transfer.transfer_media(in_from=SyncType.MAN, in_path=str(self.root),
                files=list(medias), rmt_mode=RmtMode.MOVE, target_dir=str(self.library))
        self.assertFalse(success)
        self.assertIn('冲突', message)
        publish.assert_not_called()
        transfer.dbhelper.insert_transfer_history.assert_not_called()
        transfer.dbhelper.insert_transfer_blacklist.assert_not_called()
        self.assertEqual(2, transfer.dbhelper.insert_transfer_unknown.call_count)
        self.assertTrue(all(Path(path).exists() for path in medias))
        self.assertEqual([], list(self.library.iterdir()))
