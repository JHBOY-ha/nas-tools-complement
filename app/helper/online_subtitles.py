"""Thunder / Assrt protocols based on MeiamSubtitles (Apache-2.0).

Reference: https://github.com/91270/MeiamSubtitles
Implemented for NASTool; downloaded content is validated by Subtitle.upload_subtitle.
"""
import hashlib
import io
import ipaddress
import os
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

    def search(self, keyword, media_path, provider="all"):
        if provider not in ("all", "thunder", "assrt"):
            raise ValueError("请选择有效的字幕来源")
        keyword = str(keyword or "").strip()
        if not keyword or len(keyword) > 200:
            raise ValueError("请输入 1–200 字的影片名称")
        results, warnings = [], []
        if provider in ("all", "thunder"):
            try:
                try:
                    cid = self.cid(media_path)
                except OSError:
                    cid = ""
                    warnings.append("无法读取文件特征，迅雷仅按名称检索")
                data = self._json("https://api-shoulei-ssl.xunlei.com/oracle/subtitle", {"name": keyword})
                if data.get("code") != 0:
                    raise ValueError("迅雷字幕接口暂不可用")
                seen = set()
                for entry in data.get("data") or []:
                    ext = str(entry.get("ext") or "").lower().lstrip(".")
                    url = entry.get("url")
                    if not url or url in seen or ext not in self.FORMATS:
                        continue
                    seen.add(url)
                    results.append({"provider": "thunder", "name": entry.get("name") or keyword,
                                    "format": ext, "language": ", ".join(entry.get("languages") or []),
                                    "hash_match": bool(cid and cid == str(entry.get("cid") or "").upper()),
                                    "url": url})
                results.sort(key=lambda item: (item["hash_match"], "简体" in item["language"]), reverse=True)
                results = results[:20]
            except (ValueError, TypeError, AttributeError):
                warnings.append("迅雷检索失败，请稍后重试")
        if provider in ("all", "assrt"):
            if not self.token:
                warnings.append("Assrt 未配置 API Token，请在设置 → 字幕 → 在线字幕中配置")
            else:
                try:
                    data = self._json("https://api.assrt.net/v1/sub/search",
                                      {"token": self.token, "q": keyword, "is_file": 1, "cnt": 15})
                    if data.get("status") != 0:
                        raise ValueError()
                    seen = set()
                    for entry in (data.get("sub") or {}).get("subs") or []:
                        identity = entry.get("id")
                        if identity is None or str(identity) in seen:
                            continue
                        seen.add(str(identity))
                        results.append({"provider": "assrt", "remote_id": str(identity),
                                        "name": entry.get("native_name") or entry.get("videoname") or keyword,
                                        "format": entry.get("subtype") or "字幕包",
                                        "language": (entry.get("lang") or {}).get("desc") or "未知",
                                        "hash_match": False})
                except (ValueError, TypeError, AttributeError):
                    warnings.append("Assrt 检索失败，请检查网络、Token 或接口配额")
        return results, warnings

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
                if member is None:
                    return [entry.filename for entry in entries], None
                matches = [entry for entry in entries if entry.filename == member]
                if len(matches) != 1:
                    raise ValueError("字幕包文件选择无效，请重新搜索")
                content = archive.read(matches[0])
                name = member
        elif item["provider"] == "thunder":
            name = os.path.splitext(name)[0] + "." + item["format"]
        name = os.path.basename(name.replace("\\", "/"))
        if os.path.splitext(name)[1].lower().lstrip(".") not in self.FORMATS:
            raise ValueError("字幕源返回了不支持的文件格式")
        return name, content
