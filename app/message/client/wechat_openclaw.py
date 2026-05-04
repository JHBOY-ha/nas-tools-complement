import base64
import hashlib
import json
import os
import secrets
import threading

from app.message.client._base import _IMessageClient
from app.utils import RequestUtils, ExceptionUtils
from config import Config
import log


# 与 Tencent/openclaw-weixin 保持一致的常量
# package.json: ilink_appid="bot", version="2.1.7"
# clientVersion = (major<<16)|(minor<<8)|patch
ILINK_APP_ID = "bot"
ILINK_APP_VERSION = "2.1.7"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (1 << 8) | 7  # 131335
CHANNEL_VERSION = ILINK_APP_VERSION
DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"

# session-guard
SESSION_EXPIRED_ERRCODE = -14
LONG_POLL_TIMEOUT_S = 35
DEFAULT_API_TIMEOUT_S = 15
RETRY_DELAY_S = 2
BACKOFF_DELAY_S = 30
MAX_CONSECUTIVE_FAILURES = 3


class WeChatOpenClaw(_IMessageClient):
    """
    基于 Tencent/openclaw-weixin 的 ilink bot 通道（普通微信号）。

    协议要点（见 openclaw-weixin/src/api/api.ts、messaging/inbound.ts）：
    - 必须在后台不断长轮询 ilink/bot/getupdates，并把响应中的
      get_updates_buf 回传到下一次请求；
    - 每条入站消息会带 context_token，发送方必须在 sendmessage 的
      msg.context_token 中原样回写，否则服务端会拒绝发送；
    - **同一 bot_token 不能并发长轮询**，否则服务端会把所有连接踢成 -14。
      因此本类用进程级单例线程：每个 bot_token 只有一条长轮询线程，
      所有 WeChatOpenClaw 实例（包括测试消息触发的临时实例）共享之。
    """

    schema = "wechat_openclaw"

    # 进程级长轮询登记表：{bot_token: WeChatOpenClaw 主实例}
    # 由该实例负责跑唯一的 __poll_loop 线程；其他实例从这里读 context_token / buf。
    _poll_owners = {}
    _poll_owners_lock = threading.Lock()

    def __init__(self, config):
        self._client_config = config or {}
        self._interactive = self._client_config.get("interactive")
        self._bot_token = None
        self._to_user_id = None
        self._base_url = DEFAULT_BASE_URL
        self._route_tag = None
        self._test = False
        # context_token 缓存：{user_id: token}
        self._context_tokens = {}
        # get_updates_buf 持久化
        self._get_updates_buf = ""
        self._state_lock = threading.Lock()
        # 长轮询线程
        self._poll_thread = None
        self._stop_event = threading.Event()
        self.init_config()

    def init_config(self):
        cfg = self._client_config or {}
        self._bot_token = (cfg.get("bot_token") or "").strip() or None
        self._to_user_id = (cfg.get("to_user_id") or "").strip() or None
        self._test = bool(cfg.get("test"))
        base = (cfg.get("base_url") or "").strip()
        self._base_url = base.rstrip("/") if base else DEFAULT_BASE_URL
        self._route_tag = (cfg.get("route_tag") or "").strip() or None
        self.__load_state()
        if not self._test:
            self.__start_poll_thread()

    @classmethod
    def match(cls, ctype):
        return ctype == cls.schema

    # ------------------------------------------------------------------
    # 状态持久化（context_token + get_updates_buf）
    # ------------------------------------------------------------------

    def __state_file(self):
        if not self._bot_token:
            return None
        try:
            base_dir = Config().get_temp_path()
        except Exception:
            return None
        os.makedirs(base_dir, exist_ok=True)
        # 用 bot_token 的 hash 做账号 ID，避免不同 token 互串
        h = hashlib.sha1(self._bot_token.encode("utf-8")).hexdigest()[:12]
        return os.path.join(base_dir, f"openclaw_{h}.json")

    def __load_state(self):
        path = self.__state_file()
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            with self._state_lock:
                self._get_updates_buf = data.get("get_updates_buf") or ""
                tokens = data.get("context_tokens") or {}
                if isinstance(tokens, dict):
                    self._context_tokens = {str(k): str(v) for k, v in tokens.items() if v}
        except Exception as e:
            log.warn(f"【WeChatOpenClaw】加载状态失败: {e}")

    def __save_state(self):
        path = self.__state_file()
        if not path:
            return
        try:
            with self._state_lock:
                data = {
                    "get_updates_buf": self._get_updates_buf or "",
                    "context_tokens": dict(self._context_tokens),
                }
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as e:
            log.warn(f"【WeChatOpenClaw】保存状态失败: {e}")

    # ------------------------------------------------------------------
    # HTTP 基础
    # ------------------------------------------------------------------

    @staticmethod
    def __random_wechat_uin():
        n = int.from_bytes(secrets.token_bytes(4), "big")
        return base64.b64encode(str(n).encode("utf-8")).decode("ascii")

    def __build_headers(self):
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": self.__random_wechat_uin(),
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        }
        if self._bot_token:
            headers["Authorization"] = f"Bearer {self._bot_token}"
        if self._route_tag:
            headers["SKRouteTag"] = self._route_tag
        return headers

    def __post_raw(self, endpoint, payload, timeout):
        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            return RequestUtils(headers=self.__build_headers(),
                                proxies=Config().get_proxies(),
                                timeout=timeout).post_res(url, params=body)
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return None

    # ------------------------------------------------------------------
    # 长轮询线程：拉取 context_token，维护 get_updates_buf
    # ------------------------------------------------------------------

    def __start_poll_thread(self):
        """
        进程级单例：每个 bot_token 只跑一条长轮询线程。
        若已存在同 token 的活跃 owner 线程，本实例不再启动，避免并发被服务端踢成 -14。
        """
        if not self._bot_token:
            return
        with WeChatOpenClaw._poll_owners_lock:
            owner = WeChatOpenClaw._poll_owners.get(self._bot_token)
            if owner is not None and owner._poll_thread \
                    and owner._poll_thread.is_alive() \
                    and not owner._stop_event.is_set():
                # 已有 owner 在跑；本实例只读它的 context_token/buf
                log.debug(f"【WeChatOpenClaw】复用已有长轮询 owner，token={self._bot_token[:6]}...")
                return
            WeChatOpenClaw._poll_owners[self._bot_token] = self
        self._stop_event.clear()
        t = threading.Thread(target=self.__poll_loop,
                             name="WeChatOpenClawPoll",
                             daemon=True)
        self._poll_thread = t
        t.start()

    def __get_poll_owner(self):
        """返回当前 token 的 owner 实例（可能是自己），或 None。"""
        if not self._bot_token:
            return None
        with WeChatOpenClaw._poll_owners_lock:
            return WeChatOpenClaw._poll_owners.get(self._bot_token)

    def stop_service(self):
        """Message 在 init_config 时会调用以停止旧客户端"""
        self._stop_event.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=2)
        # 仅当自己是 owner 时才从登记表里摘除
        with WeChatOpenClaw._poll_owners_lock:
            if WeChatOpenClaw._poll_owners.get(self._bot_token) is self:
                WeChatOpenClaw._poll_owners.pop(self._bot_token, None)

    def __do_get_updates(self, reset_buf=False):
        """单次调用 getUpdates；返回 (data, ok)。reset_buf=True 时强制使用空 buf。"""
        if reset_buf:
            buf = ""
        else:
            with self._state_lock:
                buf = self._get_updates_buf or ""
        payload = {
            "get_updates_buf": buf,
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        res = self.__post_raw("ilink/bot/getupdates", payload,
                              timeout=LONG_POLL_TIMEOUT_S + 5)
        if not res:
            return None, False
        try:
            data = res.json() if res.text else {}
        except Exception:
            return None, False
        ret = data.get("ret")
        errcode = data.get("errcode")
        ok = (ret in (None, 0)) and (errcode in (None, 0))
        # 处理消息体（无论成功失败都尽量提取）
        if ok:
            new_buf = data.get("get_updates_buf")
            updated = False
            with self._state_lock:
                if new_buf:
                    if self._get_updates_buf != new_buf:
                        self._get_updates_buf = new_buf
                        updated = True
                for m in data.get("msgs") or []:
                    from_uid = m.get("from_user_id") or ""
                    ctok = m.get("context_token")
                    if from_uid and ctok and self._context_tokens.get(from_uid) != ctok:
                        self._context_tokens[from_uid] = ctok
                        updated = True
                        log.info(f"【WeChatOpenClaw】更新 context_token user={from_uid[:8]}...")
            if updated:
                self.__save_state()
        return data, ok

    def __poll_loop(self):
        log.info("【WeChatOpenClaw】长轮询线程启动")
        consecutive_failures = 0
        while not self._stop_event.is_set():
            if not self._bot_token:
                break
            data, ok = self.__do_get_updates()
            if self._stop_event.is_set():
                break
            if ok:
                consecutive_failures = 0
                continue
            # 失败：可能是网络超时（data=None）或服务端错误
            errcode = (data or {}).get("errcode")
            ret = (data or {}).get("ret")
            errmsg = (data or {}).get("errmsg")
            if errcode == SESSION_EXPIRED_ERRCODE or ret == SESSION_EXPIRED_ERRCODE:
                # 会话过期：清空 buf 重试一次（使用最新 token 重建会话）
                log.warn(f"【WeChatOpenClaw】getUpdates 会话过期，重置 buf 重试")
                with self._state_lock:
                    self._get_updates_buf = ""
                self.__save_state()
                self._stop_event.wait(RETRY_DELAY_S)
                continue
            if data is not None:
                log.warn(f"【WeChatOpenClaw】getUpdates 失败 ret={ret} "
                         f"errcode={errcode} errmsg={errmsg}")
            consecutive_failures += 1
            delay = BACKOFF_DELAY_S if consecutive_failures >= MAX_CONSECUTIVE_FAILURES else RETRY_DELAY_S
            self._stop_event.wait(delay)
        log.info("【WeChatOpenClaw】长轮询线程退出")

    # ------------------------------------------------------------------
    # sendmessage
    # ------------------------------------------------------------------

    def __post_send_once(self, payload):
        """单次 sendmessage；返回 (ok, errcode, errmsg)。"""
        res = self.__post_raw("ilink/bot/sendmessage", payload,
                              timeout=DEFAULT_API_TIMEOUT_S)
        if not res:
            return False, None, "未获取到响应"
        raw_text = res.text[:500] if res.text else ""
        log.info(f"【WeChatOpenClaw】resp status={res.status_code} body={raw_text}")
        try:
            ret_json = res.json()
        except Exception:
            if res.status_code // 100 == 2:
                return True, 0, ""
            return False, None, f"HTTP {res.status_code}: {raw_text}"
        ret = ret_json.get("ret")
        errcode = ret_json.get("errcode")
        errmsg = ret_json.get("errmsg")
        if res.status_code // 100 == 2 and ret in (None, 0) and errcode in (None, 0):
            return True, 0, ""
        # 归一化 errcode：errcode 优先，其次 ret
        ec = errcode if errcode not in (None, 0) else ret
        return False, ec, f"ret={ret} errcode={errcode} errmsg={errmsg}"

    def __post_send(self, payload):
        """
        发送消息。-14 时不在此处主动调 getUpdates，避免与 owner 长轮询线程并发，
        被服务端再次踢成 -14。让 owner 的 __poll_loop 自己负责会话恢复。
        """
        ok, errcode, msg = self.__post_send_once(payload)
        if ok:
            return True, ""
        if errcode == SESSION_EXPIRED_ERRCODE:
            return False, ("会话过期(errcode=-14)，"
                           "稍候由后台轮询自动重建会话；"
                           "若持续失败请用该微信号给 bot 发一条任意消息后重试")
        return False, msg

    def __read_context_token(self, to_user_id):
        """优先从 owner 读 context_token；fallback 自身缓存。"""
        if not to_user_id:
            return None
        owner = self.__get_poll_owner()
        if owner is not None and owner is not self:
            with owner._state_lock:
                ctok = owner._context_tokens.get(to_user_id)
                if ctok:
                    return ctok
        with self._state_lock:
            return self._context_tokens.get(to_user_id)

    def __build_text_message(self, content, to_user_id):
        client_id = "nastools-" + secrets.token_hex(8)
        ctok = self.__read_context_token(to_user_id)
        msg = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            # MessageType.BOT=2, MessageState.FINISH=2, MessageItemType.TEXT=1
            "message_type": 2,
            "message_state": 2,
            "item_list": [
                {"type": 1, "text_item": {"text": content}}
            ],
        }
        if ctok:
            msg["context_token"] = ctok
        return {
            "msg": msg,
            "base_info": {"channel_version": CHANNEL_VERSION},
        }

    def __precheck(self, to_user_id):
        if not self._bot_token:
            return False, "bot_token 未配置，请先扫码登录获取"
        if not to_user_id:
            return False, "to_user_id 未配置"
        return True, ""

    def send_msg(self, title, text="", image="", url="", user_id=None):
        """
        发送文本消息。openclaw-weixin 的 sendmessage 仅支持文本/图片/语音/文件/视频；
        图片需经 CDN 上传并加密，复杂度较高，本实现先将图片/链接拼成文本发送。
        """
        if not title and not text:
            return False, "标题和内容不能同时为空"
        to = (user_id or "").strip() or self._to_user_id
        ok, err = self.__precheck(to)
        if not ok:
            return False, err
        parts = []
        if title:
            parts.append(title)
        if text:
            parts.append(text.replace("\n\n", "\n"))
        if url:
            parts.append(url)
        if image:
            parts.append(image)
        content = "\n".join(parts)
        return self.__post_send(self.__build_text_message(content, to))

    def send_list_msg(self, medias: list, user_id="", title="", **kwargs):
        if not isinstance(medias, list) or not medias:
            return False, "数据错误"
        to = (user_id or "").strip() or self._to_user_id
        ok, err = self.__precheck(to)
        if not ok:
            return False, err
        lines = []
        if title:
            lines.append(title)
        for idx, media in enumerate(medias, 1):
            try:
                vote = media.get_vote_string()
                head = f"{idx}. {media.get_title_string()}"
                meta = media.get_type_string()
                if vote:
                    meta = f"{meta}，{vote}"
                detail = media.get_detail_url()
                lines.append(f"{head}\n{meta}\n{detail}")
            except Exception:
                continue
        return self.__post_send(self.__build_text_message("\n\n".join(lines), to))
