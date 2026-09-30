"""Parse untrusted configuration as bounded data, never executable Python."""
import ast
from fractions import Fraction
import ipaddress
import json
import operator
import re
from urllib.parse import urlsplit


def parse_episode_offset(expression):
    """Keep EP arithmetic compatible while excluding calls and unbounded powers."""
    if not isinstance(expression, str) or not 0 < len(expression) <= 128:
        raise ValueError("集偏移表达式长度无效")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, RecursionError) as err:
        raise ValueError("集偏移表达式格式无效") from err
    nodes = list(ast.walk(tree))
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name,
               ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
               ast.UAdd, ast.USub)
    if len(nodes) > 64 or not any(isinstance(node, ast.Name) and node.id == "EP" for node in nodes):
        raise ValueError("集偏移表达式必须包含 EP，且不能过于复杂")
    for node in nodes:
        if not isinstance(node, allowed):
            raise ValueError("集偏移仅支持 EP 与整数的加减乘除")
        if isinstance(node, ast.Name) and node.id != "EP":
            raise ValueError("集偏移仅允许 EP 变量")
        if isinstance(node, ast.Constant) and (type(node.value) is not int or abs(node.value) > 1000000):
            raise ValueError("集偏移常量必须为范围内的整数")
    return tree.body


def evaluate_episode_offset(expression, episode):
    """Use exact rational arithmetic; fractional/negative episodes stay unmodified."""
    root = parse_episode_offset(expression)
    operations = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                  ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv}

    def calculate(node):
        if isinstance(node, ast.Constant):
            value = Fraction(node.value)
        elif isinstance(node, ast.Name):
            value = Fraction(episode)
        elif isinstance(node, ast.UnaryOp):
            value = calculate(node.operand)
            if isinstance(node.op, ast.USub):
                value = -value
        else:
            value = Fraction(operations[type(node.op)](calculate(node.left), calculate(node.right)))
        if abs(value.numerator) > 1000000000 or value.denominator > 1000000000:
            raise ValueError("集偏移计算超出允许范围")
        return value

    try:
        result = calculate(root)
    except ZeroDivisionError as err:
        raise ValueError("集偏移不能除以零") from err
    if result.denominator != 1 or result < 0:
        raise ValueError("集偏移结果必须为非负整数")
    return int(result)


def parse_rule_dict(value):
    """Read new JSON and legacy dict repr without evaluating database expressions."""
    if isinstance(value, dict):
        result = value
    elif isinstance(value, str) and len(value) <= 65536:
        try:
            result = json.loads(value)
        except RecursionError as err:
            raise ValueError("规则过于复杂") from err
        except ValueError:
            try:
                tree = ast.parse(value, mode="eval")
                if sum(1 for _ in ast.walk(tree)) > 4096:
                    raise ValueError("规则过于复杂")
                result = ast.literal_eval(tree)
            except (SyntaxError, ValueError, TypeError, RecursionError) as err:
                raise ValueError("规则必须为字典数据") from err
    else:
        raise ValueError("规则数据类型或长度无效")
    if not isinstance(result, dict) or any(not isinstance(key, str) for key in result):
        raise ValueError("规则必须为字符串键的字典")
    try:
        # Reject non-JSON objects and NaN before passing data to business logic.
        json.dumps(result, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as err:
        raise ValueError("规则包含无效数据") from err
    return result


def normalize_proxies(proxies):
    """Accept supported proxy URLs, including credentials and bracketed IPv6."""
    if proxies is None:
        return {}
    if not isinstance(proxies, dict) or any(key not in ("http", "https") for key in proxies):
        raise ValueError("代理配置必须为 http/https 字典")
    result = {}
    for key, value in proxies.items():
        if value in (None, ""):
            continue
        if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError("代理地址格式无效")
        try:
            parts = urlsplit(value)
            hostname = parts.hostname
            if parts.scheme not in ("http", "https", "socks4", "socks4a", "socks5", "socks5h") or not hostname:
                raise ValueError("代理协议或主机无效")
            if parts.path not in ("", "/") or parts.query or parts.fragment:
                raise ValueError("代理地址不能包含路径、查询或片段")
            if parts.port is not None and not 0 < parts.port <= 65535:
                raise ValueError("代理端口无效")
            try:
                ipaddress.ip_address(hostname)
            except ValueError:
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", hostname.encode("idna").decode("ascii")):
                    raise ValueError("代理主机无效")
        except (ValueError, UnicodeError) as err:
            # Never echo a URL that may contain passwords in validation errors.
            raise ValueError("代理地址格式无效") from err
        result[key] = value
    return result


def compile_ignore_pattern(value):
    """Validate the existing semicolon-separated regex convention before saving."""
    if not value:
        return None
    if not isinstance(value, str):
        raise ValueError("忽略词必须为文本")
    pattern = value.rstrip(";").replace(";", "|")
    # A delimiter-only filter otherwise matches every file and silently stops transfers.
    if not pattern:
        raise ValueError("忽略词不能仅包含分隔符")
    try:
        return re.compile(pattern)
    except re.error as err:
        raise ValueError("忽略词正则表达式无效") from err
