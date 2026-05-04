#!/usr/bin/env python3
"""
openclaw-weixin 扫码登录 CLI（独立脚本，不依赖 nastools 主程序）

用法:
    python3 app/helper/openclaw_wechat_login.py

流程:
    1. 调用 ilink/bot/get_bot_qrcode 获取二维码 URL
    2. 终端用 qrcode 库渲染二维码（若已安装），并打印图片 URL
    3. 长轮询 ilink/bot/get_qrcode_status，处理 wait/scaned/expired/scaned_but_redirect/confirmed
    4. 登录成功后输出 bot_token / ilink_user_id / base_url，
       将它们粘贴到 nastools 的「微信(OpenClaw)」消息渠道配置即可

协议参考: https://github.com/Tencent/openclaw-weixin/blob/main/src/auth/login-qr.ts
"""
import sys
import time
from urllib.parse import quote

import requests

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
BOT_TYPE = "3"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (1 << 8) | 7
QR_POLL_TIMEOUT = 35
MAX_REFRESH = 3
LOGIN_TIMEOUT = 480


def _headers():
    return {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }


def fetch_qrcode(base_url):
    url = f"{base_url}/ilink/bot/get_bot_qrcode?bot_type={quote(BOT_TYPE)}"
    r = requests.get(url, headers=_headers(), timeout=15, verify=False)
    r.raise_for_status()
    return r.json()


def poll_status(base_url, qrcode):
    url = f"{base_url}/ilink/bot/get_qrcode_status?qrcode={quote(qrcode)}"
    try:
        r = requests.get(url, headers=_headers(), timeout=QR_POLL_TIMEOUT, verify=False)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.Timeout:
        return {"status": "wait"}
    except Exception as e:
        print(f"[warn] poll error, retry: {e}", file=sys.stderr)
        return {"status": "wait"}


def render_qr(content):
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(content)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        print("(未安装 qrcode 库，跳过终端渲染。可执行 `pip install qrcode` 启用)")


def main():
    requests.packages.urllib3.disable_warnings()
    base_url = DEFAULT_BASE_URL
    print(f"获取二维码 from {base_url} ...")
    qr = fetch_qrcode(base_url)
    qrcode_token = qr["qrcode"]
    qr_img = qr["qrcode_img_content"]
    print(f"二维码链接: {qr_img}\n")
    render_qr(qr_img)

    deadline = time.time() + LOGIN_TIMEOUT
    refresh = 1
    poll_base = base_url
    scanned = False

    while time.time() < deadline:
        resp = poll_status(poll_base, qrcode_token)
        status = resp.get("status")

        if status == "wait":
            sys.stdout.write("."); sys.stdout.flush()
        elif status == "scaned":
            if not scanned:
                print("\n👀 已扫码，请在微信中确认...")
                scanned = True
        elif status == "scaned_but_redirect":
            host = resp.get("redirect_host")
            if host:
                poll_base = f"https://{host}"
                print(f"\n[info] IDC 重定向到 {poll_base}")
        elif status == "expired":
            refresh += 1
            if refresh > MAX_REFRESH:
                print("\n二维码多次过期，已放弃。")
                return 1
            print(f"\n二维码过期，刷新中 ({refresh}/{MAX_REFRESH})...")
            qr = fetch_qrcode(base_url)
            qrcode_token = qr["qrcode"]
            qr_img = qr["qrcode_img_content"]
            print(f"二维码链接: {qr_img}\n")
            render_qr(qr_img)
            scanned = False
        elif status == "confirmed":
            bot_token = resp.get("bot_token")
            bot_id = resp.get("ilink_bot_id")
            user_id = resp.get("ilink_user_id")
            new_base = resp.get("baseurl") or base_url
            print("\n\n✅ 登录成功！请将以下信息填入 nastools 「微信(OpenClaw)」配置：\n")
            print(f"  bot_token   : {bot_token}")
            print(f"  to_user_id  : {user_id}")
            print(f"  base_url    : {new_base}")
            print(f"  (ilink_bot_id={bot_id})")
            return 0
        else:
            print(f"\n未知状态: {status}, resp={resp}")

        time.sleep(1)

    print("\n登录超时。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
