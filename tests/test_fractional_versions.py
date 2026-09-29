"""Decimal release evidence and editorial-cut naming regressions (offline TMDB)."""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app.media.meta import MetaInfo
from app.media.meta.fractional import episode_key, release_references
from app.media.meta.llm_parser import LLMMetaParser
from app.media.media import Media
from app.filetransfer import FileTransfer
from app.downloader.downloader import Downloader
from app.utils.types import MediaType, RmtMode, SyncType, DownloaderType
from app.media.tmdbv3api.as_obj import AsObj


class FractionalWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.media = Media.__new__(Media)
        self.media.tmdb = object()
        self.info = {"id": 1429, "name": "进击的巨人", "media_type": MediaType.TV,
                     "seasons": [{"season_number": 0}, {"season_number": 1}]}
        # Captured public TMDB numbering, not a default per-work production rule.
        self.records = [("13.5", 1, "从那天起", "第一季13.5，总集篇。"),
                        ("3.5", 7, "伊尔泽的笔记", "#3.5收录于单行本第12卷限定版。"),
                        ("3.25", 13, "突然的造访者", "#3.25收录于单行本第13卷限定版。"),
                        ("3.75", 14, "困难", "#3.75收录于单行本第14卷限定版。"),
                        ("0.5A", 15, "无悔的选择 上", "#0.5A收录于单行本第15卷限定版。"),
                        ("0.5B", 16, "无悔的选择 下", "#0.5B收录于单行本第16卷限定版。")]

    def detail(self, tmdbid, season):
        return {"episodes": [{"id": 100 + number, "episode_number": number,
                "season_number": 0, "show_id": 1429, "name": title, "overview": overview}
                for _, number, title, overview in self.records]} if season == 0 else {"episodes": []}

    def test_parse_boundaries_and_keys(self):
        for name, expected in [("Show S01E13.5.1080p.mkv", "13.5"),
                               ("Show EP1.5.WEB-DL.mkv", "1.5"),
                               ("Show 第6.5集.mkv", "6.5"), ("Show 第6.5話.mkv", "6.5"),
                               ("Show [0.5a].mkv", "0.5A"), ("Show - 1.5.mkv", "1.5"),
                               ("Show S01EP00.5B.mkv", "0.5B")]:
            with self.subTest(name=name):
                meta = MetaInfo(name, use_llm=False)
                self.assertEqual(expected, meta.note["fractional_episode"]["key"])
                self.assertIsNone(meta.begin_episode)
        self.assertNotEqual(episode_key("0.5A"), episode_key("0.5B"))
        self.assertEqual("1.50", episode_key("001.50"))
        for name in ("Show [5.1].mkv", "Show [7.1].mkv", "Show AAC5.1.mkv",
                     "Show [1920.1080].mkv", "Show - 1920.1080.mkv",
                     "Show 1920.1080.mkv", "Show S01E01.1080p.WEB.mkv",
                     "Show S01E01.2022.1080p.mkv"):
            self.assertNotIn("fractional_episode", MetaInfo(name, use_llm=False).note)
        malformed = MetaInfo("Show E01.123.mkv", use_llm=False)
        self.assertIsNone(malformed.begin_episode)
        self.assertIsNone(malformed.note["fractional_episode"]["key"])
        for name in ("Show [1.123].mkv", "Show 第1.123集.mkv", "Show E0.5AB.mkv"):
            self.assertIsNone(MetaInfo(name, use_llm=False).begin_episode)

    def test_integer_metadata_and_date_context(self):
        # Resource tokens must preserve the integer episode, not enter confirmation.
        for token in ("8bit", "10bit", "12bit", "4k", "1080p"):
            meta = MetaInfo("Show.S01E01.%s.mkv" % token, use_llm=False)
            self.assertNotIn("fractional_episode", meta.note)
            self.assertEqual((1, 1), (meta.begin_season, meta.begin_episode))
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "Show - 2022.08.01.mkv")
            open(path, "wb").close()
            meta = self.media.get_media_info_on_files([path], download_context={
                "tmdb_info": self.info, "seasons": [1], "episodes": [8]})[path]
            self.assertIsNone(meta.skip_reason)
            self.assertEqual((1, 8), (meta.begin_season, meta.begin_episode))

    def test_ambiguous_resolution_range(self):
        for ending in ("480", "720", "1080", "2160", "1080p"):
            meta = MetaInfo("Show S01E01-%s.mkv" % ending, use_llm=False)
            self.assertEqual([1], meta.get_episode_list())
        for ending in ("03", "E03"):
            meta = MetaInfo("Show S01E01-%s.mkv" % ending, use_llm=False)
            self.assertEqual([1, 2, 3], meta.get_episode_list())

    def test_new_season_pack_scope_allows_only_matching_specials(self):
        downloader = object.__new__(type(Downloader()))
        downloader.dbhelper = MagicMock()
        release = MetaInfo("Show S01", use_llm=False)
        release.set_tmdb_info(self.info)
        downloader._create_download_context(release, DownloaderType.QB)
        context = downloader.dbhelper.save_download_context.call_args.args[2]
        self.assertEqual("season_pack", context["scope"])
        self.assertEqual([1], context["release_seasons"])
        context["tmdb_info"] = self.info
        with tempfile.TemporaryDirectory() as root, patch.object(
                self.media, "get_tmdb_tv_season_detail", side_effect=self.detail):
            path = os.path.join(root, "Show S01E13.5.mkv")
            open(path, "wb").close()
            meta = self.media.get_media_info_on_files([path], download_context=context)[path]
            self.assertIsNone(meta.skip_reason)
            self.assertEqual((0, 1), (meta.begin_season, meta.begin_episode))
            # A different release season, selected episodes, or legacy context stays strict.
            for changes in ({"release_seasons": [2]}, {"episodes": [1]}, {"scope": None}):
                meta = self.media.get_media_info_on_files(
                    [path], download_context=dict(context, **changes))[path]
                self.assertTrue(meta.skip_reason)
        single = MetaInfo("Show S01E01", use_llm=False)
        single.set_tmdb_info(self.info)
        downloader._create_download_context(single, DownloaderType.QB)
        self.assertNotIn("scope", downloader.dbhelper.save_download_context.call_args.args[2])

    def test_episode_title_does_not_replace_series_search_name(self):
        meta = MetaInfo("Show E13.5 - 从那天起 [1080p].mkv", use_llm=False)
        self.assertEqual("Show", meta.get_name())
        self.assertEqual("从那天起", meta.note["fractional_episode"]["episode_title"])
        self.assertEqual("1080p", meta.resource_pix)

    def test_aot_all_labels_and_batch_cache(self):
        cache = {}
        with patch("app.media.media.Config") as config, \
                patch.object(self.media, "get_tmdb_tv_season_detail", side_effect=self.detail) as detail:
            config.return_value.get_config.return_value = {}
            for raw, expected, _, _ in self.records:
                meta = MetaInfo("Show S01E%s.mkv" % raw, use_llm=False)
                self.assertTrue(self.media._confirm_fractional_episode(meta, self.info, cache))
                self.assertEqual((0, expected), (meta.begin_season, meta.begin_episode))
            self.assertEqual(2, detail.call_count)

    def test_reference_boundaries_and_conflicting_evidence(self):
        self.assertEqual([], release_references("评分 8.5 时长 13.5 分钟 2013.07.07"))
        self.assertEqual("0.5A", release_references("#0.5A收录")[0][0])
        with patch("app.media.media.Config") as config, \
                patch.object(self.media, "get_tmdb_tv_season_detail", side_effect=self.detail):
            config.return_value.get_config.return_value = {}
            meta = MetaInfo("Show S02E13.5.mkv", use_llm=False)
            self.assertFalse(self.media._confirm_fractional_episode(meta, self.info))
            meta = MetaInfo("Show E3.5 - 从那天起.mkv", use_llm=False)
            self.assertFalse(self.media._confirm_fractional_episode(meta, self.info))
            self.records.append(("13.5", 25, "重复候选", "第13.5集"))
            self.assertFalse(self.media._confirm_fractional_episode(
                MetaInfo("Show E13.5.mkv", use_llm=False), self.info))

    def test_query_failure_retries_next_batch(self):
        with patch("app.media.media.Config") as config, \
                patch.object(self.media, "get_tmdb_tv_season_detail") as detail:
            config.return_value.get_config.return_value = {}
            detail.side_effect = RuntimeError("offline")
            cache = {}
            for _ in range(2):
                self.assertFalse(self.media._confirm_fractional_episode(
                    MetaInfo("Show E13.5.mkv", use_llm=False), self.info, cache))
            self.assertEqual(1, detail.call_count)
            detail.side_effect = self.detail
            self.assertTrue(self.media._confirm_fractional_episode(
                MetaInfo("Show E13.5.mkv", use_llm=False), self.info, {}))

    def test_normalized_config_duplicates_fail_closed(self):
        rule = {"tmdb_id": 1429, "source_episode": "013.5", "target_season": 0,
                "target_episode": 1, "episode_title": "从那天起"}
        with patch("app.media.media.Config") as config, \
                patch.object(self.media, "get_tmdb_season_episodes") as query:
            config.return_value.get_config.return_value = {"fractional_episode_mappings": [
                rule, dict(rule, source_episode="13.5")]}
            self.assertFalse(self.media._confirm_fractional_episode(
                MetaInfo("Show E13.5.mkv", use_llm=False), self.info))
            query.assert_not_called()

    def test_sdk_objects_and_partial_season_data(self):
        with patch("app.media.media.Config") as config, \
                patch.object(self.media, "get_tmdb_tv_season_detail", side_effect=lambda **kw: AsObj(**self.detail(**kw))):
            config.return_value.get_config.return_value = {}
            self.assertTrue(self.media._confirm_fractional_episode(
                MetaInfo("Show E13.5.mkv", use_llm=False), AsObj(**self.info)))
            partial = dict(self.info, seasons=[{"season_number": 0, "episode_count": 7},
                                               {"season_number": 1}])
            self.assertFalse(self.media._confirm_fractional_episode(
                MetaInfo("Show E13.5.mkv", use_llm=False), partial))

    def test_only_same_task_can_reuse_identity(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.object(self.media, "save_rename_cache"), \
                patch.object(self.media, "get_tmdb_tv_season_detail", side_effect=self.detail), \
                patch.object(self.media, "get_cache_info", return_value={}), \
                patch.object(self.media, "_Media__search_media_with_name", return_value=None):
            normal, special = [os.path.join(root, n) for n in ("Show S01E01.mkv", "Show E13.5.mkv")]
            for path in (normal, special):
                open(path, "wb").close()
            contexts = {normal: {"task_key": "qb:one", "tmdb_info": self.info},
                        special: {"task_key": "qb:one"}}
            result = self.media.get_media_info_on_files([special, normal], download_contexts=contexts)
            self.assertEqual(1429, result[special].tmdb_id)
            # 同目录而任务不同，不能偷用已确认的作品身份。
            contexts[special] = {"task_key": "qb:two"}
            result = self.media.get_media_info_on_files([special, normal], download_contexts=contexts)
            self.assertTrue(result[special].skip_reason)
            self.assertFalse(result[special].tmdb_id)

    def test_integer_first_and_unconfirmed_retained(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.object(self.media, "save_rename_cache"), \
                patch.object(self.media, "get_tmdb_tv_season_detail", side_effect=self.detail), \
                patch("app.media.media.MetaInfo", wraps=MetaInfo) as parser:
            paths = [os.path.join(root, name) for name in
                     ("Show E13.5.mkv", "Show E99.5.mkv", "Show S01E01.mkv")]
            for path in paths:
                open(path, "wb").close()
            result = self.media.get_media_info_on_files(paths, tmdb_info=self.info)
            self.assertEqual("Show S01E01.mkv", parser.call_args_list[0].kwargs["title"])
            self.assertEqual(set(paths), set(result))
            self.assertIsNone(result[paths[2]].skip_reason)
            self.assertEqual(0, result[paths[0]].begin_season)
            self.assertTrue(result[paths[1]].skip_reason)

    def test_context_numbering_and_task_conflicts(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.object(self.media, "get_tmdb_tv_season_detail", side_effect=self.detail):
            path = os.path.join(root, "Show S01E13.5.mkv")
            open(path, "wb").close()
            for numbering, skipped in (("release", False), ("tmdb", True), ("unknown", True)):
                meta = self.media.get_media_info_on_files([path], download_context={
                    "tmdb_info": self.info, "seasons": [1], "numbering": numbering})[path]
                self.assertEqual(skipped, bool(meta.skip_reason))
                self.assertEqual((0, 1), (meta.begin_season, meta.begin_episode))
            other = dict(self.info, id=999)
            result = self.media.get_media_info_on_files([path], tmdb_info=other,
                download_context={"tmdb_info": self.info})
            self.assertIn("身份冲突", result[path].skip_reason)
            normal = os.path.join(root, "Show S01E01.mkv")
            open(normal, "wb").close()
            contexts = {normal: {"task_key": "qb:same", "tmdb_info": other},
                        path: {"task_key": "qb:same", "tmdb_info": self.info}}
            result = self.media.get_media_info_on_files([path, normal], download_contexts=contexts)
            self.assertIn("冲突作品身份", result[path].skip_reason)

    def test_destination_collision_and_same_inode_retry(self):
        transfer = FileTransfer.__new__(FileTransfer)
        with tempfile.TemporaryDirectory() as root:
            paths = [os.path.join(root, n) for n in ("a.mkv", "b.mkv")]
            for path in paths:
                open(path, "wb").close()
            metas = {}
            for path, raw in zip(paths, ("13.5", "3.5")):
                meta = MetaInfo("Show E%s.mkv" % raw, use_llm=False)
                meta.note["fractional_episode"]["status"] = "confirmed"
                meta.set_tmdb_info(self.info)
                metas[path] = meta
            target = os.path.join(root, "Show S00E01")
            with patch.object(transfer, "_FileTransfer__is_media_exists", return_value=(True, root, False, target)):
                transfer._check_fractional_destinations(metas, root, RmtMode.LINK)
                self.assertTrue(all(m.skip_reason for m in metas.values()))
            metas[paths[0]].skip_reason = None
            os.link(paths[0], target + ".mkv")
            with patch.object(transfer, "_FileTransfer__is_media_exists", return_value=(True, root, True, target + ".mkv")):
                transfer._check_fractional_destinations({paths[0]: metas[paths[0]]}, root, RmtMode.LINK)
                self.assertIsNone(metas[paths[0]].skip_reason)

    def test_preflight_disappearing_source_does_not_abort_batch(self):
        transfer = FileTransfer.__new__(FileTransfer)
        with tempfile.TemporaryDirectory() as root:
            paths = [os.path.join(root, name) for name in ("a.mkv", "b.mkv", "safe.mkv")]
            metas = {}
            for path in paths:
                open(path, "wb").close()
                meta = MetaInfo("Show E13.5.mkv", use_llm=False)
                meta.set_tmdb_info(self.info)
                meta.note["fractional_episode"]["status"] = "confirmed"
                metas[path] = meta
            def destination(_, meta):
                if meta is metas[paths[1]]:
                    os.unlink(paths[0])
                name = "safe" if meta is metas[paths[2]] else "collision"
                return False, root, False, os.path.join(root, name)
            with patch.object(transfer, "_FileTransfer__is_media_exists", side_effect=destination):
                transfer._check_fractional_destinations(metas, root, RmtMode.LINK)
            self.assertTrue(metas[paths[0]].skip_reason)
            self.assertTrue(metas[paths[1]].skip_reason)
            self.assertIsNone(metas[paths[2]].skip_reason)

    def test_mixed_batch_publishes_valid_hardlinks_and_preserves_skipped_source(self):
        transfer = FileTransfer.__new__(FileTransfer)
        transfer.media, transfer.dbhelper = MagicMock(), MagicMock()
        transfer.progress, transfer.message, transfer.threadhelper = MagicMock(), MagicMock(), MagicMock()
        transfer._filesize_cover = transfer._scraper_flag = transfer._refresh_mediaserver = False
        transfer._tv_category_flag = transfer._anime_category_flag = transfer._movie_category_flag = False
        transfer._tv_dir_rmt_format = "{title} ({year})"
        transfer._tv_season_rmt_format = "Season {season}"
        transfer._tv_file_rmt_format = "{title} - {season_episode}"
        info = dict(self.info, first_air_date="2013-04-07")
        with tempfile.TemporaryDirectory() as root, \
                patch("app.filetransfer.Subtitle"), \
                patch.object(Media, "get_tmdb_en_title", return_value="Attack on Titan"), \
                patch.object(transfer, "_existing_media_files", return_value=[]), \
                patch.object(transfer, "_FileTransfer__transfer_subtitles", return_value=0):
            target = os.path.join(root, "library")
            os.mkdir(target)
            medias = {}
            for name in ("Show S01E01.mkv", "Show E13.5.mkv", "Show E99.5.mkv"):
                path = os.path.join(root, name)
                with open(path, "wb") as stream:
                    stream.write(name.encode())
                meta = MetaInfo(name, use_llm=False)
                meta.set_tmdb_info(info)
                if "13.5" in name:
                    meta.begin_season, meta.begin_episode = 0, 1
                    meta.note["fractional_episode"]["status"] = "confirmed"
                if "99.5" in name:
                    meta.skip_reason = "没有对应单集"
                medias[path] = meta
            transfer.media.get_media_info_on_files.return_value = medias
            transfer.media.get_tmdb_info.return_value = info
            transfer.media.get_episode_title.return_value = None
            transfer.check_ignore = lambda file_list: (file_list, "")
            success, _ = transfer.transfer_media(
                in_from=SyncType.MAN, in_path=root, files=list(medias),
                target_dir=target, rmt_mode=RmtMode.LINK)
            self.assertFalse(success)
            for season, name in ((1, "Show S01E01.mkv"), (0, "Show E13.5.mkv")):
                dest = os.path.join(target, "进击的巨人 (2013)", "Season %d" % season,
                                    "进击的巨人 - S%02dE01.mkv" % season)
                self.assertTrue(os.path.samefile(os.path.join(root, name), dest))
            self.assertTrue(os.path.isfile(os.path.join(root, "Show E99.5.mkv")))
            recorded = [call.args[0] for call in transfer.dbhelper.insert_transfer_blacklist.call_args_list]
            self.assertNotIn(os.path.join(root, "Show E99.5.mkv"), recorded)
            self.assertEqual(2, len(recorded))

            special = next(path for path in medias if "13.5" in path)
            transfer.media.get_media_info_on_files.return_value = {special: medias[special]}
            existing = os.path.join(target, "进击的巨人 (2013)", "Season 0", "进击的巨人 - S00E01.mkv")
            second_target = os.path.join(root, "second-library")
            os.mkdir(second_target)
            transfer._existing_media_files.return_value = [(existing, "S00E01")]
            success, _ = transfer.transfer_media(
                in_from=SyncType.MON, in_path=special, files=[special],
                target_dir=second_target, rmt_mode=RmtMode.LINK)
            self.assertTrue(success)
            self.assertEqual([], os.listdir(second_target))
            # No history yet: the same inode in the target must still reach history writing.
            transfer._existing_media_files.return_value = []
            transfer.dbhelper.insert_transfer_blacklist.reset_mock()
            success, _ = transfer.transfer_media(
                in_from=SyncType.MON, in_path=special, files=[special],
                target_dir=target, rmt_mode=RmtMode.LINK)
            self.assertTrue(success)
            transfer.dbhelper.insert_transfer_blacklist.assert_called_once_with(special)


class ReleaseCutTest(unittest.TestCase):
    def test_explicit_cut_before_year(self):
        # 用户提供的真实发布名：剪辑版在年份前，而非年份后的技术区域。
        filename = "Rambo.Extended.Cut.2008.BluRay.1080p.DTS.2Audio.x264-DreamHD.mkv"
        meta = MetaInfo(filename, use_llm=False)
        self.assertEqual("Rambo", meta.get_name())
        self.assertEqual("2008", meta.year)
        self.assertEqual("Extended", meta.cut)
        self.assertEqual("BluRay", meta.get_edtion_string())
        self.assertEqual("1080p", meta.resource_pix)
        self.assertEqual("X264", meta.video_encode)
        self.assertEqual("DTS-2Audio", meta.audio_encode)
        self.assertEqual(filename, meta.org_string)
        for title, cut in (("Rambo_Extended_Cut_2008_BluRay.mkv", "Extended"),
                           ("Rambo Directors Cut 2008 1080p.mkv", "Directors Cut"),
                           ("兰博 导演剪辑版 2008 BluRay.mkv", "Directors Cut")):
            self.assertEqual(cut, MetaInfo(title, use_llm=False).cut)
        # 标记缺少作品名前缀，或属于普通片名，不可直接删除。
        for title in ("The Directors Cut 2024 1080p.mkv", "Extended Cut 2024 BluRay.mkv",
                      "Uncut Gems 2019 BluRay.mkv", "Rambo Extended Story 2008 BluRay.mkv"):
            self.assertIsNone(MetaInfo(title, use_llm=False).cut)

    def test_cut_is_independent_and_stable(self):
        for text, cut in (("Extended", "Extended"), ("Director's Cut", "Directors Cut"),
                          ("院线版", "Theatrical"), ("未删减版", "Uncut"),
                          ("Unrated Extended Extended Cut", "Extended Unrated")):
            meta = MetaInfo("Movie 2024 %s BluRay 1080p HEVC.mkv" % text, use_llm=False)
            self.assertEqual(cut, meta.cut)
            self.assertEqual("BluRay", meta.get_edtion_string())
        self.assertIsNone(MetaInfo("The Directors Cut 2024 1080p.mkv", use_llm=False).cut)
        self.assertEqual("Extended", MetaInfo("Movie [Extended] 1080p.mkv", use_llm=False).cut)
        self.assertIsNone(MetaInfo("Movie 2024 1080p.mkv", use_llm=False).cut)

    def test_hdr10_plus_and_llm_cut_leak(self):
        meta = MetaInfo("Movie 2024 WEB-DL 2160p HDR10+ HEVC.mkv", use_llm=False)
        self.assertEqual("WEB-DL HDR10+", meta.get_edtion_string())
        result = LLMMetaParser()._LLMMetaParser__normalize_result({
            "resource_type": "BluRay Extended", "resource_effect": "DV Directors Cut",
            "video_encode": "HEVC Uncut", "cut": "Theatrical"})
        self.assertEqual("BluRay", result["resource_type"])
        self.assertEqual("DV", result["resource_effect"])
        self.assertEqual("HEVC", result["video_encode"])
        self.assertNotIn("cut", result)

    def test_real_template_uses_cut_and_preserves_old_output(self):
        transfer = FileTransfer.__new__(FileTransfer)
        transfer.media = Media.__new__(Media)
        transfer._movie_dir_rmt_format = "{title} ({year})"
        transfer._movie_file_rmt_format = "{title} ({year}) - {videoFormat} {edition} {cut}"
        with patch.object(Media, "get_episode_title", return_value=None), \
                patch.object(Media, "get_tmdb_en_title", return_value="Movie"):
            names = []
            for cut in ("Extended", "Theatrical", ""):
                meta = MetaInfo("Movie 2024 %s BluRay 1080p.mkv" % cut, use_llm=False)
                meta.title = "Movie"
                folder, name = transfer.get_moive_dest_path(meta)
                self.assertTrue(name.startswith(folder + " - "))
                self.assertNotIn("None", name)
                names.append(name)
            self.assertEqual(3, len(set(names)))
            self.assertEqual("Movie (2024) - 1080p BluRay", names[-1])
