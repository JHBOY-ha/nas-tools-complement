from abc import ABCMeta, abstractmethod


class _IMediaClient(metaclass=ABCMeta):

    @abstractmethod
    def match(self, ctype):
        """
        匹配实例
        """
        pass

    @abstractmethod
    def get_status(self):
        """
        检查连通性
        """
        pass

    @abstractmethod
    def get_user_count(self):
        """
        获得用户数量
        """
        pass

    @abstractmethod
    def get_activity_log(self, num):
        """
        获取Emby活动记录
        """
        pass

    @abstractmethod
    def get_medias_count(self):
        """
        获得电影、电视剧、动漫媒体数量
        :return: MovieCount SeriesCount SongCount
        """
        pass

    @abstractmethod
    def get_movies(self, title, year):
        """
        根据标题和年份，检查电影是否在存在，存在则返回列表
        :param title: 标题
        :param year: 年份，可以为空，为空时不按年份过滤
        :return: 含title、year属性的字典列表
        """
        pass

    @abstractmethod
    def get_no_exists_episodes(self, meta_info, season, total_num, episode_numbers=None):
        """
        根据标题、年份、季、总集数，查询缺少哪几集
        :param meta_info: 已识别的需要查询的媒体信息
        :param season: 季号，数字
        :param total_num: 该季的总集数
        :return: 该季不存在的集号列表
        """
        pass

    @abstractmethod
    def get_image_by_id(self, item_id, image_type):
        """
        根据ItemId查询图片地址
        :param item_id: 在服务器中的ID
        :param image_type: 图片的类弄地，poster或者backdrop等
        :return: 图片对应在TMDB中的URL
        """
        pass

    @abstractmethod
    def refresh_root_library(self):
        """
        刷新整个媒体库
        """
        pass

    @abstractmethod
    def refresh_library_by_items(self, items):
        """
        按类型、名称、年份来刷新媒体库
        :param items: 已识别的需要刷新媒体库的媒体信息列表
        """
        pass

    @abstractmethod
    def get_libraries(self, strict=False):
        """
        strict=True 时读取失败须抛出异常，供全量快照同步使用。
        获取媒体服务器所有媒体库列表
        """
        pass

    @abstractmethod
    def get_items(self, parent, strict=False):
        """
        strict=True 时包括递归读取失败在内的异常均须向上传递。
        获取媒体库中的所有媒体
        :param parent: 上一级的ID
        """
        pass

    def get_episodes(self, series_id):
        """
        获取电视剧/动漫下的所有剧集
        :param series_id: 电视剧/动漫项目ID
        """
        return []

    def get_item_image(self, item_id, image_type="Primary", max_width=None, quality=None, tag=None):
        """
        获取媒体服务器项目图片响应
        :param item_id: 媒体服务器项目ID
        :param image_type: 图片类型
        :param max_width: 缩略图最大宽度，空表示原图
        :param quality: 图片质量，仅支持的服务器使用
        :param tag: 图片版本，仅用于让服务器与浏览器区分新旧图片
        """
        return None

    def get_sync_item(self, item_id):
        """
        读取单个项目的最新同步记录，字段与 get_items 产出的记录一致。
        读取失败须抛出异常，由调用方决定是否保留旧记录；不触发服务器刮削。
        :param item_id: 媒体服务器项目ID
        """
        return {}

    @abstractmethod
    def get_playing_sessions(self):
        """
        获取正在播放的会话
        """
        pass
