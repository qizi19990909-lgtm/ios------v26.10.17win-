# -*- coding: utf-8 -*-
"""Proxy text importer: host:port:user:pass, URI, curl and labelled blocks."""
import ipaddress
import re
import shlex
from urllib.parse import urlsplit, unquote

_LABELS = {
    'host': r'(?:proxy\s*server|proxy\s*host|server|host|服务器|代理服务器|代理地址|地址|ip(?:\s*address)?)',
    'port': r'(?:port|端口)',
    'username': r'(?:username|user(?:\s*name)?|login|账号|账户|用户名|用户)',
    'password': r'(?:password|passwd|pass|pwd|密码)',
}
_LABEL_RE = re.compile(r'^\s*(' + '|'.join(f'(?P<{k}>{v})' for k,v in _LABELS.items()) + r')\s*[:：=]\s*(.*?)\s*$', re.I)
_SCHEMES = ('http://', 'https://', 'socks5://', 'socks5h://', 'socks4://', 'socks4a://')

def _validate(host, port, username, password):
    host = str(host).strip().strip('[]')
    if not host or any(c.isspace() for c in host) or '/' in host or '@' in host:
        raise ValueError('服务器地址无效')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or not re.fullmatch(r'(?i)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host) or '..' in host:
            raise ValueError('服务器域名无效')
        if any(not part or len(part) > 63 or part.startswith('-') or part.endswith('-') for part in host.split('.')):
            raise ValueError('服务器域名无效')
    try:
        port = int(str(port).strip())
    except (ValueError, TypeError):
        raise ValueError('端口不是数字') from None
    if not 1 <= port <= 65535:
        raise ValueError('端口超出范围')
    if not username or not password:
        raise ValueError('账号或密码为空')
    return dict(host=host, port=port, username=str(username), password=str(password))

def _parse_uri(value):
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in ('http','https','socks5','socks5h','socks4','socks4a'):
        raise ValueError('不支持的代理协议')
    try:
        record = _validate(parsed.hostname, parsed.port, unquote(parsed.username or ''), unquote(parsed.password or ''))
    except (ValueError, TypeError) as exc:
        raise ValueError('代理链接无效：' + str(exc)) from None
    record['type'] = 'socks5' if parsed.scheme.lower().startswith('socks5') else 'http'
    return record

def _parse_colon(value):
    value = value.strip()
    if value.startswith('['):
        match = re.fullmatch(r'\[([^]]+)\]:(\d+):([^:]+):(.+)', value)
        if not match:
            raise ValueError('IPv6 格式应为 [地址]:端口:账号:密码')
        return _validate(*match.groups())
    parts = value.split(':', 3)
    if len(parts) != 4:
        raise ValueError('格式不是 地址:端口:账号:密码')
    return _validate(*parts)

def _parse_curl(line):
    try:
        tokens = shlex.split(line, posix=True)
    except ValueError:
        raise ValueError('curl 命令引号不完整') from None
    proxy = None
    credentials = None
    for i, token in enumerate(tokens):
        if token in ('-x','--proxy') and i + 1 < len(tokens):
            proxy = tokens[i+1]
        elif token.startswith('--proxy='):
            proxy = token.partition('=')[2]
        elif token in ('-U','--proxy-user') and i + 1 < len(tokens):
            credentials = tokens[i+1]
        elif token.startswith('--proxy-user='):
            credentials = token.partition('=')[2]
    if not proxy:
        raise ValueError('curl 命令没有 -x/--proxy')
    if '://' not in proxy:
        proxy = 'http://' + proxy
    if credentials:
        u,sep,p = credentials.partition(':')
        if not sep:
            raise ValueError('curl 代理账号应为 账号:密码')
        parsed = urlsplit(proxy)
        if parsed.username is None:
            from urllib.parse import quote
            auth = quote(u,safe='') + ':' + quote(p,safe='') + '@'
            proxy = parsed.scheme + '://' + auth + parsed.netloc + (parsed.path or '')
    return _parse_uri(proxy)

def qizi_parse_proxy_lines(raw):
    """Return (records, errors). Never echo passwords in errors."""
    records, errors, seen = [], [], set()
    labelled = {}
    label_start = 0
    def append(record):
        key = (record['host'].lower(),record['port'],record['username'],record['password'])
        if key not in seen:
            seen.add(key)
            records.append(record)
    def flush():
        nonlocal labelled, label_start
        if labelled:
            missing = [k for k in ('host','port','username','password') if not labelled.get(k)]
            if missing:
                errors.append(f'第 {label_start} 行起的字段块不完整，缺少：' + ', '.join(missing))
            else:
                try: append(_validate(**labelled))
                except ValueError as exc: errors.append(f'第 {label_start} 行字段块：{exc}')
            labelled = {}
            label_start = 0
    for lineno, original in enumerate(str(raw).splitlines(), 1):
        line = original.strip()
        if not line:
            continue
        match = _LABEL_RE.match(line)
        if match:
            key = next(k for k in _LABELS if match.group(k) is not None)
            if key in labelled:
                flush()
            if not labelled: label_start = lineno
            labelled[key] = match.group(0).split(':',1)[-1].strip() if ':' in match.group(0) else re.split(r'[=：]', match.group(0), maxsplit=1)[-1].strip()
            if len(labelled) == 4: flush()
            continue
        flush()
        try:
            if re.match(r'(?i)^curl(?:\.exe)?\s',line):
                append(_parse_curl(line))
            elif line.lower().startswith(_SCHEMES):
                append(_parse_uri(line))
            else:
                append(_parse_colon(line))
        except ValueError as exc:
            errors.append(f'第 {lineno} 行：{exc}')
    flush()
    return records, errors
