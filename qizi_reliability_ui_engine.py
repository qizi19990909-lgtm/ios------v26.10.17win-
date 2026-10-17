# -*- coding: utf-8 -*-
"""V3.10.18 integrated reliability, durable live coordinates and route replay guards.

A cooperative, final MRO layer: no monkeypatches and no changes to the original
USB pairing, DVT sender, HTTPS proxy security or licensing implementation.
Only Tk's main thread accesses widgets and device/configuration state.
"""
from __future__ import annotations

import ipaddress
import json
import math
import random
import socket
import threading
import time
import tkinter as tk
from pathlib import Path

from qizi_route_engine import load_json_dict, save_route_plans


# 出口列表只使用已有 MMDB/代理检测结果，不为了展示城市而请求网络。
# GeoLite2-City 查询会优先返回 zh-CN；下表兼容历史版本保存的英文城市。
_QIZI_CITY_ZH = {
    'los angeles': '洛杉矶', 'new york': '纽约', 'new york city': '纽约',
    'san francisco': '旧金山', 'san jose': '圣何塞', 'san diego': '圣迭戈',
    'sacramento': '萨克拉门托', 'fresno': '弗雷斯诺',
    'washington': '华盛顿', 'washington d.c.': '华盛顿',
    'chicago': '芝加哥', 'houston': '休斯敦', 'dallas': '达拉斯',
    'austin': '奥斯汀', 'fort worth': '沃思堡', 'san antonio': '圣安东尼奥',
    'miami': '迈阿密', 'orlando': '奥兰多', 'tampa': '坦帕',
    'boston': '波士顿', 'seattle': '西雅图', 'portland': '波特兰',
    'las vegas': '拉斯维加斯', 'phoenix': '菲尼克斯',
    'philadelphia': '费城', 'detroit': '底特律', 'denver': '丹佛',
    'atlanta': '亚特兰大', 'charlotte': '夏洛特', 'nashville': '纳什维尔',
    'new orleans': '新奥尔良', 'minneapolis': '明尼阿波利斯',
    'salt lake city': '盐湖城', 'columbus': '哥伦布',
    'indianapolis': '印第安纳波利斯', 'cleveland': '克利夫兰',
    'pittsburgh': '匹兹堡', 'baltimore': '巴尔的摩', 'honolulu': '檀香山',
    'london': '伦敦', 'manchester': '曼彻斯特', 'birmingham': '伯明翰',
    'liverpool': '利物浦', 'leeds': '利兹', 'bristol': '布里斯托尔',
    'sheffield': '谢菲尔德', 'nottingham': '诺丁汉',
    'glasgow': '格拉斯哥', 'edinburgh': '爱丁堡', 'cardiff': '加的夫',
    'belfast': '贝尔法斯特', 'oxford': '牛津', 'cambridge': '剑桥',
    'paris': '巴黎', 'marseille': '马赛', 'lyon': '里昂',
    'toulouse': '图卢兹', 'nice': '尼斯', 'lille': '里尔',
    'bordeaux': '波尔多', 'nantes': '南特', 'strasbourg': '斯特拉斯堡',
    'berlin': '柏林', 'munich': '慕尼黑', 'muenchen': '慕尼黑',
    'hamburg': '汉堡', 'frankfurt': '法兰克福',
    'frankfurt am main': '法兰克福', 'cologne': '科隆', 'köln': '科隆',
    'düsseldorf': '杜塞尔多夫', 'dusseldorf': '杜塞尔多夫',
    'stuttgart': '斯图加特', 'dortmund': '多特蒙德',
    'leipzig': '莱比锡', 'dresden': '德累斯顿', 'nuremberg': '纽伦堡',
    'toronto': '多伦多', 'vancouver': '温哥华', 'montreal': '蒙特利尔',
    'montréal': '蒙特利尔', 'ottawa': '渥太华', 'calgary': '卡尔加里',
    'edmonton': '埃德蒙顿', 'quebec city': '魁北克市',
    'winnipeg': '温尼伯', 'halifax': '哈利法克斯',
    'sydney': '悉尼', 'melbourne': '墨尔本', 'brisbane': '布里斯班',
    'perth': '珀斯', 'adelaide': '阿德莱德', 'canberra': '堪培拉',
    'gold coast': '黄金海岸', 'hobart': '霍巴特', 'darwin': '达尔文',
    'tokyo': '东京', 'osaka': '大阪', 'kyoto': '京都',
    'yokohama': '横滨', 'nagoya': '名古屋', 'sapporo': '札幌',
    'fukuoka': '福冈', 'kobe': '神户', 'hiroshima': '广岛',
    'singapore': '新加坡', 'hong kong': '香港', 'macau': '澳门',
    'seoul': '首尔', 'busan': '釜山', 'incheon': '仁川',
    'amsterdam': '阿姆斯特丹', 'rotterdam': '鹿特丹',
    'the hague': '海牙', 'brussels': '布鲁塞尔',
    'zurich': '苏黎世', 'zürich': '苏黎世', 'geneva': '日内瓦',
    'vienna': '维也纳', 'prague': '布拉格', 'warsaw': '华沙',
    'rome': '罗马', 'milan': '米兰', 'naples': '那不勒斯',
    'madrid': '马德里', 'barcelona': '巴塞罗那', 'valencia': '瓦伦西亚',
    'lisbon': '里斯本', 'porto': '波尔图',
    'dubai': '迪拜', 'abu dhabi': '阿布扎比',
    'bangkok': '曼谷', 'chiang mai': '清迈',
    'kuala lumpur': '吉隆坡', 'jakarta': '雅加达',
    'ho chi minh city': '胡志明市', 'hanoi': '河内',
    'vientiane': '万象', 'phnom penh': '金边',
    'mumbai': '孟买', 'new delhi': '新德里', 'delhi': '德里',
    'beijing': '北京', 'shanghai': '上海', 'guangzhou': '广州',
    'shenzhen': '深圳', 'chengdu': '成都', 'hangzhou': '杭州',
    'wuhan': '武汉', 'chongqing': '重庆', 'nanjing': '南京',
    'taipei': '台北', 'kaohsiung': '高雄',
}

_QIZI_COUNTRY_ZH = {
    'us': '美国', 'usa': '美国', 'united states': '美国',
    'united states of america': '美国', 'ca': '加拿大', 'canada': '加拿大',
    'gb': '英国', 'uk': '英国', 'united kingdom': '英国',
    'great britain': '英国', 'au': '澳大利亚', 'australia': '澳大利亚',
    'de': '德国', 'germany': '德国', 'fr': '法国', 'france': '法国',
    'jp': '日本', 'japan': '日本', 'sg': '新加坡', 'singapore': '新加坡',
    'hk': '中国香港', 'hong kong': '中国香港',
    'cn': '中国', 'china': '中国', 'tw': '中国台湾', 'taiwan': '中国台湾',
    'kr': '韩国', 'south korea': '韩国', 'nl': '荷兰', 'netherlands': '荷兰',
    'ch': '瑞士', 'switzerland': '瑞士', 'it': '意大利', 'italy': '意大利',
    'es': '西班牙', 'spain': '西班牙', 'pt': '葡萄牙', 'portugal': '葡萄牙',
    'be': '比利时', 'belgium': '比利时', 'at': '奥地利', 'austria': '奥地利',
    'cz': '捷克', 'czechia': '捷克', 'pl': '波兰', 'poland': '波兰',
    'ae': '阿联酋', 'united arab emirates': '阿联酋',
    'th': '泰国', 'thailand': '泰国', 'my': '马来西亚', 'malaysia': '马来西亚',
    'id': '印度尼西亚', 'indonesia': '印度尼西亚',
    'vn': '越南', 'vietnam': '越南', 'la': '老挝', 'laos': '老挝',
    'kh': '柬埔寨', 'cambodia': '柬埔寨', 'in': '印度', 'india': '印度',
}


def _qizi_chinese_country(value):
    import re
    name = str(value or '').strip()
    # 兼容「US 美国」「US United States」等历史 country 数据。
    parts = re.match(r'^[A-Za-z]{2,3}\s+(.+)$', name)
    if parts:
        iso = name.split(None, 1)[0].lower()
        name = parts.group(1)
        if re.search(r'[\u4e00-\u9fff]', name):
            return name
        return _QIZI_COUNTRY_ZH.get(name.lower(), _QIZI_COUNTRY_ZH.get(iso, '未知国家'))
    return _QIZI_COUNTRY_ZH.get(name.lower(), name or '未知国家')


def _qizi_chinese_city(value):
    import re
    name = str(value or '').strip()
    if not name:
        return '城市未知'
    # MMDB 的 zh-CN 字段直接使用，不二次翻译。
    if re.search(r'[\u4e00-\u9fff]', name):
        return name
    key = ' '.join(name.casefold().replace('_', ' ').replace('-', ' ').split())
    # 不擅自把不认识的地名音译成错误城市，也不在中文模式混入英文。
    return _QIZI_CITY_ZH.get(key, name)  # 未翻译成功先显示真实英文，不冒充无城市


def qizi_exit_country_city_label(proxy):
    """面向右侧出口管理的中文标签，不修改真实路由数据。"""
    if not proxy:
        return '未绑定'
    country = _qizi_chinese_country(proxy.get('country'))
    public = str(proxy.get('public_ip') or '').strip()
    source = str(proxy.get('geo_source_ip') or '').strip()
    # 当动态出口变化而新 IP 尚未完成本地 MMDB 解析时，绝不显示旧 IP 城市。
    city = ('城市待识别' if not public or (source and source != public)
            else _qizi_chinese_city(proxy.get('city_zh') or proxy.get('city')))
    return f"{country} {city} {public or proxy.get('host') or '?'}"


class QiziReliabilityMixin:
    PRESENCE_LEASE_SECONDS = 100.0
    RECONNECT_LIMIT_SECONDS = 180.0
    HEALTH_BATCH_SIZE = 3

    def __init__(self, *args, **kwargs):
        # Set up BEFORE parent constructors: Tk may dynamically dispatch methods
        # during their initialization.
        self._qizi_presence_seen = {}
        self._qizi_probe_running = False
        self._qizi_probe_cursor = 0
        self._qizi_reconnect_at = {}
        self._qizi_reconnect_errors = {}
        self._qizi_reconnect_authorized = set()
        self._qizi_last_wifi = {}
        self._qizi_intent_file = None
        self._qizi_location_intent = {}
        self._qizi_syncing_live_ui = False
        self._qizi_exit_origin_pending = {}
        # Last accepted route point is distinct from the original planning origin.
        # 5-second bounded writes during playback; manual stop/exit writes immediately.
        self._qizi_route_coord_save_at = 0.0
        self._qizi_route_ack_index = {}
        self._qizi_route_hold = {}
        self._qizi_route_pending_points = {}
        # Each Wi-Fi worker generation may replay the previous second once only.
        self._qizi_route_replayed_sessions = set()
        # Live random-walk config is throttled independently per device.
        self._qizi_walk_config_written_at = {}
        self._qizi_stale_worker_warnings = set()
        # All route devices share ONE Tk timer. Each iPhone retains its own DVT
        # session (required by the iOS protocol); there is no per-device walk thread.
        self._qizi_route_schedule = {}
        self._qizi_route_scheduler_job = None
        self._qizi_route_ack_waiting = {}
        self._qizi_route_last_warning = {}

        super().__init__(*args, **kwargs)
        path = Path(getattr(self, '_qizi_live_positions_path')).parent
        from qizi_city_translation_engine import CityTranslationCache
        self._qizi_city_translation = CityTranslationCache(path / 'qizi_city_zh_cache.json')
        self.after(900, self._qizi_city_translation_pulse)
        self._qizi_intent_file = path / 'qizi_location_intent.json'
        self._qizi_location_intent = {
            str(k): bool(v) for k, v in load_json_dict(self._qizi_intent_file).items()
        }
        # Lower background resource use: at most 2 ordinary probes per sweep,
        # random intervals; dynamic-API checks remain separate.
        scheduler = getattr(self, 'exit_health_scheduler', None)
        if scheduler is not None:
            scheduler.normal_interval = 115.0
            scheduler.offline_interval = 75.0
            scheduler.batch_size = 2
            scheduler.start_spacing = 2.0
        self.after(1800, self._qizi_reliability_tick)
        self._qizi_route_scheduler_job = self.after(200, self._qizi_shared_route_pulse)
        self._qizi_restore_route_progress_from_state()
        self.log_operation('V3.10.18 综合稳定性修复已启用：游走/寻路实时同步、断点续飞、手动意图保护和轻量在线检测')

    def _qizi_restore_configured_coordinates(self):
        """启动时绝不把路线当前位置还原为旧的 IP/规划起点。

        旧版本 RouteUi 在某些匹配条件下会把坐标恢复成 original。
        路线状态的 last_lat/last_lon 才是飞行中/暂停后的坐标。
        """
        return None

    def _qizi_valid_route_point(self, lat, lon):
        try:
            lat, lon = float(lat), float(lon)
            if (math.isfinite(lat) and math.isfinite(lon)
                    and -90 <= lat <= 90 and -180 <= lon <= 180):
                return (lat, lon)
        except (TypeError, ValueError):
            pass
        return None

    def _qizi_recent_route_ack(self, uid, seconds=15.0):
        state = self.route_states.get(str(uid), {})
        try:
            return (state.get('last_confirmed_index') is not None
                    and 0 <= time.time() - float(state['last_confirmed_at']) < seconds)
        except (KeyError, TypeError, ValueError):
            return False

    def _qizi_route_position(self, uid, prefer_ack=False):
        """Position from state, never from saved planning origin.

        The last verified DVT ACK is preferred for STOP so an unsent queued
        future second cannot leap the phone forward. Fall back to last queued
        point when the device runtime has no ACK (USB/CLI compatibility mode).
        """
        uid = str(uid)
        state = self.route_states.get(uid, {})
        if prefer_ack and self._qizi_recent_route_ack(uid):
            point = self._qizi_valid_route_point(state.get('last_confirmed_lat'),
                                                 state.get('last_confirmed_lon'))
            if point:
                return point
        point = self._qizi_valid_route_point(state.get('last_lat'), state.get('last_lon'))
        if point:
            return point
        live = getattr(self, '_qizi_live_positions', {}).get(uid, {})
        if str(live.get('mode')) in ('route', 'route_hold'):
            return self._qizi_valid_route_point(live.get('lat'), live.get('lon'))
        checkpoint = getattr(self, '_qizi_route_positions', {}).get(uid, {})
        if state.get('running') or state.get('paused'):
            return self._qizi_valid_route_point(checkpoint.get('last_lat'),
                                                checkpoint.get('last_lon'))
        return None

    def _qizi_restore_route_progress_from_state(self):
        """Pull persisted active/paused coordinates into main config at startup."""
        changed = False
        for uid, state in list(self.route_states.items()):
            if not isinstance(state, dict) or not (state.get('running') or state.get('paused')):
                continue
            pos = self._qizi_route_position(uid)
            if pos:
                changed |= self._qizi_write_route_coordinate(uid, pos, save=False)
                if state.get('paused'):
                    self._qizi_route_hold[str(uid)] = pos
        if changed:
            self.save_ios()
        self._qizi_sync_panel_coordinates()

    def _qizi_write_route_coordinate(self, uid, pos, save=False):
        """Update main persistent config without changing immutable plan start."""
        uid = str(uid)
        pos = self._qizi_valid_route_point(*pos)
        if pos is None:
            return False
        old = self.coordinates.get(uid) or {}
        try:
            interval = max(1.0, min(60.0, float(old.get('interval') or 1)))
        except (TypeError, ValueError):
            interval = 1.0
        current = dict(lat=str(pos[0]), lon=str(pos[1]), interval=interval)
        changed = (str(old.get('lat')) != current['lat'] or
                   str(old.get('lon')) != current['lon'])
        if changed:
            self.coordinates[uid] = current
        if save and changed:
            self.save_ios()
        return changed

    def _qizi_persist_route_now(self, uid):
        """Force all three records on pause/close, avoiding 5s durability gap."""
        uid = str(uid)
        self.save_ios()
        try:
            self._qizi_save_checkpoint(uid, force=True)
        except Exception as exc:
            self.log_operation('路线检查点强制保存失败：'+str(exc)[:100])
        try:
            save_route_plans(self._qizi_live_positions_path, self._qizi_live_positions)
        except OSError as exc:
            self.log_operation('飞行实时坐标保存失败：'+str(exc)[:120])
        self._qizi_route_coord_save_at = time.monotonic()

    def _qizi_save_intent(self):
        if self._qizi_intent_file is not None:
            try:
                save_route_plans(self._qizi_intent_file, self._qizi_location_intent)
            except OSError as exc:
                self.log_operation('定位意图保存失败：'+str(exc)[:120])

    def _qizi_set_intent(self, uid, enabled):
        if not uid:
            return
        uid = str(uid)
        if self._qizi_location_intent.get(uid) is enabled:
            return
        self._qizi_location_intent[uid] = bool(enabled)
        self._qizi_save_intent()

    def _qizi_can_restore_location(self, ip, uid):
        if self._route_is_running(uid):
            return False
        flag = self._qizi_location_intent.get(str(uid))
        if flag is not None:
            return flag
        # Match original behavior for an already bound remote exit. Local/block
        # are NOT implicit commands to start a new GPS simulation.
        eid = self.devices.get(ip, {}).get('exit_id')
        return any(p.get('id') == eid for p in self.exits)

    def locate_row(self, ip):
        uid = str(self.uid_for_ip(ip) or '')
        state = self.ios_states.get(uid, {}) if uid else {}
        active = state.get('location') in ('定位中', '准备发送', '正在恢复')
        if (uid and state.get('wifi') == '连接中' and not active
                and not self._route_is_running(uid)
                and (self.route_states.get(uid, {}).get('paused')
                     or uid in self._qizi_route_hold)):
            # Restarting GPS while a route is PAUSED must maintain the same
            # coordinate, not launch the random-walk generator.
            pos = self._qizi_route_position(uid) or self._qizi_route_hold.get(uid)
            if pos and self.ios.send(uid, dict(op='hold', lat=pos[0], lon=pos[1], interval=1)):
                self._qizi_route_hold[uid] = pos
                self._qizi_set_intent(uid, True)
                self.ios_states.setdefault(uid, {})['location'] = '准备发送'
                self._qizi_write_route_coordinate(uid, pos, save=True)
                self.log_operation(f'{ip}：重新开启定位，在已暂停的飞行坐标持续保持（不随机游走）')
                self.refresh_devices(ip)
                return True
        result = super().locate_row(ip)
        if uid and state.get('wifi') == '连接中' and not self._route_is_running(uid):
            if active and state.get('location') == '正在停止':
                self._qizi_set_intent(uid, False)
            elif not active and state.get('location') == '准备发送':
                self._qizi_set_intent(uid, True)
        return result

    def clear_row(self, ip):
        uid = str(self.uid_for_ip(ip) or '')
        if uid and self._route_is_running(uid):
            # Restore-real-location is an explicit operator override. Pause the
            # route before CLEAR, otherwise the next route tick would set the
            # simulated coordinate again one second later.
            self._stop_route(uid, manual=True)
        if uid:
            # Stop-route can turn the hold intent back on. CLEAR is the last
            # manual instruction and must always take precedence.
            self._qizi_set_intent(uid, False)
        return super().clear_row(ip)

    def _maybe_auto_start_location(self, ip, uid=None, reason='Wi-Fi 已连接'):
        uid = str(uid or self.uid_for_ip(ip) or '')
        if not uid or not self._qizi_can_restore_location(ip, uid):
            if uid and self._route_is_running(uid):
                # The route owns the simulation. Never briefly start an older
                # walk coordinate between reconnect and route resumption.
                self.after(100, self._resume_saved_routes)
            return False
        state = self.ios_states.setdefault(uid, {})
        if state.get('wifi') != '连接中' or state.get('location') in (
            '定位中', '准备发送', '正在恢复', '正在清除', '正在停止'):
            return False
        coord = self._configured_coordinate(ip, uid)
        if coord is None:
            return False
        paused = self.route_states.get(uid, {}).get('paused')
        hold_point = self._qizi_route_position(uid) if paused else None
        if paused and hold_point is not None:
            coord = dict(lat=str(hold_point[0]), lon=str(hold_point[1]), interval=1)
        op = 'hold' if paused and hold_point is not None else 'start'
        if self.ios.send(uid, dict(op=op, **coord)):
            state.update(location='准备发送', error='')
            self._qizi_set_intent(uid, True)
            self.log_operation(f'{ip}：{reason}，从最近记录坐标恢复定位：{coord["lat"]}, {coord["lon"]}')
            self.refresh_devices(ip)
            return True
        return False

    def _qizi_apply_exit_coordinates(self, ip, eid, reason='出口切换'):
        """Switch the active GPS target immediately without stopping the Tunnel.

        Local/block preserve location exactly. A new named exit with a VERIFIED
        GeoIP source is a deliberate override of walk origin; a route stays owner.
        """
        if not ip or ip not in self.devices or self.devices[ip].get('exit_id') != eid:
            return False
        proxy = next((p for p in self.exits if p.get('id') == eid), None)
        if proxy is None:
            return False  # Local / Block only changes network policy.
        uid = str(self.uid_for_ip(ip) or '')
        if uid and (self._route_is_running(uid)
                    or self.route_states.get(uid, {}).get('paused')
                    or uid in self._qizi_route_hold):
            # A background GeoIP callback can arrive long after manual pause.
            # It must never overwrite the actual stopped flight position.
            self.log_operation(f'{ip}：已保留寻路当前位置；出口定位不覆盖运行/暂停中的航线坐标')
            return False
        source_ip = str(proxy.get('public_ip') or '')
        if not source_ip or str(proxy.get('geo_source_ip') or '') != source_ip:
            self.log_operation(f'{ip}：新出口公网IP坐标待核实，暂不使用旧IP定位')
            return False
        try:
            lat = float(proxy['geo_lat'])
            lon = float(proxy['geo_lon'])
            if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90
                    and -180 <= lon <= 180):
                raise ValueError('无效地理坐标')
        except (KeyError, ValueError, TypeError):
            return False
        lat_text, lon_text = str(proxy['geo_lat']), str(proxy['geo_lon'])
        key = uid or 'ip:' + ip
        previous = self.coordinates.get(key, {})
        try:
            interval = max(1, min(60, float(previous.get('interval') or 1)))
        except (TypeError, ValueError):
            interval = 1
        self.coordinates[key] = {'lat': lat_text, 'lon': lon_text, 'interval': interval}
        if uid:
            self.coordinates.pop('ip:' + ip, None)
        self.save_ios()
        row = self.row_widgets.get(ip)
        if row:
            row['lat'].set(lat_text)
            row['lon'].set(lon_text)
            row['key'] = key
        self.devices[ip]['qizi_last_auto_exit'] = eid
        self.devices[ip]['qizi_last_auto_lat'] = lat_text
        self.devices[ip]['qizi_last_auto_lon'] = lon_text
        self._qizi_save_devices(self.devices)
        state = self.ios_states.get(uid, {}) if uid else {}
        active = (state.get('wifi') == '连接中' and state.get('location') in (
            '定位中', '准备发送', '正在恢复'))
        if active and self.ios.send(uid, {'op': 'start', 'lat': lat_text,
                                          'lon': lon_text, 'interval': interval}):
            self._qizi_set_intent(uid, True)
            # Previous walk events can still be queued. An explicit new origin
            # should be authoritative until the next walk ACK arrives.
            self._qizi_store_live_coordinate(uid, lat_text, lon_text, 'walk', False)
            self._qizi_exit_origin_pending[uid] = (time.monotonic(), lat, lon)
            state['location'] = '准备发送'
            self.log_operation(f'{ip}：{reason}，新出口已立即下发定位 {lat_text}, {lon_text}（Wi-Fi未断开）')
        else:
            self.log_operation(f'{ip}：{reason}，坐标已更新 {lat_text}, {lon_text}；定位未运行，未自动发送')
        return True

    def bind_row_exit(self, ip):
        before = self.devices.get(ip, {}).get('exit_id')
        uid = self.uid_for_ip(ip)
        state = self.ios_states.get(uid, {}) if uid else {}
        was_active = state.get('wifi') == '连接中' and state.get('location') in (
            '定位中', '准备发送', '正在恢复')
        result = super().bind_row_exit(ip)
        after = self.devices.get(ip, {}).get('exit_id')
        if before != after and after is not None and was_active:
            # Do not let Local/Block mode clear location intent, even though
            # the original LAN policy doesn't require location to allow traffic.
            self._qizi_set_intent(uid, True)
            if not any(p.get('id') == after for p in self.exits):
                self.log_operation(f'{ip}：仅切换网络出口，Wi-Fi/信任/当前定位继续保持')
        return result

    def _qizi_track_device_coordinate(self, uid, data):
        # An old walk ACK can arrive just after switching to a new exit. Do
        # not let that delayed event override the new explicit starting point.
        pending = self._qizi_exit_origin_pending.get(str(uid))
        if pending and data.get('movement') == 'walk':
            started, lat, lon = pending
            if time.monotonic() - started < 15:
                try:
                    from qizi_route_engine import haversine_m
                    distance = haversine_m((float(data['lat']), float(data['lon'])), (lat, lon))
                except (ValueError, TypeError, KeyError):
                    distance = float('inf')
                if distance > 65:
                    return None
                self._qizi_exit_origin_pending.pop(str(uid), None)
            else:
                self._qizi_exit_origin_pending.pop(str(uid), None)
        uid = str(uid)
        movement = data.get('movement')
        # Ignore stale random-walk events after the route has taken ownership.
        if movement == 'walk' and (self._route_is_running(uid)
                                   or self.route_states.get(uid, {}).get('paused')
                                   or uid in self._qizi_route_hold
                                   or self._qizi_location_intent.get(uid) is False
                                   or self.ios_states.get(uid, {}).get('location')
                                   in ('正在停止', '已停止', '正在清除', '已清除模拟')):
            return None
        point = self._qizi_valid_route_point(data.get('lat'), data.get('lon'))
        route_state = self.route_states.get(uid, {})
        if movement == 'route' and point is not None:
            # A delayed ACK from an earlier flight second must not unfreeze a
            # manually stopped flight. Keep exactly the selected hold position.
            hold = self._qizi_route_hold.get(uid)
            if hold is not None and not self._route_is_running(uid):
                if point != hold:
                    return None
            if data.get('confirmed') and self._route_is_running(uid):
                history = self._qizi_route_pending_points.get(uid, {})
                # Last-wins workers coalesce second-by-second updates. Match
                # surviving ACK coordinates instead of assuming every step ACKed.
                index = history.get((round(point[0], 10), round(point[1], 10)))
                old_index = int(route_state.get('last_confirmed_index', 0) or 0)
                if index is not None and index >= old_index:
                    route_state.update(last_confirmed_index=index,
                                       last_confirmed_lat=point[0],
                                       last_confirmed_lon=point[1],
                                       last_confirmed_at=time.time())
                    self._qizi_route_ack_index[uid] = index
        result = super()._qizi_track_device_coordinate(uid, data)
        current_state = self.ios_states.get(uid, {})
        if movement == 'walk' and point is not None:
            # The live-positions JSON alone is not enough: the main coordinate
            # configuration must also point at the last sent wandering point.
            self._qizi_set_intent(uid, True)
            changed = self._qizi_write_route_coordinate(uid, point, save=False)
            now = time.monotonic()
            if changed and now - self._qizi_walk_config_written_at.get(uid, -1e12) >= 4.0:
                self.save_ios()
                self._qizi_walk_config_written_at[uid] = now
        # Do not force a 1Hz full UI rebuild. Only update the two small Tk vars.
        self._qizi_sync_panel_coordinates(uid)
        return result

    def _schedule_route_tick(self, uid, delay=1000):
        """Coalesce all device playback timers into one cooperative Tk loop."""
        uid = str(uid)
        runtime = self.route_runtime.setdefault(uid, {})
        old = runtime.get('job')
        if old and old != 'qizi-shared-route':
            try: self.after_cancel(old)
            except Exception: pass
        self._qizi_route_schedule[uid] = time.monotonic() + max(0.001, int(delay)/1000.0)
        runtime['job'] = 'qizi-shared-route'  # truthy: no spurious resume/restart

    def _qizi_shared_route_pulse(self):
        if getattr(self, '_closing', False):
            return
        now = time.monotonic()
        for uid, due in list(self._qizi_route_schedule.items()):
            if due > now:
                continue
            self._qizi_route_schedule.pop(uid, None)
            runtime = self.route_runtime.get(uid, {})
            if runtime.get('job') != 'qizi-shared-route':
                continue  # manual stop/pause superseded this task
            runtime['job'] = None
            if self.route_states.get(uid, {}).get('running'):
                try:
                    self._route_tick(uid)
                except Exception as exc:
                    state = self.route_states.get(uid, {})
                    state['waiting_reason'] = '统一调度异常：'+str(exc)[:160]
                    self.log_operation(f'{uid[-8:]}：{state["waiting_reason"]}')
                    self._schedule_route_tick(uid, 2000)
        self._qizi_route_scheduler_job = self.after(200, self._qizi_shared_route_pulse)

    def _route_tick(self, uid):
        """Never finish or advance Wi-Fi flight merely because a command entered a queue."""
        uid = str(uid)
        st = self.route_states.get(uid, {})
        if st.get('running') and st.get('mode', 'WiFi模式') != 'USB数据线模式':
            index = int(st.get('next_index', 0) or 0)
            confirmed = int(st.get('last_confirmed_index', 0) or 0)
            if index > 0 and confirmed < index:
                # After restart, the old confirmation may be missing, so
                # resend the LAST unacknowledged point rather than advancing.
                now = time.monotonic()
                pending = self._qizi_route_ack_waiting.get(uid)
                if not pending or pending[0] != index:
                    self._qizi_route_ack_waiting[uid] = (index, now)
                    pending = (index, now)
                elapsed = now - pending[1]
                if elapsed >= 8.0:
                    point = self._qizi_valid_route_point(st.get('last_lat'), st.get('last_lon'))
                    if point and self.ios_states.get(uid, {}).get('wifi') == '连接中':
                        self.ios.send(uid, dict(op='route', lat=point[0], lon=point[1], interval=1))
                    self._qizi_route_ack_waiting[uid] = (index, now)
                    if now - self._qizi_route_last_warning.get(uid, -1e12) > 15:
                        self.log_operation(f'{st.get("ip") or uid}：航线坐标 {index} 尚无设备DVT成功回执，'
                                           '保持禁网并重发该点，不再假装完成寻路')
                        self._qizi_route_last_warning[uid] = now
                self._schedule_route_tick(uid, 300)
                return
            self._qizi_route_ack_waiting.pop(uid, None)
        return super()._route_tick(uid)

    def _start_route(self, ip, uid, mode, status_var=None, resume=False, target_exit_id=None):
        # Replay the last queued second after a restart/reconnect. A command
        # may have been queued but not ACKed when the Wi-Fi link disappeared.
        # One duplicated position is safer than silently skipping that point.
        uid = str(uid)
        if not resume:
            # A brand-new route must never inherit an ACK from an older flight.
            self._qizi_route_pending_points.pop(uid, None)
            self._qizi_route_ack_waiting.pop(uid, None)
            state = self.route_states.get(uid, {})
            for key in ('last_confirmed_index', 'last_confirmed_at',
                        'last_confirmed_lat', 'last_confirmed_lon'):
                state.pop(key, None)
        if resume:
            state = self.route_states.get(uid, {})
            generation = self.ios_states.get(uid, {}).get('generation', 0)
            # Per Wi-Fi Tunnel generation, replay at most one queued but
            # unacknowledged coordinate. Repeated recovery callbacks must NOT
            # keep decrementing next_index (which could rewind the whole flight).
            marker = (uid, generation)
            if state.get('running') and marker not in self._qizi_route_replayed_sessions:
                self._qizi_route_replayed_sessions.add(marker)
                index = int(state.get('next_index', 0) or 0)
                if index > 0:
                    state['next_index'] = index - 1
                    # Previous DVT generation's ACK must not satisfy a new
                    # session's final-arrival confirmation.
                    old_ack = int(state.get('last_confirmed_index', 0) or 0)
                    state['last_confirmed_index'] = min(old_ack, index - 1)
                    state['last_confirmed_at'] = 0
                    self._save_route_state()
        return super()._start_route(ip, uid, mode, status_var,
                                    resume=resume, target_exit_id=target_exit_id)

    def _apply_route_coordinate(self, ip, uid, lat, lon):
        result = super()._apply_route_coordinate(ip, uid, lat, lon)
        uid = str(uid)
        point = self._qizi_valid_route_point(lat, lon)
        state = self.route_states.get(uid)
        if point is None:
            return result
        if isinstance(state, dict) and state.get('running'):
            # _route_tick invokes this hook before advancing next_index.
            index = int(state.get('next_index', 0) or 0) + 1
            state['last_lat'], state['last_lon'] = point
            pending = self._qizi_route_pending_points.setdefault(uid, {})
            pending[(round(point[0], 10), round(point[1], 10))] = index
            # Bound the tiny ACK lookup so multi-day flights cannot leak RAM.
            if len(pending) > 60:
                for old in list(pending)[:len(pending)-40]:
                    pending.pop(old, None)
        changed = self._qizi_write_route_coordinate(uid, point)
        now = time.monotonic()
        if changed and now - self._qizi_route_coord_save_at >= 4.0:
            self.save_ios()  # disk throttled; final step is forced at stop/exit
            self._qizi_route_coord_save_at = now
        self._qizi_sync_panel_coordinates(uid)
        return result

    def _qizi_on_new_route_plan(self, uid):
        result = super()._qizi_on_new_route_plan(uid)
        self._qizi_route_hold.pop(str(uid), None)
        self._qizi_route_pending_points.pop(str(uid), None)
        return result

    def _qizi_hold_route_after_stop(self, uid, coord, manual):
        """Called by base route stop in place of random-walk start."""
        uid = str(uid)
        pos = self._qizi_route_position(uid, prefer_ack=False)
        if pos is None:
            pos = self._qizi_valid_route_point(coord.get('lat'), coord.get('lon'))
        if pos is None:
            return False
        self._qizi_route_hold[uid] = pos
        self._qizi_write_route_coordinate(uid, pos, save=False)
        self._qizi_store_live_coordinate(uid, pos[0], pos[1], 'route_hold', False)
        self._qizi_set_intent(uid, True)
        self._qizi_persist_route_now(uid)
        # hold op is a stationary repeated DVT set, not the 'start' operation
        # which resets the random-walk generator every time it is called.
        sent = self.ios.send(uid, dict(op='hold', lat=pos[0], lon=pos[1], interval=1))
        if sent:
            self.ios_states.setdefault(uid, {})['location'] = '准备发送'
            self.log_operation(f'{self._ip_for_uid(uid)}：寻路结束冻结坐标 '
                               f'{pos[0]:.10f}, {pos[1]:.10f}（不进行随机游走）')
        else:
            self.log_operation(f'{self._ip_for_uid(uid)}：冻结坐标已保存，定位命令待Wi-Fi恢复')
        self._qizi_sync_panel_coordinates(uid)
        return True  # do not fall back to random walk if hold send fails

    def _stop_route(self, uid, manual=True, status_var=None):
        """Freeze the last ACKed route location before the base stop."""
        uid = str(uid)
        state = self.route_states.get(uid)
        if isinstance(state, dict) and state.get('running'):
            # Pausing means keep the current virtual GPS coordinate, even if
            # the Wi-Fi tunnel happened to be down at the time of the click.
            self._qizi_set_intent(uid, True)
            pos = self._qizi_route_position(uid, prefer_ack=True)
            if pos:
                state['last_lat'], state['last_lon'] = pos
                confirmed_idx = state.get('last_confirmed_index')
                if confirmed_idx is not None and self._qizi_recent_route_ack(uid):
                    state['next_index'] = min(int(state.get('next_index', 0)),
                                              int(confirmed_idx))
                self._qizi_write_route_coordinate(uid, pos, save=False)
                self._qizi_store_live_coordinate(uid, *pos, 'route_hold', False)
                self._qizi_persist_route_now(uid)
        result = super()._stop_route(uid, manual=manual, status_var=status_var)
        # The paused state is recreated by RouteUi after base stop.
        stopped = self.route_states.get(uid, {})
        if isinstance(stopped, dict) and stopped.get('paused'):
            pos = self._qizi_route_position(uid)
            if pos:
                self._qizi_route_hold[uid] = pos
                self._qizi_write_route_coordinate(uid, pos, save=False)
                self._qizi_persist_route_now(uid)
                self._qizi_sync_panel_coordinates(uid)
        return result

    def open_route_dialog(self, ip):
        uid = str(self.uid_for_ip(ip) or '')
        pos = self._qizi_route_position(uid) if uid else None
        if pos and uid:
            self._qizi_write_route_coordinate(uid, pos, save=False)
            self._qizi_sync_panel_coordinates(uid)
        result = super().open_route_dialog(ip)
        if pos and uid:
            self._qizi_sync_panel_coordinates(uid)
        return result

    def _qizi_sync_panel_coordinates(self, only_uid=None):
        if self._qizi_syncing_live_ui or not hasattr(self, 'row_widgets'):
            return
        self._qizi_syncing_live_ui = True
        try:
            for ip, row in list(self.row_widgets.items()):
                uid = str(self.uid_for_ip(ip) or '')
                if not uid or (only_uid is not None and uid != str(only_uid)):
                    continue
                state = self.route_states.get(uid, {})
                running = bool(state.get('running'))
                device_state = self.ios_states.get(uid, {})
                active = device_state.get('location') in ('定位中','准备发送','正在恢复')
                recorded = (getattr(self, '_qizi_live_positions', {}) or {}).get(uid, {})
                if running or state.get('paused') or uid in getattr(self, '_qizi_route_hold', {}):
                    pos = self._qizi_route_position(uid)
                    if pos is None:
                        continue
                    lat, lon = pos
                elif str(recorded.get('mode')) == 'walk' and (active or (
                        device_state.get('wifi') != '连接中' and
                        self._qizi_location_intent.get(uid) is True)):
                    lat, lon = recorded.get('lat'), recorded.get('lon')
                else:
                    continue
                try:
                    latitude, longitude = float(lat), float(lon)
                    if not (math.isfinite(latitude) and math.isfinite(longitude)
                            and -90 <= latitude <= 90 and -180 <= longitude <= 180):
                        continue
                except (TypeError, ValueError):
                    continue
                if not running:
                    # Don't clobber deliberate user edits; these will be applied
                    # explicitly when a new route is planned or manual GPS starts.
                    focused = self.focus_get()
                    if any(entry == focused for entry in row.get('entries', ())):
                        continue
                new_lat, new_lon = str(lat), str(lon)
                if row['lat'].get() != new_lat:
                    row['lat'].set(new_lat)
                if row['lon'].get() != new_lon:
                    row['lon'].set(new_lon)
                win = getattr(self, 'route_dialogs', {}).get(uid)
                if win is not None:
                    try:
                        if win.winfo_exists():
                            fields = getattr(win, 'qizi_route_values', None)
                            if fields is not None:
                                # Do not overwrite the operator typing a new
                                # planned origin while an earlier route is paused.
                                if not running:
                                    body = win.winfo_children()[0]
                                    if self.focus_get() in (
                                            *body.grid_slaves(row=0, column=1),
                                            *body.grid_slaves(row=1, column=1)):
                                        continue
                                fields['start_lat'].set(new_lat)
                                fields['start_lon'].set(new_lon)
                    except (AttributeError, tk.TclError):
                        pass
        finally:
            self._qizi_syncing_live_ui = False

    def refresh_devices(self, selected_ip=None):
        # Legacy scans and ARP caches are NOT fresh proof; leases and live
        # application sessions are. Never persist online as a durable fact.
        if hasattr(self, 'devices') and hasattr(self, '_startup_saved_links'):
            now = time.monotonic()
            for ip, device in self.devices.items():
                uid = self.uid_for_ip(ip)
                worker = self.ios.workers.get(uid) if uid else None
                # A cached worker is not itself liveness proof. Its last
                # emitted heartbeat must be fresh and it must not be stopping.
                connected = bool(worker and not worker.get('stopping')
                                 and self.ios_states.get(uid, {}).get('wifi') == '连接中'
                                 and now - float(worker.get('last', -1e12)) < 15.0)
                recently_seen = now - self._qizi_presence_seen.get(ip, -1e12) < self.PRESENCE_LEASE_SECONDS
                device['online'] = bool(connected or recently_seen)
        result = super().refresh_devices(selected_ip)
        for ip, row in list(getattr(self, 'row_widgets', {}).items()):
            if self.devices.get(ip, {}).get('online'):
                continue
            uid = str(self.uid_for_ip(ip) or '')
            state = self.ios_states.get(uid, {})
            # Red = known Wi-Fi control link has actually disconnected;
            # gray = insufficient recent evidence. Do not call unknown offline.
            truly_disconnected = bool(uid in self._qizi_reconnect_authorized
                                      and state.get('wifi') == '已断开')
            label = row.get('ip')
            if label is not None:
                label.configure(fg=self.RED if truly_disconnected else self.GRAY)
        self._qizi_sync_panel_coordinates()
        return result

    def log_operation(self, text, *args, **kwargs):
        # The old ARP-based UI conflates an expired lease with definite
        # physical offline. State the uncertainty explicitly in the log.
        text = str(text).replace('：设备已离线', '：设备在线证据已超时（待确认）')
        return super().log_operation(text, *args, **kwargs)

    def record_diagnostic(self, source, message):
        # Called by the original event consumer on this Tk thread for real
        # incoming proxy traffic. Counting it as a lease does not add I/O.
        if source in getattr(self, 'devices', {}):
            self._qizi_presence_seen[str(source)] = time.monotonic()
        return super().record_diagnostic(source, message)

    def _qizi_city_translation_pulse(self):
        if getattr(self, '_closing', False): return
        translator = getattr(self, '_qizi_city_translation', None)
        if translator is not None and translator.drain():
            self.refresh_exits()
            self.refresh_devices()
        self.after(900, self._qizi_city_translation_pulse)

    def exit_label(self, proxy):
        # 不为翻译阻塞 Tk，也不向翻译接口提供任何出口IP或代理账号。
        if not proxy: return qizi_exit_country_city_label(proxy)
        city = str(proxy.get('city') or '').strip()
        english = _qizi_chinese_city(city)
        translator = getattr(self, '_qizi_city_translation', None)
        if city and english == city and translator is not None:
            translated = translator.get(city, proxy.get('country') or '')
            if translated != city:
                show = dict(proxy, city_zh=translated)
                return qizi_exit_country_city_label(show)
        return qizi_exit_country_city_label(proxy)

    def build_ui(self):
        result = super().build_ui()
        try:
            # 增加城市后适当拓宽右侧，保留原来的单列、状态图标和排序。
            self.exit_table.master.master.configure(width=278)
            self.exit_table.heading('name', text='数量  国家 + 城市 + IP')
            self.exit_table.column('name', width=260, minwidth=190)
            topbar = getattr(self, '_qizi_right_topbar', None)
            if topbar is not None:
                topbar.configure(width=278)
        except (AttributeError, tk.TclError) as exc:
            if hasattr(self, 'log_operation'):
                self.log_operation('出口中文城市界面调整失败：' + str(exc)[:120])
        return result

    def _qizi_refresh_usage(self):
        # After the dynamic mixin's occupancy/sorting work, decorate the only
        # visible Treeview column with a verified-health symbol.
        result = super()._qizi_refresh_usage()
        if not hasattr(self, 'exit_table'):
            return result
        for index, proxy in enumerate(getattr(self, 'exits', [])):
            iid = str(index)
            if not self.exit_table.exists(iid):
                continue
            values = list(self.exit_table.item(iid, 'values'))
            if not values:
                continue
            status = str(proxy.get('status') or '')
            indicator = '√' if self._exit_is_online(proxy) else ('X' if status.startswith(('断线', '连接失败')) else '·')
            # The parent created a complete label, including count / refresh
            # marker, so keep it; remove just an older indicator on updates.
            import re
            base = re.sub(r'^[√X·]\s*', '', str(values[0]))
            values[0] = f'{indicator} {base}'
            self.exit_table.item(iid, values=tuple(values))
        return result

    def _qizi_probe_devices_background(self, ips):
        # A TCP connect success is positive proof only. Failure is NOT proof
        # of offline: port 62078 can be closed while Wi-Fi is still usable.
        try:
            for ip in ips:
                try:
                    with socket.create_connection((ip, 62078), timeout=0.4):
                        self._qizi_device_probe_events.put(ip)
                except (OSError, TimeoutError):
                    pass
        finally:
            self._qizi_probe_running = False

    def _qizi_reliability_tick(self):
        if getattr(self, '_closing', False):
            return
        now = time.monotonic()
        if not hasattr(self, '_qizi_device_probe_events'):
            import queue
            self._qizi_device_probe_events = queue.Queue()
        while True:
            try:
                ip = self._qizi_device_probe_events.get_nowait()
            except Exception:
                break
            if ip in self.devices:
                self._qizi_presence_seen[ip] = now
        # Limit to one small group and one background worker per randomized
        # round. No full network sweep and no synchronous socket on GUI thread.
        if not self._qizi_probe_running:
            pool = list(self.devices)
            if pool:
                count = min(len(pool), self.HEALTH_BATCH_SIZE)
                start = self._qizi_probe_cursor % len(pool)
                candidates = [pool[(start+i) % len(pool)] for i in range(count)]
                self._qizi_probe_cursor = start + count
                chosen = []
                for ip in candidates:
                    uid = self.uid_for_ip(ip)
                    if uid in getattr(self.ios, 'workers', {}):
                        continue  # connected worker already provides liveness
                    try:
                        if ipaddress.ip_address(ip).version == 4:
                            chosen.append(ip)
                    except ValueError:
                        continue
                if chosen:
                    self._qizi_probe_running = True
                    threading.Thread(target=self._qizi_probe_devices_background,
                        args=(chosen,), name='qizi-presence-light', daemon=True).start()
        self._qizi_recover_stalled_workers(now)
        self._qizi_reconnect_maintenance(now)
        self.refresh_devices()
        self.after(int(random.uniform(17000, 29000)), self._qizi_reliability_tick)

    def _qizi_recover_stalled_workers(self, now):
        """A real worker heartbeat should arrive every second; recover a stale
        helper without restarting this app or touching other iPhone tunnels."""
        if not getattr(self, 'ios_ready', False):
            return
        for uid, worker in list(getattr(self.ios, 'workers', {}).items()):
            if worker.get('stopping'):
                continue
            if uid in getattr(self, '_qizi_wifi_manual_off', set()):
                continue
            if self.ios_states.get(uid, {}).get('wifi') != '连接中':
                continue
            if now - float(worker.get('last', now)) <= 42.0:
                self._qizi_stale_worker_warnings.discard(uid)
                continue
            if uid not in self._qizi_stale_worker_warnings:
                self._qizi_stale_worker_warnings.add(uid)
                self.log_operation(f'{self._ip_for_uid(uid)}：Wi-Fi 会话超过42秒没有心跳，回收本设备会话并尝试自动恢复')
            self.ios.disconnect(uid)

    def _qizi_reconnect_maintenance(self, now):
        if not getattr(self, 'ios_ready', False):
            return
        if hasattr(self, '_license_valid') and not self._license_valid():
            return
        discovered = getattr(self, 'discovered', {}) or {}
        manual_off = getattr(self, '_qizi_wifi_manual_off', set())
        wanted = set(getattr(self, '_auto_restore_uids', set())) | self._qizi_reconnect_authorized
        disconnected = []
        for uid in wanted:
            uid = str(uid)
            if uid in manual_off:
                continue
            ip = self._ip_for_uid(uid)
            if ip not in self.devices:
                continue
            state = self.ios_states.get(uid, {})
            wifi = state.get('wifi')
            # A live helper is still building or draining; never start a second.
            if uid in self.ios.workers or wifi in ('建立中', '连接中', '断开中'):
                continue
            if state.get('developer_ready') is False or state.get('remote_pair_ready') is False:
                continue
            disconnected.append((uid, ip))
        if not disconnected:
            return
        if now - float(getattr(self, 'discovery_at', 0)) > 32:
            # Discovery is needed even when the user didn't tick auto-scan.
            # Run at most once per 32 seconds regardless of device count.
            last_scan = getattr(self, '_qizi_reliable_last_browse', -1e12)
            if now - last_scan >= 32:
                self._qizi_reliable_last_browse = now
                self.ios.scan()
                self.ios.identity_scan()
        for uid, ip in disconnected:
            if now < self._qizi_reconnect_at.get(uid, 0):
                continue
            data = discovered.get(uid)
            if not data or now - float(getattr(self, 'discovery_at', 0)) > 120:
                self._qizi_reconnect_at[uid] = now + random.uniform(12, 20)
                continue
            if self.discovery_ip(uid, data) != ip:
                # IP might have changed after router DHCP renewal. The normal
                # MAC migration is kept intact; refresh it sparingly.
                if now - getattr(self, '_qizi_last_migration_scan', -1e12) >= 90:
                    self._qizi_last_migration_scan = now
                    self.scan_devices()
                self._qizi_reconnect_at[uid] = now + 20
                continue
            if uid not in self._qizi_reconnect_authorized and not self._startup_pair_matches(ip, uid):
                continue
            failures = self._qizi_reconnect_errors.get(uid, 0) + 1
            self._qizi_reconnect_errors[uid] = failures
            delay = min(self.RECONNECT_LIMIT_SECONDS, 12 * (2 ** min(4, failures - 1)))
            self._qizi_reconnect_at[uid] = now + delay + random.uniform(0, 9)
            generation = self.ios.connect(uid)
            if generation:
                self.ios_states.setdefault(uid, {}).update(
                    generation=generation, wifi='建立中', location='未开启', error='')
                self.log_operation(f'{ip}：Wi-Fi自动重连第{failures}次，正在恢复Tunnel（失败自动退避）')

    def poll_ios(self):
        # Let the original consumer process all events and invoke
        # _maybe_auto_start_location on the new, verified Wi-Fi session.
        result = super().poll_ios()
        for uid, state in list(getattr(self, 'ios_states', {}).items()):
            wifi = state.get('wifi')
            old = self._qizi_last_wifi.get(uid)
            if wifi == '连接中':
                self._qizi_reconnect_authorized.add(str(uid))
                self._qizi_reconnect_errors.pop(str(uid), None)
                self._qizi_reconnect_at.pop(str(uid), None)
                if old != '连接中':
                    # Restart at checkpoint, not a newly generated starting point.
                    if self._route_is_running(uid):
                        self.after(100, self._resume_saved_routes)
                    self._qizi_sync_panel_coordinates(uid)
            elif old == '连接中' and wifi in ('已断开', '不可连接'):
                self._qizi_reconnect_at[str(uid)] = time.monotonic() + random.uniform(3, 7)
            self._qizi_last_wifi[str(uid)] = wifi
        return result

    def commit_row(self, ip, quiet=False, auto_start=False):
        # Route/active walk display fields are readouts, not user-edited
        # coordinates. On close, preserve original config and live checkpoint.
        uid = self.uid_for_ip(ip)
        if quiet and uid and self._route_is_running(uid):
            return True
        if quiet and uid and self.ios_states.get(uid, {}).get('location') == '定位中':
            return True
        return super().commit_row(ip, quiet=quiet, auto_start=auto_start)

    def delete_device(self):
        ip = getattr(self, 'selected_ip', None)
        uid = self.uid_for_ip(ip) if ip else None
        result = super().delete_device()
        if uid and ip not in self.devices:
            self._qizi_location_intent.pop(str(uid), None)
            self._qizi_reconnect_authorized.discard(str(uid))
            self._qizi_reconnect_at.pop(str(uid), None)
            self._qizi_presence_seen.pop(str(ip), None)
            self._qizi_save_intent()
        return result

    def close(self):
        self._qizi_save_intent()
        # Save active playback index before the base shuts down callbacks and
        # destroys the Tk context. It will be resumed by _resume_saved_routes.
        for uid, state in list(getattr(self, 'route_states', {}).items()):
            if state.get('running') or state.get('paused'):
                pos = self._qizi_route_position(uid)
                if pos:
                    self._qizi_write_route_coordinate(uid, pos, save=False)
                self._qizi_persist_route_now(uid)
        return super().close()
