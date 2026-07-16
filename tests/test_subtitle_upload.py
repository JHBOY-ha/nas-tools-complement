# -*- coding: utf-8 -*-

import os
import sys
import tempfile
import types
from unittest import TestCase
from unittest.mock import patch

if not os.environ.get("NASTOOL_CONFIG"):
    _ROOT_PATH = os.path.dirname(os.path.dirname(__file__))
    os.environ["NASTOOL_CONFIG"] = os.path.join(_ROOT_PATH, "config", "config.yaml")

if "undetected_chromedriver" not in sys.modules:
    uc_stub = types.ModuleType("undetected_chromedriver")
    uc_stub.find_chrome_executable = lambda: None
    uc_stub.ChromeOptions = object
    uc_stub.Chrome = object
    sys.modules["undetected_chromedriver"] = uc_stub

if "webdriver_manager.chrome" not in sys.modules:
    webdriver_manager_stub = types.ModuleType("webdriver_manager")
    chrome_stub = types.ModuleType("webdriver_manager.chrome")

    class _ChromeDriverManager:
        def install(self):
            return ""

    chrome_stub.ChromeDriverManager = _ChromeDriverManager
    sys.modules["webdriver_manager"] = webdriver_manager_stub
    sys.modules["webdriver_manager.chrome"] = chrome_stub

if "parse" not in sys.modules:
    parse_stub = types.ModuleType("parse")
    parse_stub.parse = lambda *args, **kwargs: None
    sys.modules["parse"] = parse_stub

if "dateparser" not in sys.modules:
    dateparser_stub = types.ModuleType("dateparser")
    dateparser_stub.parse = lambda value: None
    sys.modules["dateparser"] = dateparser_stub

if "cn2an" not in sys.modules:
    cn2an_stub = types.ModuleType("cn2an")
    cn2an_stub.cn2an = lambda value, mode=None: int(value)
    sys.modules["cn2an"] = cn2an_stub

if "bencode" not in sys.modules:
    bencode_stub = types.ModuleType("bencode")
    bencode_stub.bdecode = lambda value: {}
    sys.modules["bencode"] = bencode_stub

if "cacheout" not in sys.modules:
    cacheout_stub = types.ModuleType("cacheout")

    class _Cache:
        def __init__(self, *args, **kwargs):
            self._values = {}

        def get(self, key, default=None):
            return self._values.get(key, default)

        def set(self, key, value, *args, **kwargs):
            self._values[key] = value

        def delete(self, key):
            self._values.pop(key, None)

    class _CacheManager:
        def __init__(self, *args, **kwargs):
            pass

    cacheout_stub.Cache = _Cache
    cacheout_stub.LRUCache = _Cache
    cacheout_stub.CacheManager = _CacheManager
    sys.modules["cacheout"] = cacheout_stub

if "pyvirtualdisplay" not in sys.modules:
    pyvirtualdisplay_stub = types.ModuleType("pyvirtualdisplay")

    class _Display:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return self

        def stop(self):
            return None

    pyvirtualdisplay_stub.Display = _Display
    sys.modules["pyvirtualdisplay"] = pyvirtualdisplay_stub

if "pyquery" not in sys.modules:
    pyquery_stub = types.ModuleType("pyquery")

    class _PyQuery:
        def __init__(self, *args, **kwargs):
            pass

    pyquery_stub.PyQuery = _PyQuery
    sys.modules["pyquery"] = pyquery_stub

from app.subtitle import Subtitle, SystemUtils, SubtitleAligner
from app.helper.subtitle_health import SubtitleHealth
from app.utils.types import RmtMode


class _UploadFile:
    def __init__(self, filename, content=None):
        self.filename = filename
        if content is None:
            ext = os.path.splitext(filename)[-1].lower()
            defaults = {
                ".srt": b"1\n00:00:01,000 --> 00:00:02,000\nSubtitle\n",
                ".ass": b"[Script Info]\nTitle: Test\n[Events]\nFormat: Start, End, Text\nDialogue: 0:00:01.00,0:00:02.00,Subtitle\n",
                ".ssa": b"[Script Info]\nTitle: Test\n[Events]\nFormat: Start, End, Text\nDialogue: 0:00:01.00,0:00:02.00,Subtitle\n",
                ".vtt": b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nSubtitle\n",
                ".smi": b"<SAMI><BODY><SYNC Start=1000><P>Subtitle</BODY></SAMI>",
                ".sub": b"{1}{25}Subtitle"
            }
            content = defaults.get(ext, b"subtitle")
        self._content = content

    def save(self, path):
        if hasattr(path, "write"):
            path.write(self._content)
            return
        with open(path, "wb") as file_obj:
            file_obj.write(self._content)


class _FailingUploadFile(_UploadFile):
    def save(self, path):
        if hasattr(path, "write"):
            path.write(b"partial")
        else:
            with open(path, "wb") as file_obj:
                file_obj.write(b"partial")
        raise IOError("simulated save failure")


class SubtitleUploadTest(TestCase):
    def test_jellyfin_upload_uses_defined_chinese_language_tag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()
            content = b"1\r\n00:00:01,000 --> 00:00:02,000\r\nHello\r\n"

            success, msg, data = Subtitle().upload_subtitle(
                _UploadFile("Movie.zh-cn.srt", content),
                movie,
                server_type="jellyfin"
            )

            self.assertTrue(success, msg)
            self.assertEqual(data["language"], "zh-CN")
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.chi.zh-cn.srt")))

    def test_jellyfin_upload_preserves_source_title_before_language(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()

            success, msg, data = Subtitle().upload_subtitle(
                _UploadFile("Movie.YYeTs.zh-cn.srt"),
                movie,
                server_type="jellyfin"
            )

            self.assertTrue(success, msg)
            self.assertEqual(data["source"], "YYeTs")
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.YYeTs.chi.zh-cn.srt")))

    def test_jellyfin_same_language_collision_uses_readable_source_title(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()
            open(os.path.join(tmpdir, "Movie.chi.zh-cn.srt"), "wb").close()

            success, msg, _ = Subtitle().upload_subtitle(
                _UploadFile("subtitle.zh-cn.srt"),
                movie,
                server_type="jellyfin"
            )

            self.assertTrue(success, msg)
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.source-2.chi.zh-cn.srt")))
            self.assertFalse(os.path.exists(os.path.join(tmpdir, "Movie.chi.zh-cn(1).srt")))

    def test_repair_jellyfin_warning_subtitles_keeps_multiple_sources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            first = os.path.join(tmpdir, "Movie.zh-CN.srt")
            second = os.path.join(tmpdir, "Movie.YYeTs.zh-CN.srt")
            open(movie, "wb").close()
            for subtitle_file in [first, second]:
                with open(subtitle_file, "wb") as file_obj:
                    file_obj.write(b"subtitle")

            valid = {"valid": True, "probe_available": True, "message": "ok"}
            with patch.object(SubtitleHealth, "validate_subtitle", return_value=valid), \
                    patch.object(SubtitleHealth, "normalize_uploaded_subtitle", return_value=valid):
                success, msg, data = Subtitle().repair_external_subtitles(movie, "jellyfin")

            self.assertTrue(success, msg)
            self.assertEqual(len(data["processed"]), 2)
            self.assertEqual(data["subtitle_count"], 2)
            self.assertEqual(data["status"], "ok")
            self.assertFalse(os.path.exists(first))
            self.assertFalse(os.path.exists(second))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.chi.zh-cn.srt")))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.YYeTs.chi.zh-cn.srt")))

    def test_repair_gate_cancel_preserves_already_processed_items_as_partial_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            first = os.path.join(tmpdir, "Movie.first.eng.srt")
            second = os.path.join(tmpdir, "Movie.second.eng.srt")
            open(movie, "wb").close()
            for subtitle_file in [first, second]:
                with open(subtitle_file, "wb") as file_obj:
                    file_obj.write(b"1\n00:00:01,000 --> 00:00:02,000\nEnglish\n")

            gate_calls = [0]

            class _Gate:
                def __enter__(self):
                    gate_calls[0] += 1
                    if gate_calls[0] == 2:
                        raise InterruptedError("task canceled while waiting for heavy gate")

                @staticmethod
                def __exit__(_exc_type, _exc, _traceback):
                    return False

            warning = {"status": "warning", "reason": "needs normalization"}
            valid = {"valid": True, "probe_available": True, "message": "ok"}
            with patch.object(SubtitleHealth, "requires_external_probe", return_value=True), \
                    patch.object(SubtitleHealth, "inspect_external_subtitle", return_value=warning), \
                    patch.object(SubtitleHealth, "normalize_uploaded_subtitle", return_value=valid), \
                    patch.object(SubtitleHealth, "language_defined", return_value=True):
                success, msg, data = Subtitle().repair_external_subtitles(
                    movie, "jellyfin", heavy_operation=lambda _kind: _Gate()
                )

            self.assertTrue(success, msg)
            self.assertTrue(data["canceled"])
            self.assertEqual(len(data["processed"]), 1)
            self.assertEqual(data["processed"][0]["source"], first)

    def test_repair_processes_vobsub_sub_and_idx_as_one_logical_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            source_sub = os.path.join(tmpdir, "Movie.sub")
            source_idx = os.path.join(tmpdir, "Movie.idx")
            open(movie, "wb").close()
            with open(source_sub, "wb") as file_obj:
                file_obj.write(b"\x00binary-vobsub-payload")
            with open(source_idx, "wb") as file_obj:
                file_obj.write(b"VobSub index file, v7")

            def normalize_pair(work_sub, **_kwargs):
                self.assertTrue(os.path.isfile(os.path.splitext(work_sub)[0] + ".idx"))
                return {"valid": True, "probe_available": True, "message": "ok"}

            def inspect(path, _media, _server, **_kwargs):
                return {
                    "path": path,
                    "status": "warning" if os.path.normcase(path) == os.path.normcase(source_sub) else "ok",
                    "reason": "missing language" if path == source_sub else "ok"
                }

            with patch.object(SubtitleHealth, "normalize_uploaded_subtitle",
                              side_effect=normalize_pair), \
                    patch.object(SubtitleHealth, "inspect_external_subtitle", side_effect=inspect), \
                    patch.object(SubtitleHealth, "language_defined", return_value=False):
                success, msg, data = Subtitle().repair_external_subtitles(
                    movie, "jellyfin",
                    policy={"vobsub_limit_mb": 200, "ffprobe_timeout_seconds": 10}
                )

            target_sub = os.path.join(tmpdir, "Movie.chi.zh-cn.sub")
            target_idx = os.path.join(tmpdir, "Movie.chi.zh-cn.idx")
            self.assertTrue(success, msg)
            self.assertFalse(os.path.exists(source_sub))
            self.assertFalse(os.path.exists(source_idx))
            self.assertTrue(os.path.isfile(target_sub))
            self.assertTrue(os.path.isfile(target_idx))
            self.assertEqual(data["processed"][0]["target_companion"], target_idx)

    def test_repair_keeps_new_vobsub_pair_when_old_pair_restore_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            source_sub = os.path.join(tmpdir, "Movie.sub")
            source_idx = os.path.join(tmpdir, "Movie.idx")
            open(movie, "wb").close()
            with open(source_sub, "wb") as file_obj:
                file_obj.write(b"\x00binary-vobsub-payload")
            with open(source_idx, "wb") as file_obj:
                file_obj.write(b"VobSub index file, v7")

            def inspect(path, _media, _server, **_kwargs):
                return {
                    "path": path,
                    "status": "warning" if os.path.normcase(path) == os.path.normcase(source_sub) else "ok",
                    "reason": "missing language" if path == source_sub else "ok"
                }

            real_replace = os.replace
            subtitle_class = type(Subtitle())
            real_rename_no_replace = subtitle_class._Subtitle__rename_no_replace

            def fail_old_primary_retirement(source, target):
                target_name = os.path.basename(target)
                retiring_old_primary = os.path.normcase(source) == os.path.normcase(source_sub) \
                    and ".subtitle-repair-old-" in target_name
                if retiring_old_primary:
                    raise PermissionError("simulated VobSub retirement failure")
                return real_replace(source, target)

            def fail_old_companion_restore(source, target):
                restoring_old_companion = ".subtitle-repair-old-" in os.path.basename(source) \
                    and os.path.normcase(target) == os.path.normcase(source_idx)
                if restoring_old_companion:
                    raise PermissionError("simulated VobSub restore failure")
                return real_rename_no_replace(source, target)

            valid = {"valid": True, "probe_available": True, "message": "ok"}
            with patch.object(SubtitleHealth, "normalize_uploaded_subtitle", return_value=valid), \
                    patch.object(SubtitleHealth, "inspect_external_subtitle", side_effect=inspect), \
                    patch.object(SubtitleHealth, "language_defined", return_value=False), \
                    patch("app.subtitle.os.replace", side_effect=fail_old_primary_retirement), \
                    patch.object(subtitle_class, "_Subtitle__rename_no_replace",
                                 side_effect=fail_old_companion_restore):
                success, msg, data = Subtitle().repair_external_subtitles(
                    movie, "jellyfin",
                    policy={"vobsub_limit_mb": 200, "ffprobe_timeout_seconds": 10}
                )

            target_sub = os.path.join(tmpdir, "Movie.chi.zh-cn.sub")
            target_idx = os.path.join(tmpdir, "Movie.chi.zh-cn.idx")
            self.assertTrue(success, msg)
            self.assertTrue(os.path.isfile(target_sub))
            self.assertTrue(os.path.isfile(target_idx))
            self.assertTrue(os.path.isfile(source_sub))
            self.assertFalse(os.path.exists(source_idx))
            self.assertEqual(len(data["processed"]), 1)
            self.assertEqual(len(data["failures"]), 1)
            self.assertIn("已保留新的完整字幕对", data["failures"][0]["reason"])

    def test_jellyfin_region_language_tags_are_not_relabelled_as_chinese(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()

            success, msg, data = Subtitle().upload_subtitle(
                _UploadFile("Movie.en-US.srt"),
                movie,
                server_type="jellyfin"
            )

            self.assertTrue(success, msg)
            self.assertEqual(data["language"], "en-US")
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.eng.en-us.srt")))
            self.assertFalse(os.path.exists(os.path.join(tmpdir, "Movie.chi.zh-cn.srt")))

    def test_repair_normalizes_error_subtitle_before_replacing_original(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            subtitle_file = os.path.join(tmpdir, "Movie.eng.srt")
            open(movie, "wb").close()
            text = "1\n00:00:01,000 --> 00:00:02,000\n繁體中文字幕測試\n"
            with open(subtitle_file, "wb") as file_obj:
                file_obj.write(text.encode("big5"))

            valid = {"valid": True, "probe_available": True, "message": "ok"}

            def inspect(path, media_path, server_type):
                with open(path, "rb") as file_obj:
                    raw = file_obj.read()
                try:
                    raw.decode("utf-8")
                    status = "ok"
                except UnicodeDecodeError:
                    status = "error"
                return {
                    "path": path,
                    "media_path": media_path,
                    "server": server_type,
                    "status": status,
                    "reason": "编码异常" if status == "error" else "ok"
                }

            with patch.object(SubtitleHealth, "validate_subtitle", return_value=valid), \
                    patch.object(SubtitleHealth, "inspect_external_subtitle", side_effect=inspect):
                success, msg, data = Subtitle().repair_external_subtitles(movie, "jellyfin")

            self.assertTrue(success, msg)
            self.assertEqual(data["status"], "ok")
            with open(subtitle_file, "r", encoding="utf-8") as file_obj:
                self.assertIn("繁體中文字幕測試", file_obj.read())

    def test_repair_infers_language_when_filename_has_no_language_tag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            subtitle_file = os.path.join(tmpdir, "Movie.srt")
            open(movie, "wb").close()
            with open(subtitle_file, "w", encoding="utf-8") as file_obj:
                file_obj.write(
                    "1\n00:00:01,000 --> 00:00:02,000\n"
                    "这是没有语言标签的简体中文字幕。\n"
                )

            valid = {"valid": True, "probe_available": True, "message": "ok", "encoding": "utf-8"}
            with patch.object(SubtitleHealth, "validate_subtitle", return_value=valid):
                success, msg, data = Subtitle().repair_external_subtitles(movie, "jellyfin")

            self.assertTrue(success, msg)
            self.assertEqual(data["processed"][0]["language"], "zh-CN")
            self.assertFalse(os.path.exists(subtitle_file))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.chi.zh-cn.srt")))

    def test_repair_infers_english_when_filename_has_no_language_tag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            subtitle_file = os.path.join(tmpdir, "Movie.srt")
            open(movie, "wb").close()
            with open(subtitle_file, "w", encoding="utf-8") as file_obj:
                file_obj.write(
                    "1\n00:00:01,000 --> 00:00:02,000\n"
                    "This subtitle has no language tag in its filename.\n"
                )

            valid = {"valid": True, "probe_available": True, "message": "ok", "encoding": "utf-8"}
            with patch.object(SubtitleHealth, "validate_subtitle", return_value=valid):
                success, msg, data = Subtitle().repair_external_subtitles(movie, "jellyfin")

            self.assertTrue(success, msg)
            self.assertEqual(data["processed"][0]["language"], "eng")
            self.assertFalse(os.path.exists(subtitle_file))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.eng.srt")))

    def test_big5_normalization_preserves_traditional_chinese_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subtitle_file = os.path.join(tmpdir, "Movie.chi.srt")
            text = (
                "1\n00:00:01,000 --> 00:00:02,000\n"
                "繁體中文字幕測試，這是一段常見的字幕內容。\n"
            )
            with open(subtitle_file, "wb") as file_obj:
                file_obj.write(text.encode("big5"))

            valid = {"valid": True, "probe_available": True, "message": "ok"}
            with patch.object(SubtitleHealth, "validate_subtitle", return_value=valid):
                result = SubtitleHealth.normalize_uploaded_subtitle(subtitle_file)

            self.assertTrue(result["valid"])
            self.assertEqual(result["encoding"].lower(), "big5")
            with open(subtitle_file, "r", encoding="utf-8") as file_obj:
                self.assertIn("繁體中文字幕測試", file_obj.read())

    def test_cp1252_normalization_preserves_western_punctuation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subtitle_file = os.path.join(tmpdir, "Movie.eng.srt")
            text = (
                "1\n00:00:01,000 --> 00:00:02,000\n"
                "It’s déjà vu — don’t worry.\n"
            )
            with open(subtitle_file, "wb") as file_obj:
                file_obj.write(text.encode("cp1252"))

            valid = {"valid": True, "probe_available": True, "message": "ok"}
            with patch.object(SubtitleHealth, "validate_subtitle", return_value=valid):
                result = SubtitleHealth.normalize_uploaded_subtitle(subtitle_file)

            self.assertTrue(result["valid"])
            self.assertEqual(result["encoding"].lower(), "cp1252")
            with open(subtitle_file, "r", encoding="utf-8") as file_obj:
                self.assertIn("It’s déjà vu — don’t worry.", file_obj.read())

    def test_ass_normalization_repairs_truncated_header_and_timestamp_separator(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subtitle_file = os.path.join(tmpdir, "Movie.zh.ass")
            content = (
                "ipt Info]\r\n"
                "ScriptType: v4.00+\r\n\r\n"
                "[V4+ Styles]\r\n"
                "Format: Name, Fontname, Fontsize\r\n"
                "Style: Default,Arial,20\r\n\r\n"
                "[Events]\r\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\r\n"
                "Dialogue: 0,0:00:00.45,0:00:05:45,Default,,0,0,0,,测试字幕\r\n"
            )
            with open(subtitle_file, "wb") as file_obj:
                file_obj.write(content.encode("utf-8"))

            valid = {"valid": True, "probe_available": True, "message": "ok"}
            with patch.object(SubtitleHealth, "validate_subtitle", return_value=valid):
                result = SubtitleHealth.normalize_uploaded_subtitle(subtitle_file)

            self.assertTrue(result["valid"])
            self.assertTrue(result["repaired"])
            self.assertEqual(
                result["ass_repairs"],
                ["恢复缺失的 [Script Info] 段头", "规范化 1 个 Dialogue 时间分隔符"]
            )
            with open(subtitle_file, "r", encoding="utf-8") as file_obj:
                repaired = file_obj.read()
            self.assertTrue(repaired.startswith("[Script Info]\n"))
            self.assertIn("0:00:00.45,0:00:05.45", repaired)

    def test_ass_normalization_repairs_deeply_truncated_script_header(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subtitle_file = os.path.join(tmpdir, "Movie.zh.ass")
            content = (
                " Info]\r\n"
                "ScriptType: v4.00\r\n\r\n"
                "[V4 Styles]\r\n"
                "Format: Name, Fontname, Fontsize\r\n"
                "Style: Default,Arial,20\r\n\r\n"
                "[Events]\r\n"
                "Format: Marked, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\r\n"
                "Dialogue: Marked=0,0:00:00.45,0:00:05.45,Default,,0,0,0,,测试字幕\r\n"
            )
            with open(subtitle_file, "wb") as file_obj:
                file_obj.write(content.encode("utf-8"))

            valid = {"valid": True, "probe_available": True, "message": "ok"}
            with patch.object(SubtitleHealth, "validate_subtitle", return_value=valid):
                result = SubtitleHealth.normalize_uploaded_subtitle(subtitle_file)

            self.assertTrue(result["valid"])
            self.assertEqual(result["ass_repairs"], ["恢复缺失的 [Script Info] 段头"])
            with open(subtitle_file, "r", encoding="utf-8") as file_obj:
                self.assertTrue(file_obj.read().startswith("[Script Info]\n"))

    def test_upload_reports_ass_repairs_without_srt_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()
            content = (
                "ipt Info]\nScriptType: v4.00+\n\n"
                "[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize\n"
                "Style: Default,Arial,20\n\n"
                "[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:00.45,0:00:05:45,Default,,0,0,0,,测试字幕\n"
            ).encode("utf-8")

            valid = {"valid": True, "probe_available": True, "message": "ok"}
            with patch.object(SubtitleHealth, "validate_subtitle", return_value=valid):
                success, msg, data = Subtitle().upload_subtitle(
                    _UploadFile("Movie.zh-cn.ass", content),
                    movie,
                    server_type="jellyfin"
                )

            self.assertTrue(success, msg)
            self.assertIn("恢复缺失的 [Script Info] 段头", msg)
            self.assertIn("规范化 1 个 Dialogue 时间分隔符", msg)
            self.assertNotIn("SRT 异常空行", msg)
            self.assertEqual(len(data["validation"]["ass_repairs"]), 2)

    def test_upload_repairs_blank_line_between_srt_index_and_timing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()
            content = "\ufeff0\r\n\r\n00:00:01,000 --> 00:00:02,000\r\nHello\r\n".encode("utf-8")

            success, msg, data = Subtitle().upload_subtitle(
                _UploadFile("Movie.zh-cn.srt", content),
                movie,
                server_type="jellyfin"
            )

            self.assertTrue(success, msg)
            subtitle_file = os.path.join(tmpdir, "Movie.chi.zh-cn.srt")
            with open(subtitle_file, "rb") as file_obj:
                normalized = file_obj.read()
            self.assertNotIn(b"0\n\n00:00:01,000", normalized)
            self.assertIn(b"0\n00:00:01,000", normalized)
            self.assertTrue(data["validation"]["repaired"])
            self.assertIn("已修复 1 处 SRT 异常空行", msg)

    def test_upload_rejects_subtitle_when_ffprobe_validation_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()
            original_normalize = SubtitleHealth.__dict__["normalize_uploaded_subtitle"]
            try:
                SubtitleHealth.normalize_uploaded_subtitle = classmethod(
                    lambda cls, path: {
                        "valid": False,
                        "probe_available": True,
                        "message": "Invalid data found when processing input"
                    }
                )
                success, msg, _ = Subtitle().upload_subtitle(
                    _UploadFile("Movie.zh-cn.srt"),
                    movie,
                    server_type="jellyfin"
                )
            finally:
                SubtitleHealth.normalize_uploaded_subtitle = original_normalize

            self.assertFalse(success)
            self.assertIn("字幕无法被媒体服务器解析", msg)
            self.assertFalse(os.path.exists(os.path.join(tmpdir, "Movie.chi.zh-cn.srt")))

    def test_upload_rejects_invalid_srt_with_basic_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()

            success, msg, _ = Subtitle().upload_subtitle(
                _UploadFile("Movie.zh-cn.srt", b"this is not an srt subtitle"),
                movie,
                server_type="jellyfin"
            )

            self.assertFalse(success)
            self.assertIn("字幕无法被媒体服务器解析", msg)
            self.assertFalse(os.path.exists(os.path.join(tmpdir, "Movie.chi.zh-cn.srt")))

    def test_upload_uses_target_media_basename_when_syncing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = os.path.join(tmpdir, "src")
            target_dir = os.path.join(tmpdir, "target")
            os.makedirs(src_dir)
            os.makedirs(target_dir)
            src_movie = os.path.join(src_dir, "Original.mkv")
            target_movie = os.path.join(target_dir, "Movie (2024).mkv")
            open(src_movie, "wb").close()
            open(target_movie, "wb").close()

            success, msg, data = Subtitle().upload_subtitle(
                _UploadFile("subtitle.eng.srt"),
                src_movie,
                target_movie,
                RmtMode.COPY
            )

            self.assertTrue(success, msg)
            self.assertTrue(data["synced"])
            self.assertTrue(os.path.exists(os.path.join(src_dir, "Original.eng.srt")))
            self.assertTrue(os.path.exists(os.path.join(target_dir, "Movie (2024).eng.srt")))

    def test_upload_without_target_only_saves_source_subtitle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()

            success, msg, data = Subtitle().upload_subtitle(_UploadFile("Movie.zh-cn.ass"), movie)

            self.assertTrue(success, msg)
            self.assertFalse(data["synced"])
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.zh-CN.ass")))

    def test_upload_rejects_unsupported_subtitle_extension(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()

            success, msg, _ = Subtitle().upload_subtitle(_UploadFile("Movie.txt"), movie)

            self.assertFalse(success)
            self.assertIn("仅支持", msg)

    def test_upload_defaults_unknown_language_to_zh_cn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()

            success, msg, _ = Subtitle().upload_subtitle(_UploadFile("Movie.subtitle.srt"), movie)

            self.assertTrue(success, msg)
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.zh-CN.srt")))

    def test_upload_detects_traditional_chinese(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()

            success, msg, _ = Subtitle().upload_subtitle(_UploadFile("Movie.zh-tw.ssa"), movie)

            self.assertTrue(success, msg)
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.zh-TW.ssa")))

    def test_upload_adds_number_when_subtitle_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()
            open(os.path.join(tmpdir, "Movie.zh-CN.srt"), "wb").close()

            success, msg, _ = Subtitle().upload_subtitle(_UploadFile("Movie.zh-cn.srt"), movie)

            self.assertTrue(success, msg)
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.zh-CN(1).srt")))

    def test_upload_does_not_duplicate_when_target_is_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()

            success, msg, data = Subtitle().upload_subtitle(
                _UploadFile("Movie.eng.srt"),
                movie,
                movie,
                RmtMode.COPY
            )

            self.assertTrue(success, msg)
            self.assertTrue(data["synced"])
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.eng.srt")))
            self.assertFalse(os.path.exists(os.path.join(tmpdir, "Movie.eng(1).srt")))

    def test_plex_upload_preserves_chinese_region_tag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()

            success, msg, data = Subtitle().upload_subtitle(
                _UploadFile("Movie.zh-cn.srt"),
                movie,
                server_type="plex"
            )

            self.assertTrue(success, msg)
            self.assertEqual(data["language"], "zh-CN")
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.zh-CN.srt")))

    def test_plex_upload_preserves_forced_and_sdh_flags(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()

            success, msg, _ = Subtitle().upload_subtitle(
                _UploadFile("Movie.en.forced.sdh.vtt"),
                movie,
                server_type="plex"
            )

            self.assertTrue(success, msg)
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.en.forced.sdh.vtt")))

    def test_plex_upload_collision_keeps_language_token_clean(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()
            open(os.path.join(tmpdir, "Movie.zh-TW.srt"), "wb").close()

            success, msg, _ = Subtitle().upload_subtitle(
                _UploadFile("Movie.zh-tw.srt"),
                movie,
                server_type="plex"
            )

            self.assertTrue(success, msg)
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie(1).zh-TW.srt")))
            self.assertFalse(os.path.exists(os.path.join(tmpdir, "Movie.zh-TW(1).srt")))

    def test_upload_accepts_sub_extension(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()

            success, msg, _ = Subtitle().upload_subtitle(_UploadFile("Movie.chinese.sub"), movie)

            self.assertTrue(success, msg)
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.zh-CN.sub")))

    def test_upload_chinese_priority_over_english(self):
        """文件名同时含中英文标记时，简体中文优先级高于英文"""
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()

            success, msg, data = Subtitle().upload_subtitle(_UploadFile("Movie.chinese.eng.srt"), movie)

            self.assertTrue(success, msg)
            self.assertEqual(data["language"], "zh-CN")
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.zh-CN.srt")))

    def test_upload_target_subtitle_renamed_to_match_target_media(self):
        """同步时字幕文件名应与目标媒体文件名一致"""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = os.path.join(tmpdir, "src")
            target_dir = os.path.join(tmpdir, "target")
            os.makedirs(src_dir)
            os.makedirs(target_dir)
            src_movie = os.path.join(src_dir, "Source.mkv")
            target_movie = os.path.join(target_dir, "Target.mkv")
            open(src_movie, "wb").close()
            open(target_movie, "wb").close()

            success, msg, data = Subtitle().upload_subtitle(
                _UploadFile("subtitle.eng.srt"),
                src_movie,
                target_movie,
                RmtMode.COPY
            )

            self.assertTrue(success, msg)
            self.assertTrue(data["synced"])
            self.assertTrue(os.path.exists(os.path.join(src_dir, "Source.eng.srt")))
            self.assertTrue(os.path.exists(os.path.join(target_dir, "Target.eng.srt")))

    def test_upload_save_failure_removes_partial_source_subtitle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()

            success, msg, _ = Subtitle().upload_subtitle(_FailingUploadFile("Movie.eng.srt"), movie)

            self.assertFalse(success)
            self.assertIn("保存源字幕失败", msg)
            self.assertFalse(os.path.exists(os.path.join(tmpdir, "Movie.eng.srt")))

    def test_sync_does_not_overwrite_existing_target_subtitle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_subtitle = os.path.join(tmpdir, "Source.eng.srt")
            target_subtitle = os.path.join(tmpdir, "Target.eng.srt")
            with open(source_subtitle, "wb") as file_obj:
                file_obj.write(b"new subtitle")
            with open(target_subtitle, "wb") as file_obj:
                file_obj.write(b"existing subtitle")

            original_link = SystemUtils.__dict__["link"]
            try:
                SystemUtils.link = staticmethod(lambda src, dest: (-1, "link failed"))
                retcode, retmsg = Subtitle()._Subtitle__sync_manual_subtitle(
                    source_subtitle,
                    target_subtitle,
                    RmtMode.LINK
                )
            finally:
                SystemUtils.link = original_link

            self.assertNotEqual(retcode, 0)
            self.assertIn("已存在", retmsg)
            with open(target_subtitle, "rb") as file_obj:
                self.assertEqual(file_obj.read(), b"existing subtitle")

    def test_upload_default_does_not_call_auto_alignment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = os.path.join(tmpdir, "src")
            target_dir = os.path.join(tmpdir, "target")
            os.makedirs(src_dir)
            os.makedirs(target_dir)
            src_movie = os.path.join(src_dir, "Source.mkv")
            target_movie = os.path.join(target_dir, "Target.mkv")
            open(src_movie, "wb").close()
            open(target_movie, "wb").close()

            original_align = SubtitleAligner.__dict__["align_subtitle"]
            try:
                SubtitleAligner.align_subtitle = staticmethod(
                    lambda source, target: (_ for _ in ()).throw(AssertionError("alignment should not run"))
                )
                success, msg, data = Subtitle().upload_subtitle(
                    _UploadFile("subtitle.eng.srt"),
                    src_movie,
                    target_movie,
                    RmtMode.COPY
                )
            finally:
                SubtitleAligner.align_subtitle = original_align

            self.assertTrue(success, msg)
            self.assertFalse(data["alignment"]["applied"])

    def test_upload_auto_alignment_reports_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = os.path.join(tmpdir, "src")
            target_dir = os.path.join(tmpdir, "target")
            os.makedirs(src_dir)
            os.makedirs(target_dir)
            src_movie = os.path.join(src_dir, "Source.mkv")
            target_movie = os.path.join(target_dir, "Target.mkv")
            open(src_movie, "wb").close()
            open(target_movie, "wb").close()

            original_align = SubtitleAligner.__dict__["align_subtitle"]
            try:
                captured = {}

                def _align(source, target, align_mode="auto"):
                    captured["align_mode"] = align_mode
                    return {
                        "applied": True,
                        "skipped": False,
                        "message": "自动对齐完成",
                        "mode": "segmented",
                        "anchors": 6
                    }

                SubtitleAligner.align_subtitle = staticmethod(_align)
                success, msg, data = Subtitle().upload_subtitle(
                    _UploadFile("subtitle.eng.srt"),
                    src_movie,
                    target_movie,
                    RmtMode.COPY,
                    align_mode="segmented"
                )
            finally:
                SubtitleAligner.align_subtitle = original_align

            self.assertTrue(success, msg)
            self.assertIn("已自动对齐字幕", msg)
            self.assertTrue(data["alignment"]["applied"])
            self.assertEqual(data["alignment"]["mode"], "segmented")
            self.assertEqual(captured["align_mode"], "segmented")
