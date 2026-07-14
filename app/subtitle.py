import datetime
import errno
import os.path
import re
import shutil
import tempfile
import threading

from lxml import etree

import log
from app.conf import SiteConf
from app.helper import OpenSubtitles
from app.helper.subtitle_align import SubtitleAligner
from app.helper.subtitle_health import SubtitleHealth
from app.utils import RequestUtils, PathUtils, SystemUtils, StringUtils, ExceptionUtils
from app.utils.commons import singleton
from app.utils.types import MediaType, RmtMode
from config import Config, RMT_MEDIAEXT, RMT_SUBEXT
from version import APP_VERSION


@singleton
class Subtitle:
    opensubtitles = None
    _save_tmp_path = None
    _server = None
    _host = None
    _api_key = None
    _remote_path = None
    _local_path = None
    _opensubtitles_enable = False
    _repair_lock = threading.RLock()
    _jellyfin_iso639_2 = {
        "ar": "ara", "bg": "bul", "zh": "chi", "cs": "cze", "da": "dan", "nl": "dut",
        "en": "eng", "fi": "fin", "fr": "fre", "de": "ger", "el": "gre", "he": "heb",
        "hi": "hin", "hu": "hun", "id": "ind", "it": "ita", "ja": "jpn", "ko": "kor",
        "ms": "may", "no": "nor", "fa": "per", "pl": "pol", "pt": "por", "ro": "rum",
        "ru": "rus", "es": "spa", "sv": "swe", "th": "tha", "tr": "tur", "uk": "ukr",
        "vi": "vie"
    }
    _traditional_chinese_chars = set(
        "體臺萬與為這個們來時會說後裡麼還點從對開關讓過發現長門間見當無於學國華"
        "電車書風區東氣應實種樣頭總經業數據網絡軟雲檔檢測識別聲愛寫讀買賣請問謝歡樂"
    )
    _simplified_chinese_chars = set(
        "体台万与为这个们来时会说后里面么还点从对开关让过发现长门间见当无于学国华"
        "电车书风区东气应实种样头总经业数据网络软云档检测识别声爱写读买卖请问谢欢乐"
    )

    def __init__(self):
        self.init_config()

    def init_config(self):
        self.opensubtitles = OpenSubtitles()
        self._save_tmp_path = Config().get_temp_path()
        if not os.path.exists(self._save_tmp_path):
            os.makedirs(self._save_tmp_path)
        subtitle = Config().get_config('subtitle')
        if subtitle:
            self._server = subtitle.get("server")
            if self._server == "chinesesubfinder":
                self._api_key = subtitle.get("chinesesubfinder", {}).get("api_key")
                self._host = subtitle.get("chinesesubfinder", {}).get('host')
                if self._host:
                    if not self._host.startswith('http'):
                        self._host = "http://" + self._host
                    if not self._host.endswith('/'):
                        self._host = self._host + "/"
                self._local_path = subtitle.get("chinesesubfinder", {}).get("local_path")
                self._remote_path = subtitle.get("chinesesubfinder", {}).get("remote_path")
            else:
                self._opensubtitles_enable = subtitle.get("opensubtitles", {}).get("enable")

    def download_subtitle(self, items, server=None, selected_file_id=None):
        """
        字幕下载入口
        :param items: {"type":, "file", "file_ext":, "name":, "title", "year":, "season":, "episode":, "bluray":}
        :param server: 字幕下载服务器
        :return: 是否成功，消息内容
        """
        if not items:
            return False, "参数有误"
        _server = self._server if not server else server
        if not _server:
            return False, "未配置字幕下载器"
        if _server == "opensubtitles":
            if server or self._opensubtitles_enable:
                return self.__download_opensubtitles(items, selected_file_id=selected_file_id)
        elif _server == "chinesesubfinder":
            return self.__download_chinesesubfinder(items)
        return False, "未配置字幕下载器"

    def upload_subtitle(self, upload_file, media_file, target_media_file=None, rmt_mode=None, server_type=None,
                        align_mode=None):
        """
        手动上传字幕并同步到目标媒体文件目录。
        流程：
          1. 校验参数（媒体文件、字幕格式）
          2. 从文件名识别语言和标记
          3. 生成不重名字幕路径并原子写入源目录（防并发覆盖）
          4. 若目标媒体文件存在且路径不同，按 rmt_mode 同步到媒体库目录
        :param upload_file: Flask上传文件对象
        :param media_file: 原媒体文件路径（字幕首先保存在该文件同目录）
        :param target_media_file: 媒体库目标媒体文件路径（字幕同步到该文件同目录）
        :param rmt_mode: 目标同步方式（link/softlink/copy）
        :param server_type: 目标媒体服务器类型（emby/jellyfin/plex）
        :param align_mode: 字幕时间轴对齐模式（auto/offset/segmented/llm/none）
        """
        if not upload_file or not media_file:
            return False, "参数有误", {}
        if not os.path.exists(media_file) or not os.path.isfile(media_file):
            return False, "媒体文件不存在", {}
        if os.path.splitext(media_file)[-1].lower() not in RMT_MEDIAEXT:
            return False, "请选择有效的媒体文件", {}

        upload_name = os.path.basename(upload_file.filename or "")
        sub_ext = os.path.splitext(upload_name)[-1].lower()
        if sub_ext not in RMT_SUBEXT:
            return False, "仅支持上传 srt、ass、ssa、smi、vtt、sub 字幕文件", {}

        subtitle_profile = self.__guess_subtitle_profile(upload_name)
        subtitle_profile["source"] = self.__guess_subtitle_source(upload_name, media_file)

        # 通过重试机制规避并发上传导致的 TOCTOU 竞态条件
        # __build_subtitle_path 检查文件不存在后返回路径，但 save 期间可能被其他请求先创建
        _max_retry = 3
        source_sub_file = None
        for _retry in range(_max_retry):
            try:
                src_path = self.__build_subtitle_path(media_file, subtitle_profile, sub_ext, server_type)
            except FileExistsError:
                return False, "字幕文件重名过多", {}
            try:
                self.__save_upload_file_exclusive(upload_file, src_path)
                source_sub_file = src_path
                break
            except FileExistsError:
                if _retry < _max_retry - 1:
                    continue
                return False, "字幕文件保存失败，重试次数耗尽", {}
            except OSError as e:
                ExceptionUtils.exception_traceback(e)
                return False, f"保存源字幕失败：{str(e)}", {}
            except Exception as e:
                ExceptionUtils.exception_traceback(e)
                return False, f"保存源字幕失败：{str(e)}", {}
        if not source_sub_file:
            return False, "保存源字幕失败", {}

        validation = SubtitleHealth.normalize_uploaded_subtitle(source_sub_file)
        if not validation.get("valid"):
            try:
                os.remove(source_sub_file)
            except OSError:
                pass
            return False, f"字幕无法被媒体服务器解析：{validation.get('message') or '格式无效'}", {}

        normalize_messages = []
        if validation.get("normalized"):
            normalize_messages.append("已转换为 UTF-8")
        if validation.get("ass_repairs"):
            normalize_messages.extend(validation.get("ass_repairs"))
        elif validation.get("repaired"):
            normalize_messages.append(f"已修复 {validation.get('removed_blank_lines') or 0} 处 SRT 异常空行")
        normalize_msg = f"，{'，'.join(normalize_messages)}" if normalize_messages else ""

        result = {
            "source_subtitle": source_sub_file,
            "target_subtitle": "",
            "language": subtitle_profile.get("language"),
            "source": subtitle_profile.get("source") or "",
            "server": server_type or "",
            "synced": False,
            "validation": validation,
            "alignment": {"applied": False, "skipped": False, "message": "", "mode": ""}
        }
        if not target_media_file:
            return True, f"字幕已保存到源目录{normalize_msg}，未找到媒体库目标文件", result
        if not os.path.exists(target_media_file) or not os.path.isfile(target_media_file):
            return True, f"字幕已保存到源目录{normalize_msg}，媒体库目标文件不存在，未同步", result
        if os.path.splitext(target_media_file)[-1].lower() not in RMT_MEDIAEXT:
            return True, f"字幕已保存到源目录{normalize_msg}，目标文件不是有效媒体文件，未同步", result

        alignment_msg = ""
        if str(align_mode or "").lower() in ["auto", "offset", "segmented", "llm"]:
            alignment = SubtitleAligner.align_subtitle(source_sub_file, target_media_file, align_mode=align_mode)
            result["alignment"] = alignment
            if alignment.get("applied"):
                alignment_msg = "，已自动对齐字幕"
            elif alignment.get("message"):
                alignment_msg = f"，自动对齐跳过：{alignment.get('message')}"

        if os.path.normpath(media_file) == os.path.normpath(target_media_file):
            result["target_subtitle"] = source_sub_file
            result["synced"] = True
            return True, f"字幕已保存到媒体目录{normalize_msg}{alignment_msg}", result

        source_norm = os.path.normpath(source_sub_file)
        target_sub_file = ""
        retmsg = ""
        for _retry in range(_max_retry):
            try:
                target_sub_file = self.__build_subtitle_path(target_media_file, subtitle_profile, sub_ext, server_type)
            except Exception as e:
                ExceptionUtils.exception_traceback(e)
                return False, f"生成目标字幕文件名失败：{str(e)}", result
            target_norm = os.path.normpath(target_sub_file)
            if source_norm == target_norm:
                result["target_subtitle"] = target_sub_file
                result["synced"] = True
                return True, "字幕已保存到媒体目录", result

            retcode, retmsg = self.__sync_manual_subtitle(source_sub_file, target_sub_file, rmt_mode)
            result["target_subtitle"] = target_sub_file
            if retcode == 0:
                break
            if retmsg == "目标字幕文件已存在" and _retry < _max_retry - 1:
                continue
            return False, f"字幕已保存到源目录，同步到媒体库失败：{retmsg}", result
        result["synced"] = True
        return True, f"字幕已保存并同步到媒体库目录{normalize_msg}{alignment_msg}", result

    def repair_external_subtitles(self, media_file, server_type=None):
        """二次处理已有外挂字幕，使文件名、编码和内容符合目标影视服务器规则。"""
        server_type = str(server_type or "emby").lower()
        if not media_file or not os.path.isfile(media_file):
            return False, "媒体文件不存在", {}
        if os.path.splitext(media_file)[-1].lower() not in RMT_MEDIAEXT:
            return False, "请选择有效的媒体文件", {}
        if server_type not in ["emby", "jellyfin", "plex"]:
            return False, "目标影视服务器无效", {}

        with self._repair_lock:
            subtitle_files = SubtitleHealth.list_external_subtitles(media_file)
            if not subtitle_files:
                return False, "未找到可处理的外挂字幕", {}

            processed = []
            skipped = []
            failures = []
            for subtitle_file in subtitle_files:
                inspection = SubtitleHealth.inspect_external_subtitle(subtitle_file, media_file, server_type)
                if inspection.get("status") == "ok":
                    skipped.append({"path": subtitle_file, "reason": "已符合影视服务器规范"})
                    continue
                if not SubtitleHealth.is_supported_extension(subtitle_file, server_type):
                    failures.append({"path": subtitle_file, "reason": inspection.get("reason") or "字幕格式不受支持"})
                    continue

                sub_ext = os.path.splitext(subtitle_file)[-1].lower()
                work_file = ""
                try:
                    fd, work_file = tempfile.mkstemp(
                        prefix=".subtitle-repair-",
                        suffix=sub_ext,
                        dir=os.path.dirname(subtitle_file)
                    )
                    os.close(fd)
                    shutil.copy2(subtitle_file, work_file)

                    validation = SubtitleHealth.normalize_uploaded_subtitle(work_file)
                    if not validation.get("valid"):
                        failures.append({
                            "path": subtitle_file,
                            "reason": validation.get("message") or "字幕内容无法解析"
                        })
                        continue

                    subtitle_profile = self.__guess_subtitle_profile(
                        os.path.basename(subtitle_file),
                        default_language=""
                    )
                    if not subtitle_profile.get("language"):
                        subtitle_profile["language"] = self.__infer_subtitle_language(
                            work_file,
                            validation
                        ) or "zh-CN"
                    subtitle_profile["source"] = self.__guess_subtitle_source(
                        os.path.basename(subtitle_file),
                        media_file
                    )

                    if SubtitleHealth.language_defined(subtitle_file, media_file, server_type):
                        os.replace(work_file, subtitle_file)
                        work_file = ""
                        target_subtitle = subtitle_file
                    else:
                        target_subtitle = self.__build_subtitle_path(
                            media_file,
                            subtitle_profile,
                            sub_ext,
                            server_type
                        )
                        retcode, retmsg = self.__copy_subtitle_exclusive(work_file, target_subtitle)
                        if retcode != 0:
                            failures.append({"path": subtitle_file, "reason": retmsg or "生成规范字幕失败"})
                            continue

                        repaired_inspection = SubtitleHealth.inspect_external_subtitle(
                            target_subtitle,
                            media_file,
                            server_type
                        )
                        if repaired_inspection.get("status") != "ok":
                            try:
                                os.remove(target_subtitle)
                            except OSError:
                                pass
                            failures.append({
                                "path": subtitle_file,
                                "reason": repaired_inspection.get("reason") or "规范化后仍无法识别"
                            })
                            continue
                        try:
                            os.remove(subtitle_file)
                        except OSError as e:
                            try:
                                os.remove(target_subtitle)
                            except OSError:
                                pass
                            failures.append({"path": subtitle_file, "reason": f"替换原字幕失败：{str(e)}"})
                            continue

                    processed.append({
                        "source": subtitle_file,
                        "target": target_subtitle,
                        "language": subtitle_profile.get("language") or "",
                        "source_name": subtitle_profile.get("source") or ""
                    })
                except (FileExistsError, OSError) as e:
                    failures.append({"path": subtitle_file, "reason": str(e)})
                finally:
                    if work_file and os.path.exists(work_file):
                        try:
                            os.remove(work_file)
                        except OSError:
                            pass

            results = SubtitleHealth.inspect_media_subtitles(media_file, server_type)
            aggregate = SubtitleHealth.aggregate_media_subtitles(results)
            data = {
                "media_file": media_file,
                "server": server_type,
                "processed": processed,
                "skipped": skipped,
                "failures": failures,
                "subtitle_count": len(results),
                "status": aggregate.get("status") or "",
                "reason": aggregate.get("reason") or ""
            }
            if not processed:
                reason = failures[0].get("reason") if failures else "现有字幕已符合规范"
                return False, f"没有可完成二次处理的字幕：{reason}", data
            message = f"已完成 {len(processed)} 个外挂字幕的二次处理"
            if len(results) > 1:
                message += f"，保留 {len(results)} 个可独立选择的字幕轨道"
            if failures:
                message += f"，另有 {len(failures)} 个字幕处理失败"
            return True, message, data

    @classmethod
    def __guess_subtitle_profile(cls, file_name, default_language="zh-CN"):
        """
        根据字幕文件名识别语言标签和字幕标记
        匹配优先级：简体中文 > 繁体中文 > 英文 > 通用 ISO/BCP-47 标签 > 默认语言
        zh-CN 优先级最高，避免文件名同时含中英文标记（如 .chinese.eng.）时误判为英文
        """
        name = file_name or ""
        _zhcn_sub_re = r"([.\[(](((zh[-_])?(cn|ch[si]|sg|sc))|zho?|chi|chinese" \
                       r"|简[体中]?)[.\])])|中文字幕|简体|简中"
        _zhtw_sub_re = r"([.\[(](((zh[-_])?(hk|tw|cht|tc))|繁[体中]?)[.\])])" \
                       r"|繁体中[文字]|中[文字]繁体|繁体|繁中"
        _eng_sub_re = r"([.\[(](en|eng|english)[.\])])|英文"
        if re.search(_zhcn_sub_re, name, re.I):
            language = "zh-CN"
        elif re.search(_zhtw_sub_re, name, re.I):
            language = "zh-TW"
        elif re.search(_eng_sub_re, name, re.I):
            language = "eng"
        else:
            language = ""
            base_name = os.path.splitext(os.path.basename(name))[0]
            for token in reversed(re.split(r"[.\s\[\](){}]+", base_name)):
                language = cls.__normalize_language_token(token)
                if language:
                    break
            if not language:
                language = default_language
        # 识别字幕特殊标记（forced / SDH / CC）
        lower_name = name.lower()
        flags = []
        if re.search(r"(^|[.\-_\[( ])forced($|[.\-_\]) ])", lower_name):
            flags.append("forced")
        if re.search(r"(^|[.\-_\[( ])sdh($|[.\-_\]) ])", lower_name):
            flags.append("sdh")
        elif re.search(r"(^|[.\-_\[( ])cc($|[.\-_\]) ])", lower_name):
            flags.append("cc")
        return {"language": language, "flags": flags}

    @classmethod
    def __guess_subtitle_source(cls, file_name, media_file):
        """从同名字幕后缀提取来源标题，供 Jellyfin 区分同语种多字幕。"""
        subtitle_base = os.path.splitext(os.path.basename(file_name or ""))[0]
        media_base = os.path.splitext(os.path.basename(media_file or ""))[0]
        if not subtitle_base or not media_base \
                or not subtitle_base.casefold().startswith(media_base.casefold()):
            return ""
        suffix = subtitle_base[len(media_base):].strip(" .-_[]()")
        if not suffix:
            return ""
        ignored = {
            "zh", "zh-cn", "zh-tw", "zh-hans", "zh-hant", "cn", "tw", "chi", "zho", "chs", "cht",
            "en", "eng", "english", "chinese", "forced", "foreign", "sdh", "cc", "hi",
            "subtitle", "sub", "简", "简中", "简体", "繁", "繁中", "繁体", "中文", "中文字幕"
        }
        tokens = []
        for token in re.split(r"[.\s\[\](){}]+", suffix):
            token = token.strip(" .-_")
            if not token or token.casefold() in ignored or cls.__normalize_language_token(token):
                continue
            tokens.append(token)
        source = "-".join(tokens)
        source = re.sub(r"[^\w-]+", "-", source, flags=re.UNICODE).strip("-_")
        return source[:32]

    @staticmethod
    def __normalize_language_token(token):
        token = str(token or "").strip().lower().replace("_", "-")
        if not token:
            return ""
        primary, _, region = token.partition("-")
        if primary not in SubtitleHealth._iso639_1_tokens \
                and token not in SubtitleHealth._jellyfin_language_tokens:
            return ""
        if region and re.fullmatch(r"[a-z0-9]{2,8}", region):
            region = region.upper() if len(region) == 2 and region.isalpha() else region
            return f"{primary}-{region}"
        return token

    @classmethod
    def __infer_subtitle_language(cls, subtitle_file, validation=None):
        """在文件名没有语言标记时，从规范化后的字幕正文推断常见语言。"""
        encoding = str((validation or {}).get("encoding") or "").lower().replace("-", "")
        if "big5" in encoding:
            return "zh-TW"
        try:
            with open(subtitle_file, "r", encoding="utf-8-sig", errors="ignore") as file_obj:
                text = file_obj.read(512 * 1024)
        except OSError:
            return ""
        if not text:
            return ""

        ext = os.path.splitext(subtitle_file)[-1].lower()
        if ext in [".ass", ".ssa"]:
            dialogue = []
            for line in text.splitlines():
                if line.lstrip().lower().startswith("dialogue:"):
                    dialogue.append(line.split(",", 9)[-1])
            if dialogue:
                text = "\n".join(dialogue)
        text = re.sub(r"\{[^}]*}|<[^>]*>|\\[NnH]", " ", text)

        if len(re.findall(r"[\u3040-\u30ff]", text)) >= 3:
            return "jpn"
        if len(re.findall(r"[\uac00-\ud7af]", text)) >= 3:
            return "kor"

        chinese_chars = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text)
        if len(chinese_chars) >= 3:
            traditional_hits = sum(char in cls._traditional_chinese_chars for char in chinese_chars)
            simplified_hits = sum(char in cls._simplified_chinese_chars for char in chinese_chars)
            return "zh-TW" if traditional_hits > simplified_hits else "zh-CN"

        if len(re.findall(r"[\u0400-\u04ff]", text)) >= 3:
            return "rus"
        if len(re.findall(r"[\u0370-\u03ff]", text)) >= 3:
            return "gre"
        if len(re.findall(r"[\u0590-\u05ff]", text)) >= 3:
            return "heb"
        if len(re.findall(r"[\u0600-\u06ff]", text)) >= 3:
            return "ara"
        if len(re.findall(r"[\u0e00-\u0e7f]", text)) >= 3:
            return "tha"
        if len(re.findall(r"[a-zA-Z]", text)) >= 20:
            return "eng"
        return ""

    @classmethod
    def __build_subtitle_path(cls, media_file, subtitle_profile, sub_ext, server_type=None):
        """
        生成不覆盖已有文件的外挂字幕路径。
        命名格式：
          - Emby:          Movie.zh-CN.srt → Movie.zh-CN(1).srt → ...
          - Jellyfin:      Movie.chi.zh-cn.srt → Movie.source-2.chi.zh-cn.srt → ...
          - Plex:          Movie.zh-CN.srt → Movie(1).zh-CN.srt → ...
                           (forced/sdh/cc 标记拼接在语言标签后: Movie.en.forced.sdh.srt)
        重名时最多尝试 100 个编号，超过则抛出 FileExistsError。
        """
        media_base = os.path.splitext(media_file)[0]
        suffix_parts = [cls.__subtitle_language_tag(subtitle_profile.get("language"), server_type)]
        normalized_server = str(server_type or "").lower()
        is_plex = normalized_server == "plex"
        is_jellyfin = normalized_server == "jellyfin"
        if normalized_server in ["plex", "jellyfin"]:
            suffix_parts.extend(subtitle_profile.get("flags") or [])
        suffix = ".".join([part for part in suffix_parts if part])
        source = str(subtitle_profile.get("source") or "").strip(" .-_")
        target = f"{media_base}.{source}.{suffix}{sub_ext}" if is_jellyfin and source \
            else f"{media_base}.{suffix}{sub_ext}"
        if not os.path.exists(target):
            return target
        for index in range(1, 100):
            if is_plex:
                target = f"{media_base}({index}).{suffix}{sub_ext}"
            elif is_jellyfin:
                source_name = f"{source}-{index + 1}" if source else f"source-{index + 1}"
                target = f"{media_base}.{source_name}.{suffix}{sub_ext}"
            else:
                target = f"{media_base}.{suffix}({index}){sub_ext}"
            if not os.path.exists(target):
                return target
        raise FileExistsError("字幕文件重名过多")

    @classmethod
    def __subtitle_language_tag(cls, language_tag, server_type=None):
        """
        按媒体服务器规范转换外挂字幕语言标签。
        Jellyfin 使用 ISO 639-2 语言码并保留简繁标题，Plex 使用 ISO 639-1，Emby保留原始标签。
        """
        language_tag = language_tag or "zh-CN"
        server_type = str(server_type or "").lower()
        if server_type == "jellyfin":
            jellyfin_tags = {
                "zh-CN": "chi.zh-cn",
                "zh-TW": "chi.zh-tw",
                "eng": "eng"
            }
            if language_tag in jellyfin_tags:
                return jellyfin_tags[language_tag]
            normalized = language_tag.lower().replace("_", "-")
            primary, _, region = normalized.partition("-")
            jellyfin_language = cls._jellyfin_iso639_2.get(primary, primary)
            if region:
                return f"{jellyfin_language}.{normalized}"
            return jellyfin_language
        if server_type != "plex":
            return language_tag
        plex_tags = {
            "zh-CN": "zh-CN",
            "zh-TW": "zh-TW",
            "eng": "en"
        }
        return plex_tags.get(language_tag, language_tag.lower())

    @staticmethod
    def __save_upload_file_exclusive(upload_file, target_file):
        """
        独占创建并保存上传文件，避免并发上传覆盖同名字幕。
        """
        fd = None
        try:
            fd = os.open(target_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "wb") as file_obj:
                fd = None
                upload_file.save(file_obj)
        except OSError as e:
            if e.errno == errno.EEXIST:
                raise FileExistsError(target_file)
            if fd is not None:
                os.close(fd)
            if os.path.exists(target_file):
                try:
                    os.remove(target_file)
                except Exception:
                    pass
            raise
        except Exception:
            if fd is not None:
                os.close(fd)
            if os.path.exists(target_file):
                try:
                    os.remove(target_file)
                except Exception:
                    pass
            raise

    @staticmethod
    def __copy_subtitle_exclusive(source_sub_file, target_sub_file):
        """
        独占复制字幕，目标存在时直接失败，避免硬链接降级复制时覆盖其它请求刚写入的字幕。
        """
        fd = None
        try:
            fd = os.open(target_sub_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with open(os.path.normpath(source_sub_file), "rb") as src_obj:
                with os.fdopen(fd, "wb") as dest_obj:
                    fd = None
                    shutil.copyfileobj(src_obj, dest_obj)
            shutil.copystat(os.path.normpath(source_sub_file), os.path.normpath(target_sub_file))
            return 0, ""
        except OSError as e:
            if fd is not None:
                os.close(fd)
            if e.errno == errno.EEXIST:
                return -1, "目标字幕文件已存在"
            if os.path.exists(target_sub_file):
                try:
                    os.remove(target_sub_file)
                except Exception:
                    pass
            ExceptionUtils.exception_traceback(e)
            return -1, str(e)
        except Exception as e:
            if fd is not None:
                os.close(fd)
            if os.path.exists(target_sub_file):
                try:
                    os.remove(target_sub_file)
                except Exception:
                    pass
            ExceptionUtils.exception_traceback(e)
            return -1, str(e)

    @classmethod
    def __sync_manual_subtitle(cls, source_sub_file, target_sub_file, rmt_mode=None):
        """
        根据整理模式同步手动上传字幕（链接/复制）
        硬链接失败时自动降级为复制，兼容跨文件系统场景
        """
        try:
            target_dir = os.path.dirname(target_sub_file)
            if target_dir and not os.path.exists(target_dir):
                os.makedirs(target_dir)
            if rmt_mode == RmtMode.LINK:
                retcode, retmsg = SystemUtils.link(source_sub_file, target_sub_file)
                if retcode != 0:
                    return cls.__copy_subtitle_exclusive(source_sub_file, target_sub_file)
                return 0, ""
            if rmt_mode == RmtMode.SOFTLINK:
                return SystemUtils.softlink(source_sub_file, target_sub_file)
            return cls.__copy_subtitle_exclusive(source_sub_file, target_sub_file)
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return -1, str(e)

    @staticmethod
    def __subtitle_language_suffix(language):
        return "chi.zh-tw" if language == "zh-tw" else "chi.zh-cn"

    def __subtitle_target(self, item, language):
        return "%s.%s.srt" % (item.get("file"), self.__subtitle_language_suffix(language))

    def __existing_opensubtitles_target(self, item):
        media_base = str(item.get("file") or "")
        media_dir = os.path.dirname(media_base) or "."
        media_name = os.path.basename(media_base)
        try:
            for file_name in os.listdir(media_dir):
                stem, extension = os.path.splitext(file_name)
                if extension.lower() in RMT_SUBEXT and (stem == media_name or stem.startswith("%s." % media_name)):
                    return os.path.join(media_dir, file_name)
        except OSError:
            pass

        checked = set()
        for language in self.opensubtitles.languages:
            base_target = "%s.%s" % (item.get("file"), self.__subtitle_language_suffix(language))
            for extension in RMT_SUBEXT:
                target = "%s%s" % (base_target, extension)
                if target not in checked and os.path.exists(target):
                    return target
                checked.add(target)
        return None

    @staticmethod
    def __public_candidates(candidates):
        return [{
            "file_id": candidate.get("file_id"),
            "language": candidate.get("language"),
            "release": candidate.get("release"),
            "match_type": candidate.get("match_type"),
            "similarity": candidate.get("similarity"),
            "from_trusted": candidate.get("from_trusted"),
            "ai_translated": candidate.get("ai_translated"),
            "ratings": candidate.get("ratings"),
            "download_count": candidate.get("download_count"),
        } for candidate in candidates[:5]]

    @staticmethod
    def __valid_subtitle_content(content):
        if not content or len(content) > 20 * 1024 * 1024:
            return False, None
        text = None
        encodings = ["utf-8-sig"]
        if content.startswith((b"\xff\xfe", b"\xfe\xff")):
            encodings.append("utf-16")
        encodings.extend(("gb18030", "big5"))
        for encoding in encodings:
            try:
                text = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            return False, None
        leading = text.lstrip().lower()
        if leading.startswith("<!doctype html") or leading.startswith("<html") \
                or leading.startswith("{") or leading.startswith("["):
            return False, None
        if "-->" not in text:
            return False, None
        return True, text

    def __fetch_temporary_subtitle(self, link):
        headers = {"User-Agent": "NAS-Tools %s" % APP_VERSION, "Accept": "text/plain,*/*"}
        response = None
        for _ in range(2):
            response = RequestUtils(headers=headers, proxies=Config().get_proxies(), timeout=30).get_res(link)
            if response is not None and response.status_code == 200:
                valid, text = self.__valid_subtitle_content(response.content)
                if valid:
                    return text, ""
        status = response.status_code if response is not None else "N/A"
        return None, "字幕临时链接下载失败（HTTP %s），未再次消耗下载配额" % status

    def __save_opensubtitles_content(self, item, candidate, text):
        target = self.__subtitle_target(item, candidate.get("language"))
        if os.path.exists(target):
            return True, "字幕已存在：%s" % target
        temp_target = "%s.nastool.tmp" % target
        try:
            with open(temp_target, "w", encoding="utf-8", newline="\n") as subtitle_file:
                subtitle_file.write(text)
            os.replace(temp_target, target)
            return True, target
        except OSError as err:
            try:
                if os.path.exists(temp_target):
                    os.remove(temp_target)
            except OSError:
                pass
            return False, "字幕写入失败：%s" % err

    def __download_opensubtitles(self, items, selected_file_id=None):
        """Search freely, then consume at most one /download quota per item."""
        if not self.opensubtitles:
            return False, "未配置OpenSubtitles"
        configured, error = self.opensubtitles.is_configured(require_login=True)
        if not configured:
            return False, error
        for item in items or []:
            if not item or not item.get("name") or not item.get("file"):
                continue
            existing = self.__existing_opensubtitles_target(item)
            if existing:
                return True, "字幕已存在：%s" % existing
            log.info("【Subtitle】开始通过OpenSubtitles.com API检索字幕：%s" % item.get("name"))
            candidates, error = self.opensubtitles.search_subtitles(item)
            if error:
                return False, error
            if not candidates:
                return False, "%s 未检索到中文字幕" % item.get("name")

            selected = None
            if selected_file_id is not None:
                try:
                    selected_id = int(selected_file_id)
                except (TypeError, ValueError):
                    return False, "字幕候选ID无效"
                selected = next((candidate for candidate in candidates
                                 if candidate.get("file_id") == selected_id), None)
                if not selected:
                    return False, "所选字幕已不在当前检索结果中，请重新选择"
            else:
                selected = next((candidate for candidate in candidates
                                 if candidate.get("high_confidence")), None)
                if not selected:
                    return False, {
                        "msg": "未找到高置信字幕，请确认候选后再消耗一次下载配额",
                        "candidates": self.__public_candidates(candidates)
                    }

            payload, error = self.opensubtitles.download(selected.get("file_id"))
            if error:
                return False, error
            text, error = self.__fetch_temporary_subtitle(payload.get("link"))
            if error:
                return False, error
            success, result = self.__save_opensubtitles_content(item, selected, text)
            if not success:
                return False, result
            remaining = payload.get("remaining")
            reset_time = payload.get("reset_time")
            quota = "，剩余下载次数：%s" % remaining if remaining is not None else ""
            if reset_time:
                quota += "，重置时间：%s" % reset_time
            log.info("【Subtitle】OpenSubtitles字幕下载成功：%s%s" % (result, quota))
            return True, "字幕下载成功：%s%s" % (result, quota)
        return False, "没有可处理的媒体文件"

    def __download_chinesesubfinder(self, items):
        """
        调用ChineseSubFinder下载字幕
        """
        if not self._host or not self._api_key:
            return False, "未配置ChineseSubFinder"
        req_url = "%sapi/v1/add-job" % self._host
        notify_items = []
        success = False
        ret_msg = ""
        for item in items:
            if not item:
                continue
            if not item.get("name") or not item.get("file"):
                continue
            if item.get("bluray"):
                file_path = "%s.mp4" % item.get("file")
            else:
                if os.path.splitext(item.get("file"))[-1] != item.get("file_ext"):
                    file_path = "%s%s" % (item.get("file"), item.get("file_ext"))
                else:
                    file_path = item.get("file")

            # 路径替换
            if self._local_path and self._remote_path and file_path.startswith(self._local_path):
                file_path = file_path.replace(self._local_path, self._remote_path).replace('\\', '/')

            # 一个名称只建一个任务
            if file_path not in notify_items:
                notify_items.append(file_path)
                log.info("【Subtitle】通知ChineseSubFinder下载字幕: %s" % file_path)
                params = {
                    "video_type": 0 if item.get("type") == MediaType.MOVIE else 1,
                    "physical_video_file_full_path": file_path,
                    "task_priority_level": 3,
                    "media_server_inside_video_id": "",
                    "is_bluray": item.get("bluray")
                }
                try:
                    res = RequestUtils(headers={
                        "Authorization": "Bearer %s" % self._api_key
                    }).post(req_url, json=params)
                    if not res or res.status_code != 200:
                        log.error("【Subtitle】调用ChineseSubFinder API失败！")
                        ret_msg = "调用ChineseSubFinder API失败"
                    else:
                        # 如果文件目录没有识别的nfo元数据， 此接口会返回控制符，推测是ChineseSubFinder的原因
                        # emby refresh元数据时异步的
                        if res.text:
                            job_id = res.json().get("job_id")
                            message = res.json().get("message")
                            if not job_id:
                                log.warn("【Subtitle】ChineseSubFinder下载字幕出错：%s" % message)
                                ret_msg = "ChineseSubFinder下载字幕出错：%s" % message
                            else:
                                log.info("【Subtitle】ChineseSubFinder任务添加成功：%s" % job_id)
                                ret_msg = "ChineseSubFinder任务添加成功：%s" % job_id
                                success = True
                        else:
                            log.error("【Subtitle】%s 目录缺失nfo元数据" % file_path)
                            ret_msg = "%s 目录下缺失nfo元数据：" % file_path
                except Exception as e:
                    ExceptionUtils.exception_traceback(e)
                    log.error("【Subtitle】连接ChineseSubFinder出错：" + str(e))
                    ret_msg = "连接ChineseSubFinder出错：%s" % str(e)
        if success:
            return True, ret_msg
        else:
            return False, ret_msg

    @staticmethod
    def __transfer_subtitle(sub_file, media_file):
        """
        转移字幕
        """
        new_sub_file = "%s%s" % (os.path.splitext(media_file)[0], os.path.splitext(sub_file)[-1])
        if os.path.exists(new_sub_file):
            return 1
        else:
            return SystemUtils.copy(sub_file, new_sub_file)

    def download_subtitle_from_site(self, media_info, cookie, ua, download_dir):
        """
        从站点下载字幕文件，并保存到本地
        """
        if not media_info.page_url:
            return
        # 字幕下载目录
        log.info("【Subtitle】开始从站点下载字幕：%s" % media_info.page_url)
        if not download_dir:
            log.warn("【Subtitle】未找到字幕下载目录")
            return
        # 读取网站代码
        request = RequestUtils(cookies=cookie, headers=ua)
        res = request.get_res(media_info.page_url)
        if res and res.status_code == 200:
            if not res.text:
                log.warn(f"【Subtitle】读取页面代码失败：{media_info.page_url}")
                return
            html = etree.HTML(res.text)
            sublink_list = []
            for xpath in SiteConf.SITE_SUBTITLE_XPATH:
                sublinks = html.xpath(xpath)
                if sublinks:
                    for sublink in sublinks:
                        if not sublink:
                            continue
                        if not sublink.startswith("http"):
                            base_url = StringUtils.get_base_url(media_info.page_url)
                            if sublink.startswith("/"):
                                sublink = "%s%s" % (base_url, sublink)
                            else:
                                sublink = "%s/%s" % (base_url, sublink)
                        sublink_list.append(sublink)
            # 下载所有字幕文件
            for sublink in sublink_list:
                log.info(f"【Subtitle】找到字幕下载链接：{sublink}，开始下载...")
                # 下载
                ret = request.get_res(sublink)
                if ret and ret.status_code == 200:
                    # 创建目录
                    if not os.path.exists(download_dir):
                        os.makedirs(download_dir)
                    # 保存ZIP
                    file_name = self.__get_url_subtitle_name(ret.headers.get('content-disposition'), sublink)
                    if not file_name:
                        log.warn(f"【Subtitle】链接不是字幕文件：{sublink}")
                        continue
                    if file_name.lower().endswith(".zip"):
                        # ZIP包
                        zip_file = os.path.join(self._save_tmp_path, file_name)
                        # 解压路径
                        zip_path = os.path.splitext(zip_file)[0]
                        with open(zip_file, 'wb') as f:
                            f.write(ret.content)
                        # 解压文件
                        shutil.unpack_archive(zip_file, zip_path, format='zip')
                        # 遍历转移文件
                        for sub_file in PathUtils.get_dir_files(in_path=zip_path, exts=RMT_SUBEXT):
                            target_sub_file = os.path.join(download_dir,
                                                           os.path.splitext(os.path.basename(sub_file))[0])
                            log.info(f"【Subtitle】转移字幕 {sub_file} 到 {target_sub_file}")
                            self.__transfer_subtitle(sub_file, target_sub_file)
                        # 删除临时文件
                        try:
                            shutil.rmtree(zip_path)
                            os.remove(zip_file)
                        except Exception as err:
                            ExceptionUtils.exception_traceback(err)
                    else:
                        sub_file = os.path.join(self._save_tmp_path, file_name)
                        # 保存
                        with open(sub_file, 'wb') as f:
                            f.write(ret.content)
                        target_sub_file = os.path.join(download_dir,
                                                       os.path.splitext(os.path.basename(sub_file))[0])
                        log.info(f"【Subtitle】转移字幕 {sub_file} 到 {target_sub_file}")
                        self.__transfer_subtitle(sub_file, target_sub_file)
                else:
                    log.error(f"【Subtitle】下载字幕文件失败：{sublink}")
                    continue
            if sublink_list:
                log.info(f"【Subtitle】{media_info.page_url} 页面字幕下载完成")
        elif res is not None:
            log.warn(f"【Subtitle】连接 {media_info.page_url} 失败，状态码：{res.status_code}")
        else:
            log.warn(f"【Subtitle】无法打开链接：{media_info.page_url}")

    @staticmethod
    def __get_url_subtitle_name(disposition, url):
        """
        从下载请求中获取字幕文件名
        """
        file_name = re.findall(r"filename=\"?(.+)\"?", disposition or "")
        if file_name:
            file_name = str(file_name[0].encode('ISO-8859-1').decode()).split(";")[0].strip()
            if file_name.endswith('"'):
                file_name = file_name[:-1]
        elif url and os.path.splitext(url)[-1] in (RMT_SUBEXT + ['.zip']):
            file_name = url.split("/")[-1]
        else:
            file_name = str(datetime.datetime.now())
        return file_name
