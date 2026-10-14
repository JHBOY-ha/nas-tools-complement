import os
import logging
import shutil
import sys
from threading import Lock
import ruamel.yaml
from requests.utils import default_user_agent

# 种子名/文件名要素分隔字符
SPLIT_CHARS = r"\.|\s+|\(|\)|\[|]|-|\+|【|】|/|～|;|&|\||#|_|「|」|（|）|~"
# 默认User-Agent
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/98.0.4758.102 Safari/537.36"
# 收藏了的媒体的目录名，名字可以改，在Emby中点击红星则会自动将电影转移到此分类下，需要在Emby Webhook中配置用户行为通知
RMT_FAVTYPE = '精选'
# 支持的媒体文件后缀格式
RMT_MEDIAEXT = ['.mp4', '.mkv', '.ts', '.iso',
                '.rmvb', '.avi', '.mov', '.mpeg',
                '.mpg', '.wmv', '.3gp', '.asf',
                '.m4v', '.flv', '.m2ts', '.strm']
# 支持的字幕文件后缀格式
RMT_SUBEXT = ['.srt', '.ass', '.ssa', '.smi', '.vtt', '.sub']
# 电视剧动漫的分类genre_ids
ANIME_GENREIDS = ['16']
# 默认过滤的文件大小，150M
RMT_MIN_FILESIZE = 150 * 1024 * 1024
# 删种检查时间间隔
AUTO_REMOVE_TORRENTS_INTERVAL = 1800
# 下载文件转移检查时间间隔，
PT_TRANSFER_INTERVAL = 300
# TMDB信息缓存定时保存时间
METAINFO_SAVE_INTERVAL = 600
# SYNC目录同步聚合转移时间
SYNC_TRANSFER_INTERVAL = 60
# RSS队列中处理时间间隔
RSS_CHECK_INTERVAL = 300
# 站点流量数据刷新时间间隔（小时）
REFRESH_PT_DATA_INTERVAL = 6
# 刷新订阅TMDB数据的时间间隔（小时）
RSS_REFRESH_TMDB_INTERVAL = 6
# 刷流删除的检查时间间隔
BRUSH_REMOVE_TORRENTS_INTERVAL = 300
# 定时清除未识别的缓存时间间隔（小时）
META_DELETE_UNKNOWN_INTERVAL = 12
# 定时刷新壁纸的间隔（小时）
REFRESH_WALLPAPER_INTERVAL = 1
# apscheduler 任务默认值。库默认的 misfire_grace_time 只有 1 秒，进程繁忙或
# NAS I/O 阻塞时会静默丢弃本次执行；显式放宽，同时保持不重入与合并错过执行。
SCHEDULER_JOB_DEFAULTS = {
    'coalesce': True,
    'max_instances': 1,
    'misfire_grace_time': 60
}
# Single-process NAS budgets. Queue and worker changes take effect on restart;
# interactive callbacks have their own capacity instead of waiting for scans.
WORKLOAD_DEFAULTS = {
    'background_workers': 6,
    'background_queue_size': 64,
    'interactive_workers': 2,
    'interactive_queue_size': 16,
    'scheduler_workers': 6,
    'scheduler_queue_size': 64,
    'search_workers': 4,
    'search_queue_size': 64,
    'transfer_concurrency': 2,
}
_WORKLOAD_RANGES = {
    'background_workers': (1, 32), 'background_queue_size': (1, 256),
    'interactive_workers': (1, 8), 'interactive_queue_size': (2, 128),
    'scheduler_workers': (1, 20), 'scheduler_queue_size': (1, 256),
    'search_workers': (1, 16), 'search_queue_size': (1, 256),
    'transfer_concurrency': (1, 8),
}
# fanart的api，用于拉取封面图片
FANART_MOVIE_API_URL = 'https://webservice.fanart.tv/v3/movies/%s?api_key=d2d31f9ecabea050fc7d68aa3146015f'
FANART_TV_API_URL = 'https://webservice.fanart.tv/v3/tv/%s?api_key=d2d31f9ecabea050fc7d68aa3146015f'
# 默认背景图地址
DEFAULT_TMDB_IMAGE = 'https://s3.bmp.ovh/imgs/2022/07/10/77ef9500c851935b.webp'
# 默认微信消息代理服务器地址
DEFAULT_WECHAT_PROXY = 'https://wechat.nastool.cn'
# 默认OCR识别服务地址
DEFAULT_OCR_SERVER = 'https://nastool.cn'
# 默认TMDB代理服务地址
DEFAULT_TMDB_PROXY = 'https://tmdb.nastool.cn'
# 默认CookieCloud服务地址
DEFAULT_COOKIECLOUD_SERVER = 'http://nastool.cn:8088'
# TMDB图片地址
TMDB_IMAGE_W500_URL = 'https://image.tmdb.org/t/p/w500%s'
TMDB_IMAGE_ORIGINAL_URL = 'https://image.tmdb.org/t/p/original%s'
TMDB_IMAGE_FACE_URL = 'https://image.tmdb.org/t/p/h632%s'
TMDB_PEOPLE_PROFILE_URL = 'https://www.themoviedb.org/person/%s'
# 添加下载时增加的标签，开始只监控NASTool添加的下载时有效
PT_TAG = "NASTOOL"
# 电影默认命名格式
DEFAULT_MOVIE_FORMAT = '{title} ({year})/{title} ({year})-{part} - {videoFormat}'
# 电视剧默认命名格式
DEFAULT_TV_FORMAT = '{title} ({year})/Season {season}/{title} - {season_episode}-{part} - 第 {episode} 集'
# 辅助识别参数
KEYWORD_SEARCH_WEIGHT_1 = [10, 3, 2, 0.5, 0.5]
KEYWORD_SEARCH_WEIGHT_2 = [10, 2, 1]
KEYWORD_SEARCH_WEIGHT_3 = [10, 2]
KEYWORD_STR_SIMILARITY_THRESHOLD = 0.2
KEYWORD_DIFF_SCORE_THRESHOLD = 30
KEYWORD_BLACKLIST = ['中字', '韩语', '双字', '中英', '日语', '双语', '国粤', 'HD', 'BD', '中日', '粤语', '完全版',
                     '法语', '西班牙语', 'HRHDTVAC3264', '未删减版', '未删减', '国语', '字幕组', '人人影视', 'www66ystv',
                     '人人影视制作', '英语', 'www6vhaotv', '无删减版', '完成版', '德意']

# WebDriver路径
WEBDRIVER_PATH = {
    "Docker": "/usr/lib/chromium/chromedriver",
    "Synology": "/var/packages/NASTool/target/bin/chromedriver"
}

# Xvfb虚拟显示路程
XVFB_PATH = [
    "/usr/bin/Xvfb",
    "/usr/local/bin/Xvfb"
]

# 线程锁
lock = Lock()

# 全局实例
_CONFIG = None


def singleconfig(cls):
    def _singleconfig(*args, **kwargs):
        global _CONFIG
        if not _CONFIG:
            with lock:
                _CONFIG = cls(*args, **kwargs)
        return _CONFIG

    return _singleconfig


@singleconfig
class Config(object):
    _config = {}
    _config_path = None

    def __init__(self):
        self._config_path = os.environ.get('NASTOOL_CONFIG')
        if not os.environ.get('TZ'):
            os.environ['TZ'] = 'Asia/Shanghai'
        self.init_syspath()
        self.init_config()

    def init_config(self):
        try:
            if not self._config_path:
                print("【Config】NASTOOL_CONFIG 环境变量未设置，程序无法工作，正在退出...")
                quit()
            if not os.path.exists(self._config_path):
                cfg_tp_path = os.path.join(self.get_inner_config_path(), "config.yaml")
                cfg_tp_path = cfg_tp_path.replace("\\", "/")
                shutil.copy(cfg_tp_path, self._config_path)
                print("【Config】config.yaml 配置文件不存在，已将配置文件模板复制到配置目录...")
            with open(self._config_path, mode='r', encoding='utf-8') as cf:
                try:
                    # 读取配置
                    print("正在加载配置：%s" % self._config_path)
                    self._config = ruamel.yaml.YAML().load(cf)
                except Exception as e:
                    print("【Config】配置文件 config.yaml 格式出现严重错误！请检查：%s" % str(e))
                    self._config = {}
        except Exception as err:
            print("【Config】加载 config.yaml 配置出错：%s" % str(err))
            return False

    def init_syspath(self):
        with open(os.path.join(self.get_root_path(),
                               "third_party.txt"), "r") as f:
            for third_party_lib in f.readlines():
                module_path = os.path.join(self.get_root_path(),
                                           "third_party",
                                           third_party_lib.strip()).replace("\\", "/")
                if module_path not in sys.path:
                    sys.path.append(module_path)

    def get_proxies(self):
        # Import after Config initialization to avoid app.utils bootstrap cycles.
        from app.utils.security_utils import normalize_proxies

        proxies = self.get_config('app').get("proxies")
        try:
            # All HTTP consumers share the same legacy-format normalization.
            return normalize_proxies(proxies)
        except ValueError:
            # Keep the original invalid value for TMDb's fail-closed setter so
            # startup remains available and reports the actual validation error.
            return proxies

    def get_ua(self):
        return self.get_config('app').get("user_agent") or DEFAULT_UA

    def get_tmdb_web_ua(self):
        """仅供 TMDB 官网使用；旧配置或空值保持 Requests 默认 UA。"""
        user_agent = (self.get_config('app') or {}).get("tmdb_web_user_agent")
        if not isinstance(user_agent, str):
            return default_user_agent()
        return user_agent.strip() or default_user_agent()

    def get_config(self, node=None):
        if not node:
            return self._config
        return self._config.get(node, {})

    def get_workload_limit(self, name):
        """Read a bounded NAS budget without accepting booleans or fractional counts."""
        default = WORKLOAD_DEFAULTS[name]
        workload = (self.get_config('app') or {}).get('workload') or {}
        raw = workload.get(name, default) if isinstance(workload, dict) else default
        minimum, maximum = _WORKLOAD_RANGES[name]
        try:
            value = int(str(raw))
            if isinstance(raw, bool) or not minimum <= value <= maximum:
                raise ValueError()
        except (TypeError, ValueError):
            # Standard logging avoids importing the application logger back
            # into Config during bootstrap.
            logging.getLogger(__name__).warning(
                "【Config】app.workload.%s 配置无效，使用默认值 %s", name, default)
            return default
        return value

    def save_config(self, new_cfg):
        self._config = new_cfg
        with open(self._config_path, mode='w', encoding='utf-8') as sf:
            yaml = ruamel.yaml.YAML()
            return yaml.dump(new_cfg, sf)

    def get_config_path(self):
        return os.path.dirname(self._config_path)

    def get_temp_path(self):
        return os.path.join(self.get_config_path(), "temp")

    @staticmethod
    def get_root_path():
        return os.path.dirname(os.path.realpath(__file__))

    def get_inner_config_path(self):
        return os.path.join(self.get_root_path(), "config")

    def get_domain(self):
        domain = (self.get_config('app') or {}).get('domain')
        if domain and not domain.startswith('http'):
            domain = "http://" + domain
        return domain

    @staticmethod
    def get_timezone():
        return os.environ.get('TZ')
