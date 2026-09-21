"""Thunder / Assrt protocols based on MeiamSubtitles (Apache-2.0).

Reference: https://github.com/91270/MeiamSubtitles
Implemented for NASTool; downloaded content is validated by Subtitle.upload_subtitle.
"""
import hashlib
import io
import ipaddress
import os
import re
import unicodedata
import socket
import zipfile
from urllib.parse import urljoin, urlsplit

import requests


class OnlineSubtitles:
    FORMATS = {"srt", "ass", "ssa", "vtt", "smi", "sub"}
    MAX_BYTES = 20 * 1024 * 1024

    def __init__(self, token=""):
        self.token = str(token or "").strip()

    @staticmethod
    def cid(path):
        size = os.path.getsize(path)
        with open(path, "rb") as stream:
            if size < 0xf000:
                data = stream.read()
            else:
                data = stream.read(0x5000)
                stream.seek(size // 3)
                data += stream.read(0x5000)
                stream.seek(size - 0x5000)
                data += stream.read(0x5000)
        return hashlib.sha1(data).hexdigest().upper()

    def _json(self, url, params):
        # Do not surface requests exceptions: Assrt URLs contain credentials.
        try:
            with requests.get(url, params=params, timeout=(5, 20)) as response:
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError()
                return result
        except (requests.RequestException, ValueError):
            raise ValueError("字幕源请求失败，请检查网络或 API Token") from None

    @staticmethod
    def episode_numbers(text):
        """Read common release names, including ranges and Chinese season/episode labels."""
        text = unicodedata.normalize("NFKC", str(text or ""))
        def number(value):
            if value.isdigit():
                return int(value)
            digits = {char: index for index, char in enumerate("零一二三四五六七八九")}
            if "十" in value:
                left, right = value.split("十", 1)
                return (digits.get(left, 1) * 10) + digits.get(right, 0)
            return digits.get(value, -1)
        seasons = {int(n) for n in re.findall(r"(?i)(?<![a-z0-9])s(\d{1,3})(?!\d)", text)}
        episodes = set()
        for match in re.finditer(r"(?i)(?<![a-z])e(?:p)?[ ._-]?(\d{1,4})(?:\s*[-~]\s*(?:e(?:p)?)?(\d{1,4}))?(?!\d)", text):
            start, end = int(match[1]), int(match[2] or match[1])
            if start <= end <= start + 500:
                episodes.update(range(start, end + 1))
        for season, episode in re.findall(r"(?i)(?<!\d)(\d{1,3})x(\d{1,4})(?!\d)", text):
            seasons.add(int(season))
            episodes.add(int(episode))
        for season in re.findall(r"第([零一二三四五六七八九十\d]+)季", text):
            seasons.add(number(season))
        for episode in re.findall(r"第([零一二三四五六七八九十\d]+)[集话話]", text):
            episodes.add(number(episode))
        return seasons, episodes

    @staticmethod
    def _title_text(text):
        text = unicodedata.normalize("NFKC", str(text or "")).casefold()
        text = re.sub(r"(第[零一二三四五六七八九十\d]+[季集话話])", r" \1 ", text)
        return re.sub(r"[^\w]+", " ", text.replace("_", " ")).strip()

    @classmethod
    def _query_title(cls, text):
        text = str(text)
        marker = re.search(r"(?i)(?<![a-z0-9])(?:s\d{1,3}(?:[ ._-]*e\d+)?|\d{1,3}x\d+|(?:19|20)\d{2})(?!\d)|第[零一二三四五六七八九十\d]+[季集]", text)
        prefix = text[:marker.start()].strip(" ._-") if marker else ""
        return prefix or text.strip()

    @classmethod
    def _matches_title(cls, text, titles):
        # Do not match Alien against Resident Alien / Alien Covenant / Alien 2.
        # Brackets and slashes commonly separate release groups and bilingual titles.
        metadata = r"(?i)^(?:(?:19|20)\d{2}|\d{3,4}p|[248]k|s\d+(?:e\d+)*|e(?:p)?\d+|\d+x\d+|第[零一二三四五六七八九十\d]+[季集]|全[季集]|字幕|中[英文字]|简[体中]|繁[体中]|双语|blu\s?ray|b[dr]rip|web|hdtv|dvd|remux|x26[45]|h26[45]|hevc|aac|dts|srt|ass|ssa|zip|rar|chs|cht|eng|zh|subtitles?|complete|season)(?:\b|[\u4e00-\u9fff])"
        aliases = [cls._title_text(title) for title in titles]
        for segment in re.split(r"[\[\]【】()/|]", str(text or "")):
            segment = cls._title_text(segment)
            for title in aliases:
                if not title or not (segment == title or segment.startswith(title + " ")):
                    continue
                rest = segment[len(title):].strip()
                if not rest or re.match(metadata, rest) or any(rest == alias or rest.startswith(alias + " ") for alias in aliases if alias != title):
                    return True
        return False

    @classmethod
    def search_target(cls, keyword, media_path, context):
        context = context or {}
        target = {"titles": list(dict.fromkeys(str(value).strip() for value in
                  [cls._query_title(keyword), context.get("title"), context.get("original_title")]
                  if value and str(value).strip())), "year": str(context.get("year") or "")}
        years = re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", keyword)
        if not target["year"] and len(years) == 1 and cls._query_title(keyword) != keyword:
            target["year"] = years[0]
        path_seasons, path_episodes = cls.episode_numbers(os.path.basename(media_path))
        query_seasons, query_episodes = cls.episode_numbers(keyword)
        for key, inferred in (("season", path_seasons or query_seasons), ("episode", path_episodes or query_episodes)):
            value = context.get(key)
            if value in (None, "") and len(inferred) == 1:
                value = next(iter(inferred))
            if value not in (None, ""):
                try:
                    target[key] = int(value)
                except (TypeError, ValueError):
                    raise ValueError("季集编号必须是整数") from None
                if not 0 <= target[key] <= 9999 or (key == "episode" and target[key] == 0):
                    raise ValueError("季集编号无效")
                if len(inferred) == 1 and key in context and context[key] not in (None, "") and target[key] != next(iter(inferred)):
                    raise ValueError("季集编号与目标媒体文件不一致，请重新选择剧集")
        if context.get("media_type") in ("tv", "anime", "episode") or "episode" in target:
            if "season" not in target or "episode" not in target:
                raise ValueError("缺少明确的季、集编号，请先同步媒体库并选择具体剧集")
        return target

    @classmethod
    def match_result(cls, item, target):
        if item.get("hash_match"):
            return "文件特征匹配"
        text = " ".join(str(item.get(key) or "") for key in ("name", "video_name"))
        if not any(cls._matches_title(item.get(key), target["titles"]) for key in ("name", "video_name")):
            return ""
        seasons, episodes = cls.episode_numbers(text)
        if "episode" in target:
            if seasons != {target["season"]}:
                return ""
            if episodes == {target["episode"]}:
                return "剧名与季集匹配"
            archive = bool(re.search(r"(?i)zip|rar|字幕包|全集|全季|complete|season pack", str(item.get("format")) + " " + text))
            if archive and (not episodes or target["episode"] in episodes):
                return "本季字幕包（需选择本集）"
            return ""
        if seasons or episodes:
            return ""
        years = set(re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", text))
        if target["year"] and years and target["year"] not in years:
            return ""
        return "片名与年份匹配" if target["year"] in years else "片名匹配（年份未确认）"

    @classmethod
    def _member_matches(cls, name, target, parent_name):
        seasons, episodes = cls.episode_numbers(name)
        if not seasons:
            seasons, _ = cls.episode_numbers(parent_name)
        if seasons != {target["season"]} or episodes != {target["episode"]}:
            return False
        basename = os.path.basename(name.replace("\\", "/"))
        prefix = re.split(r"(?i)(?<![a-z0-9])(?:s\d+|e(?:p)?\d+|\d+x\d+)|第[零一二三四五六七八九十\d]+[季集]", basename)[0].strip(" ._-")
        return not prefix or not target.get("titles") or cls._matches_title(basename, target["titles"])

    def search(self, keyword, media_path, provider="all", context=None):
        if provider not in ("all", "thunder", "assrt"):
            raise ValueError("请选择有效的字幕来源")
        keyword = str(keyword or "").strip()
        if not keyword or len(keyword) > 200:
            raise ValueError("请输入 1–200 字的影片名称")
        target = self.search_target(keyword, media_path, context)
        query = self._query_title(keyword)
        if "episode" in target:
            query += f" S{target['season']:02d}E{target['episode']:02d}"
        elif target["year"]:
            query += " " + target["year"]
        results, warnings = [], []
        if provider in ("all", "thunder"):
            try:
                try:
                    cid = self.cid(media_path)
                except OSError:
                    cid = ""
                    warnings.append("无法读取文件特征，迅雷仅按名称检索")
                data = self._json("https://api-shoulei-ssl.xunlei.com/oracle/subtitle", {"name": query})
                if data.get("code") != 0:
                    raise ValueError("迅雷字幕接口暂不可用")
                seen = set()
                for entry in data.get("data") or []:
                    ext = str(entry.get("ext") or "").lower().lstrip(".")
                    url = entry.get("url")
                    if not url or url in seen or ext not in self.FORMATS:
                        continue
                    seen.add(url)
                    results.append({"provider": "thunder", "name": entry.get("name") or "",
                                    "format": ext, "language": ", ".join(entry.get("languages") or []),
                                    "hash_match": bool(cid and cid == str(entry.get("cid") or "").upper()),
                                    "url": url})
                results.sort(key=lambda item: (item["hash_match"], "简体" in item["language"]), reverse=True)
            except (ValueError, TypeError, AttributeError):
                warnings.append("迅雷检索失败，请稍后重试")
        if provider in ("all", "assrt"):
            if not self.token:
                warnings.append("Assrt 未配置 API Token，请在设置 → 字幕 → 在线字幕中配置")
            else:
                try:
                    data = self._json("https://api.assrt.net/v1/sub/search",
                                      {"token": self.token, "q": query, "is_file": 1, "cnt": 15})
                    if data.get("status") != 0:
                        raise ValueError()
                    seen = set()
                    for entry in (data.get("sub") or {}).get("subs") or []:
                        identity = entry.get("id")
                        if identity is None or str(identity) in seen:
                            continue
                        seen.add(str(identity))
                        results.append({"provider": "assrt", "remote_id": str(identity),
                                        "name": entry.get("native_name") or entry.get("videoname") or "",
                                        "video_name": entry.get("videoname") or "",
                                        "format": entry.get("subtype") or "字幕包",
                                        "language": (entry.get("lang") or {}).get("desc") or "未知",
                                        "hash_match": False})
                except (ValueError, TypeError, AttributeError):
                    warnings.append("Assrt 检索失败，请检查网络、Token 或接口配额")
        matched = []
        for item in results:
            label = self.match_result(item, target)
            if label:
                item["match_label"] = label
                item["target"] = target
                matched.append(item)
        rejected = len(results) - len(matched)
        if rejected:
            warnings.append(f"已过滤 {rejected} 条片名、年份或季集不匹配／无法确认的结果")
        priorities = {"文件特征匹配": 4, "剧名与季集匹配": 3, "片名与年份匹配": 3,
                      "本季字幕包（需选择本集）": 2, "片名匹配（年份未确认）": 1}
        matched.sort(key=lambda item: (priorities[item["match_label"]], "简体" in item["language"]), reverse=True)
        limits = {"thunder": 20, "assrt": 15}
        selected = []
        for item in matched:
            if limits[item["provider"]] > 0:
                selected.append(item)
                limits[item["provider"]] -= 1
        return selected, warnings

    @staticmethod
    def _check_url(url):
        parsed = urlsplit(url)
        if (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in (None, 80, 443)):
            raise ValueError("字幕下载地址无效")
        try:
            addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
            # Clash-style proxies resolve public hosts into this reserved Fake-IP range.
            # Only known provider domains may use it; LAN/loopback remain blocked.
            known_host = (parsed.hostname == "subtitle.v.geilijiasu.com" or parsed.hostname == "assrt.net"
                          or parsed.hostname.endswith(".assrt.net"))
            fake_ip_range = ipaddress.ip_network("198.18.0.0/15")
            for address in addresses:
                ip = ipaddress.ip_address(address[4][0])
                if not ip.is_global and not (known_host and ip in fake_ip_range):
                    raise ValueError("字幕下载地址不可访问")
            if not addresses:
                raise ValueError("无法解析字幕下载地址")
        except OSError:
            raise ValueError("无法解析字幕下载地址") from None

    def _download(self, url):
        try:
            for _ in range(6):
                self._check_url(url)
                with requests.get(url, headers={"Referer": "https://assrt.net/", "User-Agent": "NASTool"},
                                  stream=True, timeout=(5, 30), allow_redirects=False) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers.get("Location", ""))
                        continue
                    response.raise_for_status()
                    content = bytearray()
                    for chunk in response.iter_content(65536):
                        content.extend(chunk)
                        if len(content) > self.MAX_BYTES:
                            raise ValueError("字幕文件超过 20 MB 限制")
                    if not content:
                        raise ValueError("字幕源返回空文件")
                    return bytes(content)
            raise ValueError("字幕下载重定向过多")
        except requests.RequestException:
            raise ValueError("字幕下载失败，请稍后重新搜索") from None

    def files(self, item, member=None):
        """Return choices for an archive, or a single selected file; never extract paths."""
        name = item["name"]
        url = item.get("url")
        if item["provider"] == "assrt":
            if not self.token:
                raise ValueError("请先配置 Assrt API Token")
            data = self._json("https://api.assrt.net/v1/sub/detail", {"token": self.token, "id": item["remote_id"]})
            subs = (data.get("sub") or {}).get("subs") or []
            if data.get("status") != 0 or not subs or not subs[0].get("url"):
                raise ValueError("Assrt 未返回下载地址，请重新搜索")
            url, name = subs[0]["url"], subs[0].get("filename") or name
        content = self._download(url)
        stream = io.BytesIO(content)
        archive = None
        if zipfile.is_zipfile(stream):
            archive = zipfile.ZipFile(stream)
        elif content.startswith(b"Rar!"):
            try:
                import rarfile
                archive = rarfile.RarFile(stream)
            except ImportError:
                raise ValueError("RAR 字幕包需要安装 rarfile 和 unrar") from None
        if archive is not None:
            with archive:
                entries = [entry for entry in archive.infolist()
                           if not entry.is_dir() and os.path.splitext(entry.filename)[1].lower().lstrip(".") in self.FORMATS]
                if not entries:
                    raise ValueError("压缩包中没有支持的字幕文件")
                if len(entries) > 200 or sum(entry.file_size for entry in entries) > self.MAX_BYTES:
                    raise ValueError("字幕包解压后过大")
                target = item.get("target") or {}
                if "episode" in target:
                    entries = [entry for entry in entries if self._member_matches(entry.filename, target, item["name"] + " " + name)]
                    if not entries:
                        raise ValueError("字幕包中未找到明确匹配当前季集的字幕，请选择其他结果")
                if member is None:
                    return [entry.filename for entry in entries], None
                matches = [entry for entry in entries if entry.filename == member]
                if len(matches) != 1:
                    raise ValueError("字幕包文件选择无效，请重新搜索")
                content = archive.read(matches[0])
                name = member
        else:
            target = item.get("target") or {}
            if "episode" in target and not item.get("hash_match"):
                # A season pack response must never silently become a single unrelated episode.
                # Check explicit download filenames even if the search metadata matched.
                seasons, episodes = self.episode_numbers(name)
                if (seasons and seasons != {target["season"]}) or (episodes and episodes != {target["episode"]}):
                    raise ValueError("下载文件的季集与目标剧集不一致")
                if "字幕包" in item.get("match_label", ""):
                    raise ValueError("字幕源未返回可选择本集的字幕包")
            if item["provider"] == "thunder":
                name = os.path.splitext(name)[0] + "." + item["format"]
        name = os.path.basename(name.replace("\\", "/"))
        if os.path.splitext(name)[1].lower().lstrip(".") not in self.FORMATS:
            raise ValueError("字幕源返回了不支持的文件格式")
        return name, content
