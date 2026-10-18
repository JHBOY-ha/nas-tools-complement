import datetime
import hmac
from functools import wraps

import jwt
from flask import g, request

from app.utils import TokenCache
from config import Config
from web.backend.user import User
from web.backend.action_permissions import ACTION_PERMISSIONS, action_allowed


def require_auth(func):
    """
    API安全认证
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization")
        parts = auth.split() if auth else []
        key = Config().get_config("security").get("api_key")
        if parts and isinstance(key, str) and key:
            # Byte comparison also handles non-ASCII input without a TypeError.
            if hmac.compare_digest(parts[-1].encode("utf-8"), key.encode("utf-8")):
                g.api_key_authenticated = True
                return func(*args, **kwargs)
        return {
            "code": 401,
            "success": False,
            "message": "安全认证未通过，请检查ApiKey"
        }

    return wrapper


def generate_access_token(username: str, algorithm: str = 'HS256', exp: float = 2):
    """
    生成access_token
    :param username: 用户名(自定义部分)
    :param algorithm: 加密算法
    :param exp: 过期时间，默认2小时
    :return:token
    """

    now = datetime.datetime.utcnow()
    exp_datetime = now + datetime.timedelta(hours=exp)
    access_payload = {
        'exp': exp_datetime,
        'iat': now,
        'username': username
    }
    access_token = jwt.encode(access_payload,
                              Config().get_config("security").get("api_key"),
                              algorithm=algorithm)
    return access_token


def __decode_auth_token(token: str, algorithms='HS256'):
    """
    解密token
    :param token:token字符串
    :return: 是否有效，playload
    """
    key = Config().get_config("security").get("api_key")
    try:
        payload = jwt.decode(token,
                             key=key,
                             algorithms=algorithms,
                             options={"require": ["exp", "iat", "username"]})
    except jwt.InvalidTokenError:
        # Expiration is an authentication failure, never an implicit refresh.
        return False, {}
    else:
        return True, payload


def identify(auth_header: str):
    """
    用户鉴权，返回是否有效、用户名
    """
    flag = False
    if auth_header:
        flag, payload = __decode_auth_token(auth_header)
        if payload:
            return flag, payload.get("username") or ""
    return flag, ""


def _authorization_token():
    """Accept the existing raw token and the standard Bearer form."""
    parts = (request.headers.get("Authorization") or "").split()
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def _cached_token_user(token):
    """Validate expiration, revocation and current identity before any API action."""
    if not token:
        return None
    latest_token = TokenCache.get(token)
    if not isinstance(latest_token, str) or not hmac.compare_digest(token.encode(), latest_token.encode()):
        return None
    flag, username = identify(token)
    if not flag or not username:
        TokenCache.delete(token)
        return None
    user = User().get_user(username)
    if not user:
        TokenCache.delete(token)
    return user


def require_api_auth(func):
    """REST resources accept scoped user JWTs or the trusted integration API key.

    Keep require_auth master-key-only for native webhook/automation endpoints.
    """
    key_authenticated = require_auth(func)

    @wraps(func)
    def wrapper(*args, **kwargs):
        user = _cached_token_user(_authorization_token())
        if user:
            g.api_user = user
            return func(*args, **kwargs)
        return key_authenticated(*args, **kwargs)

    return wrapper


def api_permission_required(command):
    """Protect resource methods that call services without the action dispatcher."""
    def decorate(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if command in ACTION_PERMISSIONS and (getattr(g, "api_key_authenticated", False)
                                                  or action_allowed(getattr(g, "api_user", None), command)):
                return func(*args, **kwargs)
            return {"code": 403, "success": False, "message": "没有执行此操作的权限"}
        return wrapper
    return decorate


def login_required(func):
    """
    登录保护，验证用户是否登录
    :param func:
    :return:
    """

    @wraps(func)
    def wrapper(*args, **kwargs):

        def auth_failed():
            return {
                "code": 403,
                "success": False,
                "message": "安全认证未通过，请检查Token"
            }

        user = _cached_token_user(_authorization_token())
        if not user:
            return auth_failed()
        g.api_user = user
        return func(*args, **kwargs)

    return wrapper
