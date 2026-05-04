"""
openclaw-weixin 扫码登录辅助：被 Web UI 调用，封装 ilink/bot/get_bot_qrcode
和 ilink/bot/get_qrcode_status 两个接口的状态机。

会话状态在内存里按 qrcode 字符串保存，TTL 5 分钟。前端拿到 qrcode 后
轮询 status，处理 wait/scaned/expired/scaned_but_redirect/confirmed。
"""
import threading
import time
from urllib.parse import quote

from app.utils import RequestUtils, ExceptionUtils
from app.utils.commons import singleton
from config import Config

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
BOT_TYPE = "3"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (1 << 8) | 7
SESSION_TTL = 5 * 60
POLL_TIMEOUT = 30


def _headers():
    return {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        "User-Agent": Config().get_ua(),
    }


def _proxies():
    return Config().get_proxies()


@singleton
class OpenClawHelper(object):
    """会话状态：{ qrcode: { 'poll_base': str, 'created_at': float } }"""

    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def __purge(self):
        now = time.time()
        for k in [k for k, v in self._sessions.items()
                  if now - v.get("created_at", 0) > SESSION_TTL]:
            self._sessions.pop(k, None)

    def start(self):
        """获取二维码。返回 {ok, qrcode, qrcode_img_content, msg}"""
        try:
            url = f"{DEFAULT_BASE_URL}/ilink/bot/get_bot_qrcode?bot_type={quote(BOT_TYPE)}"
            res = RequestUtils(headers=_headers(), proxies=_proxies(), timeout=15).get_res(url)
            if not res:
                return {"ok": False, "msg": "无法连接 ilinkai.weixin.qq.com（请检查网络/代理）"}
            if res.status_code // 100 != 2:
                return {"ok": False, "msg": f"获取二维码失败 HTTP {res.status_code}"}
            data = res.json()
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return {"ok": False, "msg": f"请求失败: {e}"}
        qrcode = data.get("qrcode")
        img = data.get("qrcode_img_content")
        if not qrcode or not img:
            return {"ok": False, "msg": "服务端未返回二维码"}
        with self._lock:
            self.__purge()
            self._sessions[qrcode] = {
                "poll_base": DEFAULT_BASE_URL,
                "created_at": time.time(),
            }
        return {"ok": True, "qrcode": qrcode, "qrcode_img_content": img}

    def status(self, qrcode):
        """轮询状态。返回 {status, ...}：
           - wait/scaned -> 继续轮询
           - expired -> 前端调用 start 重新生成
           - confirmed -> 包含 bot_token / to_user_id / base_url
        """
        if not qrcode:
            return {"status": "error", "msg": "缺少 qrcode 参数"}
        with self._lock:
            sess = self._sessions.get(qrcode)
            if not sess:
                return {"status": "error", "msg": "会话不存在或已过期"}
            if time.time() - sess["created_at"] > SESSION_TTL:
                self._sessions.pop(qrcode, None)
                return {"status": "expired", "msg": "会话已超时"}
            poll_base = sess["poll_base"]
        try:
            url = f"{poll_base}/ilink/bot/get_qrcode_status?qrcode={quote(qrcode)}"
            res = RequestUtils(headers=_headers(), proxies=_proxies(), timeout=POLL_TIMEOUT).get_res(url)
            if not res:
                return {"status": "wait"}
            data = res.json() if res.status_code // 100 == 2 else {}
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return {"status": "wait"}
        st = data.get("status")
        if st == "scaned_but_redirect":
            host = data.get("redirect_host")
            if host:
                with self._lock:
                    if qrcode in self._sessions:
                        self._sessions[qrcode]["poll_base"] = f"https://{host}"
            return {"status": "scaned"}
        if st == "expired":
            with self._lock:
                self._sessions.pop(qrcode, None)
            return {"status": "expired"}
        if st == "confirmed":
            with self._lock:
                self._sessions.pop(qrcode, None)
            return {
                "status": "confirmed",
                "bot_token": data.get("bot_token"),
                "to_user_id": data.get("ilink_user_id"),
                "base_url": data.get("baseurl") or DEFAULT_BASE_URL,
                "ilink_bot_id": data.get("ilink_bot_id"),
            }
        return {"status": st or "wait"}
