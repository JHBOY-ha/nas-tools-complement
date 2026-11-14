"""Manual EP wins over release labels without bypassing TMDB or file safeguards."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.filetransfer import FileTransfer
from app.media.media import Media
from app.media.meta import MetaInfo
from app.media.meta.fractional import protect_fractional_episode
from app.utils import EpisodeFormat, PathUtils
from app.utils.types import MediaType, RmtMode, SyncType
from web.action import WebAction
from web.backend.special_confirmation import SpecialConfirmation


class ManualEpisodePrecedenceTest(unittest.TestCase):
    filename = "Cyberpunk.Edgerunners.S02E03.021.1080p.NF.WEB-DL.DUAL.DDP5.1.H.264-VARYG.mkv"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / self.filename
        self.path.write_bytes(b"source media")
        self.info = dict(id=42, name="Cyberpunk Edgerunners", media_type=MediaType.TV,
                         first_air_date="2020-01-01", seasons=[dict(season_number=2)])
        self.episodes = [dict(episode_number=n, season_number=2, show_id=42, name=str(n))
                         for n in (0, 3, 4)]
        self.media = Media.__new__(Media)
        self.media.tmdb = object()
        self.detail = self.enter(patch.object(self.media, "get_tmdb_tv_season_detail",
                                             return_value=dict(episodes=self.episodes)))
        self.saved = self.enter(patch.object(self.media, "save_rename_cache"))

    def enter(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def recognize(self, formatter=None, **kwargs):
        options = dict(tmdb_info=self.info, season=2,
                       episode_format=formatter or EpisodeFormat(None, "3"))
        options.update(kwargs)
        return self.media.get_media_info_on_files([str(self.path)], **options)[str(self.path)]

    def test_numeric_title_accepts_manual_ep_and_preserves_original_filename(self):
        meta = self.recognize()
        self.assertIsNone(meta.skip_reason)
        self.assertEqual((2, [3]), (meta.begin_season, meta.get_episode_list()))
        self.assertEqual(self.filename, self.path.name)
        self.assertTrue(meta.note["fractional_episode"]["manual"])
        self.assertEqual("confirmed", meta.note["fractional_episode"]["status"])
        self.saved.assert_not_called()

    def test_manual_ep_recovers_explicit_source_season_if_not_overridden(self):
        meta = self.recognize(season=None)
        self.assertIsNone(meta.skip_reason)
        self.assertEqual(2, meta.begin_season)

    def test_selected_season_zero_and_episode_zero_are_valid(self):
        self.detail.return_value = dict(episodes=[dict(episode_number=0, season_number=0,
                                                       show_id=42, name="Special")])
        meta = self.recognize(EpisodeFormat(None, "0"), season=0)
        self.assertIsNone(meta.skip_reason)
        self.assertEqual((0, [0]), (meta.begin_season, meta.get_episode_list()))

    def test_manual_pattern_offset_and_fixed_range(self):
        for formatter, expected in (
                (EpisodeFormat("Cyberpunk.Edgerunners.S02E{ep}.021.{tail}", offset=1), [4]),
                (EpisodeFormat(None, "3-4"), [3, 4])):
            with self.subTest(expected=expected):
                meta = self.recognize(formatter)
                self.assertIsNone(meta.skip_reason)
                self.assertEqual(expected, meta.get_episode_list())

    def test_empty_or_nonmatching_rules_do_not_bypass_automatic_protection(self):
        for formatter in (EpisodeFormat(None), EpisodeFormat("Other.{ep}.mkv"),
                          EpisodeFormat(None, offset=1)):
            with self.subTest(format=formatter.format, offset=formatter.offset):
                meta = self.recognize(formatter)
                self.assertTrue(meta.skip_reason)
                self.assertNotEqual("confirmed", meta.note["fractional_episode"]["status"])
        self.detail.assert_not_called()

    def test_manual_ep_overrides_real_decimals_and_configured_mapping(self):
        self.path = self.root / "Show.S02E13.5.mkv"
        self.path.write_bytes(b"decimal")
        with patch("app.media.media.Config") as config:
            config.return_value.get_config.return_value = dict(fractional_episode_mappings=[
                dict(tmdb_id=42, source_episode="13.5", target_season=0, target_episode=1)])
            meta = self.recognize()
        self.assertIsNone(meta.skip_reason)
        self.assertEqual((2, [3]), (meta.begin_season, meta.get_episode_list()))

    def test_missing_invalid_or_incomplete_targets_fail_closed(self):
        # Structurally invalid controls now fail before recognition/provider lookup.
        for value in ("-1", "4-3"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "集数"):
                EpisodeFormat(None, value)
        for value in ("9", "3-5"):
            with self.subTest(value=value):
                self.assertTrue(self.recognize(EpisodeFormat(None, value)).skip_reason)
        for episodes in ([], [dict(self.episodes[1], show_id=99)],
                         [dict(self.episodes[1], season_number=1)],
                         [dict(self.episodes[1], name="")],
                         [self.episodes[1], self.episodes[1]]):
            with self.subTest(episodes=episodes):
                self.detail.return_value = dict(episodes=episodes)
                self.assertTrue(self.recognize().skip_reason)
        self.detail.side_effect = RuntimeError("offline")
        self.assertIn("不可用", self.recognize().skip_reason)
        self.assertEqual(b"source media", self.path.read_bytes())

    def test_missing_season_is_not_guessed(self):
        self.path = self.root / "Show.E13.5.mkv"
        self.path.write_bytes(b"decimal")
        self.assertTrue(self.recognize(season=None).skip_reason)
        self.detail.assert_not_called()

    def test_name_search_also_applies_manual_target(self):
        self.enter(patch.object(self.media, "get_cache_info", return_value={}))
        self.enter(patch.object(self.media, "_Media__search_media_info", return_value=self.info))
        self.enter(patch.object(self.media, "_Media__insert_media_cache"))
        meta = self.recognize(tmdb_info=None)
        self.assertIsNone(meta.skip_reason)
        self.assertEqual((2, [3]), (meta.begin_season, meta.get_episode_list()))

    def test_ordinary_file_manual_controls_are_consistent_for_all_identity_paths(self):
        # Search, cache hit and explicit TMDB ID must all honor the same controls.
        self.path = self.root / "Show.S02E03-05.mkv"
        self.path.write_bytes(b"ordinary media")
        self.enter(patch.object(self.media, "_Media__search_media_info", return_value=self.info))
        self.enter(patch.object(self.media, "_Media__insert_media_cache"))
        self.enter(patch.object(self.media, "_Media__load_cached_identity", return_value=(self.info, False)))
        cache = self.enter(patch.object(self.media, "get_cache_info", return_value={}))
        for lookup in ("explicit", "search", "cache"):
            cache.return_value = {"id": 42} if lookup == "cache" else {}
            for formatter, season, expected in (
                    (EpisodeFormat(None, "4"), 0, (0, [4])),
                    (EpisodeFormat(None, "3-4"), 2, (2, [3, 4])),
                    (EpisodeFormat("Show.S02E{ep}.mkv", offset=1), None, (2, [4, 5, 6])),
                    (EpisodeFormat(None), 1, (1, [3, 4, 5])),
                    (EpisodeFormat(None), None, (2, [3, 4, 5]))):
                with self.subTest(lookup=lookup, season=season, expected=expected):
                    meta = self.recognize(formatter, season=season,
                                          tmdb_info=self.info if lookup == "explicit" else None)
                    self.assertIsNone(meta.skip_reason)
                    self.assertEqual(expected, (meta.begin_season, meta.get_episode_list()))
                    if len(expected[1]) == 1:
                        self.assertIsNone(meta.end_episode)

    def test_numeric_title_keeps_release_metadata_without_auto_confirming(self):
        for suffix in ("1080p.NF.WEB-DL", "720p.WEBRip", "2160p.NF.WEB-DL",
                       "NF.WEB-DL.1080p", "10bit.1080p.WEB-DL"):
            name = "Cyberpunk.Edgerunners.S02E03.021.%s.DDP5.1.H.264-VARYG.mkv" % suffix
            with self.subTest(suffix=suffix):
                cleaned, note = protect_fractional_episode(name)
                self.assertIn(suffix, cleaned)
                self.assertEqual("S02E03.021", note["raw"])
                meta = MetaInfo(name, use_llm=False)
                self.assertEqual(2, meta.begin_season)
                self.assertIsNone(meta.begin_episode)
                self.assertEqual("unconfirmed", meta.note["fractional_episode"]["status"])
                self.assertTrue(meta.resource_pix)
        meta = self.recognize()
        self.assertEqual("1080p", meta.resource_pix)
        self.assertEqual("WEB-DL", meta.resource_type)
        self.assertEqual(self.filename, meta.org_string)

    def test_manual_season_precedes_real_identity_validation_and_mapping(self):
        # Do not mock the identity validator: a bad source season must not veto
        # the valid manual target before the late override is reached.
        self.path = self.root / "Cyberpunk.Edgerunners.S09E03.mkv"
        self.path.write_bytes(b"ordinary media")
        self.enter(patch.object(self.media, "_Media__search_media_with_name", return_value=self.info))
        self.enter(patch.object(self.media, "_Media__search_media_by_alias_candidates", return_value={}))
        self.enter(patch.object(self.media, "get_tmdb_info", return_value=self.info))
        self.enter(patch.object(self.media, "_Media__insert_media_cache"))
        cache = self.enter(patch.object(self.media, "get_cache_info", return_value={}))
        config = self.enter(patch("app.media.media.Config"))
        # Automatic release numbering must not remap an explicit target a second time.
        config.return_value.get_config.return_value = {"episode_mappings": [
            dict(tmdb_id=42, source_season=2, source_begin=1, source_end=12,
                 target_season=1, offset=0)]}
        for lookup in ("search", "cache"):
            cache.return_value = {"id": 42, "type": MediaType.TV} if lookup == "cache" else {}
            with self.subTest(lookup=lookup):
                meta = self.recognize(EpisodeFormat(None, "4"), tmdb_info=None, media_type=MediaType.TV)
                self.assertEqual(42, meta.tmdb_id)
                self.assertIsNone(meta.skip_reason)
                self.assertEqual((2, [4]), (meta.begin_season, meta.get_episode_list()))
                self.assertNotIn("_manual_numbering", meta.note)

    def test_manual_lookup_keeps_candidate_type_and_season_guards(self):
        self.path = self.root / "Cyberpunk.Edgerunners.S09E03.mkv"
        self.path.write_bytes(b"ordinary media")
        self.enter(patch.object(self.media, "get_cache_info", return_value={}))
        candidate = self.enter(patch.object(self.media, "_Media__search_media_with_name"))
        detail = self.enter(patch.object(self.media, "get_tmdb_info"))
        self.enter(patch.object(self.media, "_Media__search_media_by_alias_candidates", return_value={}))
        self.enter(patch.object(self.media, "_Media__insert_media_cache"))
        for wrong in (dict(self.info, seasons=[dict(season_number=1)]),
                      dict(self.info, media_type=MediaType.MOVIE)):
            with self.subTest(wrong=wrong):
                candidate.return_value = detail.return_value = wrong
                meta = self.recognize(tmdb_info=None, media_type=MediaType.TV)
                self.assertFalse(meta.tmdb_id)
                self.assertNotIn("_manual_numbering", meta.note)

    def test_movie_does_not_receive_leftover_episode_controls(self):
        self.path = self.root / "Example.Movie.2020.mkv"
        self.path.write_bytes(b"movie")
        movie = dict(id=99, title="Example Movie", media_type=MediaType.MOVIE, release_date="2020-01-01")
        self.enter(patch.object(self.media, "get_cache_info", return_value={}))
        self.enter(patch.object(self.media, "_Media__search_media_info", return_value=movie))
        self.enter(patch.object(self.media, "_Media__insert_media_cache"))
        for info in (movie, None):
            with self.subTest(explicit_id=bool(info)):
                meta = self.recognize(tmdb_info=info, media_type=MediaType.MOVIE)
                self.assertEqual(MediaType.MOVIE, meta.type)
                self.assertIsNone(meta.begin_episode)

    def test_numeric_title_resource_boundary_accepts_brackets(self):
        # Brackets are release separators too, not part of a malformed EP label.
        for resource in ("1080p[H.264]", "720p(HEVC)", "2160p【H265】"):
            name = "Show.S02E03.021.%s.mkv" % resource
            with self.subTest(resource=resource):
                cleaned, _ = protect_fractional_episode(name)
                self.assertIn(resource, cleaned)
                self.assertIsNone(MetaInfo(name, use_llm=False).begin_episode)

    def test_numeric_title_review_checks_path_and_target_before_acceptance(self):
        self.enter(patch.object(self.media, "get_tmdb_season_episodes", return_value=self.episodes))
        info = dict(self.info, _file_episode_confirmation=dict(path=str(self.path), season=2, episode=3))
        meta = self.recognize(tmdb_info=info)
        self.assertIsNone(meta.skip_reason)
        self.assertEqual((2, [3]), (meta.begin_season, meta.get_episode_list()))
        self.assertEqual("confirmed", meta.note["fractional_episode"]["status"])
        self.assertEqual("1080p", meta.resource_pix)
        for change in (dict(path=str(self.root / "other.mkv")), dict(episode=9)):
            with self.subTest(change=change):
                changed = dict(info, _file_episode_confirmation=dict(info["_file_episode_confirmation"], **change))
                self.assertTrue(self.recognize(tmdb_info=changed).skip_reason)
        self.saved.assert_not_called()

    def test_numeric_title_review_service_completes_valid_selection(self):
        # Exercise the actual dialog service through recognition, not a canned transfer success.
        db, transfer = MagicMock(), MagicMock()
        db.get_unknown_path_by_id.return_value = [SimpleNamespace(PATH=str(self.path), DEST="", MODE=None)]
        self.enter(patch.object(self.media, "get_tmdb_infos", return_value=[self.info]))
        self.enter(patch.object(self.media, "get_tmdb_info", return_value=self.info))
        self.enter(patch.object(self.media, "get_tmdb_season_episodes", return_value=self.episodes))
        def recognize_transfer(**kwargs):
            result = self.media.get_media_info_on_files([kwargs["in_path"]], tmdb_info=kwargs["tmdb_info"],
                season=kwargs["season"], episode_format=kwargs["episode"][0])[kwargs["in_path"]]
            self.assertEqual((2, [3]), (result.begin_season, result.get_episode_list()))
            return not bool(result.skip_reason), result.skip_reason or ""
        transfer.transfer_media.side_effect = recognize_transfer
        service = SpecialConfirmation(db, self.media, transfer)
        preview = service.run(dict(flag="unidentification", id=1))
        result = service.run(dict(flag="unidentification", id=1, stage="confirm", confirmed=True,
            fingerprint=preview["fingerprint"], type="tv", tmdb_id=42, season=2, episode=3))
        self.assertEqual(0, result["retcode"])
        db.update_transfer_unknown_state.assert_called_once_with(str(self.path))

    def test_download_constraints_check_every_manual_target(self):
        context = dict(tmdb_info=self.info, seasons=[2], episodes=[3], numbering="tmdb")
        self.assertIsNone(self.recognize(download_context=context).skip_reason)
        self.assertTrue(self.recognize(EpisodeFormat(None, "3-4"),
                                       download_context=context).skip_reason)
        self.assertTrue(self.recognize(download_context=dict(context, seasons=[1])).skip_reason)

    def test_scoped_review_confirmation_cannot_be_reused_for_another_file(self):
        self.path = self.root / "Show.S02E13.5.mkv"
        self.path.write_bytes(b"decimal")
        info = dict(self.info, _file_episode_confirmation=dict(
            path=str(self.root / "other.mkv"), season=2, episode=3))
        self.assertTrue(self.recognize(tmdb_info=info).skip_reason)
        self.detail.assert_not_called()

    def test_batch_validates_each_target_and_shares_only_tmdb_details(self):
        paths = [self.root / ("Show.S02E%s.021.mkv" % number) for number in ("03", "09")]
        for path in paths:
            path.write_bytes(b"source")
        result = self.media.get_media_info_on_files([str(path) for path in paths],
            tmdb_info=self.info, season=2, episode_format=EpisodeFormat("Show.S02E{ep}.021.mkv"))
        self.assertIsNone(result[str(paths[0])].skip_reason)
        self.assertEqual([3], result[str(paths[0])].get_episode_list())
        self.assertTrue(result[str(paths[1])].skip_reason)
        self.detail.assert_called_once()

    def test_directory_scan_respects_fixed_target_even_with_stale_pattern(self):
        # The same fixed EP that split_episode returns must survive directory filtering.
        for value in ("3", "3-4"):
            formatter = EpisodeFormat("Other.{ep}.mkv", value)
            self.assertEqual([str(self.path)], PathUtils.get_dir_files(
                str(self.root), exts=[".mkv"], episode_format=formatter))
        self.assertEqual([], PathUtils.get_dir_files(str(self.root), exts=[".mkv"],
            episode_format=EpisodeFormat("Other.{ep}.mkv", "3,4")))

    def test_web_manual_transfer_preserves_season_without_explicit_tmdb_id(self):
        with patch("web.action.FileTransfer") as transfer:
            transfer.return_value.transfer_media.return_value = (True, "")
            WebAction._WebAction__manual_transfer(str(self.path), RmtMode.LINK,
                media_type=MediaType.TV, season=0, episode_details="3")
            self.assertEqual(0, transfer.return_value.transfer_media.call_args.kwargs["season"])

    def test_manual_metadata_keeps_existing_and_batch_collision_guards(self):
        transfer = FileTransfer.__new__(FileTransfer)
        meta = self.recognize()
        target = self.root / "existing.mkv"
        target.write_bytes(b"existing media")
        with patch.object(transfer, "_FileTransfer__is_media_exists",
                          return_value=(True, str(self.root), True, str(target))):
            transfer._check_fractional_destinations({str(self.path): meta}, str(self.root), RmtMode.LINK)
        self.assertTrue(meta.skip_reason)
        self.assertEqual(b"existing media", target.read_bytes())

        meta = self.recognize()
        other = self.root / "Show.S02E03.mkv"
        other.write_bytes(b"other source")
        normal = MetaInfo(other.name, use_llm=False)
        normal.set_tmdb_info(self.info)
        with patch.object(transfer, "_FileTransfer__is_media_exists",
                          return_value=(False, str(self.root), False, str(target.with_suffix("")))):
            transfer._check_fractional_destinations(
                {str(self.path): meta, str(other): normal}, str(self.root), RmtMode.LINK)
        self.assertTrue(meta.skip_reason)
        self.assertIsNone(normal.skip_reason)

    def test_manual_transfer_publishes_link_but_never_overwrites_existing_file(self):
        # Exercise actual filesystem publication, including force/size-cover settings.
        transfer = FileTransfer.__new__(FileTransfer)
        transfer.media = self.media
        transfer.dbhelper, transfer.progress, transfer.message = MagicMock(), MagicMock(), MagicMock()
        transfer.threadhelper = MagicMock()
        transfer._filesize_cover = True
        transfer._scraper_flag = transfer._refresh_mediaserver = False
        transfer._tv_category_flag = transfer._anime_category_flag = transfer._movie_category_flag = False
        transfer._tv_dir_rmt_format = "{title} ({year})"
        transfer._tv_season_rmt_format = "Season {season}"
        transfer._tv_file_rmt_format = "{title} - {season_episode}"
        self.enter(patch("app.filetransfer.Subtitle"))
        self.enter(patch.object(Media, "get_tmdb_en_title", return_value="Cyberpunk Edgerunners"))
        self.enter(patch.object(self.media, "get_episode_title", return_value="021"))
        self.enter(patch.object(self.media, "get_tmdb_info", return_value=self.info))
        self.enter(patch.object(transfer, "_existing_media_files", return_value=[]))
        self.enter(patch.object(transfer, "_FileTransfer__transfer_subtitles", return_value=0))
        self.enter(patch.object(transfer, "check_ignore", side_effect=lambda file_list: (file_list, "")))
        library = self.root / "library"
        library.mkdir()
        arguments = dict(in_from=SyncType.MAN, in_path=str(self.root), target_dir=str(library),
            rmt_mode=RmtMode.LINK, tmdb_info=self.info, season=2, udf_flag=True,
            episode=(EpisodeFormat("Other.{ep}.mkv", "3"), False), min_filesize=0)
        self.assertTrue(transfer.transfer_media(**arguments)[0])
        published = list(library.rglob("*.mkv"))
        self.assertEqual(1, len(published))
        self.assertTrue(os.path.samefile(self.path, published[0]))
        # Replacing the destination creates a different inode and must block even force mode.
        published[0].unlink()
        published[0].write_bytes(b"existing media")
        arguments["in_path"] = str(self.path)
        self.assertFalse(transfer.transfer_media(**arguments)[0])
        self.assertEqual(b"existing media", published[0].read_bytes())
        self.assertEqual(b"source media", self.path.read_bytes())
