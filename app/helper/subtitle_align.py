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

import log
from app.utils import ExceptionUtils
from app.utils.llm_client import LLMClient
from config import Config


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
    _llm_translation_cache = {}
    _llm_translation_cache_ttl = 60 * 60

    @classmethod
    def align_subtitle(cls, subtitle_file, media_file, align_mode="auto"):
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
            source_language = cls.__detect_subtitle_language(subtitle_file)
            stream = cls.__select_reference_stream(
                media_file,
                preferred_language=source_language,
                allow_cross_language=align_mode == "llm"
            )
            if not stream:
                if align_mode == "llm":
                    return cls.__skip("未找到可用的文本字幕参考轨")
                return cls.__skip("未找到同语种文本字幕参考轨")
            with tempfile.TemporaryDirectory() as tmpdir:
                reference_file = os.path.join(tmpdir, "reference.srt")
                ok, msg = cls.__extract_reference_subtitle(media_file, stream.get("index"), reference_file)
                if not ok:
                    return cls.__skip(msg)
                reference_language = stream.get("_language") or "unknown"
                ret = cls.align_with_reference_file(
                    subtitle_file,
                    reference_file,
                    align_mode="auto" if align_mode == "llm" else align_mode,
                    source_language=source_language,
                    reference_language=reference_language,
                    allow_llm=align_mode == "llm"
                )
                ret["stream_index"] = stream.get("index")
                ret["source_language"] = source_language
                ret["reference_language"] = reference_language
                return ret
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return cls.__skip(f"自动对齐失败：{str(e)}")
        finally:
            cls._lock.release()

    @classmethod
    def align_with_reference_file(cls, subtitle_file, reference_file, align_mode="auto",
                                  source_language=None, reference_language=None, allow_llm=False):
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
        if cross_language:
            if not allow_llm:
                return cls.__skip("参考字幕与上传字幕语言不同，未启用 LLM 跨语言对齐")
            translated_cues, translate_msg = cls.__translate_reference_cues(
                reference.get("cues"),
                target_language=source_language
            )
            if not translated_cues:
                return cls.__skip(translate_msg or "LLM 翻译参考字幕失败")
            reference["cues"] = translated_cues
        anchors, budget_exhausted = cls.__match_anchors(source.get("cues"), reference.get("cues"))
        if budget_exhausted:
            return cls.__skip("字幕匹配超出处理预算，跳过自动对齐")
        valid, reason = cls.__validate_anchors(anchors)
        if not valid:
            return cls.__skip(reason)
        aligned_cues, mode = cls.__align_cues(source.get("cues"), anchors, align_mode=align_mode)
        source["cues"] = aligned_cues
        cls.write_file(subtitle_file, source)
        return {
            "applied": True,
            "skipped": False,
            "message": "自动对齐完成",
            "mode": mode,
            "anchors": len(anchors),
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
        return {"applied": False, "skipped": True, "message": message, "mode": "skip", "anchors": 0}

    @classmethod
    def __select_reference_stream(cls, media_file, preferred_language="zh-CN", allow_cross_language=False):
        try:
            ret = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", media_file],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=cls._ffprobe_timeout,
                text=True
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
    def __extract_reference_subtitle(cls, media_file, stream_index, output_file):
        if stream_index is None:
            return False, "参考字幕轨索引无效"
        try:
            ret = subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error", "-i", media_file,
                    "-map", f"0:{stream_index}", "-c:s", "srt", output_file
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=cls._ffmpeg_timeout,
                text=True
            )
            if ret.returncode != 0 or not os.path.exists(output_file) or os.path.getsize(output_file) <= 0:
                err = (ret.stderr or "").strip()
                if err:
                    log.warn(f"【Subtitle】抽取参考字幕失败：{err}")
                return False, "参考字幕轨无法转换为文本字幕"
            return True, ""
        except subprocess.TimeoutExpired:
            return False, "抽取参考字幕超时"
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return False, f"抽取参考字幕失败：{str(e)}"

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
    def __translate_reference_cues(cls, reference_cues, target_language):
        if not cls.__is_llm_alignment_enabled():
            return None, "LLM 跨语言对齐未启用"
        client = LLMClient()
        if not client.is_ready(require_enable=False):
            return None, "LLM 配置不完整"
        translated_cues = [dict(cue) for cue in reference_cues]
        target_name = cls.__language_display_name(target_language)
        batch_count = 0
        for batch in cls.__iter_translation_batches(reference_cues):
            batch_count += 1
            cache_key = cls.__translation_cache_key(client, target_language, batch)
            cached = cls.__get_translation_cache(cache_key)
            if cached is None:
                translated, msg = cls.__request_translation_batch(client, target_name, batch)
                if not translated:
                    return None, msg
                cls.__set_translation_cache(cache_key, translated)
            else:
                translated = cached
            for cue_id, text in translated.items():
                if cue_id < 0 or cue_id >= len(translated_cues):
                    return None, "LLM 翻译结果索引异常"
                translated_cues[cue_id]["text"] = text
        log.info("【Subtitle】LLM参考字幕翻译完成：provider=%s, batches=%s" % (client.provider, batch_count))
        return translated_cues, ""

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
            item = {"id": index, "text": text}
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
    def __request_translation_batch(cls, client, target_language_name, batch):
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
            max_tokens=2048
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
    def __translation_cache_key(cls, client, target_language, batch):
        payload = json.dumps({
            "provider": client.provider,
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
        anchors = []
        last_reference_index = -1
        comparisons = 0
        normalized_references = [cls.__normalize_text(cue.get("text")) for cue in reference_cues]
        for source_index, source in enumerate(source_cues):
            source_text = cls.__normalize_text(source.get("text"))
            if len(source_text) < 4:
                continue
            best = None
            search_start = last_reference_index + 1
            search_end = min(len(reference_cues), search_start + cls._reference_search_window)
            for reference_index in range(search_start, search_end):
                comparisons += 1
                if comparisons > cls._max_match_comparisons:
                    return anchors, True
                reference_text = normalized_references[reference_index]
                if len(reference_text) < 4:
                    continue
                score = SequenceMatcher(None, source_text, reference_text).ratio()
                if score >= cls._min_match_score and (not best or score > best.get("score")):
                    best = {
                        "source_index": source_index,
                        "reference_index": reference_index,
                        "source_time": source.get("start"),
                        "reference_time": reference_cues[reference_index].get("start"),
                        "score": score
                    }
                    if score >= 0.98:
                        break
            if best:
                anchors.append(best)
                last_reference_index = best.get("reference_index")
        return anchors, False

    @staticmethod
    def __normalize_text(text):
        text = str(text or "").lower()
        text = re.sub(r"\{\\[^}]*\}", "", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"&[a-z]+;", "", text)
        text = re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)
        return text

    @classmethod
    def __validate_anchors(cls, anchors):
        if len(anchors) < cls._min_anchors:
            return False, "匹配锚点不足，跳过自动对齐"
        avg_score = sum(anchor.get("score", 0) for anchor in anchors) / len(anchors)
        if avg_score < cls._min_avg_score:
            return False, "字幕文本匹配度不足，跳过自动对齐"
        offsets = [anchor.get("reference_time") - anchor.get("source_time") for anchor in anchors]
        for left, right in zip(offsets, offsets[1:]):
            if abs(right - left) > cls._max_offset_jump_ms:
                return False, "时间轴跳变过大，跳过自动对齐"
        for left, right in zip(anchors, anchors[1:]):
            source_delta = right.get("source_time") - left.get("source_time")
            reference_delta = right.get("reference_time") - left.get("reference_time")
            if source_delta <= 0 or reference_delta <= 0:
                return False, "锚点顺序异常，跳过自动对齐"
            ratio = reference_delta / source_delta
            if ratio < cls._min_stretch_ratio or ratio > cls._max_stretch_ratio:
                return False, "分段变速超出安全范围，跳过自动对齐"
        return True, ""

    @classmethod
    def __align_cues(cls, cues, anchors, align_mode="auto"):
        offsets = [anchor.get("reference_time") - anchor.get("source_time") for anchor in anchors]
        median_offset = sorted(offsets)[len(offsets) // 2]
        if align_mode == "offset":
            return [cls.__shift_cue(cue, median_offset) for cue in cues], "offset"
        if align_mode == "segmented":
            return [cls.__map_cue(cue, anchors) for cue in cues], "segmented"
        if max(abs(offset - median_offset) for offset in offsets) <= cls._stable_offset_ms:
            return [cls.__shift_cue(cue, median_offset) for cue in cues], "offset"
        return [cls.__map_cue(cue, anchors) for cue in cues], "segmented"

    @classmethod
    def __shift_cue(cls, cue, offset):
        start = max(0, cue.get("start") + offset)
        end = max(start + 200, cue.get("end") + offset)
        return dict(cue, start=start, end=end)

    @classmethod
    def __map_cue(cls, cue, anchors):
        original_duration = max(200, cue.get("end") - cue.get("start"))
        start = cls.__map_time(cue.get("start"), anchors)
        end = cls.__map_time(cue.get("end"), anchors)
        if end <= start:
            end = start + original_duration
        return dict(cue, start=max(0, start), end=max(start + 200, end))

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
