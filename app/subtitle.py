import datetime
import os.path
import re
import shutil

from lxml import etree

import log
from app.conf import SiteConf
from app.helper import OpenSubtitles
from app.utils import RequestUtils, PathUtils, SystemUtils, StringUtils, ExceptionUtils
from app.utils.commons import singleton
from app.utils.types import MediaType, RmtMode
from config import Config, RMT_MEDIAEXT, RMT_SUBEXT


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

    def download_subtitle(self, items, server=None):
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
                return self.__download_opensubtitles(items)
        elif _server == "chinesesubfinder":
            return self.__download_chinesesubfinder(items)
        return False, "未配置字幕下载器"

    def upload_subtitle(self, upload_file, media_file, target_media_file=None, rmt_mode=None, server_type=None):
        """
        手动上传字幕并同步到目标媒体文件目录
        :param upload_file: Flask上传文件对象
        :param media_file: 原媒体文件路径
        :param target_media_file: 媒体库目标媒体文件路径
        :param rmt_mode: 目标同步方式
        :param server_type: 目标媒体服务器类型
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
            return False, "仅支持上传 srt、ass、ssa、smi、vtt 字幕文件", {}

        subtitle_profile = self.__guess_subtitle_profile(upload_name)
        try:
            source_sub_file = self.__build_subtitle_path(media_file, subtitle_profile, sub_ext, server_type)
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return False, f"生成源字幕文件名失败：{str(e)}", {}
        try:
            upload_file.save(source_sub_file)
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return False, f"保存源字幕失败：{str(e)}", {}

        result = {
            "source_subtitle": source_sub_file,
            "target_subtitle": "",
            "language": subtitle_profile.get("language"),
            "server": server_type or "",
            "synced": False
        }
        if not target_media_file:
            return True, "字幕已保存到源目录，未找到媒体库目标文件", result
        if not os.path.exists(target_media_file) or not os.path.isfile(target_media_file):
            return True, "字幕已保存到源目录，媒体库目标文件不存在，未同步", result
        if os.path.splitext(target_media_file)[-1].lower() not in RMT_MEDIAEXT:
            return True, "字幕已保存到源目录，目标文件不是有效媒体文件，未同步", result

        if os.path.normpath(media_file) == os.path.normpath(target_media_file):
            result["target_subtitle"] = source_sub_file
            result["synced"] = True
            return True, "字幕已保存到媒体目录", result

        source_norm = os.path.normpath(source_sub_file)
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
        if retcode != 0:
            return False, f"字幕已保存到源目录，同步到媒体库失败：{retmsg}", result
        result["synced"] = True
        return True, "字幕已保存并同步到媒体库目录", result

    @staticmethod
    def __guess_subtitle_profile(file_name):
        """
        根据字幕文件名识别语言标签和字幕标记
        """
        name = file_name or ""
        _zhcn_sub_re = r"([.\[(](((zh[-_])?(cn|ch[si]|sg|sc))|zho?|chi|chinese" \
                       r"|简[体中]?)[.\])])|中文字幕|简体|简中"
        _zhtw_sub_re = r"([.\[(](((zh[-_])?(hk|tw|cht|tc))|繁[体中]?)[.\])])" \
                       r"|繁体中[文字]|中[文字]繁体|繁体|繁中"
        _eng_sub_re = r"([.\[(](en|eng|english)[.\])])|英文"
        if re.search(_zhtw_sub_re, name, re.I):
            language = "zh-TW"
        elif re.search(_eng_sub_re, name, re.I):
            language = "eng"
        elif re.search(_zhcn_sub_re, name, re.I):
            language = "zh-CN"
        else:
            language = "zh-CN"
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
    def __build_subtitle_path(cls, media_file, subtitle_profile, sub_ext, server_type=None):
        """
        生成不覆盖已有文件的外挂字幕路径
        """
        media_base = os.path.splitext(media_file)[0]
        suffix_parts = [cls.__subtitle_language_tag(subtitle_profile.get("language"), server_type)]
        is_plex = str(server_type or "").lower() == "plex"
        if is_plex:
            suffix_parts.extend(subtitle_profile.get("flags") or [])
        suffix = ".".join([part for part in suffix_parts if part])
        target = f"{media_base}.{suffix}{sub_ext}"
        if not os.path.exists(target):
            return target
        for index in range(1, 100):
            if is_plex:
                target = f"{media_base}({index}).{suffix}{sub_ext}"
            else:
                target = f"{media_base}.{suffix}({index}){sub_ext}"
            if not os.path.exists(target):
                return target
        raise FileExistsError("字幕文件重名过多")

    @staticmethod
    def __subtitle_language_tag(language_tag, server_type=None):
        """
        按媒体服务器规范转换外挂字幕语言标签
        """
        language_tag = language_tag or "zh-CN"
        if str(server_type or "").lower() != "plex":
            return language_tag
        plex_tags = {
            "zh-CN": "zh-CN",
            "zh-TW": "zh-TW",
            "eng": "en"
        }
        return plex_tags.get(language_tag, language_tag.lower())

    @staticmethod
    def __sync_manual_subtitle(source_sub_file, target_sub_file, rmt_mode=None):
        """
        根据整理模式同步手动上传字幕
        """
        try:
            target_dir = os.path.dirname(target_sub_file)
            if target_dir and not os.path.exists(target_dir):
                os.makedirs(target_dir)
            if rmt_mode == RmtMode.LINK:
                return SystemUtils.link(source_sub_file, target_sub_file)
            if rmt_mode == RmtMode.SOFTLINK:
                return SystemUtils.softlink(source_sub_file, target_sub_file)
            return SystemUtils.copy(source_sub_file, target_sub_file)
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return -1, str(e)

    def __search_opensubtitles(self, item):
        """
        爬取OpenSubtitles.org字幕
        """
        if not self.opensubtitles:
            return []
        return self.opensubtitles.search_subtitles(item)

    def __download_opensubtitles(self, items):
        """
        调用OpenSubtitles Api下载字幕
        """
        if not self.opensubtitles:
            return False, "未配置OpenSubtitles"
        subtitles_cache = {}
        success = False
        ret_msg = ""
        for item in items:
            if not item:
                continue
            if not item.get("name") or not item.get("file"):
                continue
            if item.get("type") == MediaType.TV and not item.get("imdbid"):
                log.warn("【Subtitle】电视剧类型需要imdbid检索字幕，跳过...")
                ret_msg = "电视剧需要imdbid检索字幕"
                continue
            subtitles = subtitles_cache.get(item.get("name"))
            if subtitles is None:
                log.info(
                    "【Subtitle】开始从Opensubtitle.org检索字幕: %s，imdbid=%s" % (item.get("name"), item.get("imdbid")))
                subtitles = self.__search_opensubtitles(item)
                if not subtitles:
                    subtitles_cache[item.get("name")] = []
                    log.info("【Subtitle】%s 未检索到字幕" % item.get("name"))
                    ret_msg = "%s 未检索到字幕" % item.get("name")
                else:
                    subtitles_cache[item.get("name")] = subtitles
                    log.info("【Subtitle】opensubtitles.org返回数据：%s" % len(subtitles))
            if not subtitles:
                continue
            # 成功数
            subtitle_count = 0
            for subtitle in subtitles:
                # 标题
                if not item.get("imdbid"):
                    if str(subtitle.get('title')) != "%s (%s)" % (item.get("name"), item.get("year")):
                        continue
                # 季
                if item.get('season') \
                        and str(subtitle.get('season').replace("Season", "").strip()) != str(item.get('season')):
                    continue
                # 集
                if item.get('episode') \
                        and str(subtitle.get('episode')) != str(item.get('episode')):
                    continue
                # 字幕文件名
                SubFileName = subtitle.get('description')
                # 下载链接
                Download_Link = subtitle.get('link')
                # 下载后的字幕文件路径
                Media_File = "%s.chi.zh-cn%s" % (item.get("file"), item.get("file_ext"))
                log.info("【Subtitle】正在从opensubtitles.org下载字幕 %s 到 %s " % (SubFileName, Media_File))
                # 下载
                ret = RequestUtils(cookies=self.opensubtitles.get_cookie(),
                                   headers=self.opensubtitles.get_ua()).get_res(Download_Link)
                if ret and ret.status_code == 200:
                    # 保存ZIP
                    file_name = self.__get_url_subtitle_name(ret.headers.get('content-disposition'), Download_Link)
                    if not file_name:
                        continue
                    zip_file = os.path.join(self._save_tmp_path, file_name)
                    zip_path = os.path.splitext(zip_file)[0]
                    with open(zip_file, 'wb') as f:
                        f.write(ret.content)
                    # 解压文件
                    shutil.unpack_archive(zip_file, zip_path, format='zip')
                    # 遍历转移文件
                    for sub_file in PathUtils.get_dir_files(in_path=zip_path, exts=RMT_SUBEXT):
                        self.__transfer_subtitle(sub_file, Media_File)
                    # 删除临时文件
                    try:
                        shutil.rmtree(zip_path)
                        os.remove(zip_file)
                    except Exception as err:
                        ExceptionUtils.exception_traceback(err)
                else:
                    log.error("【Subtitle】下载字幕文件失败：%s" % Download_Link)
                    continue
                # 最多下载3个字幕
                subtitle_count += 1
                if subtitle_count > 2:
                    break
            if not subtitle_count:
                if item.get('episode'):
                    log.info("【Subtitle】%s 第%s季 第%s集 未找到符合条件的字幕" % (
                        item.get("name"), item.get("season"), item.get("episode")))
                    ret_msg = "%s 第%s季 第%s集 未找到符合条件的字幕" % (
                        item.get("name"), item.get("season"), item.get("episode"))
                else:
                    log.info("【Subtitle】%s 未找到符合条件的字幕" % item.get("name"))
                    ret_msg = "%s 未找到符合条件的字幕" % item.get("name")
            else:
                log.info("【Subtitle】%s 共下载了 %s 个字幕" % (item.get("name"), subtitle_count))
                ret_msg = "%s 共下载了 %s 个字幕" % (item.get("name"), subtitle_count)
                success = True
        if success:
            return True, ret_msg
        else:
            return False, ret_msg

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
