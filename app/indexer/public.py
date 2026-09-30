"""Native public indexer adapters and runtime configuration."""

import threading
import time
import os

from app.indexer.providers import bt52, seedhub
from app.indexer.providers.common import network_options
from app.utils import ExceptionUtils
from config import Config


ADAPTERS = {
    "52bt": {
        "id": "52bt",
        "name": "52BT",
        "module": bt52,
        "base_help": "每行一个 HTTPS 基础链接。留空自动从发布页发现最新地址。"
    },
    "seedhub": {
        "id": "seedhub",
        "name": "SeedHub",
        "module": seedhub,
        "base_help": "每行一个 HTTPS 基础链接。留空使用内置镜像并自动记录有效跳转。"
    },
}

_LOCK = threading.Lock()
_LAST_REQUEST = {"52bt": 0.0}


def _provider_config():
    value = Config().get_config("pt").get("public_indexers") or {}
    return value if isinstance(value, dict) else {}


def _selected_ids():
    enabled = _provider_config().get("enabled")
    return set(enabled or [])


def get_indexers(check=True):
    selected = _selected_ids()
    output = []
    for item in ADAPTERS.values():
        if check and item["id"] not in selected:
            continue
        site = (_provider_config().get("sites") or {}).get(item["id"]) or {}
        output.append({
            "id": item["id"],
            "name": item["name"],
            "public": True,
            "builtin": True,
            "selected": item["id"] in selected,
            "bases": site.get("bases") or [],
            "base_help": item["base_help"],
            "module": "public",
            "pri": 1,
            "rule": None,
            "language": None,
        })
    return output


def _apply_bases():
    conf = _provider_config()
    seedhub.configure_session(state_path=os.path.join(Config().get_temp_path(),
                                                       "seedhub-circuit.json"))
    for item in ADAPTERS.values():
        site = (conf.get("sites") or {}).get(item["id"]) or {}
        bases = [str(value).strip() for value in (site.get("bases") or [])
                 if str(value).strip()]
        item["module"].configure_bases(bases)


def search(indexer_id, keyword, categories=None, fetch=150):
    item = ADAPTERS.get(indexer_id)
    if not item:
        return []
    _apply_bases()
    if indexer_id == "52bt":
        with _LOCK:
            wait = 20 - (time.monotonic() - _LAST_REQUEST[indexer_id])
            if wait > 0:
                time.sleep(wait)
            try:
                return item["module"].search(keyword, categories, fetch)
            finally:
                _LAST_REQUEST[indexer_id] = time.monotonic()
    return item["module"].search(keyword, categories, fetch)


def resolve(indexer_id, result):
    item = ADAPTERS.get(indexer_id)
    if not item:
        return ""
    _apply_bases()
    return item["module"].resolve_result(result)


def _convert(indexer_id, result):
    enclosure = (result.get("magnetUrl") or result.get("detailsUrl")
                 or result.get("downloadUrl") or "")
    if not str(enclosure).startswith("magnet:"):
        enclosure = f"public:{indexer_id}:{enclosure}"
    return {
        "indexer": indexer_id,
        "title": result.get("title") or "",
        "enclosure": enclosure,
        "description": "",
        "size": result.get("size") or 0,
        "seeders": result.get("seeders") or 0,
        "peers": result.get("leechers") or 0,
        "freeleech": True,
        "downloadvolumefactor": 0.0,
        "uploadvolumefactor": 1.0,
        "page_url": result.get("detailsUrl") or result.get("magnetUrl") or "",
        "imdbid": "",
        "_public_result": result,
    }


def search_for_nastools(indexer_id, keyword, categories=None, fetch=150):
    try:
        with network_options(proxies=Config().get_proxies(), seconds=90):
            results = search(indexer_id, keyword, categories, fetch)
        return [_convert(indexer_id, result) for result in results]
    except Exception as err:
        ExceptionUtils.exception_traceback(err)
        return []


def resolve_for_nastools(indexer_id, result):
    try:
        with network_options(proxies=Config().get_proxies(), seconds=30):
            magnet = resolve(indexer_id, result)
        return magnet if str(magnet).startswith("magnet:") else ""
    except Exception as err:
        ExceptionUtils.exception_traceback(err)
        return ""


def status():
    output = {}
    for item in ADAPTERS.values():
        state = item["module"].BASE_URLS.status()
        state.update({
            "id": item["id"],
            "name": item["name"],
            "bases": (_provider_config().get("sites") or {}).get(item["id"], {}).get("bases") or [],
        })
        output[item["id"]] = state
    return output


def refresh(indexer_id=None):
    _apply_bases()
    output = {}
    for item in ADAPTERS.values():
        if indexer_id and item["id"] != indexer_id:
            continue
        module = item["module"]
        if item["id"] == "52bt":
            try:
                with network_options(proxies=Config().get_proxies(), seconds=30):
                    discovered = module.discover_bases()
                custom = (_provider_config().get("sites") or {}).get(item["id"], {}).get("bases") or []
                module.BASE_URLS.update(tuple(custom) + tuple(discovered))
                message = "基础链接已重新获取"
            except Exception:
                message = "重新获取失败，继续使用当前候选"
        else:
            module.clear_cache()
            try:
                with network_options(proxies=Config().get_proxies(), seconds=30):
                    module.refresh_base()
                message = "基础链接已重新获取"
            except Exception:
                message = "重新获取失败，继续使用当前候选"
        state = module.BASE_URLS.status()
        state.update({"id": item["id"], "name": item["name"],
                      "bases": (_provider_config().get("sites") or {}).get(item["id"], {}).get("bases") or [],
                      "message": message})
        output[item["id"]] = state
    return output
