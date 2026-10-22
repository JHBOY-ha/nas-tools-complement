import re
import parse


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
        if details is not None and details != "":
            # Validate the whole input; never collapse a reversed selection into
            # a fixed EP or accept a valid-looking prefix of malformed text.
            value = str(details).strip()
            match = re.fullmatch(r"(\d+)(?:\s*([,-])\s*(\d+))?", value)
            if not match:
                raise ValueError("集数参数无效：请输入非负整数、筛选区间（如 2,4）或合并范围（如 2-4）")
            start, end = int(match[1]), int(match[3]) if match[3] else int(match[1])
            self._validate_result(start, end)
            if match[2] == "-":
                self._start_ep = self._end_ep = "%s-%s" % (start, end)
            else:
                self._start_ep, self._end_ep = start, end
        value = "0" if offset is None or offset == "" else str(offset).strip()
        if not re.fullmatch(r"[+-]?\d+", value):
            raise ValueError("集数偏移无效：请输入整数")
        self.__offset = int(value)
        self._key = key
        if self._start_ep is not None and self._start_ep == self._end_ep:
            # Fixed targets can be checked before any directory scan or provider request.
            self.split_episode("")

    @staticmethod
    def _validate_result(start, end):
        """Validate numeric results only; provider episode existence is a separate policy."""
        if start is not None and (start < 0 or (end is not None and (end < 0 or end < start))):
            raise ValueError("集数结果无效：偏移后集数必须为非负整数，且终止集不能小于起始集")
        return start, end

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
        # Fixed manual targets do not depend on the filename pattern. Directory
        # scanning must use the same precedence as split_episode().
        if self._start_ep is not None and self._start_ep == self._end_ep:
            return True
        if not self._format:
            return True
        s, e = self.__handle_single(file)
        if s is None:
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
                    return self._validate_result(int(s) + self.__offset, None)
                return self._validate_result(int(s) + self.__offset, int(e) + self.__offset)
            return self._validate_result(self._start_ep + self.__offset, None)
        if not self._format:
            return None, None
        s, e = self.__handle_single(file_name)
        return self._validate_result(s + self.__offset if s is not None else None,
                                     e + self.__offset if e is not None else None)

    def __handle_single(self, file: str):
        if not self._format:
            return None, None
        ret = parse.parse(self._format, file)
        if not ret or not ret.__contains__(self._key):
            return None, None
        episodes = ret.__getitem__(self._key)
        match = re.fullmatch(r"(?:EP)?(\d{1,4})(?:-(?:EP)?(\d{1,4}))?", episodes, re.IGNORECASE)
        if not match:
            return None, None
        # Keep E00 valid and reject a reversed range before filtering or offsetting it.
        return self._validate_result(int(match[1]), int(match[2]) if match[2] else None)
