# -*- coding: utf-8 -*-
"""动态 HTTP 代理 API 解析与验证（不操作 Tk / 设备 / Windows 网络）。

API 返回 IP 不等于已验证公网出口 IP。验证由主程序的 HTTPS-through-proxy
probe 提供。所有 API 读取/代理测试都必须在后台线程中执行。
"""
from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import urlsplit, unquote

import requests

MAX_API_RESPONSE_BYTES = 256 * 1024
MAX_FIELD_CHARS = 2048


def _path(obj, path):
    """配置路径如 data.proxy.ip 或 data.items[0].ip；不执行表达式。"""
    if not path:
        return None
    parts = re.findall(r'[^.\[\]]+', str(path))
    cur = obj
    for key in parts:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and key.isdecimal():
            i = int(key)
            cur = cur[i] if 0 <= i < len(cur) else None
        else:
            return None
    return cur


def _first_path(data, names):
    for path in names:
        value = _path(data, path)
        if value is not None and str(value).strip():
            return value
    return None


def validate_host(value):
    host = str(value or '').strip()
    if not host or len(host) > 253 or any(c.isspace() for c in host):
        raise ValueError('API 没有返回有效的代理服务器地址')
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    # Hostname (no URL/path/userinfo), IPv6 only accepted as literal via ipaddress.
    if not re.fullmatch(r'(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host) or '..' in host:
        raise ValueError('代理服务器地址不是有效 IP / 域名')
    return host.lower()


def parse_response(payload, config, current=None):
    """解析 JSON；必须有上游 host，可配置复用当前端口与认证。"""
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, (dict, list)):
        raise ValueError('API 应返回 JSON 对象或数组')
    config = config or {}
    current = current or {}
    host_paths = [config.get('host_path')] if config.get('host_path') else [
        'data.proxy.host', 'data.proxy.ip', 'data.host', 'data.ip',
        'proxy.host', 'proxy.ip', 'host', 'ip', 'data.proxy', 'proxy',
        'result.proxy.host', 'result.ip', 'result.host', 'result',
    ]
    value = _first_path(payload, host_paths)
    if value is None:
        raise ValueError('JSON 未找到代理主机，请设置 host_path（例如 data.ip）')
    host = None
    port_from_url = None
    username_from_url = None
    password_from_url = None
    if isinstance(value, dict):
        host = value.get('host') or value.get('ip')
        port_from_url = value.get('port')
    else:
        val = str(value).strip()
        # Support http://user:pass@host:port, and host:port; avoid accidental parsing
        # a pure IPv6 address as host:port.
        if '://' in val:
            u = urlsplit(val)
            if u.scheme not in ('http', 'https') or u.path not in ('', '/') or u.query or u.fragment:
                raise ValueError('当前只支持 HTTP / HTTPS 上游代理服务器地址，不允许附带路径')
            host = u.hostname
            port_from_url = u.port
            username_from_url = unquote(u.username) if u.username is not None else None
            password_from_url = unquote(u.password) if u.password is not None else None
        elif val.count(':') == 1 and '/' not in val:
            a, b = val.rsplit(':', 1)
            if b.isdecimal():
                host, port_from_url = a, b
            else:
                host = val
        else:
            host = val
    host = validate_host(host)
    port_raw = (_path(payload, config['port_path']) if config.get('port_path') else
                _first_path(payload, ('data.proxy.port', 'data.port', 'proxy.port', 'port', 'result.port')))
    if port_raw in (None, ''):
        port_raw = port_from_url or current.get('port') or config.get('port')
    try:
        port = int(port_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError('API 未返回代理端口，且没有可复用的端口') from exc
    if not 1 <= port <= 65535:
        raise ValueError('代理端口必须在 1~65535')
    user = _path(payload, config['username_path']) if config.get('username_path') else None
    pwd = _path(payload, config['password_path']) if config.get('password_path') else None
    username = str(user if user is not None else username_from_url if username_from_url is not None
                   else current.get('username', '') or config.get('username', ''))
    password = str(pwd if pwd is not None else password_from_url if password_from_url is not None
                   else current.get('password', '') or config.get('password', ''))
    if len(username) > MAX_FIELD_CHARS or len(password) > MAX_FIELD_CHARS:
        raise ValueError('代理用户名/密码字段过长')
    # A public exit IP (if present) is *not* the upstream connection address.
    expected = _path(payload, config.get('public_ip_path')) if config.get('public_ip_path') else None
    if expected:
        try:
            expected = str(ipaddress.ip_address(str(expected)))
        except ValueError as exc:
            raise ValueError('public_ip_path 不是合法 IP') from exc
    return dict(host=host, port=port, username=username, password=password, expected_public_ip=expected)


def normalise_api_config(config):
    if not isinstance(config, dict):
        raise ValueError('配置 JSON 必须是对象')
    obj = dict(config)
    if 'api' in obj and isinstance(obj['api'], dict):
        obj = dict(obj['api'])
    url = str(obj.get('url') or obj.get('api_url') or '').strip()
    if not url:
        raise ValueError('自动更新需要 URL；单次 JSON 数据无法判断未来是否发生变化')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('API URL 应为有效 http(s) 地址，账号请通过请求头提供')
    if not 1 <= len(url) <= 2048:
        raise ValueError('API URL 太长')
    method = str(obj.get('method', 'GET')).upper()
    if method not in ('GET', 'POST'):
        raise ValueError('只支持 GET / POST')
    headers = obj.get('headers') or {}
    if not isinstance(headers, dict) or len(headers) > 30:
        raise ValueError('headers 必须是 JSON 对象')
    headers = {str(k): str(v) for k, v in headers.items()}
    if any('\r' in x or '\n' in x for pair in headers.items() for x in pair):
        raise ValueError('请求头不能包含换行')
    if len(json.dumps(obj, ensure_ascii=False)) > 32768:
        raise ValueError('API 配置过大')
    return {**obj, 'url': url, 'method': method, 'headers': headers, 'enabled': bool(obj.get('enabled', True))}


def fetch_json(config):
    config = normalise_api_config(config)
    with requests.Session() as sess:
        sess.trust_env = False
        args = dict(headers=config.get('headers') or {}, timeout=(4, 8), allow_redirects=False, stream=True)
        if config['method'] == 'POST':
            args['json'] = config.get('body') if 'body' in config else {}
        with sess.request(config['method'], config['url'], **args) as response:
            response.raise_for_status()
            if 300 <= response.status_code <= 399:
                raise ValueError('API 发生重定向，已拒绝自动发送凭据到其他地址')
            content = bytearray()
            for chunk in response.iter_content(chunk_size=16384):
                content.extend(chunk)
                if len(content) > MAX_API_RESPONSE_BYTES:
                    raise ValueError('API 返回 JSON 超过 256KB')
            try:
                return json.loads(bytes(content).decode('utf-8-sig'))
            except (ValueError, UnicodeError) as exc:
                raise ValueError('API 响应不是有效的 UTF-8 JSON') from exc


def safe_status(exc):
    """避免输出含有 API key / 用户名 / URL 的异常字符串。"""
    if isinstance(exc, requests.exceptions.Timeout):
        return 'API 请求超时'
    if isinstance(exc, requests.exceptions.RequestException):
        return 'API 网络请求失败（请检查 URL、证书和网络）'
    value = str(exc)
    if isinstance(exc, ValueError):
        return value[:130]
    return f'{type(exc).__name__}（详细信息已隐藏）'
