# -*- coding: utf-8 -*-

import os
import sys
import tempfile
import types
from unittest import TestCase

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
