# -*- coding: utf-8 -*-

import os
import sys
import tempfile
import types
from unittest import TestCase
from unittest.mock import Mock, patch

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

from app.helper.subtitle_align import SubtitleAligner


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as file_obj:
        file_obj.write(text)


def _srt(entries):
    blocks = []
    for index, (start, end, text) in enumerate(entries, 1):
        blocks.append("\n".join([str(index), f"{start} --> {end}", text]))
    return "\n\n".join(blocks) + "\n"


def _vtt(entries, extra_header="STYLE\n::cue { color: white; }"):
    blocks = ["WEBVTT", extra_header]
    for index, (start, end, text) in enumerate(entries, 1):
        blocks.append("\n".join([
            f"cue-{index}",
            f"{start} --> {end} line:90% position:50%",
            text
        ]))
    return "\n\n".join(blocks) + "\n"


class SubtitleAlignTest(TestCase):
    def test_align_fixed_offset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, "source.srt")
            reference = os.path.join(tmpdir, "reference.srt")
            _write(source, _srt([
                ("00:00:01,000", "00:00:02,000", "第一句对白"),
                ("00:00:03,000", "00:00:04,000", "第二句对白"),
                ("00:00:05,000", "00:00:06,000", "第三句对白"),
                ("00:00:07,000", "00:00:08,000", "第四句对白"),
                ("00:00:09,000", "00:00:10,000", "第五句对白"),
            ]))
            _write(reference, _srt([
                ("00:00:03,000", "00:00:04,000", "第一句对白"),
                ("00:00:05,000", "00:00:06,000", "第二句对白"),
                ("00:00:07,000", "00:00:08,000", "第三句对白"),
                ("00:00:09,000", "00:00:10,000", "第四句对白"),
                ("00:00:11,000", "00:00:12,000", "第五句对白"),
            ]))

            ret = SubtitleAligner.align_with_reference_file(source, reference)

            self.assertTrue(ret["applied"], ret)
            self.assertEqual(ret["mode"], "offset")
            with open(source, "r", encoding="utf-8") as file_obj:
                self.assertIn("00:00:03,000 --> 00:00:04,000", file_obj.read())

    def test_align_segmented_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, "source.srt")
            reference = os.path.join(tmpdir, "reference.srt")
            _write(source, _srt([
                ("00:00:01,000", "00:00:02,000", "第一句对白"),
                ("00:00:03,000", "00:00:04,000", "第二句对白"),
                ("00:00:05,000", "00:00:06,000", "第三句对白"),
                ("00:00:07,000", "00:00:08,000", "第四句对白"),
                ("00:00:09,000", "00:00:10,000", "第五句对白"),
                ("00:00:11,000", "00:00:12,000", "第六句对白"),
            ]))
            _write(reference, _srt([
                ("00:00:01,000", "00:00:02,000", "第一句对白"),
                ("00:00:03,000", "00:00:04,000", "第二句对白"),
                ("00:00:06,200", "00:00:07,200", "第三句对白"),
                ("00:00:08,400", "00:00:09,400", "第四句对白"),
                ("00:00:10,600", "00:00:11,600", "第五句对白"),
                ("00:00:12,800", "00:00:13,800", "第六句对白"),
            ]))

            ret = SubtitleAligner.align_with_reference_file(source, reference)

            self.assertTrue(ret["applied"], ret)
            self.assertEqual(ret["mode"], "segmented")
            with open(source, "r", encoding="utf-8") as file_obj:
                content = file_obj.read()
            self.assertIn("00:00:08,400 --> 00:00:09,500", content)
            self.assertIn("00:00:12,800 --> 00:00:13,800", content)

    def test_forced_offset_mode_uses_single_offset_for_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, "source.srt")
            reference = os.path.join(tmpdir, "reference.srt")
            _write(source, _srt([
                ("00:00:01,000", "00:00:02,000", "第一句对白"),
                ("00:00:03,000", "00:00:04,000", "第二句对白"),
                ("00:00:05,000", "00:00:06,000", "第三句对白"),
                ("00:00:07,000", "00:00:08,000", "第四句对白"),
                ("00:00:09,000", "00:00:10,000", "第五句对白"),
                ("00:00:11,000", "00:00:12,000", "第六句对白"),
            ]))
            _write(reference, _srt([
                ("00:00:01,000", "00:00:02,000", "第一句对白"),
                ("00:00:03,000", "00:00:04,000", "第二句对白"),
                ("00:00:06,200", "00:00:07,200", "第三句对白"),
                ("00:00:08,400", "00:00:09,400", "第四句对白"),
                ("00:00:10,600", "00:00:11,600", "第五句对白"),
                ("00:00:12,800", "00:00:13,800", "第六句对白"),
            ]))

            ret = SubtitleAligner.align_with_reference_file(source, reference, align_mode="offset")

            self.assertTrue(ret["applied"], ret)
            self.assertEqual(ret["mode"], "offset")
            with open(source, "r", encoding="utf-8") as file_obj:
                content = file_obj.read()
            self.assertIn("00:00:08,400 --> 00:00:09,400", content)

    def test_forced_segmented_mode_uses_segment_mapping_for_stable_offset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, "source.srt")
            reference = os.path.join(tmpdir, "reference.srt")
            _write(source, _srt([
                ("00:00:01,000", "00:00:02,000", "第一句对白"),
                ("00:00:03,000", "00:00:04,000", "第二句对白"),
                ("00:00:05,000", "00:00:06,000", "第三句对白"),
                ("00:00:07,000", "00:00:08,000", "第四句对白"),
                ("00:00:09,000", "00:00:10,000", "第五句对白"),
            ]))
            _write(reference, _srt([
                ("00:00:03,000", "00:00:04,000", "第一句对白"),
                ("00:00:05,000", "00:00:06,000", "第二句对白"),
                ("00:00:07,000", "00:00:08,000", "第三句对白"),
                ("00:00:09,000", "00:00:10,000", "第四句对白"),
                ("00:00:11,000", "00:00:12,000", "第五句对白"),
            ]))

            ret = SubtitleAligner.align_with_reference_file(source, reference, align_mode="segmented")

            self.assertTrue(ret["applied"], ret)
            self.assertEqual(ret["mode"], "segmented")

    def test_low_confidence_keeps_original_subtitle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, "source.srt")
            reference = os.path.join(tmpdir, "reference.srt")
            original = _srt([
                ("00:00:01,000", "00:00:02,000", "完全不同一"),
                ("00:00:03,000", "00:00:04,000", "完全不同二"),
            ])
            _write(source, original)
            _write(reference, _srt([
                ("00:00:01,000", "00:00:02,000", "参考文本一"),
                ("00:00:03,000", "00:00:04,000", "参考文本二"),
                ("00:00:05,000", "00:00:06,000", "参考文本三"),
                ("00:00:07,000", "00:00:08,000", "参考文本四"),
                ("00:00:09,000", "00:00:10,000", "参考文本五"),
            ]))

            ret = SubtitleAligner.align_with_reference_file(source, reference)

            self.assertFalse(ret["applied"])
            self.assertTrue(ret["skipped"])
            with open(source, "r", encoding="utf-8") as file_obj:
                self.assertEqual(file_obj.read(), original)

    def test_write_failure_keeps_original_subtitle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, "source.srt")
            reference = os.path.join(tmpdir, "reference.srt")
            original = _srt([
                ("00:00:01,000", "00:00:02,000", "第一句对白"),
                ("00:00:03,000", "00:00:04,000", "第二句对白"),
                ("00:00:05,000", "00:00:06,000", "第三句对白"),
                ("00:00:07,000", "00:00:08,000", "第四句对白"),
                ("00:00:09,000", "00:00:10,000", "第五句对白"),
            ])
            _write(source, original)
            _write(reference, _srt([
                ("00:00:03,000", "00:00:04,000", "第一句对白"),
                ("00:00:05,000", "00:00:06,000", "第二句对白"),
                ("00:00:07,000", "00:00:08,000", "第三句对白"),
                ("00:00:09,000", "00:00:10,000", "第四句对白"),
                ("00:00:11,000", "00:00:12,000", "第五句对白"),
            ]))

            import app.helper.subtitle_align as subtitle_align_mod
            original_replace = subtitle_align_mod.os.replace
            try:
                subtitle_align_mod.os.replace = lambda src, dest: (_ for _ in ()).throw(OSError("replace failed"))
                with self.assertRaises(OSError):
                    SubtitleAligner.align_with_reference_file(source, reference)
            finally:
                subtitle_align_mod.os.replace = original_replace

            with open(source, "r", encoding="utf-8") as file_obj:
                self.assertEqual(file_obj.read(), original)

    def test_vtt_alignment_preserves_metadata_and_cue_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, "source.vtt")
            reference = os.path.join(tmpdir, "reference.srt")
            _write(source, _vtt([
                ("00:00:01.000", "00:00:02.000", "第一句对白"),
                ("00:00:03.000", "00:00:04.000", "第二句对白"),
                ("00:00:05.000", "00:00:06.000", "第三句对白"),
                ("00:00:07.000", "00:00:08.000", "第四句对白"),
                ("00:00:09.000", "00:00:10.000", "第五句对白"),
            ]))
            _write(reference, _srt([
                ("00:00:03,000", "00:00:04,000", "第一句对白"),
                ("00:00:05,000", "00:00:06,000", "第二句对白"),
                ("00:00:07,000", "00:00:08,000", "第三句对白"),
                ("00:00:09,000", "00:00:10,000", "第四句对白"),
                ("00:00:11,000", "00:00:12,000", "第五句对白"),
            ]))

            ret = SubtitleAligner.align_with_reference_file(source, reference)

            self.assertTrue(ret["applied"], ret)
            with open(source, "r", encoding="utf-8") as file_obj:
                content = file_obj.read()
            self.assertIn("STYLE", content)
            self.assertIn("::cue { color: white; }", content)
            self.assertIn("cue-1", content)
            self.assertIn("line:90% position:50%", content)
            self.assertIn("00:00:03.000 --> 00:00:04.000 line:90% position:50%", content)

    def test_match_budget_exhaustion_skips_alignment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, "source.srt")
            reference = os.path.join(tmpdir, "reference.srt")
            _write(source, _srt([
                ("00:00:01,000", "00:00:02,000", "第一句对白"),
                ("00:00:03,000", "00:00:04,000", "第二句对白"),
                ("00:00:05,000", "00:00:06,000", "第三句对白"),
                ("00:00:07,000", "00:00:08,000", "第四句对白"),
                ("00:00:09,000", "00:00:10,000", "第五句对白"),
            ]))
            _write(reference, _srt([
                ("00:00:03,000", "00:00:04,000", "第一句对白"),
                ("00:00:05,000", "00:00:06,000", "第二句对白"),
                ("00:00:07,000", "00:00:08,000", "第三句对白"),
                ("00:00:09,000", "00:00:10,000", "第四句对白"),
                ("00:00:11,000", "00:00:12,000", "第五句对白"),
            ]))

            original_budget = SubtitleAligner._max_match_comparisons
            try:
                SubtitleAligner._max_match_comparisons = 1
                ret = SubtitleAligner.align_with_reference_file(source, reference)
            finally:
                SubtitleAligner._max_match_comparisons = original_budget

            self.assertFalse(ret["applied"])
            self.assertIn("处理预算", ret["message"])

    def test_llm_cross_language_translation_aligns_reference(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, "source.zh-cn.srt")
            reference = os.path.join(tmpdir, "reference.eng.srt")
            _write(source, _srt([
                ("00:00:01,000", "00:00:02,000", "第一句对白"),
                ("00:00:03,000", "00:00:04,000", "第二句对白"),
                ("00:00:05,000", "00:00:06,000", "第三句对白"),
                ("00:00:07,000", "00:00:08,000", "第四句对白"),
                ("00:00:09,000", "00:00:10,000", "第五句对白"),
            ]))
            _write(reference, _srt([
                ("00:00:03,000", "00:00:04,000", "first line"),
                ("00:00:05,000", "00:00:06,000", "second line"),
                ("00:00:07,000", "00:00:08,000", "third line"),
                ("00:00:09,000", "00:00:10,000", "fourth line"),
                ("00:00:11,000", "00:00:12,000", "fifth line"),
            ]))
            mock_client = Mock()
            mock_client.is_ready.return_value = True
            mock_client.complete_json.return_value = [
                {"id": 0, "text": "第一句对白"},
                {"id": 1, "text": "第二句对白"},
                {"id": 2, "text": "第三句对白"},
                {"id": 3, "text": "第四句对白"},
                {"id": 4, "text": "第五句对白"},
            ]

            with patch("app.helper.subtitle_align.LLMClient", return_value=mock_client):
                ret = SubtitleAligner.align_with_reference_file(
                    source,
                    reference,
                    source_language="zh-CN",
                    reference_language="eng",
                    allow_llm=True
                )

            self.assertTrue(ret["applied"], ret)
            self.assertTrue(ret["cross_language"])
            self.assertEqual(ret["reference_language"], "eng")
            with open(source, "r", encoding="utf-8") as file_obj:
                self.assertIn("00:00:03,000 --> 00:00:04,000", file_obj.read())

    def test_llm_cross_language_invalid_translation_keeps_original(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, "source.zh-cn.srt")
            reference = os.path.join(tmpdir, "reference.eng.srt")
            original = _srt([
                ("00:00:01,000", "00:00:02,000", "第一句对白"),
                ("00:00:03,000", "00:00:04,000", "第二句对白"),
                ("00:00:05,000", "00:00:06,000", "第三句对白"),
                ("00:00:07,000", "00:00:08,000", "第四句对白"),
                ("00:00:09,000", "00:00:10,000", "第五句对白"),
            ])
            _write(source, original)
            _write(reference, _srt([
                ("00:00:03,000", "00:00:04,000", "first line"),
                ("00:00:05,000", "00:00:06,000", "second line"),
                ("00:00:07,000", "00:00:08,000", "third line"),
                ("00:00:09,000", "00:00:10,000", "fourth line"),
                ("00:00:11,000", "00:00:12,000", "fifth line"),
            ]))
            mock_client = Mock()
            mock_client.is_ready.return_value = True
            mock_client.complete_json.return_value = [{"id": 0, "text": "第一句对白"}]

            with patch("app.helper.subtitle_align.LLMClient", return_value=mock_client):
                ret = SubtitleAligner.align_with_reference_file(
                    source,
                    reference,
                    source_language="zh-CN",
                    reference_language="eng",
                    allow_llm=True
                )

            self.assertFalse(ret["applied"])
            self.assertIn("不完整", ret["message"])
            with open(source, "r", encoding="utf-8") as file_obj:
                self.assertEqual(original, file_obj.read())

    def test_llm_translation_uses_decreasing_remaining_timeout_without_retries(self):
        cues = [
            {"start": 0, "end": 1000, "text": "first line"},
            {"start": 1000, "end": 2000, "text": "second line"}
        ]
        batches = [
            [{"id": 0, "text": "first line"}],
            [{"id": 1, "text": "second line"}]
        ]
        mock_client = Mock()
        mock_client.is_ready.return_value = True
        mock_client.complete_json.side_effect = [
            [{"id": 0, "text": "第一句"}],
            [{"id": 1, "text": "第二句"}]
        ]
        SubtitleAligner._llm_translation_cache.clear()

        with patch("app.helper.subtitle_align.LLMClient", return_value=mock_client), \
                patch.object(
                    SubtitleAligner,
                    "_SubtitleAligner__is_llm_alignment_enabled",
                    return_value=True
                ), \
                patch.object(
                    SubtitleAligner,
                    "_SubtitleAligner__iter_translation_batches",
                    return_value=batches
                ), \
                patch(
                    "app.helper.subtitle_align.time.monotonic",
                    side_effect=[100, 101, 102, 103, 104, 105, 106]
                ):
            translated, message = SubtitleAligner._SubtitleAligner__translate_reference_cues(
                cues,
                target_language="zh-CN",
                timeout=10,
                max_batches=2
            )

        self.assertEqual("", message)
        self.assertEqual(["第一句", "第二句"], [cue["text"] for cue in translated])
        self.assertEqual(2, mock_client.complete_json.call_count)
        first_kwargs = mock_client.complete_json.call_args_list[0].kwargs
        second_kwargs = mock_client.complete_json.call_args_list[1].kwargs
        self.assertEqual(9, first_kwargs["timeout"])
        self.assertEqual(6, second_kwargs["timeout"])
        self.assertEqual(0, first_kwargs["max_retries"])
        self.assertEqual(0, second_kwargs["max_retries"])

    def test_llm_translation_checks_cancel_after_request(self):
        cues = [{"start": 0, "end": 1000, "text": "first line"}]
        batch = [{"id": 0, "text": "first line"}]
        mock_client = Mock()
        mock_client.is_ready.return_value = True
        mock_client.complete_json.return_value = [{"id": 0, "text": "第一句"}]
        cancel_check = Mock(side_effect=[False, True])
        SubtitleAligner._llm_translation_cache.clear()

        with patch("app.helper.subtitle_align.LLMClient", return_value=mock_client), \
                patch.object(
                    SubtitleAligner,
                    "_SubtitleAligner__is_llm_alignment_enabled",
                    return_value=True
                ), \
                patch.object(
                    SubtitleAligner,
                    "_SubtitleAligner__iter_translation_batches",
                    return_value=[batch]
                ), \
                patch("app.helper.subtitle_align.time.monotonic", side_effect=[100, 101]):
            translated, message = SubtitleAligner._SubtitleAligner__translate_reference_cues(
                cues,
                target_language="zh-CN",
                cancel_check=cancel_check,
                timeout=10
            )

        self.assertIsNone(translated)
        self.assertEqual("任务已取消", message)
        self.assertEqual(2, cancel_check.call_count)
        self.assertEqual(0, mock_client.complete_json.call_args.kwargs["max_retries"])

    def test_align_subtitle_skips_when_ffmpeg_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, "source.srt")
            media = os.path.join(tmpdir, "Movie.mkv")
            _write(source, _srt([
                ("00:00:01,000", "00:00:02,000", "第一句对白"),
                ("00:00:03,000", "00:00:04,000", "第二句对白"),
                ("00:00:05,000", "00:00:06,000", "第三句对白"),
                ("00:00:07,000", "00:00:08,000", "第四句对白"),
                ("00:00:09,000", "00:00:10,000", "第五句对白"),
            ]))
            open(media, "wb").close()
            original_which = SubtitleAligner.__dict__["_SubtitleAligner__is_enabled"]
            try:
                SubtitleAligner._SubtitleAligner__is_enabled = classmethod(lambda cls: True)
                import app.helper.subtitle_align as subtitle_align_mod
                original_shutil_which = subtitle_align_mod.shutil.which
                subtitle_align_mod.shutil.which = lambda name: None
                ret = SubtitleAligner.align_subtitle(source, media)
            finally:
                SubtitleAligner._SubtitleAligner__is_enabled = original_which
                subtitle_align_mod.shutil.which = original_shutil_which

            self.assertFalse(ret["applied"])
            self.assertIn("ffmpeg/ffprobe", ret["message"])

    def test_align_subtitle_caps_each_blocking_step_to_upload_remaining_budget(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, "source.srt")
            media = os.path.join(tmpdir, "Movie.mkv")
            _write(source, _srt([
                ("00:00:01,000", "00:00:02,000", "A sufficiently long subtitle line")
            ]))
            open(media, "wb").close()
            task_work = os.path.join(tmpdir, "task-work")
            remaining = Mock(side_effect=[5.0, 4.0, 3.0])
            with patch.object(SubtitleAligner, "_SubtitleAligner__is_enabled", return_value=True), \
                    patch("app.helper.subtitle_align.shutil.which", return_value="tool"), \
                    patch.object(SubtitleAligner, "_SubtitleAligner__detect_subtitle_language", return_value="eng"), \
                    patch.object(
                        SubtitleAligner, "_SubtitleAligner__select_reference_stream",
                        return_value={"index": 0, "_language": "eng"}
                    ) as select_stream, \
                    patch.object(
                        SubtitleAligner, "_SubtitleAligner__extract_reference_subtitle",
                        return_value=(True, "")
                    ) as extract_reference, \
                    patch.object(
                        SubtitleAligner, "align_with_reference_file",
                        return_value={"applied": True, "skipped": False, "message": "ok"}
                    ) as align_reference:
                result = SubtitleAligner.align_subtitle(
                    source, media, align_mode="llm",
                    ffprobe_timeout=10, ffmpeg_timeout=60, llm_timeout=180,
                    remaining_budget=remaining,
                    temporary_dir=task_work,
                    reference_max_bytes=12345
                )

            self.assertTrue(result["applied"])
            self.assertEqual(select_stream.call_args.kwargs["timeout"], 5.0)
            self.assertEqual(extract_reference.call_args.kwargs["timeout"], 4.0)
            self.assertEqual(extract_reference.call_args.kwargs["max_bytes"], 12345)
            reference_path = extract_reference.call_args.args[2]
            self.assertEqual(
                os.path.commonpath([task_work, reference_path]),
                task_work
            )
            self.assertEqual(align_reference.call_args.kwargs["llm_timeout"], 3.0)

    def test_reference_extraction_has_ffmpeg_file_size_cap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media = os.path.join(tmpdir, "Movie.mkv")
            output = os.path.join(tmpdir, "reference.srt")
            open(media, "wb").close()
            with open(output, "wb") as file_obj:
                file_obj.write(b"x" * 11)
            completed = Mock(returncode=0, stderr="")
            with patch.object(
                    SubtitleAligner, "_SubtitleAligner__run_process",
                    return_value=completed) as run_process:
                ok, message = SubtitleAligner._SubtitleAligner__extract_reference_subtitle(
                    media, 0, output, max_bytes=10
                )

        self.assertFalse(ok)
        self.assertIn("大小限制", message)
        command = run_process.call_args.args[0]
        self.assertEqual(command[command.index("-fs") + 1], "11")
