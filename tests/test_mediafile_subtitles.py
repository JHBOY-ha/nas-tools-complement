"""Exercise file-manager download, real subtitle publication and targeted refresh offline."""
import ast
import errno
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from threading import Barrier
from unittest import TestCase
from unittest.mock import Mock, patch

from tests import test_opensubtitles_api as api_tests


ROOT = Path(__file__).resolve().parents[1]
SRT = "1\n00:00:01,000 --> 00:00:02,000\n这是测试字幕\n"


class RmtMode(Enum):
    LINK = "硬链接"
    SOFTLINK = "软链接"
    COPY = "复制"


class MediaFileSubtitlesTest(TestCase):
    @classmethod
    def setUpClass(cls):
        api_tests.OpenSubtitlesApiTest.setUpClass()
        cls.subtitle_module = api_tests.OpenSubtitlesApiTest.subtitle_module
        cls.health = cls.subtitle_module.SubtitleHealth
        # Load the service with its real subtitle implementation; only config,
        # database lookup and media-server I/O are replaced by test doubles.
        stubs = {
            "app.conf": types.SimpleNamespace(ModuleConf=types.SimpleNamespace(
                get_enum_item=lambda enum, value: next((item for item in enum if item.value == value), None))),
            "app.helper": types.SimpleNamespace(DbHelper=Mock()),
            "app.helper.subtitle_health": types.SimpleNamespace(SubtitleHealth=cls.health),
            "app.subtitle": cls.subtitle_module,
            "app.utils.types": types.SimpleNamespace(RmtMode=RmtMode),
            "config": types.SimpleNamespace(Config=Mock(), RMT_MEDIAEXT=[".mkv"]),
        }
        with patch.dict(sys.modules, stubs):
            spec = importlib.util.spec_from_file_location(
                "_mediafile_subtitles_under_test", ROOT / "app/helper/mediafile_subtitles.py")
            cls.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.module)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        downloads = self.root / "downloads"
        self.library = self.root / "link"
        downloads.mkdir()
        self.library.mkdir()
        self.source = downloads / "Movie.English.Release.2024.mkv"
        self.source.write_bytes(b"test video")
        self.target = self.library / "Movie (2024).mkv"
        os.link(self.source, self.target)
        self.item = {"file": str(self.source.with_suffix("")), "file_ext": ".mkv", "name": "Movie"}
        self.source_subtitle = self.source.with_suffix(".chi.zh-cn.srt")
        self.history = types.SimpleNamespace(
            DEST_PATH=str(self.library), DEST_FILENAME=self.target.name, MODE=RmtMode.LINK.value)
        self.db = Mock()
        self.db.get_latest_transfer_history_by_source_full_path.return_value = self.history
        self.db.get_config_sync_paths.return_value = []
        self.config = {"movie_path": str(self.library), "media_server": "jellyfin"}
        self.subtitle = object.__new__(self.subtitle_module.Subtitle)
        self.subtitle._server = "opensubtitles"
        self.subtitle._opensubtitles_enable = True
        self.subtitle.opensubtitles = Mock(languages=["zh-cn", "zh-tw"])
        self.subtitle.opensubtitles.is_configured.return_value = (True, "")
        self.subtitle.opensubtitles.search_subtitles.return_value = (
            [{"file_id": 42, "language": "zh-cn", "high_confidence": True}], "")
        self.subtitle.opensubtitles.download.return_value = ({"link": "https://example.test/sub"}, "")
        self.refresh = Mock(return_value={"status": "refreshed", "scope": "item"})
        for patcher in (
                patch.object(self.module, "Subtitle", return_value=self.subtitle),
                patch.object(self.module, "DbHelper", return_value=self.db),
                patch.object(self.module, "Config", return_value=types.SimpleNamespace(get_config=lambda _: self.config)),
                patch.object(self.subtitle_module, "RmtMode", RmtMode),
                patch.object(self.subtitle, "_Subtitle__fetch_temporary_subtitle", return_value=(SRT, "")),
                patch.dict(sys.modules, {"app.helper.subtitle_task_processors": types.SimpleNamespace(
                    _localized_refresh=self.refresh)})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = self.module.MediaFileSubtitles()

    def run_download(self, **kwargs):
        return self.service.download(self.item, **kwargs)

    def expected_subtitle(self):
        suffix = ".chi.zh-cn.srt" if self.config["media_server"] == "jellyfin" else ".zh-CN.srt"
        return self.target.with_suffix(suffix)

    def test_download_hardlinks_to_recorded_name_then_refreshes_that_video(self):
        def refresh(_payload, media_path, server):
            self.assertEqual(media_path, str(self.target))
            self.assertEqual(server, "jellyfin")
            self.assertEqual(self.expected_subtitle().read_text(), SRT)
            return {"status": "refreshed", "scope": "item"}

        self.refresh.side_effect = refresh
        success, message = self.run_download()
        self.assertTrue(success, message)
        self.assertEqual(self.source_subtitle.read_text(), SRT)
        self.assertTrue(os.path.samefile(self.source_subtitle, self.expected_subtitle()))
        self.subtitle.opensubtitles.download.assert_called_once_with(42)
        self.db.get_latest_transfer_history_by_source_full_path.assert_called_once_with(str(self.source))
        self.refresh.assert_called_once()

    def test_existing_source_and_repeated_click_sync_without_consuming_quota(self):
        self.source_subtitle.write_text(SRT)
        self.subtitle.opensubtitles.is_configured.return_value = (False, "凭据已过期")
        for _ in range(2):
            success, message = self.run_download()
            self.assertTrue(success, message)
        self.subtitle.opensubtitles.download.assert_not_called()
        self.assertEqual(list(self.library.glob("*.srt")), [self.expected_subtitle()])
        self.assertEqual(self.refresh.call_count, 2)

    def test_preserves_different_subtitle_and_reuses_numbered_output(self):
        existing = self.expected_subtitle()
        existing.write_text(SRT.replace("测试", "已有"))
        for _ in range(2):
            success, message = self.run_download()
            self.assertTrue(success, message)
        self.assertIn("已有", existing.read_text())
        self.assertEqual(len(list(self.library.glob("*.srt"))), 2)
        self.assertEqual(self.target.with_suffix(".source-2.chi.zh-cn.srt").read_text(), SRT)
        self.subtitle.opensubtitles.download.assert_called_once_with(42)

    def test_refresh_failure_preserves_both_files_and_retry_only_refreshes(self):
        self.refresh.return_value = {"status": "failed", "message": "连接失败"}
        success, message = self.run_download()
        self.assertFalse(success)
        self.assertIn("刷新未完成", message)
        self.assertEqual(self.expected_subtitle().read_text(), SRT)
        self.refresh.return_value = {"status": "refreshed"}
        self.assertTrue(self.run_download()[0])
        self.subtitle.opensubtitles.download.assert_called_once_with(42)
        self.assertEqual(len(list(self.library.glob("*.srt"))), 1)

    def test_no_history_keeps_source_download_and_reports_no_sync(self):
        self.db.get_latest_transfer_history_by_source_full_path.return_value = None
        success, message = self.run_download()
        self.assertTrue(success, message)
        self.assertIn("仅保存在原目录", message)
        self.assertTrue(self.source_subtitle.is_file())
        self.assertFalse(list(self.library.glob("*.srt")))
        self.refresh.assert_not_called()

    def test_invalid_history_target_is_rejected_before_download(self):
        cases = [self.root / "outside.mkv", self.library / "missing.mkv", self.library / "other.txt"]
        for path in cases:
            with self.subTest(path=path):
                if path.name != "missing.mkv":
                    path.write_bytes(b"test video")
                self.history.DEST_PATH = str(path.parent)
                self.history.DEST_FILENAME = path.name
                success, _ = self.run_download()
                self.assertFalse(success)
        self.subtitle.opensubtitles.download.assert_not_called()
        self.refresh.assert_not_called()

    def test_recorded_directory_sync_destination_is_supported_without_global_media_root(self):
        self.config["movie_path"] = []
        self.db.get_config_sync_paths.return_value = [types.SimpleNamespace(DEST=str(self.library))]
        success, message = self.run_download()
        self.assertTrue(success, message)
        self.assertTrue(self.expected_subtitle().is_file())
        self.refresh.assert_called_once_with({}, str(self.target), "jellyfin")

    def test_broken_destination_subtitle_symlink_is_preserved_and_gets_collision_name(self):
        occupied = self.expected_subtitle()
        occupied.symlink_to(self.root / "missing.srt")
        success, message = self.run_download()
        self.assertTrue(success, message)
        self.assertTrue(occupied.is_symlink())
        self.assertEqual(self.target.with_suffix(".source-2.chi.zh-cn.srt").read_text(), SRT)

    def test_target_directory_symlink_escape_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / self.target.name).write_bytes(b"foreign video")
        linked_dir = self.library / "escaped"
        linked_dir.symlink_to(outside, target_is_directory=True)
        self.history.DEST_PATH = str(linked_dir)
        self.assertFalse(self.run_download()[0])
        self.subtitle.opensubtitles.download.assert_not_called()
        self.assertFalse(list(outside.glob("*.srt")))

    def test_media_replacement_during_download_stops_publication_and_refresh(self):
        def replace_video(_link):
            self.target.unlink()
            self.target.write_bytes(b"different video")
            return SRT, ""

        self.subtitle._Subtitle__fetch_temporary_subtitle.side_effect = replace_video
        success, message = self.run_download()
        self.assertFalse(success)
        self.assertIn("发生变化", message)
        self.assertTrue(self.source_subtitle.is_file())
        self.assertFalse(list(self.library.glob("*.srt")))
        self.refresh.assert_not_called()

    def test_candidates_are_preserved_until_user_selects_one(self):
        candidates = [{"file_id": 42, "language": "zh-cn", "high_confidence": False}]
        self.subtitle.opensubtitles.search_subtitles.return_value = (candidates, "")
        success, message = self.run_download()
        self.assertFalse(success)
        self.assertEqual(message["candidates"][0]["file_id"], 42)
        self.refresh.assert_not_called()
        self.subtitle.opensubtitles.download.assert_not_called()
        self.assertTrue(self.run_download(selected_file_id=42)[0])
        self.subtitle.opensubtitles.download.assert_called_once_with(42)

    def test_hardlink_cross_device_error_falls_back_to_atomic_copy(self):
        original_link = os.link

        def cross_device(source, target, **kwargs):
            if source == str(self.source_subtitle):
                raise OSError(errno.EXDEV, "cross-device link")
            return original_link(source, target, **kwargs)

        with patch.object(self.subtitle_module.os, "link", side_effect=cross_device):
            success, message = self.run_download()
        self.assertTrue(success, message)
        self.assertEqual(self.expected_subtitle().read_text(), SRT)
        self.assertFalse(os.path.samefile(self.source_subtitle, self.expected_subtitle()))

    def test_server_naming_and_softlinked_video_keep_subtitle_in_library(self):
        self.target.unlink()
        self.target.symlink_to(self.source)
        self.history.MODE = RmtMode.SOFTLINK.value
        for server in ("emby", "jellyfin", "plex"):
            with self.subTest(server=server):
                self.config["media_server"] = server
                success, message = self.run_download()
                self.assertTrue(success, message)
                target = self.expected_subtitle()
                self.assertEqual(target.read_text(), SRT)
                self.assertFalse(target.is_symlink())
                target.unlink()
        self.assertTrue(self.target.is_symlink())

    def test_chinesesubfinder_waits_for_valid_stable_file_before_sync(self):
        self.subtitle._server = "chinesesubfinder"
        stages = []

        def completed_poll(_seconds):
            stages.append(1)
            if len(stages) <= 2:
                self.source_subtitle.write_text(SRT if len(stages) > 1 else "partial download")

        with patch.object(self.subtitle, "download_subtitle", return_value=(True, "任务已添加")), \
                patch.object(self.module.time, "sleep", side_effect=completed_poll):
            success, message = self.run_download()
        self.assertTrue(success, message)
        self.assertGreaterEqual(len(stages), 3)
        self.assertEqual(self.expected_subtitle().read_text(), SRT)
        self.refresh.assert_called_once()

    def test_chinesesubfinder_timeout_does_not_pretend_job_ack_is_download_success(self):
        self.subtitle._server = "chinesesubfinder"
        self.service.WAIT_SECONDS = 0
        with patch.object(self.subtitle, "download_subtitle", return_value=(True, "任务已添加")):
            success, message = self.run_download()
        self.assertFalse(success)
        self.assertIn("尚未落盘", message)
        self.refresh.assert_not_called()

    def test_chinesesubfinder_existing_subtitle_does_not_enqueue_another_job(self):
        self.subtitle._server = "chinesesubfinder"
        self.subtitle.opensubtitles.languages = ["en"]
        self.source_subtitle.write_text(SRT)
        with patch.object(self.subtitle, "download_subtitle") as download:
            success, message = self.run_download()
        self.assertTrue(success, message)
        download.assert_not_called()

    def test_sync_failure_keeps_source_subtitle_and_does_not_refresh(self):
        self.source_subtitle.write_text(SRT)
        with patch.object(self.subtitle, "_Subtitle__publish_file_no_replace",
                          side_effect=PermissionError("目标目录只读")):
            self.history.MODE = RmtMode.COPY.value
            success, message = self.run_download()
        self.assertFalse(success)
        self.assertIn("同步失败", message)
        self.assertTrue(self.source_subtitle.is_file())
        self.assertFalse(list(self.library.glob("*.srt")))
        self.refresh.assert_not_called()

    def test_traditional_language_and_forced_flag_survive_library_renaming(self):
        self.source.with_suffix(".chi.zh-tw.forced.srt").write_text(SRT)
        success, message = self.run_download()
        self.assertTrue(success, message)
        self.assertTrue(self.target.with_suffix(".chi.zh-tw.forced.srt").is_file())
        self.assertFalse(self.expected_subtitle().exists())

    def test_concurrent_sync_requests_publish_one_complete_subtitle(self):
        self.source_subtitle.write_text(SRT)
        ready = Barrier(2)
        original_build = self.subtitle._Subtitle__build_subtitle_path

        def choose_target(*args, **kwargs):
            target = original_build(*args, **kwargs)
            ready.wait(timeout=5)
            return target

        with patch.object(self.subtitle, "_Subtitle__build_subtitle_path", side_effect=choose_target), \
                ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.run_download) for _ in range(2)]
            for future in futures:
                success, message = future.result(timeout=10)
                self.assertTrue(success, message)
        self.assertEqual(list(self.library.glob("*.srt")), [self.expected_subtitle()])
        self.assertEqual(self.expected_subtitle().read_text(), SRT)

    def test_upload_naming_still_preserves_identical_existing_subtitles(self):
        # Hash reuse is opt-in for download retries. Ordinary uploads must keep
        # their existing collision numbering, even for identical contents.
        expected_suffixes = {
            "emby": ".zh-CN(1).srt", "jellyfin": ".source-2.chi.zh-cn.srt",
            "plex": "(1).zh-CN.srt"
        }
        for server, suffix in expected_suffixes.items():
            with self.subTest(server=server):
                self.config["media_server"] = server
                existing = self.expected_subtitle()
                existing.write_text(SRT)
                chosen = self.subtitle._Subtitle__build_subtitle_path(
                    str(self.target), {"language": "zh-CN", "flags": []}, ".srt", server)
                self.assertEqual(chosen, str(self.target.with_suffix("")) + suffix)
                self.assertEqual(existing.read_text(), SRT)
                existing.unlink()

    def test_target_replacement_during_naming_is_rechecked_before_publication(self):
        self.source_subtitle.write_text(SRT)
        original_build = self.subtitle._Subtitle__build_subtitle_path

        def replace_target(*args, **kwargs):
            name = original_build(*args, **kwargs)
            self.target.unlink()
            self.target.write_bytes(b"replacement during naming")
            return name

        with patch.object(self.subtitle, "_Subtitle__build_subtitle_path", side_effect=replace_target):
            success, message = self.run_download()
        self.assertFalse(success)
        self.assertIn("发生变化", message)
        self.assertFalse(list(self.library.glob("*.srt")))
        self.refresh.assert_not_called()

    def test_invalid_or_symlink_subtitle_is_not_published_or_refreshed(self):
        for symlink in (False, True):
            with self.subTest(symlink=symlink):
                if self.source_subtitle.exists():
                    self.source_subtitle.unlink()
                if symlink:
                    foreign = self.root / "foreign.srt"
                    foreign.write_text(SRT)
                    self.source_subtitle.symlink_to(foreign)
                else:
                    self.source_subtitle.write_text("invalid subtitle")
                self.assertFalse(self.run_download()[0])
        self.assertFalse(list(self.library.glob("*.srt")))
        self.refresh.assert_not_called()

    def test_file_manager_action_uses_service_and_preserves_candidate_response(self):
        # Compile the real action method without booting optional web/browser
        # integrations, then run it against the actual download/sync service.
        tree = ast.parse((ROOT / "web/action.py").read_text())
        klass = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "WebAction")
        klass.body = [node for node in klass.body if isinstance(node, ast.FunctionDef)
                      and node.name == "__download_subtitle"]
        media = types.SimpleNamespace(tmdb_info=True, imdb_id="tt123", type="movie", en_name="Movie",
                                     cn_name="电影", title="Movie", year=2024, begin_season=None, begin_episode=None)
        namespace = {"os": os, "Media": lambda: types.SimpleNamespace(get_media_info=lambda **_: media)}
        exec(compile(ast.Module(body=[klass], type_ignores=[]), "<file-manager-action>", "exec"), namespace)
        with patch.dict(sys.modules, {"app.helper.mediafile_subtitles": self.module}):
            result = namespace["WebAction"]._WebAction__download_subtitle(
                {"path": str(self.source), "name": self.source.name})
        self.assertEqual(result["code"], 0, result)
        self.assertTrue(self.expected_subtitle().is_file())
        self.refresh.assert_called_once()
        self.source_subtitle.unlink()
        self.expected_subtitle().unlink()
        self.subtitle.opensubtitles.search_subtitles.return_value[0][0]["high_confidence"] = False
        with patch.dict(sys.modules, {"app.helper.mediafile_subtitles": self.module}):
            candidates = namespace["WebAction"]._WebAction__download_subtitle(
                {"path": str(self.source), "name": self.source.name})
        self.assertEqual(candidates["code"], 1)
        self.assertEqual(candidates["candidates"][0]["file_id"], 42)
        self.refresh.assert_called_once()
