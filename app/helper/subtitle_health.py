import json
import os
import re
import shutil
import subprocess
import time
from contextlib import nullcontext

from charset_normalizer import from_bytes

from app.utils import ExceptionUtils
from config import RMT_MEDIAEXT, RMT_SUBEXT


class SubtitleHealth:
    """外挂字幕规范化、媒体服务器兼容性检查及全库审计。"""

    _text_extensions = {".srt", ".ass", ".ssa", ".smi", ".vtt"}
    _audit_candidate_limit = 100000
    _audit_media_limit = 100000
    _audit_root_sample_limit = 20
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
    _charset_candidates = [
        "utf_8", "utf_16", "utf_16_le", "utf_16_be",
        "gb18030", "big5", "cp1252"
    ]
    _validator_version = "subtitle-health-v2"
    _max_local_text_bytes = 20 * 1024 * 1024
    _ffprobe_version = None
    _nas_skip_directories = {
        "@eadir", "@recycle", "@sharesnap", "@snapshot", "#recycle",
        "$recycle.bin", ".snapshot", ".snapshots", ".recycle",
        ".appledouble", "lost+found"
    }

    @classmethod
    def normalize_uploaded_subtitle(cls, subtitle_file, timeout_seconds=10,
                                    cancel_check=None):
        """将文本字幕规范化为 UTF-8，并修复可安全确认的字幕结构问题。"""
        result = {
            "normalized": False,
            "repaired": False,
            "encoding": "",
            "removed_blank_lines": 0,
            "ass_repairs": [],
            "probe_available": bool(shutil.which("ffprobe")),
            "valid": False,
            "message": ""
        }
        ext = os.path.splitext(subtitle_file)[-1].lower()
        if ext not in cls._text_extensions:
            if cancel_check is None and timeout_seconds == 10:
                validation = cls.validate_subtitle(subtitle_file)
            else:
                validation = cls.validate_subtitle(
                    subtitle_file,
                    timeout_seconds=timeout_seconds,
                    cancel_check=cancel_check
                )
            result.update(validation)
            return result
        try:
            if os.path.getsize(subtitle_file) > cls._max_local_text_bytes:
                result["message"] = "文本字幕超过 20 MiB 本地处理上限"
                return result
            raw = cls.__read_file_cooperative(
                subtitle_file, cls._max_local_text_bytes, cancel_check
            )
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
            elif ext in [".ass", ".ssa"]:
                normalized_text, repairs = cls.__repair_ass_structure(normalized_text)
                result["ass_repairs"] = repairs
                result["repaired"] = bool(repairs)
            normalized_bytes = normalized_text.encode("utf-8")
            if raw != normalized_bytes:
                cls.__atomic_write(subtitle_file, normalized_bytes)
                result["normalized"] = True
            if cancel_check is None and timeout_seconds == 10:
                validation = cls.validate_subtitle(subtitle_file)
            else:
                validation = cls.validate_subtitle(
                    subtitle_file,
                    timeout_seconds=timeout_seconds,
                    cancel_check=cancel_check
                )
            result.update(validation)
            return result
        except InterruptedError:
            result["canceled"] = True
            result["message"] = "字幕规范化已取消"
            return result
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            result["message"] = f"字幕规范化失败：{str(e)}"
            return result

    @classmethod
    def validate_subtitle(cls, subtitle_file, timeout_seconds=10, cancel_check=None):
        ext = os.path.splitext(subtitle_file)[-1].lower()
        probe_available = bool(shutil.which("ffprobe"))
        if not os.path.isfile(subtitle_file):
            return {"valid": False, "probe_available": probe_available, "message": "字幕文件不存在"}
        file_size = os.path.getsize(subtitle_file)
        if file_size <= 0:
            return {"valid": False, "probe_available": probe_available, "message": "字幕文件为空"}
        # 文本字幕有明确且便宜的结构校验，不应为每次上传/审计都启动 ffprobe。
        # ffprobe 仅保留给 VobSub 等二进制或无法本地确认的格式。
        if ext in cls._text_extensions:
            if file_size > cls._max_local_text_bytes:
                return {
                    "valid": False, "probe_available": probe_available,
                    "message": "文本字幕超过 20 MiB 本地结构检查上限"
                }
            validation = cls.__fallback_validate(
                subtitle_file, ext, cancel_check=cancel_check
            )
            validation["probe_available"] = probe_available
            if validation.get("valid"):
                validation["message"] = "本地字幕结构检查通过"
            return validation
        if ext == ".sub" and not os.path.isfile(os.path.splitext(subtitle_file)[0] + ".idx"):
            try:
                microdvd = cls.__validate_microdvd_sub(
                    subtitle_file, cancel_check=cancel_check
                )
            except InterruptedError:
                return {
                    "valid": False, "probe_available": probe_available,
                    "canceled": True, "message": "字幕结构检查已取消"
                }
            if microdvd is True:
                return {
                    "valid": True, "probe_available": probe_available,
                    "message": "本地 MicroDVD 字幕结构检查通过"
                }
            if microdvd is False:
                return {
                    "valid": False, "probe_available": probe_available,
                    "message": "二进制 SUB 缺少同名 .idx 文件"
                }
        if probe_available:
            try:
                probe_file = subtitle_file
                if ext == ".sub" and os.path.exists(os.path.splitext(subtitle_file)[0] + ".idx"):
                    probe_file = os.path.splitext(subtitle_file)[0] + ".idx"
                returncode, stdout, stderr, stop_reason = cls.__run_popen(
                    [
                        "ffprobe", "-v", "error", "-print_format", "json",
                        "-show_streams", "-show_format", probe_file
                    ],
                    timeout_seconds=timeout_seconds,
                    cancel_check=cancel_check
                )
                if stop_reason == "canceled":
                    return {
                        "valid": False, "probe_available": True,
                        "canceled": True, "message": "ffprobe 检测已取消"
                    }
                if stop_reason == "timeout":
                    return {"valid": False, "probe_available": True, "message": "ffprobe 检测超时"}
                payload = json.loads(stdout or "{}") if stdout else {}
                streams = payload.get("streams") or []
                is_subtitle = any(str(stream.get("codec_type") or "").lower() == "subtitle" for stream in streams)
                if returncode == 0 and is_subtitle:
                    return {"valid": True, "probe_available": True, "message": "ffprobe 解析通过"}
                if ext == ".sub" and not os.path.exists(os.path.splitext(subtitle_file)[0] + ".idx"):
                    return {"valid": False, "probe_available": True, "message": "SUB 无法独立解析，可能缺少同名 .idx 文件"}
                error = str(stderr or "").strip().splitlines()
                detail = error[-1] if error else "ffprobe 未返回字幕流"
                return {"valid": False, "probe_available": True, "message": detail}
            except Exception as e:
                ExceptionUtils.exception_traceback(e)
                return {"valid": False, "probe_available": True, "message": f"ffprobe 检测失败：{str(e)}"}
        if ext not in cls._text_extensions:
            return {
                "valid": False, "probe_available": False,
                "message": "未安装 ffprobe，无法验证二进制或未知字幕格式"
            }
        return cls.__fallback_validate(subtitle_file, ext)

    @classmethod
    def __run_popen(cls, command, timeout_seconds, cancel_check=None):
        """可轮询、可终止的受限子进程执行器。"""
        if cls.__cancel_requested(cancel_check):
            return None, "", "", "canceled"
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True
        )
        started = time.monotonic()
        stop_reason = ""
        while process.poll() is None:
            if cls.__cancel_requested(cancel_check):
                stop_reason = "canceled"
                break
            if time.monotonic() - started >= max(float(timeout_seconds or 0), 0.001):
                stop_reason = "timeout"
                break
            time.sleep(0.05)
        if stop_reason:
            cls.__terminate_popen(process)
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        return process.returncode, stdout, stderr, stop_reason

    @staticmethod
    def __terminate_popen(process):
        try:
            process.terminate()
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    @classmethod
    def __validate_microdvd_sub(cls, subtitle_file, cancel_check=None):
        """识别确定的文本 MicroDVD；False 表示明显二进制，None 表示未知。"""
        try:
            sample = cls.__read_file_cooperative(
                subtitle_file,
                min(cls._max_local_text_bytes, 1024 * 1024),
                cancel_check,
                reject_oversize=False
            )
            if b"\x00" in sample:
                return False
            _, text = cls.__decode_text(sample)
            if text is None:
                return False
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if lines and all(
                    re.match(r"^\{\d+\}\{\d+\}", line) for line in lines[:20]):
                return True
            return None
        except InterruptedError:
            raise
        except OSError:
            return None

    @classmethod
    def __read_file_cooperative(cls, file_path, max_bytes, cancel_check=None,
                                reject_oversize=True):
        chunks = []
        total = 0
        with open(file_path, "rb") as file_obj:
            while True:
                if cls.__cancel_requested(cancel_check):
                    raise InterruptedError("任务已取消")
                if not reject_oversize and total >= max_bytes:
                    break
                allowance = max_bytes - total + (1 if reject_oversize else 0)
                chunk = file_obj.read(min(1024 * 1024, allowance))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("字幕超过本地处理上限")
        return b"".join(chunks)

    @classmethod
    def inspect_external_subtitle(cls, subtitle_file, media_file, server_type,
                                  cancel_check=None, probe_timeout_seconds=10):
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
        if cancel_check is None and probe_timeout_seconds == 10:
            validation = cls.validate_subtitle(subtitle_file)
        else:
            validation = cls.validate_subtitle(
                subtitle_file,
                timeout_seconds=probe_timeout_seconds,
                cancel_check=cancel_check
            )
        result["probe_available"] = validation.get("probe_available", False)
        if validation.get("canceled"):
            result["canceled"] = True
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
    def iter_external_subtitles(cls, media_file, max_results=None, cancel_check=None):
        """流式返回与媒体文件关联的外挂字幕，可限制最多产出数量。"""
        if not media_file or not os.path.isfile(media_file):
            return
        media_dir = os.path.dirname(media_file)
        media_base = os.path.splitext(os.path.basename(media_file))[0]
        media_bases = [(media_base, media_file)]
        if max_results is not None:
            max_results = max(int(max_results or 0), 0)
            if max_results == 0:
                return
        yielded = 0
        try:
            entries = os.scandir(media_dir)
        except OSError:
            return
        try:
            with entries:
                for entry in entries:
                    if cls.__cancel_requested(cancel_check):
                        return
                    file_name = entry.name
                    if os.path.splitext(file_name)[-1].lower() not in RMT_SUBEXT:
                        continue
                    if cls.__match_media_file(file_name, media_bases) != media_file:
                        continue
                    yield entry.path
                    yielded += 1
                    if max_results is not None and yielded >= max_results:
                        return
        except OSError:
            return

    @classmethod
    def list_external_subtitles(cls, media_file, max_results=None, cancel_check=None):
        """返回关联外挂字幕；调用方可用 ``max_results`` 建立任务硬上限。"""
        return list(cls.iter_external_subtitles(
            media_file,
            max_results=max_results,
            cancel_check=cancel_check
        ))

    @classmethod
    def inspect_media_subtitles(cls, media_file, server_type, cancel_check=None,
                                probe_timeout_seconds=10, heavy_operation=None):
        """检查单个媒体文件关联的全部外挂字幕。"""
        results = []
        for subtitle_file in cls.list_external_subtitles(media_file):
            if cls.__cancel_requested(cancel_check):
                break
            operation = nullcontext()
            if cls.requires_external_probe(subtitle_file) and heavy_operation:
                operation = heavy_operation("interactive")
            with operation:
                if cancel_check is None and probe_timeout_seconds == 10:
                    result = cls.inspect_external_subtitle(
                        subtitle_file, media_file, server_type
                    )
                else:
                    result = cls.inspect_external_subtitle(
                        subtitle_file, media_file, server_type,
                        cancel_check=cancel_check,
                        probe_timeout_seconds=probe_timeout_seconds
                    )
            results.append(result)
            if result.get("canceled"):
                break
        return results

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
    def requires_external_probe(cls, subtitle_file):
        """Whether validation may need ffprobe instead of the local parser."""
        return os.path.splitext(subtitle_file or "")[-1].lower() not in cls._text_extensions

    @classmethod
    def audit_linked_media(cls, media_files, server_type, issue_limit=200,
                           directory_limit=50000, subtitle_limit=10000,
                           time_limit_seconds=3600, probe_timeout_seconds=10,
                           cancel_check=None, progress_callback=None,
                           cache_get=None, cache_put=None,
                           heavy_operation=None):
        """只检测已链接媒体；每个目标目录恰好枚举一次且不调用 ``os.walk``。"""
        accumulator = cls.__new_audit_accumulator(
            server_type=server_type,
            roots=[],
            issue_limit=issue_limit,
            subtitle_limit=subtitle_limit,
            time_limit_seconds=time_limit_seconds,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
            cache_get=cache_get,
            cache_put=cache_put,
            heavy_operation=heavy_operation,
            mode="linked",
            probe_timeout_seconds=probe_timeout_seconds
        )
        accumulator["directory_limit"] = max(int(directory_limit or 0), 1)
        groups = {}
        seen_media = set()
        groups_truncated = False
        truncation_reason = ""
        for media_file in media_files or []:
            if cls.__should_stop_audit(accumulator):
                break
            media_file = os.path.normpath(str(media_file or "").strip())
            media_key = os.path.normcase(os.path.abspath(media_file)) if media_file else ""
            if not media_key or media_key in seen_media:
                continue
            if len(seen_media) >= cls._audit_media_limit:
                groups_truncated = True
                truncation_reason = "media_limit"
                accumulator["partial"] = True
                break
            seen_media.add(media_key)
            directory = os.path.dirname(media_file)
            if not directory:
                continue
            directory_key = os.path.normcase(os.path.abspath(directory))
            if directory_key not in groups \
                    and len(groups) >= accumulator["directory_limit"]:
                groups_truncated = True
                truncation_reason = "directory_limit"
                accumulator["partial"] = True
                break
            group = groups.setdefault(directory_key, {"directory": directory, "media": {}})
            cls.__emit_audit_progress(accumulator, media_file)
            # linked mode receives explicit, trusted TRANSFER_HISTORY targets.
            # A library media entry may itself be a symlink to the download
            # volume; keep its lexical path so subtitles beside the link are
            # still audited.  Subtitle symlinks remain rejected below.
            if os.path.isfile(media_file):
                group["media"][os.path.normcase(os.path.abspath(media_file))] = media_file
        accumulator["root_count"] = len(groups)
        accumulator["root_sample"] = []
        for group in groups.values():
            accumulator["root_sample"].append(group["directory"])
            if len(accumulator["root_sample"]) >= cls._audit_root_sample_limit:
                break
        # ``roots`` is retained as a bounded compatibility sample.  Persisting
        # every linked directory would turn a 50,000-directory audit into a
        # multi-megabyte task/history record.  Deep mode still returns exactly
        # its explicitly configured roots.
        accumulator["roots"] = list(accumulator["root_sample"])
        for group in groups.values():
            directory = group["directory"]
            selected = group["media"]
            if cls.__should_stop_audit(accumulator):
                break
            if accumulator["directories"] >= accumulator["directory_limit"]:
                accumulator["partial"] = True
                accumulator["stop_reason"] = "directory_limit"
                break
            if not os.path.isdir(directory):
                cls.__record_inaccessible(accumulator, directory)
                continue
            selected_paths = list(selected.values())
            media_bases = sorted(
                [(os.path.splitext(os.path.basename(path))[0], path) for path in selected_paths],
                key=lambda item: len(item[0]), reverse=True
            )
            try:
                with os.scandir(directory) as entries:
                    accumulator["directories"] += 1
                    cls.__emit_audit_progress(accumulator, directory)
                    for entry in entries:
                        if cls.__should_stop_audit(accumulator):
                            break
                        try:
                            if entry.is_symlink() \
                                    or not entry.is_file(follow_symlinks=False):
                                continue
                        except OSError as error:
                            cls.__record_scan_error(accumulator, error, entry.path)
                            continue
                        sub_name = entry.name
                        if os.path.splitext(sub_name)[-1].lower() not in RMT_SUBEXT:
                            continue
                        media_file = cls.__match_media_file(sub_name, media_bases)
                        if not media_file:
                            continue
                        if not cls.__audit_pair(
                                accumulator, entry.path, media_file):
                            break
            except OSError as error:
                cls.__record_scan_error(accumulator, error, directory)
                continue
        if groups_truncated and not accumulator.get("stop_reason"):
            accumulator["stop_reason"] = truncation_reason or "directory_limit"
        return cls.__finish_audit(accumulator)

    @classmethod
    def audit_roots(cls, roots, server_type, issue_limit=1000,
                    directory_limit=50000, subtitle_limit=10000,
                    time_limit_seconds=3600, probe_timeout_seconds=10, cancel_check=None,
                    progress_callback=None, cache_get=None, cache_put=None,
                    heavy_operation=None):
        """受限的深度扫描。

        目录和字幕逐个处理，不保存完整路径/Future/结果列表；不跟随软链接，
        并主动剪枝 NAS 快照、回收站及系统目录。
        """
        roots = cls.__normalize_roots(roots)
        accumulator = cls.__new_audit_accumulator(
            server_type=server_type,
            roots=roots,
            issue_limit=issue_limit,
            subtitle_limit=subtitle_limit,
            time_limit_seconds=time_limit_seconds,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
            cache_get=cache_get,
            cache_put=cache_put,
            heavy_operation=heavy_operation,
            mode="deep",
            probe_timeout_seconds=probe_timeout_seconds
        )
        accumulator["directory_limit"] = max(int(directory_limit or 0), 1)

        def _onerror(error):
            cls.__record_scan_error(
                accumulator,
                error,
                str(getattr(error, "filename", "") or "")
            )

        for root in roots:
            if cls.__should_stop_audit(accumulator):
                break
            if not os.path.isdir(root) or os.path.islink(root):
                cls.__record_inaccessible(accumulator, root)
                continue
            for current_dir, dir_names, file_names in os.walk(
                    root, topdown=True, onerror=_onerror, followlinks=False):
                # os.walk 在网络文件系统上可能阻塞于一次目录调用；返回后立即合作式取消。
                if cls.__should_stop_audit(accumulator):
                    dir_names[:] = []
                    break
                dir_names[:] = [
                    name for name in dir_names
                    if name.casefold() not in cls._nas_skip_directories
                    and not os.path.islink(os.path.join(current_dir, name))
                ]
                if accumulator["directories"] >= accumulator["directory_limit"]:
                    accumulator["partial"] = True
                    accumulator["stop_reason"] = "directory_limit"
                    dir_names[:] = []
                    break
                accumulator["directories"] += 1
                cls.__emit_audit_progress(accumulator, current_dir)
                media_files = [
                    name for name in file_names
                    if os.path.splitext(name)[-1].lower() in RMT_MEDIAEXT
                    and not os.path.islink(os.path.join(current_dir, name))
                ]
                media_bases = sorted(
                    [(os.path.splitext(name)[0], os.path.join(current_dir, name)) for name in media_files],
                    key=lambda item: len(item[0]), reverse=True
                )
                for sub_name in file_names:
                    if os.path.splitext(sub_name)[-1].lower() not in RMT_SUBEXT:
                        continue
                    subtitle_file = os.path.join(current_dir, sub_name)
                    if os.path.islink(subtitle_file):
                        continue
                    media_file = cls.__match_media_file(sub_name, media_bases)
                    if not cls.__audit_pair(
                            accumulator, subtitle_file, media_file):
                        dir_names[:] = []
                        break
                if accumulator.get("stop_reason"):
                    break
            if accumulator.get("stop_reason"):
                break
        return cls.__finish_audit(accumulator)

    @classmethod
    def subtitle_fingerprint(cls, subtitle_file, cancel_check=None):
        """生成可持久化探测缓存指纹；VobSub 同时纳入配对 IDX 元数据。"""
        subtitle_file = os.path.abspath(os.path.normpath(subtitle_file or ""))
        stat = os.stat(subtitle_file)
        fingerprint = {
            "path": os.path.normcase(subtitle_file),
            "size": stat.st_size,
            "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1000000000)),
            "validator_version": cls._validator_version,
            "ffprobe_version": cls.__get_ffprobe_version(cancel_check=cancel_check)
        }
        if os.path.splitext(subtitle_file)[-1].lower() == ".sub":
            idx_file = os.path.splitext(subtitle_file)[0] + ".idx"
            if os.path.isfile(idx_file):
                idx_stat = os.stat(idx_file)
                fingerprint.update({
                    "pair_path": os.path.normcase(os.path.abspath(idx_file)),
                    "pair_size": idx_stat.st_size,
                    "pair_mtime_ns": getattr(
                        idx_stat, "st_mtime_ns", int(idx_stat.st_mtime * 1000000000)
                    )
                })
            else:
                fingerprint["pair_missing"] = True
        return fingerprint

    @classmethod
    def __new_audit_accumulator(cls, server_type, roots, issue_limit,
                                subtitle_limit, time_limit_seconds,
                                cancel_check, progress_callback,
                                cache_get, cache_put, heavy_operation, mode,
                                probe_timeout_seconds=10):
        roots = list(roots or [])
        return {
            "server": str(server_type or "emby").lower(),
            "roots": roots,
            "root_count": len(roots),
            "root_sample": roots[:cls._audit_root_sample_limit],
            "mode": mode,
            "started": time.monotonic(),
            "time_limit_seconds": max(float(time_limit_seconds or 0), 0.001),
            "subtitle_limit": max(int(subtitle_limit or 0), 1),
            "candidate_limit": cls._audit_candidate_limit,
            "probe_timeout_seconds": max(float(probe_timeout_seconds or 10), 0.1),
            "issue_limit": max(int(issue_limit or 0), 0),
            "cancel_check": cancel_check,
            "progress_callback": progress_callback,
            "last_progress_at": 0,
            "cache_get": cache_get,
            "cache_put": cache_put,
            "heavy_operation": heavy_operation,
            "directories": 0,
            "candidates": 0,
            "inspected": 0,
            "cache_hits": 0,
            "summary": {"total": 0, "ok": 0, "warning": 0, "error": 0},
            "media_statuses": {},
            "issues": [],
            "issue_count": 0,
            "inaccessible_roots": [],
            "inaccessible_count": 0,
            "scan_errors": [],
            "scan_error_paths": [],
            "scan_error_path_set": set(),
            "partial": False,
            "canceled": False,
            "stop_reason": ""
        }

    @classmethod
    def __audit_pair(cls, accumulator, subtitle_file, media_file):
        if cls.__should_stop_audit(accumulator):
            return False
        if accumulator["candidates"] >= accumulator["candidate_limit"]:
            accumulator["partial"] = True
            accumulator["stop_reason"] = "candidate_limit"
            return False
        accumulator["candidates"] += 1
        fingerprint = None
        cached = None
        if accumulator.get("cache_get") or accumulator.get("cache_put"):
            try:
                fingerprint_operation = nullcontext()
                if cls._ffprobe_version is None and accumulator.get("heavy_operation"):
                    fingerprint_operation = accumulator["heavy_operation"]("audit")
                with fingerprint_operation:
                    fingerprint = cls.subtitle_fingerprint(
                        subtitle_file,
                        cancel_check=accumulator.get("cancel_check")
                    )
                if accumulator.get("cache_get"):
                    cached = accumulator["cache_get"](fingerprint)
            except TimeoutError:
                accumulator["partial"] = True
                accumulator["stop_reason"] = "time_limit"
                return False
            except InterruptedError:
                accumulator["canceled"] = True
                accumulator["partial"] = True
                accumulator["stop_reason"] = "canceled"
                return False
            except (OSError, ValueError):
                cached = None
            except Exception as error:
                ExceptionUtils.exception_traceback(error)
                cached = None
        if isinstance(cached, dict):
            result = dict(cached)
            result.update({
                "path": subtitle_file,
                "media_path": media_file or "",
                "server": accumulator["server"]
            })
            accumulator["cache_hits"] += 1
        else:
            if accumulator["inspected"] >= accumulator["subtitle_limit"]:
                accumulator["partial"] = True
                accumulator["stop_reason"] = "subtitle_limit"
                return False
            probe_operation = nullcontext()
            if os.path.splitext(subtitle_file)[-1].lower() not in cls._text_extensions \
                    and accumulator.get("heavy_operation"):
                probe_operation = accumulator["heavy_operation"]("audit")
            try:
                with probe_operation:
                    result = cls.inspect_external_subtitle(
                        subtitle_file, media_file, accumulator["server"],
                        cancel_check=accumulator.get("cancel_check"),
                        probe_timeout_seconds=accumulator["probe_timeout_seconds"]
                    )
            except TimeoutError:
                accumulator["partial"] = True
                accumulator["stop_reason"] = "time_limit"
                return False
            except InterruptedError:
                accumulator["canceled"] = True
                accumulator["partial"] = True
                accumulator["stop_reason"] = "canceled"
                return False
            accumulator["inspected"] += 1
            if fingerprint and accumulator.get("cache_put") and not result.get("canceled"):
                try:
                    accumulator["cache_put"](fingerprint, dict(result))
                except Exception as error:
                    ExceptionUtils.exception_traceback(error)
        cls.__accumulate_audit_result(accumulator, result)
        cls.__emit_audit_progress(accumulator, subtitle_file)
        return not cls.__should_stop_audit(accumulator)

    @staticmethod
    def __emit_audit_progress(accumulator, current_item, force=False):
        callback = accumulator.get("progress_callback")
        if not callback:
            return
        now = time.monotonic()
        if not force and now - accumulator.get("last_progress_at", 0) < 0.5:
            return
        accumulator["last_progress_at"] = now
        try:
            callback({
                "directories": accumulator["directories"],
                "candidates": accumulator["candidates"],
                "inspected": accumulator["inspected"],
                "cache_hits": accumulator["cache_hits"],
                "current_item": current_item
            })
        except Exception as error:
            ExceptionUtils.exception_traceback(error)

    @staticmethod
    def __accumulate_audit_result(accumulator, result):
        status = result.get("status") or "error"
        accumulator["summary"]["total"] += 1
        accumulator["summary"][status if status in {"ok", "warning", "error"} else "error"] += 1
        media_path = str(result.get("media_path") or "").strip()
        if media_path:
            key = os.path.normcase(os.path.normpath(media_path))
            priorities = {"ok": 0, "warning": 1, "error": 2}
            current = accumulator["media_statuses"].get(key)
            subtitle_count = int((current or {}).get("subtitle_count") or 0) + 1
            if not current or priorities.get(status, 2) > priorities.get(current.get("status"), 2):
                accumulator["media_statuses"][key] = {
                    "media_path": media_path,
                    "status": status,
                    "reason": result.get("reason") or "",
                    "subtitle_count": subtitle_count
                }
            else:
                current["subtitle_count"] = subtitle_count
        if status != "ok":
            accumulator["issue_count"] += 1
            if len(accumulator["issues"]) < accumulator["issue_limit"]:
                accumulator["issues"].append(result)

    @classmethod
    def __should_stop_audit(cls, accumulator):
        if accumulator.get("stop_reason"):
            return True
        if cls.__cancel_requested(accumulator.get("cancel_check")):
            accumulator["canceled"] = True
            accumulator["partial"] = True
            accumulator["stop_reason"] = "canceled"
            return True
        if time.monotonic() - accumulator["started"] >= accumulator["time_limit_seconds"]:
            accumulator["partial"] = True
            accumulator["stop_reason"] = "time_limit"
            return True
        return False

    @staticmethod
    def __cancel_requested(cancel_check):
        try:
            if callable(cancel_check):
                return bool(cancel_check())
            if hasattr(cancel_check, "is_set"):
                return bool(cancel_check.is_set())
            return bool(cancel_check)
        except Exception as error:
            ExceptionUtils.exception_traceback(error)
            return False

    @staticmethod
    def __record_scan_error(accumulator, error, path):
        accumulator["partial"] = True
        if len(accumulator["scan_errors"]) < 100:
            accumulator["scan_errors"].append(str(error))
        path = os.path.normpath(str(path or "").strip())
        path_key = os.path.normcase(path) if path else ""
        if path_key and path_key not in accumulator["scan_error_path_set"] \
                and len(accumulator["scan_error_path_set"]) < 100:
            accumulator["scan_error_path_set"].add(path_key)
            accumulator["scan_error_paths"].append(path)

    @staticmethod
    def __record_inaccessible(accumulator, path):
        accumulator["partial"] = True
        accumulator["inaccessible_count"] += 1
        if len(accumulator["inaccessible_roots"]) < 100:
            accumulator["inaccessible_roots"].append(path)

    @classmethod
    def __finish_audit(cls, accumulator):
        elapsed = max(time.monotonic() - accumulator["started"], 0)
        partial = bool(accumulator["partial"] or accumulator["scan_errors"])
        cls.__emit_audit_progress(accumulator, "", force=True)
        return {
            "code": 0,
            "server": accumulator["server"],
            "mode": accumulator["mode"],
            "roots": accumulator["roots"],
            "root_count": accumulator["root_count"],
            "root_sample": accumulator["root_sample"],
            "inaccessible_roots": accumulator["inaccessible_roots"],
            "scan_errors": accumulator["scan_errors"],
            "scan_error_paths": accumulator["scan_error_paths"],
            "summary": accumulator["summary"],
            "media_statuses": accumulator["media_statuses"],
            "issues": accumulator["issues"],
            "issues_truncated": max(
                accumulator["issue_count"] - len(accumulator["issues"]), 0
            ),
            "probe_available": bool(shutil.which("ffprobe")),
            "coverage_complete": not partial,
            "partial": partial,
            "canceled": accumulator["canceled"],
            "stop_reason": accumulator["stop_reason"] or (
                "scan_error" if accumulator["scan_errors"] else (
                    "inaccessible" if accumulator["inaccessible_count"] else ""
                )
            ),
            "metrics": {
                "directories": accumulator["directories"],
                "candidates": accumulator["candidates"],
                "inspected": accumulator["inspected"],
                "cache_hits": accumulator["cache_hits"],
                "candidate_limit": accumulator["candidate_limit"],
                "elapsed_seconds": round(elapsed, 3),
                "inaccessible": accumulator["inaccessible_count"]
            }
        }

    @classmethod
    def __get_ffprobe_version(cls, cancel_check=None):
        if cls._ffprobe_version is not None:
            return cls._ffprobe_version
        executable = shutil.which("ffprobe")
        if not executable:
            cls._ffprobe_version = "unavailable"
            return cls._ffprobe_version
        try:
            _, stdout, _, stop_reason = cls.__run_popen(
                [executable, "-version"], timeout_seconds=3,
                cancel_check=cancel_check
            )
            if stop_reason:
                return "unknown"
            cls._ffprobe_version = (stdout or "").splitlines()[0].strip() or "unknown"
        except Exception:
            cls._ffprobe_version = "unknown"
        return cls._ffprobe_version

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
    def __repair_ass_structure(text):
        """修复可以无歧义确认的 ASS/SSA 段头截断与 Dialogue 时间分隔符。"""
        lines = text.split("\n")
        repairs = []
        canonical_script_header = "[Script Info]"
        section_names = {
            line.strip().casefold()
            for line in lines
            if re.fullmatch(r"\[[^\]]+\]", line.strip())
        }
        has_ass_sections = "[events]" in section_names and any(
            section in section_names for section in ["[v4 styles]", "[v4+ styles]"]
        )

        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            is_script_header_suffix = len(stripped) >= len("Info]") \
                and canonical_script_header.casefold().endswith(stripped.casefold()) \
                and stripped.casefold().endswith("info]")
            if has_ass_sections and is_script_header_suffix:
                lines[index] = canonical_script_header
                repairs.append("恢复缺失的 [Script Info] 段头")
            break

        in_events = False
        repaired_timestamps = 0
        timestamp_pattern = re.compile(r"^(\d+:\d{2}:\d{2}):(\d{2})$")
        for index, line in enumerate(lines):
            stripped = line.strip()
            if re.fullmatch(r"\[[^\]]+\]", stripped):
                in_events = stripped.casefold() == "[events]"
                continue
            if not in_events or not re.match(r"^\s*dialogue\s*:", line, re.I):
                continue

            prefix, payload = line.split(":", 1)
            leading = payload[:len(payload) - len(payload.lstrip())]
            fields = payload.lstrip().split(",", 9)
            if len(fields) != 10:
                continue
            changed = False
            for field_index in [1, 2]:
                value = fields[field_index].strip()
                match = timestamp_pattern.fullmatch(value)
                if not match:
                    continue
                left_space = fields[field_index][:len(fields[field_index]) - len(fields[field_index].lstrip())]
                right_space = fields[field_index][len(fields[field_index].rstrip()):]
                fields[field_index] = f"{left_space}{match.group(1)}.{match.group(2)}{right_space}"
                repaired_timestamps += 1
                changed = True
            if changed:
                lines[index] = f"{prefix}:{leading}{','.join(fields)}"

        if repaired_timestamps:
            repairs.append(f"规范化 {repaired_timestamps} 个 Dialogue 时间分隔符")
        return "\n".join(lines), repairs

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
    def __fallback_validate(cls, subtitle_file, ext, cancel_check=None):
        if ext not in cls._text_extensions:
            return {
                "valid": False, "probe_available": False,
                "message": "未安装 ffprobe，无法验证二进制或未知字幕格式"
            }
        try:
            raw = cls.__read_file_cooperative(
                subtitle_file, cls._max_local_text_bytes, cancel_check
            )
            _, text = cls.__decode_text(raw)
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
        except InterruptedError:
            return {
                "valid": False, "probe_available": False,
                "canceled": True, "message": "字幕结构检查已取消"
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
        seen = set()
        for root in roots or []:
            root = os.path.normpath(str(root or "").strip())
            key = os.path.normcase(os.path.abspath(root)) if root else ""
            if root and key not in seen:
                seen.add(key)
                result.append(root)
        return result
