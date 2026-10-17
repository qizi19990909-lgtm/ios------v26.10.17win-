# -*- coding: utf-8 -*-
"""离线 MMDB 自动发现；未来可添加远端 GeoIP 服务适配器。

默认仅本地查询，不联网提交用户 IP。远端服务的 URL、鉴权及缓存逻辑
未来实现时加在 query_remote() 中，当前不会偷偷尝试外部 API。
"""
from pathlib import Path

SERVER_LOOKUP_ENABLED = False  # 保留未来公网查询开关，目前未实现


def discover_mmdb(app_dir, fallback=''):
    root = Path(app_dir).resolve()
    candidates = [p for p in root.glob('*.mmdb') if p.is_file()]
    def rank(p):
        n = p.name.lower()
        return (0 if ('city' in n or '城市' in n) else
                1 if 'enterprise' in n else
                3 if ('asn' in n or 'country' in n) else 2, n)
    if candidates:
        return str(sorted(candidates, key=rank)[0])
    if fallback:
        p = Path(fallback).expanduser()
        if p.is_file() and p.suffix.lower() == '.mmdb':
            return str(p.resolve())
    return ''


def query_remote(ip, **kwargs):
    """未来服务器 GeoIP 查询扩展入口，当前明确不支持。"""
    raise NotImplementedError('公网 GeoIP 服务器查询尚未启用；当前只使用程序主目录的 MMDB')
