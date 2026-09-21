from app.utils import ExceptionUtils
from app.utils.types import MediaServerType

import log
import json
import os
from config import Config
from app.mediaserver.client._base import _IMediaClient
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer


class Plex(_IMediaClient):
    schema = "plex"
    server_type = MediaServerType.PLEX.value
    _client_config = {}

    _host = None
    _token = None
    _username = None
    _password = None
    _servername = None
    _plex = None
    _libraries = []

    def __init__(self, config=None):
        if config:
            self._client_config = config
        else:
            self._client_config = Config().get_config('plex')
        self.init_config()

    def init_config(self):
        if self._client_config:
            self._host = self._client_config.get('host')
            self._token = self._client_config.get('token')
            if self._host:
                if not self._host.startswith('http'):
                    self._host = "http://" + self._host
                if not self._host.endswith('/'):
                    self._host = self._host + "/"
            self._username = self._client_config.get('username')
            self._password = self._client_config.get('password')
            self._servername = self._client_config.get('servername')
            if self._host and self._token:
                try:
                    self._plex = PlexServer(self._host, self._token)
                except Exception as e:
                    ExceptionUtils.exception_traceback(e)
                    self._plex = None
                    log.error(f"【{self.server_type}】Plex服务器连接失败：{str(e)}")
            elif self._username and self._password and self._servername:
                try:
                    self._plex = MyPlexAccount(self._username, self._password).resource(self._servername).connect()
                except Exception as e:
                    ExceptionUtils.exception_traceback(e)
                    self._plex = None
                    log.error(f"【{self.server_type}】Plex服务器连接失败：{str(e)}")

    @classmethod
    def match(cls, ctype):
        return True if ctype in [cls.schema, cls.server_type] else False

    def get_status(self):
        """
        测试连通性
        """
        return True if self._plex else False

    @staticmethod
    def get_user_count(**kwargs):
        """
        获得用户数量，Plex只能配置一个用户，固定返回1
        """
        return 1

    def get_activity_log(self, num):
        """
        获取Plex活动记录
        """
        if not self._plex:
            return []
        ret_array = []
        historys = self._plex.library.history(num)
        for his in historys:
            event_type = "PL"
            event_date = his.viewedAt.strftime('%Y-%m-%d %H:%M:%S')
            event_str = "开始播放 %s" % his.title
            activity = {"type": event_type, "event": event_str, "date": event_date}
            ret_array.append(activity)
        if ret_array:
            ret_array = sorted(ret_array, key=lambda x: x['date'], reverse=True)
        return ret_array

    def get_medias_count(self):
        """
        获得电影、电视剧、动漫媒体数量
        :return: MovieCount SeriesCount SongCount
        """
        if not self._plex:
            return {}
        sections = self._plex.library.sections()
        MovieCount = SeriesCount = SongCount = 0
        for sec in sections:
            if sec.type == "movie":
                MovieCount += sec.totalSize
            if sec.type == "show":
                SeriesCount += sec.totalSize
            if sec.type == "artist":
                SongCount += sec.totalSize
        return {"MovieCount": MovieCount, "SeriesCount": SeriesCount, "SongCount": SongCount, "EpisodeCount": 0}

    def get_movies(self, title, year=None):
        """
        根据标题和年份，检查电影是否在Plex中存在，存在则返回列表
        :param title: 标题
        :param year: 年份，为空则不过滤
        :return: 含title、year属性的字典列表
        """
        if not self._plex:
            return None
        ret_movies = []
        if year:
            movies = self._plex.library.search(title=title, year=year, libtype="movie")
        else:
            movies = self._plex.library.search(title=title, libtype="movie")
        for movie in movies:
            ret_movies.append({'title': movie.title, 'year': movie.year})
        return ret_movies

    # 根据标题、年份、季、总集数，查询Plex中缺少哪几集
    def get_no_exists_episodes(self, meta_info, season, total_num):
        """
        根据标题、年份、季、总集数，查询Plex中缺少哪几集
        :param meta_info: 已识别的需要查询的媒体信息
        :param season: 季号，数字
        :param total_num: 该季的总集数
        :return: 该季不存在的集号列表
        """
        if not self._plex:
            return None
        exists_episodes = []
        video = self._plex.library.search(title=meta_info.title, year=meta_info.year, libtype="show")
        if video:
            for episode in video[0].episodes():
                if episode.seasonNumber == season:
                    exists_episodes.append(episode.index)
        total_episodes = [episode for episode in range(1, total_num + 1)]
        return list(set(total_episodes).difference(set(exists_episodes)))

    @staticmethod
    def get_image_by_id(**kwargs):
        """
        根据ItemId从Plex查询图片地址，该函数Plex下不使用
        """
        return None

    def refresh_root_library(self):
        """
        通知Plex刷新整个媒体库
        """
        if not self._plex:
            return False
        return self._plex.library.update()

    def refresh_library_by_items(self, items):
        """
        按类型、名称、年份来刷新媒体库，未找到对应的API，直接刷整库
        """
        if not self._plex:
            return False
        return self._plex.library.update()

    def refresh_subtitle_target(self, server_item_id=None,
                                parent_server_item_id=None,
                                library_id=None, media_path=None):
        """刷新 Plex 精确项目；项目不可用时仅刷新可信 section 下的目标目录。"""
        if not self._plex:
            return {"status": "failed", "scope": "none", "message": "Plex 未连接"}
        item_ids = []
        if server_item_id:
            item_ids.append((server_item_id, "item"))
        if parent_server_item_id and str(parent_server_item_id) != str(server_item_id or ""):
            item_ids.append((parent_server_item_id, "parent"))
        for item_id, scope in item_ids:
            try:
                item = self._plex.fetchItem(item_id)
                if item and hasattr(item, "refresh"):
                    item.refresh()
                    return {
                        "status": "refreshed", "scope": scope,
                        "item_id": item_id,
                        "message": "已刷新 Plex 项目" if scope == "item" else "已刷新 Plex 父剧集"
                    }
            except Exception as e:
                ExceptionUtils.exception_traceback(e)

        if not library_id or not media_path:
            return {
                "status": "skipped", "scope": "none",
                "message": "缺少可信 Plex section 或目标路径，已跳过刷新"
            }
        target_dir = os.path.dirname(os.path.abspath(os.path.normpath(media_path)))
        try:
            section = self._plex.library.sectionByID(library_id)
            locations = [
                os.path.abspath(os.path.normpath(path))
                for path in (getattr(section, "locations", None) or []) if path
            ]
            mapped = False
            for root in locations:
                try:
                    if os.path.normcase(os.path.commonpath([target_dir, root])) \
                            == os.path.normcase(root):
                        mapped = True
                        break
                except (OSError, ValueError, TypeError):
                    # Multi-location Plex sections may span Windows drives.
                    # An incompatible root must not hide a later valid one.
                    continue
            if not section or not mapped:
                return {
                    "status": "skipped", "scope": "path",
                    "message": "目标目录不在已验证的 Plex section 映射内"
                }
            section.update(path=target_dir)
            return {
                "status": "refreshed", "scope": "path",
                "library_id": library_id, "path": target_dir,
                "message": "已刷新 Plex section 的目标父目录"
            }
        except (OSError, ValueError) as e:
            ExceptionUtils.exception_traceback(e)
            return {
                "status": "skipped", "scope": "path",
                "message": "无法验证 Plex section 路径映射"
            }
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return {
                "status": "failed", "scope": "path",
                "message": "Plex 目标目录刷新失败"
            }

    def get_libraries(self):
        """
        获取媒体服务器所有媒体库列表
        """
        if not self._plex:
            return []
        try:
            self._libraries = self._plex.library.sections()
        except Exception as err:
            ExceptionUtils.exception_traceback(err)
            return []
        libraries = []
        for library in self._libraries:
            libraries.append({"id": library.key, "name": library.title})
        return libraries

    def get_items(self, parent):
        """
        获取媒体服务器所有媒体库列表
        """
        if not parent:
            yield {}
        if not self._plex:
            yield {}
        try:
            section = self._plex.library.sectionByID(parent)
            if section:
                for item in section.all():
                    if not item:
                        continue
                    item_path = self.__item_path(item)
                    provider_ids = self.__provider_ids(item)
                    yield {"id": item.key,
                           "library": item.librarySectionID,
                           "type": item.type,
                           "title": item.title,
                           "year": item.year,
                           "tmdbid": provider_ids.get("tmdbid"),
                           "imdbid": provider_ids.get("imdbid"),
                           "path": item_path,
                           "json": json.dumps({
                               "Path": item_path,
                               "ProviderIds": provider_ids,
                               "MediaStreams": []
                           }, ensure_ascii=False)}
        except Exception as err:
            ExceptionUtils.exception_traceback(err)
        yield {}

    def get_episodes(self, series_id):
        """返回带真实 ratingKey、父剧集和 section 标识的 Plex 剧集。"""
        if not series_id or not self._plex:
            return []
        episodes = []
        try:
            series = self._plex.fetchItem(series_id)
            for episode in series.episodes() if series else []:
                item_path = self.__item_path(episode)
                item_id = getattr(episode, "ratingKey", None) or getattr(episode, "key", None)
                parent_id = getattr(episode, "grandparentRatingKey", None) or series_id
                episodes.append({
                    "id": item_id,
                    "server_item_id": item_id,
                    "series_id": parent_id,
                    "parent_server_item_id": parent_id,
                    "library": getattr(episode, "librarySectionID", None),
                    "library_id": getattr(episode, "librarySectionID", None),
                    "type": getattr(episode, "type", "episode"),
                    "title": getattr(episode, "title", ""),
                    "season": getattr(episode, "seasonNumber", None),
                    "episode": getattr(episode, "index", None),
                    "path": item_path,
                    "media_streams": []
                })
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
        return episodes

    @staticmethod
    def __item_path(item):
        """
        获取Plex媒体项目的本地文件或目录路径
        """
        try:
            locations = getattr(item, "locations", None) or []
            if locations:
                return locations[0]
        except Exception:
            pass
        try:
            for media in getattr(item, "media", None) or []:
                for part in getattr(media, "parts", None) or []:
                    file_path = getattr(part, "file", None)
                    if file_path:
                        return file_path
        except Exception:
            pass
        return ""

    @staticmethod
    def __provider_ids(item):
        """
        从Plex guid/guids 中提取常见ProviderId
        """
        guid_values = []
        try:
            if getattr(item, "guid", None):
                guid_values.append(item.guid)
        except Exception:
            pass
        provider_ids = Plex.__parse_provider_ids(guid_values)
        if provider_ids.get("tmdbid"):
            return provider_ids

        try:
            for guid in getattr(item, "guids", None) or []:
                guid_id = getattr(guid, "id", None)
                if guid_id:
                    guid_values.append(guid_id)
        except Exception:
            pass
        return Plex.__parse_provider_ids(guid_values)

    @staticmethod
    def __parse_provider_ids(guid_values):
        provider_ids = {}
        for guid_value in guid_values:
            guid_value = str(guid_value or "")
            if guid_value.startswith("tmdb://"):
                provider_ids["tmdbid"] = guid_value.replace("tmdb://", "").split("?")[0]
            elif guid_value.startswith("imdb://"):
                provider_ids["imdbid"] = guid_value.replace("imdb://", "").split("?")[0]
        return provider_ids

    def get_playing_sessions(self):
        """
        获取正在播放的会话
        """
        pass
