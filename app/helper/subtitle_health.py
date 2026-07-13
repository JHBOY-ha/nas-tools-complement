import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

from charset_normalizer import from_bytes

from app.utils import ExceptionUtils
from config import RMT_MEDIAEXT, RMT_SUBEXT


class SubtitleHealth:
    """外挂字幕规范化、媒体服务器兼容性检查及全库审计。"""

    _text_extensions = {".srt", ".ass", ".ssa", ".smi", ".vtt"}
    _server_extensions = {
        "jellyfin": {".srt", ".ass", ".ssa", ".vtt", ".sub"},
        "emby": {".srt", ".ass", ".ssa", ".smi", ".vtt", ".sub"},
        "plex": {".srt", ".ass", ".ssa", ".smi", ".vtt", ".sub"}
    }
    _srt_timing_re = re.compile(
        r"^\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+"
        r"\d{2}:\d{2}:\d{2}[,.]\d{3}(?:\s+.*)?$"
    )
    _jellyfin_language_tokens = {
        "ar", "bg", "zh", "cs", "da", "nl", "en", "fi", "fr", "de",
        "el", "he", "hi", "hu", "id", "it", "ja", "ko", "ms", "no",
        "fa", "pl", "pt", "ro", "ru", "es", "sv", "th", "tr", "uk", "vi",
        "ara", "bul", "chi", "zho", "cze", "ces", "dan", "dut", "nld",
        "eng", "fin", "fre", "fra", "ger", "deu", "gre", "ell", "heb",
        "hin", "hun", "ind", "ita", "jpn", "kor", "may", "msa", "nor",
        "per", "fas", "pol", "por", "rum", "ron", "rus", "spa", "swe",
        "tha", "tur", "ukr", "vie"
    }
    _iso639_1_tokens = {
        "aa", "ab", "ae", "af", "ak", "am", "an", "ar", "as", "av", "ay", "az",
        "ba", "be", "bg", "bh", "bi", "bm", "bn", "bo", "br", "bs", "ca", "ce",
        "ch", "co", "cr", "cs", "cu", "cv", "cy", "da", "de", "dv", "dz", "ee",
        "el", "en", "eo", "es", "et", "eu", "fa", "ff", "fi", "fj", "fo", "fr",
        "fy", "ga", "gd", "gl", "gn", "gu", "gv", "ha", "he", "hi", "ho", "hr",
        "ht", "hu", "hy", "hz", "ia", "id", "ie", "ig", "ii", "ik", "io", "is",
        "it", "iu", "ja", "jv", "ka", "kg", "ki", "kj", "kk", "kl", "km", "kn",
        "ko", "kr", "ks", "ku", "kv", "kw", "ky", "la", "lb", "lg", "li", "ln",
        "lo", "lt", "lu", "lv", "mg", "mh", "mi", "mk", "ml", "mn", "mr", "ms",
        "mt", "my", "na", "nb", "nd", "ne", "ng", "nl", "nn", "no", "nr", "nv",
        "ny", "oc", "oj", "om", "or", "os", "pa", "pi", "pl", "ps", "pt", "qu",
        "rm", "rn", "ro", "ru", "rw", "sa", "sc", "sd", "se", "sg", "si", "sk",
        "sl", "sm", "sn", "so", "sq", "sr", "ss", "st", "su", "sv", "sw", "ta",
        "te", "tg", "th", "ti", "tk", "tl", "tn", "to", "tr", "ts", "tt", "tw",
        "ty", "ug", "uk", "ur", "uz", "ve", "vi", "vo", "wa", "wo", "xh", "yi",
        "yo", "za", "zh", "zu"
    }
    _charset_candidates = ["utf_8", "utf_16", "utf_16_le", "utf_16_be", "gb18030", "big5"]

    @classmethod
    def normalize_uploaded_subtitle(cls, subtitle_file):
        """将文本字幕规范化为 UTF-8，并修复可安全确认的 SRT 结构问题。"""
        result = {
            "normalized": False,
            "repaired": False,
            "encoding": "",
            "removed_blank_lines": 0,
            "probe_available": bool(shutil.which("ffprobe")),
            "valid": False,
            "message": ""
        }
        ext = os.path.splitext(subtitle_file)[-1].lower()
        if ext not in cls._text_extensions:
            validation = cls.validate_subtitle(subtitle_file)
            result.update(validation)
            return result
        try:
            with open(subtitle_file, "rb") as file_obj:
                raw = file_obj.read()
            encoding, text = cls.__decode_text(raw)
            if text is None:
                result["message"] = "无法识别字幕字符编码"
                return result
            result["encoding"] = encoding
            normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")
            if ext == ".srt":
                normalized_text, removed = cls.__repair_srt_blank_lines(normalized_text)
                result["removed_blank_lines"] = removed
                result["repaired"] = removed > 0
            normalized_bytes = normalized_text.encode("utf-8")
            if raw != normalized_bytes:
                cls.__atomic_write(subtitle_file, normalized_bytes)
                result["normalized"] = True
            validation = cls.validate_subtitle(subtitle_file)
            result.update(validation)
            return result
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            result["message"] = f"字幕规范化失败：{str(e)}"
            return result

    @classmethod
    def validate_subtitle(cls, subtitle_file):
        ext = os.path.splitext(subtitle_file)[-1].lower()
        probe_available = bool(shutil.which("ffprobe"))
        if not os.path.isfile(subtitle_file):
            return {"valid": False, "probe_available": probe_available, "message": "字幕文件不存在"}
        if os.path.getsize(subtitle_file) <= 0:
            return {"valid": False, "probe_available": probe_available, "message": "字幕文件为空"}
        if probe_available:
            try:
                probe_file = subtitle_file
                if ext == ".sub" and os.path.exists(os.path.splitext(subtitle_file)[0] + ".idx"):
                    probe_file = os.path.splitext(subtitle_file)[0] + ".idx"
                ret = subprocess.run(
                    [
                        "ffprobe", "-v", "error", "-print_format", "json",
                        "-show_streams", "-show_format", probe_file
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=20,
                    text=True
                )
                payload = json.loads(ret.stdout or "{}") if ret.stdout else {}
                streams = payload.get("streams") or []
                is_subtitle = any(str(stream.get("codec_type") or "").lower() == "subtitle" for stream in streams)
                if ret.returncode == 0 and is_subtitle:
                    return {"valid": True, "probe_available": True, "message": "ffprobe 解析通过"}
                if ext == ".sub" and not os.path.exists(os.path.splitext(subtitle_file)[0] + ".idx"):
                    return {"valid": False, "probe_available": True, "message": "SUB 无法独立解析，可能缺少同名 .idx 文件"}
                error = str(ret.stderr or "").strip().splitlines()
                detail = error[-1] if error else "ffprobe 未返回字幕流"
                return {"valid": False, "probe_available": True, "message": detail}
            except subprocess.TimeoutExpired:
                return {"valid": False, "probe_available": True, "message": "ffprobe 检测超时"}
            except Exception as e:
                ExceptionUtils.exception_traceback(e)
                return {"valid": False, "probe_available": True, "message": f"ffprobe 检测失败：{str(e)}"}
        return cls.__fallback_validate(subtitle_file, ext)

    @classmethod
    def inspect_external_subtitle(cls, subtitle_file, media_file, server_type):
        server_type = str(server_type or "emby").lower()
        ext = os.path.splitext(subtitle_file)[-1].lower()
        result = {
            "path": subtitle_file,
            "media_path": media_file or "",
            "server": server_type,
            "recognizable": False,
            "language_defined": False,
            "status": "error",
            "reason": ""
        }
        if not media_file:
            result["reason"] = "未找到同目录且同名前缀的媒体文件"
            return result
        supported = cls._server_extensions.get(server_type, set(RMT_SUBEXT))
        if ext not in supported:
            result["reason"] = f"{server_type} 不支持或不建议使用 {ext} 外挂字幕"
            return result
        validation = cls.validate_subtitle(subtitle_file)
        result["probe_available"] = validation.get("probe_available", False)
        if not validation.get("valid"):
            result["reason"] = validation.get("message") or "字幕内容无法解析"
            return result
        result["recognizable"] = True
        result["language_defined"] = cls.__language_defined(subtitle_file, media_file, server_type)
        if result["language_defined"]:
            result["status"] = "ok"
            result["reason"] = "文件名关联、语言标签和字幕内容均可识别"
        else:
            result["status"] = "warning"
            result["reason"] = "字幕可识别，但媒体服务器可能显示语言未定义"
        return result

    @classmethod
    def list_external_subtitles(cls, media_file):
        """返回与指定媒体文件同目录、同名前缀的全部外挂字幕。"""
        if not media_file or not os.path.isfile(media_file):
            return []
        media_dir = os.path.dirname(media_file)
        media_base = os.path.splitext(os.path.basename(media_file))[0]
        media_bases = [(media_base, media_file)]
        try:
            file_names = sorted(os.listdir(media_dir))
        except OSError:
            return []
        subtitles = []
        for file_name in file_names:
            if os.path.splitext(file_name)[-1].lower() not in RMT_SUBEXT:
                continue
            if cls.__match_media_file(file_name, media_bases) == media_file:
                subtitles.append(os.path.join(media_dir, file_name))
        return subtitles

    @classmethod
    def inspect_media_subtitles(cls, media_file, server_type):
        """检查单个媒体文件关联的全部外挂字幕。"""
        return [
            cls.inspect_external_subtitle(subtitle_file, media_file, server_type)
            for subtitle_file in cls.list_external_subtitles(media_file)
        ]

    @classmethod
    def aggregate_media_subtitles(cls, results):
        """汇总单个媒体的多字幕结果，保留最严重状态并统计字幕数量。"""
        key_statuses = cls.__aggregate_media_statuses(results)
        return next(iter(key_statuses.values()), {})

    @classmethod
    def language_defined(cls, subtitle_file, media_file, server_type):
        """公开文件名语言判断，供安全修复流程在替换原文件前复用。"""
        return cls.__language_defined(subtitle_file, media_file, str(server_type or "emby").lower())

    @classmethod
    def is_supported_extension(cls, subtitle_file, server_type):
        """判断字幕扩展名是否受目标影视服务器支持。"""
        server_type = str(server_type or "emby").lower()
        supported = cls._server_extensions.get(server_type, set(RMT_SUBEXT))
        return os.path.splitext(subtitle_file or "")[-1].lower() in supported

    @classmethod
    def audit_roots(cls, roots, server_type, issue_limit=1000):
        roots = cls.__normalize_roots(roots)
        subtitle_pairs = []
        inaccessible_roots = []
        scan_errors = []
        scan_error_paths = []

        def _onerror(error):
            scan_errors.append(str(error))
            error_path = os.path.normpath(str(getattr(error, "filename", "") or "").strip())
            if error_path and error_path not in scan_error_paths:
                scan_error_paths.append(error_path)

        for root in roots:
            if not os.path.isdir(root):
                inaccessible_roots.append(root)
                continue
            for current_dir, _, file_names in os.walk(root, onerror=_onerror):
                media_files = [name for name in file_names if os.path.splitext(name)[-1].lower() in RMT_MEDIAEXT]
                subtitle_files = [name for name in file_names if os.path.splitext(name)[-1].lower() in RMT_SUBEXT]
                if not subtitle_files:
                    continue
                media_bases = sorted(
                    [(os.path.splitext(name)[0], os.path.join(current_dir, name)) for name in media_files],
                    key=lambda item: len(item[0]),
                    reverse=True
                )
                for sub_name in subtitle_files:
                    sub_file = os.path.join(current_dir, sub_name)
                    media_file = cls.__match_media_file(sub_name, media_bases)
                    subtitle_pairs.append((sub_file, media_file))

        def _inspect(pair):
            return cls.inspect_external_subtitle(pair[0], pair[1], server_type)

        results = []
        if subtitle_pairs:
            workers = min(4, len(subtitle_pairs))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                results = list(executor.map(_inspect, subtitle_pairs))
        summary = {
            "total": len(results),
            "ok": len([item for item in results if item.get("status") == "ok"]),
            "warning": len([item for item in results if item.get("status") == "warning"]),
            "error": len([item for item in results if item.get("status") == "error"])
        }
        media_statuses = cls.__aggregate_media_statuses(results)
        issues = [item for item in results if item.get("status") != "ok"]
        return {
            "code": 0,
            "server": str(server_type or "emby").lower(),
            "roots": roots,
            "inaccessible_roots": inaccessible_roots,
            "scan_errors": scan_errors[:100],
            "scan_error_paths": scan_error_paths,
            "summary": summary,
            "media_statuses": media_statuses,
            "issues": issues[:issue_limit],
            "issues_truncated": max(len(issues) - issue_limit, 0),
            "probe_available": bool(shutil.which("ffprobe"))
        }

    @staticmethod
    def __aggregate_media_statuses(results):
        """按媒体文件汇总外挂字幕状态，多个字幕时保留最严重的结果。"""
        priorities = {"ok": 0, "warning": 1, "error": 2}
        statuses = {}
        for item in results or []:
            media_path = str(item.get("media_path") or "").strip()
            if not media_path:
                continue
            key = os.path.normcase(os.path.normpath(media_path))
            status = item.get("status") or "error"
            current = statuses.get(key)
            if current:
                current["subtitle_count"] += 1
                if priorities.get(status, 2) <= priorities.get(current.get("status"), 2):
                    continue
            statuses[key] = {
                "media_path": media_path,
                "status": status,
                "reason": item.get("reason") or "",
                "subtitle_count": current.get("subtitle_count", 0) if current else 1
            }
        return statuses

    @staticmethod
    def __decode_text(raw):
        if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            try:
                return "utf-32", raw.decode("utf-32")
            except UnicodeError:
                return "", None
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            try:
                return "utf-16", raw.decode("utf-16")
            except UnicodeError:
                return "", None
        try:
            return "utf-8-sig", raw.decode("utf-8-sig")
        except UnicodeError:
            pass
        try:
            match = from_bytes(raw, cp_isolation=SubtitleHealth._charset_candidates).best()
            if match:
                return str(match.encoding or ""), str(match)
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
        return "", None

    @classmethod
    def __repair_srt_blank_lines(cls, text):
        lines = text.split("\n")
        output = []
        removed = 0
        index = 0
        while index < len(lines):
            line = lines[index]
            output.append(line)
            if line.strip().isdigit():
                cursor = index + 1
                while cursor < len(lines) and not lines[cursor].strip():
                    cursor += 1
                if cursor > index + 1 and cursor < len(lines) \
                        and cls._srt_timing_re.match(lines[cursor].strip()):
                    removed += cursor - index - 1
                    index = cursor
                    continue
            index += 1
        return "\n".join(output), removed

    @staticmethod
    def __atomic_write(file_path, content):
        temp_file = f"{file_path}.normalize.tmp"
        try:
            with open(temp_file, "wb") as file_obj:
                file_obj.write(content)
            try:
                shutil.copymode(file_path, temp_file)
            except OSError:
                pass
            os.replace(temp_file, file_path)
        finally:
            if os.path.exists(temp_file):
                os.remove(temp_file)

    @classmethod
    def __fallback_validate(cls, subtitle_file, ext):
        if ext not in cls._text_extensions:
            return {"valid": True, "probe_available": False, "message": "未安装 ffprobe，仅完成基础检查"}
        try:
            with open(subtitle_file, "rb") as file_obj:
                _, text = cls.__decode_text(file_obj.read())
            if text is None:
                return {"valid": False, "probe_available": False, "message": "无法识别字幕字符编码"}
            stripped = text.lstrip("\ufeff\r\n ")
            if ext == ".srt":
                timings = [line.strip() for line in text.splitlines() if "-->" in line]
                valid = bool(timings) and all(cls._srt_timing_re.match(line) for line in timings)
            elif ext in {".ass", ".ssa"}:
                valid = "[Script Info]" in text and "[Events]" in text
            elif ext == ".vtt":
                valid = stripped.upper().startswith("WEBVTT")
            elif ext == ".smi":
                valid = "<SAMI" in text.upper()
            else:
                valid = True
            return {
                "valid": valid,
                "probe_available": False,
                "message": "基础格式检查通过" if valid else "字幕基础格式检查失败"
            }
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return {"valid": False, "probe_available": False, "message": str(e)}

    @classmethod
    def __language_defined(cls, subtitle_file, media_file, server_type):
        sub_base = os.path.splitext(os.path.basename(subtitle_file))[0]
        media_base = os.path.splitext(os.path.basename(media_file))[0]
        suffix = sub_base[len(media_base):].lower() if sub_base.startswith(media_base) else ""
        tokens = [token.strip("-_") for token in re.split(r"[.()\[\] ]+", suffix) if token.strip("-_")]
        if server_type == "jellyfin":
            return any(
                token in cls._jellyfin_language_tokens or token in cls._iso639_1_tokens
                for token in tokens
            )
        region_tokens = {"zh-cn", "zh-tw", "zh-hans", "zh-hant", "en-us", "en-gb", "pt-br"}
        return any(cls.__is_language_token(token) or token in region_tokens for token in tokens)

    @classmethod
    def __is_language_token(cls, token):
        token = str(token or "").strip().lower().replace("_", "-")
        if token in cls._jellyfin_language_tokens or token in cls._iso639_1_tokens:
            return True
        primary = token.split("-", 1)[0]
        return bool(
            primary in cls._iso639_1_tokens
            and re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})+", token)
        )

    @staticmethod
    def __match_media_file(subtitle_name, media_bases):
        sub_base = os.path.splitext(subtitle_name)[0]
        for media_base, media_file in media_bases:
            if sub_base == media_base:
                return media_file
            if sub_base.startswith((f"{media_base}.", f"{media_base}-", f"{media_base}_", f"{media_base}(")):
                return media_file
        return ""

    @staticmethod
    def __normalize_roots(roots):
        result = []
        for root in roots or []:
            root = os.path.normpath(str(root or "").strip())
            if root and root not in result:
                result.append(root)
        return result
