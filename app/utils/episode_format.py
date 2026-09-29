import re
import parse
from config import SPLIT_CHARS


class EpisodeFormat(object):
    _key = ""

    @staticmethod
    def normalize_season(season):
        """Validate a manual season once, preserving blank auto mode and explicit S00."""
        if season is None or season == "":
            return None
        # Do not silently truncate floats or interpret booleans as season numbers.
        if type(season) is int and season >= 0:
            return season
        if isinstance(season, str) and re.fullmatch(r"[0-9]+", season.strip()):
            return int(season.strip())
        raise ValueError("季号参数无效：请输入非负整数（0 表示特别篇），或留空自动识别")

    def __init__(self, eformat, details: str = None, offset=None, key="ep"):
        self._format = eformat
        self._start_ep = None
        self._end_ep = None
        if details:
            if re.compile("\\d{1,4}-\\d{1,4}").match(details):
                self._start_ep = details
                self._end_ep = details
            else:
                tmp = details.split(",")
                if len(tmp) > 1:
                    self._start_ep = int(tmp[0])
                    self._end_ep = int(tmp[0]) if int(tmp[0]) > int(tmp[1]) else int(tmp[1])
                else:
                    self._start_ep = self._end_ep = int(tmp[0])
        self.__offset = int(offset) if offset else 0
        self._key = key

    @property
    def format(self):
        return self._format

    @property
    def start_ep(self):
        return self._start_ep

    @property
    def end_ep(self):
        return self._end_ep

    @property
    def offset(self):
        return self.__offset

    def match(self, file: str):
        if not self._format:
            return True
        s, e = self.__handle_single(file)
        if not s:
            return False
        if self._start_ep is None:
            return True
        if self._start_ep <= s <= self._end_ep:
            return True
        return False

    def split_episode(self, file_name):
        # 指定的具体集数，直接返回
        if self._start_ep is not None and self._start_ep == self._end_ep:
            if isinstance(self._start_ep, str):
                s, e = self._start_ep.split("-")
                if int(s) == int(e):
                    return int(s) + self.__offset, None
                return int(s) + self.__offset, int(e) + self.__offset
            return self._start_ep + self.__offset, None
        if not self._format:
            return None, None
        s, e = self.__handle_single(file_name)
        return s + self.__offset if s is not None else None, e + self.__offset if e is not None else None

    def __handle_single(self, file: str):
        if not self._format:
            return None, None
        ret = parse.parse(self._format, file)
        if not ret or not ret.__contains__(self._key):
            return None, None
        episodes = ret.__getitem__(self._key)
        if not re.compile(r"^(EP)?(\d{1,4})(-(EP)?(\d{1,4}))?$", re.IGNORECASE).match(episodes):
            return None, None
        episode_splits = list(filter(lambda x: re.compile(r'[a-zA-Z]*\d{1,4}', re.IGNORECASE).match(x),
                                     re.split(r'%s' % SPLIT_CHARS, episodes)))
        if len(episode_splits) == 1:
            return int(re.compile(r'[a-zA-Z]*', re.IGNORECASE).sub("", episode_splits[0])), None
        else:
            return int(re.compile(r'[a-zA-Z]*', re.IGNORECASE).sub("", episode_splits[0])), int(
                re.compile(r'[a-zA-Z]*', re.IGNORECASE).sub("", episode_splits[1]))
