# -*- coding: utf-8 -*-
"""七子地理引擎 V2.1：MMDB 离线查询、候选偏移和高位坐标格式。

这不是 GPS 精度修复：候选偏移只是可重复的模拟坐标，不可据此推断
IP 的真实地点。数值在内部使用 Python float，全程不提前做小数截断。
"""
from __future__ import annotations

import hashlib
import math
import os
import shutil
import tempfile
from pathlib import Path

try:
    import maxminddb
except ImportError:
    maxminddb = None

EARTH_RADIUS_M = 6371008.8
COORD_DECIMALS = 12
MAX_SYNTHETIC_OFFSET_M = 55.5


def exit_coordinates(proxy):
    """Return the current public IP's raw MMDB point, never the synthetic display offset."""
    if not isinstance(proxy, dict):
        return None
    public_ip = str(proxy.get('public_ip') or '').strip()
    source_ip = str(proxy.get('geo_source_ip') or '').strip()
    if not public_ip or source_ip != public_ip:
        return None

    raw_lat = proxy.get('geo_raw_lat')
    raw_lon = proxy.get('geo_raw_lon')
    if raw_lat not in (None, '') and raw_lon not in (None, ''):
        lat_text = str(raw_lat).strip()
        lon_text = str(raw_lon).strip()
    else:
        lat_text = str(proxy.get('geo_lat') or '').strip()
        lon_text = str(proxy.get('geo_lon') or '').strip()
    try:
        lat, lon = float(lat_text), float(lon_text)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)
            and -90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat_text, lon_text


def format_coordinate(value, places=COORD_DECIMALS):
    """仅对输出作固定小数位格式化；float 的计算精度不受影响。"""
    if value is None or str(value).strip() == '':
        return ''
    val = float(value)
    if not math.isfinite(val):
        raise ValueError('经纬度不能是 NaN 或无穷大')
    return f'{val:.{places}f}'


def original_coordinate(value):
    if value is None:
        return ''
    return repr(value) if isinstance(value, float) else str(value)


def text_of(obj):
    if isinstance(obj, dict):
        names = obj.get('names') or {}
        return names.get('zh-CN') or names.get('en') or obj.get('name') or ''
    return ''


def safe_database_path(path):
    """兼容 Windows 下部分 maxminddb 原生库不支持中文路径的情况。"""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f'原始数据库文件不存在：{path}')
    try:
        str(path).encode('ascii')
        return path, False
    except UnicodeEncodeError:
        pass

    candidates = [
        Path(os.environ.get('PUBLIC', r'C:\Users\Public')) / 'QiziGeoIPCache',
        Path(os.environ.get('PROGRAMDATA', r'C:\ProgramData')) / 'QiziGeoIPCache',
        Path(tempfile.gettempdir()) / 'QiziGeoIPCache',
    ]
    key = hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:16]
    last_error = None
    for folder in candidates:
        try:
            str(folder).encode('ascii')
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / f'{key}.mmdb'
            stat = path.stat()
            if (not dest.is_file() or dest.stat().st_size != stat.st_size
                    or dest.stat().st_mtime_ns != stat.st_mtime_ns):
                temp = folder / f'{key}.tmp'
                try:
                    shutil.copy2(path, temp)
                    os.replace(temp, dest)
                finally:
                    if temp.exists():
                        temp.unlink()
            return dest, True
        except (OSError, UnicodeError) as exc:
            last_error = exc
    raise RuntimeError(f'无法将中文路径数据库复制到英文缓存目录：{last_error}')


def lookup(path, ip):
    if maxminddb is None:
        raise RuntimeError('缺少 maxminddb：py -3.13 -m pip install maxminddb')
    with maxminddb.open_database(str(path)) as reader:
        data = reader.get(ip)
    if not data:
        return None
    country = data.get('country') or data.get('registered_country') or {}
    subdivisions = data.get('subdivisions') or []
    subdivision = subdivisions[0] if subdivisions else {}
    loc = data.get('location') or {}
    return {
        'IP 地址': ip,
        '国家/地区': text_of(country),
        '国家代码': country.get('iso_code', ''),
        '省/州': text_of(subdivision),
        '城市': text_of(data.get('city')),
        '邮政编码': (data.get('postal') or {}).get('code', ''),
        '纬度': original_coordinate(loc.get('latitude')),
        '经度': original_coordinate(loc.get('longitude')),
        '时区': loc.get('time_zone', ''),
        '精度半径(km)': loc.get('accuracy_radius', ''),
    }


def inferred_display_step_m(latitude_text):
    """依据小数位数估计坐标显示网格间距，*不是* IP 定位误差半径。

    例如显示小数三位，相邻纬度值相差约111米；实际定位不确定性
    可能更大，也不能认定 MMDB 使用的是截断而非四舍五入。
    """
    text = str(latitude_text).strip()
    if '.' not in text:
        places = 0
    else:
        places = len(text.split('.', 1)[1])
    places = min(15, max(0, places))
    return EARTH_RADIUS_M * math.pi / 180 * (10.0 ** (-places))


def midpoint_candidate(lat, lon, accuracy_km=None, direction='northeast',
                       *, ip='', fallback_display_step=True, max_offset_m=MAX_SYNTHETIC_OFFSET_M,
                       manual_radius_m=None):
    """产生演示用高位坐标，默认由坐标显示网格间距推算一个很小的位移。

    manual_radius_m 显式覆盖位移估算；传 111 -> 偏移 55.5米。
    accuracy_km 仅作为 MMDB 的地理误差提示，不自动拿它的一半移动：
    数据库可能报告数十公里的定位误差，移几十公里是危险的。
    """
    lat_text, lon_text = str(lat), str(lon)
    lat, lon = float(lat), float(lon)
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError('经纬度不合法')
    if max_offset_m <= 0 or not math.isfinite(float(max_offset_m)):
        raise ValueError('最大位移必须是有限正数')

    if manual_radius_m is not None:
        ref_radius = float(manual_radius_m)
        if not math.isfinite(ref_radius) or ref_radius <= 0:
            raise ValueError('手动半径必须是有限正数')
        basis = '用户输入的距离参数'
    elif fallback_display_step:
        ref_radius = inferred_display_step_m(lat_text)
        basis = '按纬度显示小数位数估算的数值网格间距（非真实定位精度）'
    else:
        ref_radius = 0.0
        basis = '未启用补偿'

    offset = min(ref_radius / 2, float(max_offset_m))
    if abs(lat) > 89.9 or offset < 1e-6:
        offset = 0.0
        new_lat, new_lon = lat, lon
    else:
        # 北、东两个方向各分配约 1/sqrt(2) 的位移，确保纬经度均出现候选变化。
        north = offset / math.sqrt(2)
        east = offset / math.sqrt(2)
        new_lat = lat + math.degrees(north / EARTH_RADIUS_M)
        cos_lat = math.cos(math.radians(lat))
        new_lon = ((lon + math.degrees(east / (EARTH_RADIUS_M * cos_lat)) + 180) % 360) - 180

    # 保留字段名称，兼容以前的主程序接口。radius_m 是本算法参考的显示间距/手动数值，
    # 数据库定位误差单独存在 mmdb_accuracy_radius_km，避免概念混淆。
    return {
        'lat': new_lat, 'lon': new_lon,
        'offset_m': offset, 'radius_m': ref_radius if ref_radius > 0 else None,
        'display_step_m': inferred_display_step_m(lat_text),
        'mmdb_accuracy_radius_km': accuracy_km,
        'lat_text': format_coordinate(new_lat),
        'lon_text': format_coordinate(new_lon),
        'method': ('原始坐标保留（无偏移）' if not offset else
                   f'候选点：取{basis}的一半偏移，最大限制 {max_offset_m:g} 米；非真实精度提升'),
    }


def resolve_ip(path, ip, override_radius_m=None):
    path, _ = safe_database_path(path)
    result = lookup(path, ip)
    if not result:
        return None
    lat = result.get('纬度')
    lon = result.get('经度')
    if lat == '' or lon == '':
        return {**result, 'candidate': None}
    candidate = midpoint_candidate(
        lat, lon, result.get('精度半径(km)'), ip=ip,
        manual_radius_m=override_radius_m,
    )
    return {**result, 'candidate': candidate}
