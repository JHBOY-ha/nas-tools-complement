import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from difflib import SequenceMatcher
from statistics import median

import log
from app.utils import ExceptionUtils
from app.utils.llm_client import LLMClient
from config import Config


class _SubtitleProcessCanceled(Exception):
    """Raised internally when a task asks a child process to stop."""


class SubtitleAligner:
    """
    使用目标视频内嵌文本字幕作为参考，对上传外挂字幕做整体或多锚点分段时间轴修正。
    """
    _lock = threading.BoundedSemaphore(1)
    _supported_exts = [".srt", ".vtt"]
    _text_codecs = {
        "subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "hdmv_text_subtitle"
    }
    _image_codecs = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
    _chinese_marks = [
        "zh", "chi", "zho", "chs", "cht", "cn", "sc", "tc", "chinese",
        "中文", "简", "繁", "国语", "中字"
    ]
    _english_marks = ["en", "eng", "english", "英文", "英语"]
    _japanese_marks = ["ja", "jpn", "jp", "japanese", "日文", "日语", "日本語"]
    _korean_marks = ["ko", "kor", "kr", "korean", "韩文", "韩语", "한국어"]

    _min_anchors = 5
    _min_match_score = 0.72
    _stable_offset_ms = 500
    _min_avg_score = 0.78
    _min_stretch_ratio = 0.50
    _max_stretch_ratio = 1.80
    _max_offset_jump_ms = 5 * 60 * 1000
    _ffprobe_timeout = 20
    _ffmpeg_timeout = 60
    _max_cues = 2500
    _reference_search_window = 350
    _max_match_comparisons = 150000
    _candidate_limit_per_cue = 24
    _min_anchor_coverage = 0.15
    _minimum_cue_duration_ms = 200
    _max_aligned_anchor_residual_ms = 2000
    _llm_translation_cache = {}
    _llm_translation_cache_ttl = 60 * 60
    _reference_cache_lock = threading.RLock()
    _reference_cache_ttl = 30 * 24 * 60 * 60
    _reference_cache_version = "embedded-text-v1"
    _ffmpeg_version = None

    @classmethod
    def align_subtitle(cls, subtitle_file, media_file, align_mode="auto", cancel_check=None,
                       ffprobe_timeout=None, ffmpeg_timeout=None, llm_timeout=180,
                       llm_max_batches=8, remaining_budget=None,
                       temporary_dir=None, reference_max_bytes=20 * 1024 * 1024,
                       reference_cache_max_bytes=16 * 1024 * 1024):
        """
        返回 {"applied": bool, "skipped": bool, "message": str, "mode": str, ...}
        低置信度或环境不可用时只跳过，不抛出业务异常。
        """
        align_mode = str(align_mode or "auto").lower()
        if align_mode not in ["auto", "offset", "segmented", "llm"]:
            return cls.__skip("字幕对齐模式无效")
        if not cls.__is_enabled():
            return cls.__skip("字幕自动对齐未启用")
        if not subtitle_file or not media_file:
            return cls.__skip("缺少字幕或目标媒体文件")
        sub_ext = os.path.splitext(subtitle_file)[-1].lower()
        if sub_ext not in cls._supported_exts:
            return cls.__skip("当前仅支持 srt/vtt 自动对齐")
        if not os.path.exists(subtitle_file) or not os.path.isfile(subtitle_file):
            return cls.__skip("字幕文件不存在")
        if not os.path.exists(media_file) or not os.path.isfile(media_file):
            return cls.__skip("目标媒体文件不存在")
        if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
            return cls.__skip("ffmpeg/ffprobe 不可用")
        if not cls._lock.acquire(blocking=False):
            return cls.__skip("已有字幕自动对齐任务正在运行")
        try:
            def bounded_timeout(configured, fallback):
                timeout = float(fallback if configured is None else configured)
                if callable(remaining_budget):
                    remaining = float(remaining_budget())
                    if remaining <= 0:
                        raise _SubtitleProcessCanceled()
                    timeout = min(timeout, remaining)
                return max(timeout, 0.1)

            source_language = cls.__detect_subtitle_language(subtitle_file)
            stream = cls.__select_reference_stream(
                media_file,
                preferred_language=source_language,
                allow_cross_language=align_mode == "llm",
                cancel_check=cancel_check,
                timeout=bounded_timeout(ffprobe_timeout, cls._ffprobe_timeout)
            )
            if not stream:
                if align_mode == "llm":
                    return cls.__skip("未找到可用的文本字幕参考轨")
                return cls.__skip("未找到同语种文本字幕参考轨")
            # Task callers place this directory under their persisted staging
            # tree so extracted references share the same quota and crash
            # cleanup boundary instead of leaking onto the system temp volume.
            temporary_parent = os.path.abspath(
                temporary_dir or os.path.dirname(os.path.abspath(subtitle_file))
            )
            os.makedirs(temporary_parent, exist_ok=True)
            with tempfile.TemporaryDirectory(
                    prefix=".subtitle-reference-", dir=temporary_parent) as tmpdir:
                reference_file = os.path.join(tmpdir, "reference.srt")
                ok, msg, cache_hit = cls.__get_or_extract_reference(
                    media_file, stream.get("index"), reference_file,
                    cancel_check=cancel_check,
                    timeout=bounded_timeout(ffmpeg_timeout, cls._ffmpeg_timeout),
                    max_bytes=reference_max_bytes,
                    cache_max_bytes=min(
                        max(int(reference_cache_max_bytes or 0), 0),
                        256 * 1024 * 1024
                    )
                )
                if not ok:
                    return cls.__skip(msg)
                reference_language = stream.get("_language") or "unknown"
                ret = cls.align_with_reference_file(
                    subtitle_file,
                    reference_file,
                    align_mode="auto" if align_mode == "llm" else align_mode,
                    source_language=source_language,
                    reference_language=reference_language,
                    allow_llm=align_mode == "llm",
                    cancel_check=cancel_check,
                    llm_timeout=bounded_timeout(llm_timeout, 180),
                    llm_max_batches=llm_max_batches
                )
                ret["stream_index"] = stream.get("index")
                ret["source_language"] = source_language
                ret["reference_language"] = reference_language
                ret["reference_cache_hit"] = cache_hit
                return ret
        except _SubtitleProcessCanceled:
            return cls.__skip("任务已取消")
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return cls.__skip(f"自动对齐失败：{str(e)}")
        finally:
            cls._lock.release()

    @classmethod
    def align_with_reference_file(cls, subtitle_file, reference_file, align_mode="auto",
                                  source_language=None, reference_language=None, allow_llm=False,
                                  cancel_check=None, llm_timeout=180, llm_max_batches=8):
        """
        纯文件对齐入口，便于单元测试；成功时原地重写 subtitle_file。
        """
        align_mode = str(align_mode or "auto").lower()
        if align_mode not in ["auto", "offset", "segmented"]:
            return cls.__skip("字幕对齐模式无效")
        source = cls.parse_file(subtitle_file)
        reference = cls.parse_file(reference_file)
        if not source.get("cues") or not reference.get("cues"):
            return cls.__skip("字幕内容为空，跳过自动对齐")
        if len(source.get("cues")) > cls._max_cues or len(reference.get("cues")) > cls._max_cues:
            return cls.__skip("字幕行数过多，跳过自动对齐")
        source_language = cls.__normalize_language(source_language)
        reference_language = cls.__normalize_language(reference_language)
        if source_language == "unknown":
            source_language = cls.__detect_cues_language(source.get("cues"))
        if reference_language == "unknown":
            reference_language = cls.__detect_cues_language(reference.get("cues"))
        cross_language = not cls.__is_same_language(source_language, reference_language)
        original_reference_cues = reference.get("cues")
        translated_ids = set()
        remaining_llm_batches = 0
        if cross_language:
            if not allow_llm:
                return cls.__skip("参考字幕与上传字幕语言不同，未启用 LLM 跨语言对齐")
            total_batches = max(1, min(int(llm_max_batches or 8), 20))
            initial_batches = max(1, (total_batches + 1) // 2)
            initial_timeout = float(llm_timeout) * initial_batches / total_batches
            initial_selection = cls.__select_translation_cues(
                original_reference_cues,
                cls.__llm_batch_size() * initial_batches
            )
            translated_cues, translate_msg = cls.__translate_reference_cues(
                original_reference_cues,
                target_language=source_language,
                cancel_check=cancel_check,
                timeout=initial_timeout,
                max_batches=initial_batches,
                selected_cues=initial_selection
            )
            if not translated_cues:
                return cls.__skip(translate_msg or "LLM 翻译参考字幕失败")
            reference["cues"] = translated_cues
            translated_ids = {
                int(cue.get("_source_index")) for cue in initial_selection
            }
            remaining_llm_batches = total_batches - initial_batches
        anchors, budget_exhausted = cls.__match_anchors(source.get("cues"), reference.get("cues"))
        if budget_exhausted:
            return cls.__skip("字幕匹配超出处理预算，跳过自动对齐")
        valid, reason, diagnostics = cls.__validate_anchors(
            anchors, source.get("cues")
        )
        if not valid and cross_language and remaining_llm_batches > 0:
            remaining_candidates = [
                dict(cue, _source_index=index)
                for index, cue in enumerate(original_reference_cues)
                if index not in translated_ids
            ]
            supplemental_selection = cls.__select_translation_cues(
                remaining_candidates,
                cls.__llm_batch_size() * remaining_llm_batches
            )
            supplemental = None
            supplemental_message = ""
            if supplemental_selection:
                supplemental, supplemental_message = cls.__translate_reference_cues(
                    original_reference_cues,
                    target_language=source_language,
                    cancel_check=cancel_check,
                    timeout=max(float(llm_timeout) - initial_timeout, 0.1),
                    max_batches=remaining_llm_batches,
                    selected_cues=supplemental_selection
                )
            if supplemental:
                for cue in supplemental_selection:
                    cue_id = int(cue.get("_source_index"))
                    reference["cues"][cue_id]["text"] = supplemental[cue_id]["text"]
                anchors, budget_exhausted = cls.__match_anchors(
                    source.get("cues"), reference.get("cues")
                )
                if budget_exhausted:
                    return cls.__skip("字幕匹配超出处理预算，跳过自动对齐")
                valid, reason, diagnostics = cls.__validate_anchors(
                    anchors, source.get("cues")
                )
            elif supplemental_message:
                reason = supplemental_message
        if not valid:
            result = cls.__skip(reason)
            result.update({
                key: diagnostics.get(key) for key in [
                    "confidence", "inliers", "outliers", "coverage", "residual_p95_ms"
                ]
            })
            return result
        robust_anchors = diagnostics.get("anchors") or anchors
        aligned_cues, mode, model = cls.__align_cues(
            source.get("cues"), robust_anchors,
            align_mode=align_mode, diagnostics=diagnostics
        )
        valid_timeline, timeline_reason, model_residual_p95 = cls.__validate_aligned_timeline(
            source.get("cues"), aligned_cues, robust_anchors
        )
        if model_residual_p95 is not None:
            diagnostics["residual_p95_ms"] = model_residual_p95
        if not valid_timeline:
            result = cls.__skip(timeline_reason)
            result.update({
                key: diagnostics.get(key) for key in [
                    "confidence", "inliers", "outliers", "coverage", "residual_p95_ms"
                ]
            })
            return result
        source["cues"] = aligned_cues
        cls.write_file(subtitle_file, source)
        return {
            "applied": True,
            "skipped": False,
            "message": "自动对齐完成",
            "mode": mode,
            "model": model,
            "anchors": len(robust_anchors),
            "confidence": diagnostics.get("confidence"),
            "inliers": diagnostics.get("inliers"),
            "outliers": diagnostics.get("outliers"),
            "coverage": diagnostics.get("coverage"),
            "residual_p95_ms": diagnostics.get("residual_p95_ms"),
            "cross_language": cross_language,
            "source_language": source_language,
            "reference_language": reference_language
        }

    @classmethod
    def parse_file(cls, path):
        ext = os.path.splitext(path)[-1].lower()
        with open(path, "r", encoding="utf-8-sig") as file_obj:
            text = file_obj.read()
        if ext == ".vtt":
            return cls.__parse_vtt(text)
        return cls.__parse_srt(text)

    @classmethod
    def write_file(cls, path, data):
        ext = os.path.splitext(path)[-1].lower()
        text = cls.__format_vtt(data) if ext == ".vtt" else cls.__format_srt(data)
        target_dir = os.path.dirname(os.path.abspath(path)) or "."
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(prefix=".subtitle-align-", suffix=ext or ".tmp", dir=target_dir)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file_obj:
                file_obj.write(text)
            os.replace(tmp_path, path)
            tmp_path = None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    @classmethod
    def __is_enabled(cls):
        subtitle = Config().get_config("subtitle") or {}
        alignment = subtitle.get("alignment") or {}
        return alignment.get("enable", True) is not False

    @staticmethod
    def __skip(message):
        return {
            "applied": False, "skipped": True, "message": message,
            "mode": "skip", "model": "none", "anchors": 0,
            "confidence": 0.0, "inliers": 0, "outliers": 0,
            "coverage": 0.0, "residual_p95_ms": None
        }

    @classmethod
    def __select_reference_stream(cls, media_file, preferred_language="zh-CN", allow_cross_language=False,
                                  cancel_check=None, timeout=None):
        try:
            ret = cls.__run_process(
                ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", media_file],
                timeout=timeout or cls._ffprobe_timeout,
                cancel_check=cancel_check
            )
            if ret.returncode != 0 or not ret.stdout:
                return None
            streams = json.loads(ret.stdout).get("streams") or []
            candidates = []
            for stream in streams:
                if str(stream.get("codec_type") or "").lower() != "subtitle":
                    continue
                codec = str(stream.get("codec_name") or "").lower()
                if codec in cls._image_codecs or codec not in cls._text_codecs:
                    continue
                language = cls.__stream_language(stream)
                if not allow_cross_language and not cls.__is_same_language(preferred_language, language):
                    continue
                stream["_language"] = language
                candidates.append(stream)
            if not candidates:
                return None
            candidates.sort(
                key=lambda item: cls.__stream_score(item, preferred_language, allow_cross_language),
                reverse=True
            )
            return candidates[0]
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return None

    @classmethod
    def __stream_score(cls, stream, preferred_language, allow_cross_language):
        score = 0
        if cls.__is_same_language(preferred_language, stream.get("_language")):
            score += 100
        if cls.__stream_chinese_score(stream) > 0:
            score += 10
        disposition = stream.get("disposition") or {}
        if disposition.get("default"):
            score += 5
        if allow_cross_language:
            score += max(0, 1000 - int(stream.get("index") or 0)) / 1000
        return score

    @classmethod
    def __stream_language(cls, stream):
        tags = stream.get("tags") or {}
        text = " ".join([
            str(tags.get("language") or ""),
            str(tags.get("title") or ""),
            str(stream.get("language") or ""),
            str(stream.get("Title") or ""),
            str(stream.get("DisplayTitle") or "")
        ]).lower()
        return cls.__detect_language_from_text(text)

    @classmethod
    def __stream_chinese_score(cls, stream):
        tags = stream.get("tags") or {}
        text = " ".join([
            str(tags.get("language") or ""),
            str(tags.get("title") or ""),
            str(stream.get("language") or ""),
            str(stream.get("Title") or ""),
            str(stream.get("DisplayTitle") or "")
        ]).lower()
        score = 0
        for mark in cls._chinese_marks:
            if mark.lower() in text:
                score += 1
        return score

    @classmethod
    def __detect_subtitle_language(cls, subtitle_file):
        name_language = cls.__detect_language_from_text(os.path.basename(subtitle_file))
        if name_language != "unknown":
            return name_language
        try:
            data = cls.parse_file(subtitle_file)
            return cls.__detect_cues_language(data.get("cues") or [])
        except Exception:
            return "zh-CN"

    @classmethod
    def __detect_cues_language(cls, cues):
        sample = "\n".join([str(cue.get("text") or "") for cue in (cues or [])[:80]])
        detected = cls.__detect_language_from_text(sample)
        return detected if detected != "unknown" else "zh-CN"

    @classmethod
    def __detect_language_from_text(cls, text):
        text = str(text or "").lower()
        for mark in cls._chinese_marks:
            if mark.lower() in text:
                return "zh-CN"
        for mark in cls._english_marks:
            if re.search(rf"(^|[.\-_\s\[(]){re.escape(mark.lower())}($|[.\-_\s\])])", text):
                return "eng"
        for mark in cls._japanese_marks:
            if mark.lower() in text:
                return "jpn"
        for mark in cls._korean_marks:
            if mark.lower() in text:
                return "kor"
        if re.search(r"[\u4e00-\u9fff]", text):
            return "zh-CN"
        if re.search(r"[\u3040-\u30ff]", text):
            return "jpn"
        if re.search(r"[\uac00-\ud7af]", text):
            return "kor"
        letters = re.findall(r"[a-zA-Z]", text)
        if len(letters) >= 20:
            return "eng"
        return "unknown"

    @classmethod
    def __normalize_language(cls, language):
        text = str(language or "").strip().lower()
        if not text:
            return "unknown"
        if text in ["zh", "zh-cn", "zho", "chi", "chs", "cn", "sc", "zh-hans"]:
            return "zh-CN"
        if text in ["zh-tw", "cht", "tc", "zh-hant"]:
            return "zh-TW"
        if text in ["en", "eng", "english"]:
            return "eng"
        if text in ["ja", "jpn", "jp", "japanese"]:
            return "jpn"
        if text in ["ko", "kor", "kr", "korean"]:
            return "kor"
        return text

    @classmethod
    def __is_same_language(cls, left, right):
        left = cls.__normalize_language(left)
        right = cls.__normalize_language(right)
        if left == "unknown" or right == "unknown":
            return False
        if left in ["zh-CN", "zh-TW"] and right in ["zh-CN", "zh-TW"]:
            return True
        return left == right

    @classmethod
    def __extract_reference_subtitle(cls, media_file, stream_index, output_file,
                                     cancel_check=None, timeout=None, max_bytes=None):
        if stream_index is None:
            return False, "参考字幕轨索引无效"
        try:
            max_bytes = max(int(max_bytes or 20 * 1024 * 1024), 1)
            ret = cls.__run_process(
                [
                    "ffmpeg", "-nostdin", "-threads", "1", "-y", "-v", "error", "-i", media_file,
                    "-map", f"0:{stream_index}", "-c:s", "srt",
                    "-fs", str(max_bytes + 1), output_file
                ],
                timeout=timeout or cls._ffmpeg_timeout,
                cancel_check=cancel_check
            )
            output_size = os.path.getsize(output_file) if os.path.exists(output_file) else 0
            if output_size > max_bytes:
                return False, "参考字幕轨超过大小限制"
            if ret.returncode != 0 or output_size <= 0:
                err = (ret.stderr or "").strip()
                if err:
                    log.warn(f"【Subtitle】抽取参考字幕失败：{err}")
                return False, "参考字幕轨无法转换为文本字幕"
            return True, ""
        except subprocess.TimeoutExpired:
            return False, "抽取参考字幕超时"
        except _SubtitleProcessCanceled:
            return False, "任务已取消"
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return False, f"抽取参考字幕失败：{str(e)}"

    @classmethod
    def __get_or_extract_reference(cls, media_file, stream_index, output_file,
                                   cancel_check=None, timeout=None, max_bytes=None,
                                   cache_max_bytes=0):
        """Reuse only complete, fingerprint-matched embedded text extraction."""
        fingerprint = cls.__reference_fingerprint(media_file, stream_index, cancel_check)
        cache_root = os.path.join(Config().get_temp_path(), "subtitle-reference-cache")
        cache_key = hashlib.sha256(json.dumps(
            fingerprint, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")).hexdigest()
        cache_file = os.path.join(cache_root, cache_key + ".srt")
        manifest_file = os.path.join(cache_root, cache_key + ".json")
        if cache_max_bytes > 0:
            with cls._reference_cache_lock:
                cls.__prune_reference_cache(cache_root, cache_max_bytes)
                try:
                    with open(manifest_file, "r", encoding="utf-8") as file_obj:
                        manifest = json.load(file_obj)
                    fresh = time.time() - float(manifest.get("created_at") or 0) <= cls._reference_cache_ttl
                    if fresh and manifest.get("fingerprint") == fingerprint \
                            and os.path.isfile(cache_file) \
                            and 0 < os.path.getsize(cache_file) <= int(max_bytes or 0):
                        shutil.copyfile(cache_file, output_file)
                        os.utime(manifest_file, None)
                        return True, "", True
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    pass
        ok, message = cls.__extract_reference_subtitle(
            media_file, stream_index, output_file,
            cancel_check=cancel_check, timeout=timeout, max_bytes=max_bytes
        )
        if not ok or cache_max_bytes <= 0 or not os.path.isfile(output_file):
            return ok, message, False
        if cancel_check and cancel_check():
            return False, "任务已取消", False
        with cls._reference_cache_lock:
            os.makedirs(cache_root, exist_ok=True)
            temp_cache = cache_file + ".tmp"
            temp_manifest = manifest_file + ".tmp"
            try:
                shutil.copyfile(output_file, temp_cache)
                with open(temp_manifest, "w", encoding="utf-8") as file_obj:
                    json.dump({
                        "fingerprint": fingerprint,
                        "created_at": time.time(),
                        "size": os.path.getsize(temp_cache)
                    }, file_obj, ensure_ascii=False, sort_keys=True)
                os.replace(temp_cache, cache_file)
                os.replace(temp_manifest, manifest_file)
                cls.__prune_reference_cache(cache_root, cache_max_bytes)
            finally:
                for path in [temp_cache, temp_manifest]:
                    if os.path.exists(path):
                        try:
                            os.remove(path)
                        except OSError:
                            pass
        return True, "", False

    @classmethod
    def __reference_fingerprint(cls, media_file, stream_index, cancel_check=None):
        stat = os.stat(media_file)
        return {
            "path": os.path.normcase(os.path.abspath(media_file)),
            "size": stat.st_size,
            "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1000000000)),
            "stream_index": int(stream_index),
            "cache_version": cls._reference_cache_version,
            "ffmpeg_version": cls.__get_ffmpeg_version(cancel_check)
        }

    @classmethod
    def __get_ffmpeg_version(cls, cancel_check=None):
        if cls._ffmpeg_version is not None:
            return cls._ffmpeg_version
        try:
            result = cls.__run_process(
                ["ffmpeg", "-version"], timeout=3, cancel_check=cancel_check
            )
            cls._ffmpeg_version = (result.stdout or "").splitlines()[0].strip() or "unknown"
        except Exception:
            cls._ffmpeg_version = "unknown"
        return cls._ffmpeg_version

    @classmethod
    def __prune_reference_cache(cls, cache_root, max_bytes):
        if not os.path.isdir(cache_root):
            return
        now = time.time()
        entries = []
        total = 0
        for name in os.listdir(cache_root):
            if not name.endswith(".srt"):
                continue
            data_file = os.path.join(cache_root, name)
            manifest_file = os.path.splitext(data_file)[0] + ".json"
            try:
                size = os.path.getsize(data_file)
                modified = os.path.getmtime(manifest_file)
            except OSError:
                for path in [data_file, manifest_file]:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                continue
            if now - modified > cls._reference_cache_ttl:
                for path in [data_file, manifest_file]:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                continue
            total += size
            entries.append((modified, size, data_file, manifest_file))
        for name in os.listdir(cache_root):
            if name.endswith(".json"):
                manifest_file = os.path.join(cache_root, name)
                data_file = os.path.splitext(manifest_file)[0] + ".srt"
                if not os.path.isfile(data_file):
                    try:
                        os.remove(manifest_file)
                    except OSError:
                        pass
        for _, size, data_file, manifest_file in sorted(entries):
            if total <= max_bytes:
                break
            for path in [data_file, manifest_file]:
                try:
                    os.remove(path)
                except OSError:
                    pass
            total -= size

    @staticmethod
    def __stop_process(process):
        """Terminate a child and escalate to kill without blocking indefinitely."""
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=2)
            return
        except Exception:
            pass
        try:
            process.kill()
            process.wait(timeout=2)
        except Exception:
            pass

    @classmethod
    def __run_process(cls, command, timeout, cancel_check=None):
        """Run ffmpeg/ffprobe with cooperative cancellation and a hard deadline."""
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        try:
            while True:
                if cancel_check and cancel_check():
                    cls.__stop_process(process)
                    raise _SubtitleProcessCanceled()
                elapsed = time.monotonic() - started
                if elapsed >= float(timeout):
                    cls.__stop_process(process)
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = process.communicate(timeout=min(0.25, max(0.05, float(timeout) - elapsed)))
                    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    continue
        except Exception:
            cls.__stop_process(process)
            raise

    @classmethod
    def __parse_srt(cls, text):
        cues = []
        for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip()):
            lines = [line for line in block.split("\n") if line.strip()]
            if not lines:
                continue
            time_index = cls.__find_time_line(lines)
            if time_index < 0:
                continue
            start_ms, end_ms = cls.__parse_time_range(lines[time_index])
            cues.append({
                "start": start_ms,
                "end": end_ms,
                "text": "\n".join(lines[time_index + 1:]),
                "identifier": []
            })
        return {"format": "srt", "cues": cues}

    @classmethod
    def __parse_vtt(cls, text):
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        header = "WEBVTT"
        body = normalized
        if normalized.lstrip("\ufeff").startswith("WEBVTT"):
            parts = normalized.split("\n", 1)
            header = parts[0].strip() or "WEBVTT"
            body = parts[1] if len(parts) > 1 else ""
        cues = []
        blocks = []
        for block in re.split(r"\n\s*\n", body.strip()):
            lines = [line for line in block.split("\n") if line.strip()]
            if not lines:
                continue
            time_index = cls.__find_time_line(lines)
            if time_index < 0:
                blocks.append({"type": "raw", "lines": lines})
                continue
            start_ms, end_ms = cls.__parse_time_range(lines[time_index])
            cue = {
                "start": start_ms,
                "end": end_ms,
                "text": "\n".join(lines[time_index + 1:]),
                "identifier": lines[:time_index],
                "settings": cls.__parse_time_settings(lines[time_index])
            }
            cues.append(cue)
            blocks.append({"type": "cue", "cue": cue})
        return {"format": "vtt", "header": header, "cues": cues, "blocks": blocks}

    @staticmethod
    def __find_time_line(lines):
        for index, line in enumerate(lines):
            if "-->" in line:
                return index
        return -1

    @classmethod
    def __parse_time_range(cls, line):
        parts = line.split("-->")
        if len(parts) < 2:
            raise ValueError("invalid subtitle time line")
        return cls.__parse_timestamp(parts[0].strip()), cls.__parse_timestamp(parts[1].strip().split()[0])

    @staticmethod
    def __parse_time_settings(line):
        parts = line.split("-->")
        if len(parts) < 2:
            return ""
        end_part = parts[1].strip()
        end_tokens = end_part.split(None, 1)
        if len(end_tokens) < 2:
            return ""
        return end_tokens[1].strip()

    @staticmethod
    def __parse_timestamp(value):
        match = re.search(r"(?:(\d+):)?(\d{2}):(\d{2})[,.](\d{3})", value)
        if not match:
            raise ValueError(f"invalid subtitle timestamp: {value}")
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        millis = int(match.group(4))
        return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis

    @classmethod
    def __format_srt(cls, data):
        blocks = []
        for index, cue in enumerate(data.get("cues") or [], 1):
            blocks.append("\n".join([
                str(index),
                f"{cls.__format_timestamp(cue.get('start'), ',')} --> {cls.__format_timestamp(cue.get('end'), ',')}",
                cue.get("text") or ""
            ]))
        return "\n\n".join(blocks).strip() + "\n"

    @classmethod
    def __format_vtt(cls, data):
        blocks = [data.get("header") or "WEBVTT"]
        source_blocks = data.get("blocks") or [{"type": "cue", "cue": cue} for cue in data.get("cues") or []]
        for block in source_blocks:
            if block.get("type") == "raw":
                blocks.append("\n".join(block.get("lines") or []))
                continue
            cue = block.get("cue") or {}
            lines = list(cue.get("identifier") or [])
            time_line = f"{cls.__format_timestamp(cue.get('start'), '.')} --> {cls.__format_timestamp(cue.get('end'), '.')}"
            if cue.get("settings"):
                time_line = f"{time_line} {cue.get('settings')}"
            lines.append(time_line)
            if cue.get("text"):
                lines.extend(str(cue.get("text")).split("\n"))
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks).strip() + "\n"

    @staticmethod
    def __format_timestamp(value, separator):
        value = max(0, int(round(value or 0)))
        millis = value % 1000
        total_seconds = value // 1000
        seconds = total_seconds % 60
        total_minutes = total_seconds // 60
        minutes = total_minutes % 60
        hours = total_minutes // 60
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"

    @classmethod
    def __translate_reference_cues(cls, reference_cues, target_language, cancel_check=None,
                                   timeout=180, max_batches=8, selected_cues=None):
        if not cls.__is_llm_alignment_enabled():
            return None, "LLM 跨语言对齐未启用"
        client = LLMClient()
        if not client.is_ready(require_enable=False):
            return None, "LLM 配置不完整"
        translated_cues = [dict(cue) for cue in reference_cues]
        target_name = cls.__language_display_name(target_language)
        batch_count = 0
        started = time.monotonic()
        try:
            total_timeout = float(180 if timeout is None else timeout)
        except (TypeError, ValueError):
            total_timeout = 180.0
        if total_timeout <= 0:
            return None, "LLM 字幕对齐超时"
        deadline = started + total_timeout
        max_batches = max(1, min(int(max_batches or 8), 20))
        if selected_cues is None:
            selected_cues = cls.__select_translation_cues(
                reference_cues, cls.__llm_batch_size() * max_batches
            )
        if not selected_cues:
            return None, "没有可用的参考字幕样本"
        for batch in cls.__iter_translation_batches(selected_cues):
            if cancel_check and cancel_check():
                return None, "任务已取消"
            if batch_count >= max_batches:
                return None, f"LLM 翻译批次数超过限制（{max_batches}）"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, "LLM 字幕对齐超时"
            batch_count += 1
            cache_key = cls.__translation_cache_key(target_language, batch)
            cached = cls.__get_translation_cache(cache_key)
            if cached is None:
                translated, msg = cls.__request_translation_batch(
                    client,
                    target_name,
                    batch,
                    timeout=remaining
                )
                if cancel_check and cancel_check():
                    return None, "任务已取消"
                if time.monotonic() >= deadline:
                    return None, "LLM 字幕对齐超时"
                if not translated:
                    return None, msg
                cls.__set_translation_cache(cache_key, translated)
            else:
                translated = cached
            for cue_id, text in translated.items():
                if cue_id < 0 or cue_id >= len(translated_cues):
                    return None, "LLM 翻译结果索引异常"
                translated_cues[cue_id]["text"] = text
            if time.monotonic() >= deadline:
                return None, "LLM 字幕对齐超时"
        log.info("【Subtitle】LLM参考字幕翻译完成：protocol=openai, batches=%s" % batch_count)
        return translated_cues, ""

    @classmethod
    def __select_translation_cues(cls, cues, limit):
        """Uniformly sample information-rich cues instead of translating a film."""
        candidates = []
        for index, cue in enumerate(cues or []):
            text = cls.__clean_llm_subtitle_text(cue.get("text"))
            normalized = cls.__normalize_text(text)
            if len(normalized) < 4:
                continue
            score = min(len(set(normalized)), 40) + min(len(normalized), 80) / 10
            source_index = int(cue.get("_source_index", index))
            candidates.append((source_index, score, dict(cue, _source_index=source_index)))
        limit = max(1, int(limit or 1))
        if len(candidates) <= limit:
            return [item[2] for item in candidates]
        selected = []
        # One best cue per temporal bucket keeps coverage while preferring
        # names/numbers/content over repeated interjections.
        for bucket in range(limit):
            start = bucket * len(candidates) // limit
            end = max((bucket + 1) * len(candidates) // limit, start + 1)
            selected.append(max(candidates[start:end], key=lambda item: item[1])[2])
        return selected

    @classmethod
    def __is_llm_alignment_enabled(cls):
        subtitle = Config().get_config("subtitle") or {}
        alignment = subtitle.get("alignment") or {}
        return alignment.get("llm_enable", True) is not False

    @classmethod
    def __iter_translation_batches(cls, cues):
        batch_size = cls.__llm_batch_size()
        char_limit = 6000
        batch = []
        char_count = 0
        for index, cue in enumerate(cues or []):
            text = cls.__clean_llm_subtitle_text(cue.get("text"))
            if not text:
                continue
            item = {"id": int(cue.get("_source_index", index)), "text": text}
            item_len = len(text)
            if batch and (len(batch) >= batch_size or char_count + item_len > char_limit):
                yield batch
                batch = []
                char_count = 0
            batch.append(item)
            char_count += item_len
        if batch:
            yield batch

    @classmethod
    def __llm_batch_size(cls):
        subtitle = Config().get_config("subtitle") or {}
        alignment = subtitle.get("alignment") or {}
        try:
            value = int(alignment.get("llm_batch_size") or 40)
        except Exception:
            value = 40
        return max(1, min(value, 40))

    @classmethod
    def __request_translation_batch(cls, client, target_language_name, batch, timeout=None):
        system_prompt = (
            "You translate subtitle cues for timestamp alignment. "
            "Return strict JSON only. Do not add explanations. "
            "Preserve names, numbers, punctuation intent, and concise subtitle style."
        )
        user_prompt = json.dumps({
            "target_language": target_language_name,
            "output_schema": [{"id": 1, "text": "translated subtitle text"}],
            "items": batch
        }, ensure_ascii=False)
        result = client.complete_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=2048,
            timeout=timeout,
            max_retries=0
        )
        if isinstance(result, dict):
            result = result.get("items") or result.get("results") or result.get("translations")
        if not isinstance(result, list):
            return None, "LLM 翻译返回非法 JSON"
        expected_ids = {item.get("id") for item in batch}
        translated = {}
        for item in result:
            if not isinstance(item, dict):
                continue
            try:
                cue_id = int(item.get("id"))
            except Exception:
                continue
            text = cls.__clean_llm_subtitle_text(item.get("text"))
            if cue_id in expected_ids and text:
                translated[cue_id] = text
        if set(translated.keys()) != expected_ids:
            return None, "LLM 翻译返回不完整"
        return translated, ""

    @classmethod
    def __translation_cache_key(cls, target_language, batch):
        payload = json.dumps({
            "protocol": "openai",
            "target_language": cls.__normalize_language(target_language),
            "items": batch
        }, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def __get_translation_cache(cls, key):
        cached = cls._llm_translation_cache.get(key)
        if not cached:
            return None
        created_at, value = cached
        if time.time() - created_at > cls._llm_translation_cache_ttl:
            cls._llm_translation_cache.pop(key, None)
            return None
        return value

    @classmethod
    def __set_translation_cache(cls, key, value):
        cls._llm_translation_cache[key] = (time.time(), value)

    @staticmethod
    def __clean_llm_subtitle_text(text):
        text = str(text or "").strip()
        text = re.sub(r"\{\\[^}]*\}", "", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\s+", " ", text)
        return text[:500]

    @classmethod
    def __language_display_name(cls, language):
        language = cls.__normalize_language(language)
        names = {
            "zh-CN": "Simplified Chinese",
            "zh-TW": "Traditional Chinese",
            "eng": "English",
            "jpn": "Japanese",
            "kor": "Korean"
        }
        return names.get(language, language or "the source subtitle language")

    @classmethod
    def __match_anchors(cls, source_cues, reference_cues):
        """Build fuzzy candidates, then select the highest-scoring monotonic chain."""
        comparisons = 0
        normalized_references = [cls.__normalize_text(cue.get("text")) for cue in reference_cues]
        index = {}
        for reference_index, text in enumerate(normalized_references):
            for gram in cls.__text_ngrams(text):
                index.setdefault(gram, []).append(reference_index)
        candidate_groups = []
        for source_index, source in enumerate(source_cues):
            source_text = cls.__normalize_text(source.get("text"))
            if len(source_text) < 4:
                continue
            overlap = {}
            for gram in cls.__text_ngrams(source_text):
                for reference_index in index.get(gram, []):
                    overlap[reference_index] = overlap.get(reference_index, 0) + 1
            expected = int(source_index * len(reference_cues) / max(len(source_cues), 1))
            candidate_indices = sorted(
                overlap,
                key=lambda value: (-overlap[value], abs(value - expected), value)
            )[:cls._candidate_limit_per_cue]
            if not candidate_indices:
                half = cls._reference_search_window // 2
                candidate_indices = range(
                    max(0, expected - half), min(len(reference_cues), expected + half)
                )
            group = []
            for reference_index in candidate_indices:
                comparisons += 1
                if comparisons > cls._max_match_comparisons:
                    return [], True
                reference_text = normalized_references[reference_index]
                if len(reference_text) < 4:
                    continue
                score = SequenceMatcher(None, source_text, reference_text).ratio()
                if score >= cls._min_match_score:
                    group.append({
                        "source_index": source_index,
                        "reference_index": reference_index,
                        "source_time": source.get("start"),
                        "reference_time": reference_cues[reference_index].get("start"),
                        "score": score
                    })
            if group:
                candidate_groups.append(group)
        return cls.__monotonic_anchor_chain(candidate_groups, len(reference_cues)), False

    @staticmethod
    def __text_ngrams(text, size=3):
        if len(text) <= size:
            return {text} if text else set()
        return {text[index:index + size] for index in range(len(text) - size + 1)}

    @classmethod
    def __monotonic_anchor_chain(cls, groups, reference_count):
        """Weighted LIS using a Fenwick tree; one anchor per source cue."""
        tree = [None] * (max(reference_count, 1) + 2)
        nodes = []

        def query(position):
            best = None
            while position > 0:
                value = tree[position]
                if value and (not best or value[0] > best[0]):
                    best = value
                position -= position & -position
            return best

        def update(position, value):
            while position < len(tree):
                if not tree[position] or value[0] > tree[position][0]:
                    tree[position] = value
                position += position & -position

        for group in groups:
            pending = []
            for anchor in group:
                ref_position = int(anchor["reference_index"]) + 1
                previous = query(ref_position - 1)
                node_index = len(nodes)
                nodes.append({
                    "anchor": anchor,
                    "previous": previous[1] if previous else None,
                    "value": (previous[0] if previous else 0) + 1 + anchor["score"]
                })
                pending.append((ref_position, (nodes[node_index]["value"], node_index)))
            for position, value in pending:
                update(position, value)
        best = query(len(tree) - 1)
        chain = []
        node_index = best[1] if best else None
        while node_index is not None:
            node = nodes[node_index]
            chain.append(node["anchor"])
            node_index = node["previous"]
        return list(reversed(chain))

    @staticmethod
    def __normalize_text(text):
        text = str(text or "").lower()
        text = re.sub(r"\{\\[^}]*\}", "", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"&[a-z]+;", "", text)
        text = re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)
        return text

    @classmethod
    def __validate_anchors(cls, anchors, source_cues=None):
        if len(anchors) < cls._min_anchors:
            return False, "匹配锚点不足，跳过自动对齐", cls.__empty_diagnostics(anchors)
        slope, intercept = cls.__robust_linear_model(anchors)
        residuals = [
            anchor["reference_time"] - (slope * anchor["source_time"] + intercept)
            for anchor in anchors
        ]
        center = median(residuals)
        mad = median([abs(value - center) for value in residuals])
        threshold = max(500, 3 * 1.4826 * mad)
        inliers = [
            anchor for anchor, residual in zip(anchors, residuals)
            if abs(residual - center) <= threshold
        ]
        outliers = len(anchors) - len(inliers)
        if len(inliers) < cls._min_anchors:
            return False, "有效匹配锚点不足，跳过自动对齐", cls.__empty_diagnostics(anchors, outliers)
        avg_score = sum(anchor.get("score", 0) for anchor in inliers) / len(inliers)
        source_span = max(
            (source_cues or [{}])[-1].get("end", 0) - (source_cues or [{}])[0].get("start", 0), 1
        )
        coverage = max(inliers[-1]["source_time"] - inliers[0]["source_time"], 0) / source_span
        inlier_residuals = [
            abs(anchor["reference_time"] - (slope * anchor["source_time"] + intercept))
            for anchor in inliers
        ]
        residual_p95 = cls.__percentile(inlier_residuals, 0.95)
        diagnostics = {
            "anchors": inliers, "inliers": len(inliers), "outliers": outliers,
            "coverage": round(min(coverage, 1.0), 4),
            "confidence": round(min(1.0, avg_score * min(1.0, coverage / 0.5)), 4),
            "residual_p95_ms": int(round(residual_p95)),
            "slope": slope, "intercept": intercept
        }
        if avg_score < cls._min_avg_score:
            return False, "字幕文本匹配度不足，跳过自动对齐", diagnostics
        if coverage < cls._min_anchor_coverage:
            return False, "匹配锚点时间覆盖不足，跳过自动对齐", diagnostics
        for left, right in zip(inliers, inliers[1:]):
            source_delta = right.get("source_time") - left.get("source_time")
            reference_delta = right.get("reference_time") - left.get("reference_time")
            if source_delta <= 0 or reference_delta <= 0:
                return False, "锚点顺序异常，跳过自动对齐", diagnostics
            ratio = reference_delta / source_delta
            if ratio < cls._min_stretch_ratio or ratio > cls._max_stretch_ratio:
                return False, "分段变速超出安全范围，跳过自动对齐", diagnostics
        return True, "", diagnostics

    @staticmethod
    def __empty_diagnostics(anchors=None, outliers=0):
        return {
            "anchors": list(anchors or []), "inliers": len(anchors or []),
            "outliers": outliers, "coverage": 0.0, "confidence": 0.0,
            "residual_p95_ms": None, "slope": 1.0, "intercept": 0.0
        }

    @classmethod
    def __robust_linear_model(cls, anchors):
        pairs = []
        step = max(1, len(anchors) // 30)
        sampled = anchors[::step]
        if sampled[-1] is not anchors[-1]:
            sampled.append(anchors[-1])
        for left_index, left in enumerate(sampled):
            for right in sampled[left_index + 1:]:
                delta = right["source_time"] - left["source_time"]
                if delta > 0:
                    pairs.append((right["reference_time"] - left["reference_time"]) / delta)
        slope = median(pairs) if pairs else 1.0
        slope = min(max(slope, cls._min_stretch_ratio), cls._max_stretch_ratio)
        intercept = median([
            anchor["reference_time"] - slope * anchor["source_time"]
            for anchor in anchors
        ])
        return slope, intercept

    @staticmethod
    def __percentile(values, fraction):
        values = sorted(values or [0])
        position = max(0, min(len(values) - 1, int(round((len(values) - 1) * fraction))))
        return values[position]

    @classmethod
    def __align_cues(cls, cues, anchors, align_mode="auto", diagnostics=None):
        offsets = [anchor.get("reference_time") - anchor.get("source_time") for anchor in anchors]
        # Upper median preserves millisecond integer behavior for even-sized
        # anchor sets while remaining insensitive to extreme offsets.
        median_offset = sorted(offsets)[len(offsets) // 2]
        if align_mode == "offset":
            return [cls.__shift_cue(cue, median_offset) for cue in cues], "offset", "median_offset"
        if align_mode == "segmented":
            if cls.__segmentation_confirmed(anchors):
                controls = cls.__segment_controls(anchors)
                return [cls.__map_cue(cue, controls) for cue in cues], "segmented", "confirmed_segments"
            return [cls.__shift_cue(cue, median_offset) for cue in cues], "offset", "median_offset"
        offset_residual = cls.__percentile(
            [abs(value - median_offset) for value in offsets], 0.95
        )
        if offset_residual <= cls._stable_offset_ms:
            return [cls.__shift_cue(cue, median_offset) for cue in cues], "offset", "median_offset"
        diagnostics = diagnostics or {}
        slope = float(diagnostics.get("slope") or 1.0)
        intercept = float(diagnostics.get("intercept") or 0.0)
        residual_value = diagnostics.get("residual_p95_ms")
        affine_residual = float(
            offset_residual if residual_value is None else residual_value
        )
        if 0.95 <= slope <= 1.05 and affine_residual < offset_residual * 0.7:
            return [
                cls.__affine_cue(cue, slope, intercept) for cue in cues
            ], "affine", "linear_drift"
        controls = cls.__segment_controls(anchors)
        if len(controls) >= 2 and cls.__segmentation_confirmed(anchors):
            return [cls.__map_cue(cue, controls) for cue in cues], "segmented", "confirmed_segments"
        return [cls.__shift_cue(cue, median_offset) for cue in cues], "offset", "median_offset"

    @classmethod
    def __segment_controls(cls, anchors):
        """Collapse consecutive anchors into robust median segment controls."""
        if len(anchors) <= 2:
            return list(anchors)
        # Three agreeing anchors are the minimum evidence for a local control;
        # cap the number of controls so long films cannot overfit every line.
        group_count = max(2, min(32, len(anchors) // 3))
        controls = []
        for group_index in range(group_count):
            start = group_index * len(anchors) // group_count
            end = (group_index + 1) * len(anchors) // group_count
            group = anchors[start:end]
            source_time = median([anchor["source_time"] for anchor in group])
            offset = median([
                anchor["reference_time"] - anchor["source_time"]
                for anchor in group
            ])
            controls.append({
                "source_time": source_time,
                "reference_time": source_time + offset,
                "score": median([anchor.get("score", 0) for anchor in group])
            })
        return controls

    @classmethod
    def __segmentation_confirmed(cls, anchors):
        if len(anchors) < cls._min_anchors:
            return False
        offsets = [anchor["reference_time"] - anchor["source_time"] for anchor in anchors]
        window = min(3, max(2, len(offsets) // 2))
        # A segment is only accepted when multiple anchors on both sides agree
        # on the change; one isolated match can never create a time-axis knot.
        return abs(median(offsets[-window:]) - median(offsets[:window])) \
            > cls._stable_offset_ms

    @classmethod
    def __affine_cue(cls, cue, slope, intercept):
        start = max(0, slope * cue.get("start") + intercept)
        end = max(start + cls._minimum_cue_duration_ms, slope * cue.get("end") + intercept)
        return dict(cue, start=start, end=end)

    @classmethod
    def __shift_cue(cls, cue, offset):
        start = max(0, cue.get("start") + offset)
        end = max(start + cls._minimum_cue_duration_ms, cue.get("end") + offset)
        return dict(cue, start=start, end=end)

    @classmethod
    def __map_cue(cls, cue, anchors):
        original_duration = max(cls._minimum_cue_duration_ms, cue.get("end") - cue.get("start"))
        start = cls.__map_time(cue.get("start"), anchors)
        end = cls.__map_time(cue.get("end"), anchors)
        if end <= start:
            end = start + original_duration
        return dict(cue, start=max(0, start), end=max(start + cls._minimum_cue_duration_ms, end))

    @classmethod
    def __validate_aligned_timeline(cls, original, aligned, anchors=None):
        if len(original) != len(aligned):
            return False, "对齐结果数量异常，已保留原字幕", None
        previous_start = -1
        for before, after in zip(original, aligned):
            start = float(after.get("start") or 0)
            end = float(after.get("end") or 0)
            if start < previous_start:
                return False, "对齐后时间轴非单调，已保留原字幕", None
            if end - start < cls._minimum_cue_duration_ms:
                return False, "对齐后字幕时长过短，已保留原字幕", None
            original_duration = max(float(before.get("end") - before.get("start")), 1)
            ratio = (end - start) / original_duration
            if ratio < cls._min_stretch_ratio or ratio > cls._max_stretch_ratio:
                return False, "对齐后字幕伸缩超出安全范围，已保留原字幕", None
            previous_start = start
        residuals = []
        for anchor in anchors or []:
            try:
                source_index = int(anchor.get("source_index"))
                mapped_start = float(aligned[source_index].get("start") or 0)
                residuals.append(abs(mapped_start - float(anchor.get("reference_time") or 0)))
            except (IndexError, TypeError, ValueError):
                return False, "对齐后锚点索引异常，已保留原字幕", None
        residual_p95 = int(round(cls.__percentile(residuals, 0.95))) if residuals else None
        if residual_p95 is not None \
                and residual_p95 > cls._max_aligned_anchor_residual_ms:
            return False, "对齐后锚点残差过大，已保留原字幕", residual_p95
        return True, "", residual_p95

    @staticmethod
    def __map_time(value, anchors):
        if value <= anchors[0].get("source_time"):
            return value + anchors[0].get("reference_time") - anchors[0].get("source_time")
        if value >= anchors[-1].get("source_time"):
            return value + anchors[-1].get("reference_time") - anchors[-1].get("source_time")
        for left, right in zip(anchors, anchors[1:]):
            if left.get("source_time") <= value <= right.get("source_time"):
                source_delta = right.get("source_time") - left.get("source_time")
                reference_delta = right.get("reference_time") - left.get("reference_time")
                ratio = reference_delta / source_delta
                return left.get("reference_time") + (value - left.get("source_time")) * ratio
        return value
