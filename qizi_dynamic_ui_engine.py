# -*- coding: utf-8 -*-
"""V3.10.15 动态IP自动寻路：检测链路隔离、执行确认、出口拦截与显式日志。

通过 Mixin 加载到现有 GUI，不改写 iOS/Wi-Fi/授权/寻路底层。
"""
import json
import os
import math
import random
import queue
import threading
import time
import uuid
import tkinter as tk
from tkinter import ttk, messagebox

from qizi_dynamic_ip_engine import (normalise_api_config, parse_response,
                                    fetch_json, safe_status)
from qizi_geo_engine import exit_coordinates

POLL_MIN_SECONDS = 10
POLL_MAX_SECONDS = 20
# 普通健康检查维持原有频率，不随API轮询频率改变。
HEALTH_MIN_SECONDS = 75
HEALTH_MAX_SECONDS = 105
# 保留用户"立即检查"操作（_qizi_api_next[eid] = 0），抵达后也可立即强制复核。
AUTO_ROUTE_RETRY_SECONDS = 60
MAX_CONCURRENT_CHECKS = 4
# 本版每次动态 IP 变更都规划路线（即使同一城市）。只有确切同点无需移动。
DYNAMIC_FORCE_ROUTE_ON_CHANGE = True
# 到达后复核：新终点相距20米以内当作同点，避免无限极小路线。
DYNAMIC_NEARBY_KM = 0.02
# 最后一次游走/路线发送记录的最大允许年龄；无新记录则等待，不拿旧出口MMDB冒充起点。
DYNAMIC_MAX_POSITION_AGE_SECONDS = 120.0
DYNAMIC_MODULE_VERSION = "3.10.22"


class QiziDynamicMixin:
    def __init__(self, *args, **kwargs):
        self._qizi_api_results = queue.Queue()
        self._qizi_api_running = set()
        self._qizi_api_next = {}
        self._qizi_egress_probe_next = {}
        self._qizi_api_generation = {}
        self._qizi_auto_route_pending = {}
        self._qizi_auto_route_planning = set()
        self._qizi_auto_route_active = {}
        self._qizi_auto_route_paused = {}  # 用户手动暂停的动态航线，下次手动继续后仍需到达复核
        self._qizi_auto_route_groups = {}  # old exit -> target + affected UIDs
        self._qizi_dynamic_last_log = {}
        self._qizi_last_retired_prune = 0.0
        # 每次换 IP 时冻结实时坐标，记录经确认的 MMDB 目的地，不写入设备默认坐标。
        self._qizi_dynamic_coord_snapshots = {}
        # 不能清空外层注入的实际 HTTPS 测试函数 / 持久化函数。
        self._qizi_api_probe = getattr(self, '_qizi_api_probe', None)
        self._qizi_api_saved_exits = getattr(self, '_qizi_api_saved_exits', None)
        super().__init__(*args, **kwargs)
        self._qizi_dynamic_load_coordinates()
        self._qizi_restore_dynamic_routes()
        self._qizi_dyn_consolidate_duplicates()
        self.after(4500, self._qizi_dynamic_prune_retired)
        # 维护 API 已核实的公网 IP，不允许普通健康检测抢先覆盖而使 IP 轮换漏报。
        self._qizi_api_verified_public = {
            str(p.get('id')): str(p.get('public_ip') or '')
            for p in getattr(self, 'exits', []) if isinstance(p.get('dynamic_api'), dict)
        }
        self.after(1200, self._qizi_dynamic_boot_diagnostic)
        self.after(3500, self._qizi_api_poll)
        self.after(500, self._qizi_api_collect)
        self.after(1000, self._qizi_auto_route_tick)

    def _qizi_restore_dynamic_routes(self):
        """断电后恢复专属动态路由身份；普通断点寻路仍由原路线模块负责。"""
        for uid, state in list(getattr(self, 'route_states', {}).items()):
            if not isinstance(state, dict) or not state.get('qizi_dynamic_route'):
                continue
            uid = str(uid)
            ip = str(state.get('ip') or '')
            dest = state.get('qizi_dynamic_frozen_dest') or ()
            if not isinstance(dest, (list, tuple)) or len(dest) != 2:
                continue
            frozen = self._qizi_coord({'lat': dest[0], 'lon': dest[1]})
            if not frozen:
                continue
            original_id = str(state.get('qizi_dynamic_original_target_id') or '')
            latest_id = str(state.get('qizi_dynamic_latest_target_id') or original_id)
            if not original_id or not latest_id:
                continue
            old_ids = [str(x) for x in state.get('qizi_dynamic_old_ids', []) if x]
            if not old_ids and state.get('qizi_dynamic_old_id'):
                old_ids = [str(state['qizi_dynamic_old_id'])]
            item = dict(target_id=original_id, latest_target_id=latest_id,
                        old_id=old_ids[0] if old_ids else '', old_ids=old_ids,
                        ip=ip, frozen_dest=frozen,
                        was_allowed=bool(state.get('qizi_dynamic_was_allowed', False)))
            for old_id in old_ids:
                group = self._qizi_auto_route_groups.setdefault(
                    old_id, dict(target_id=latest_id, uids=set(), completed=set()))
                group['uids'].add(uid)
            if state.get('paused') and not state.get('running'):
                self._qizi_auto_route_paused[uid] = item
            elif state.get('running'):
                self._qizi_auto_route_active[uid] = item
                self.log_operation(f'{ip}：已恢复动态IP自动寻路身份及终点，继续执行后仍需到达复核')
        # 检测到新 IP 但尚未完成规划时，设备关联关系已存入设备配置。
        for ip, device in list(getattr(self, 'devices', {}).items()):
            uid = str(self.uid_for_ip(ip) or '')
            if not uid or uid in self._qizi_auto_route_active or uid in self._qizi_auto_route_paused:
                continue
            target_id = str(device.get('qizi_dynamic_follow_exit_id') or '')
            old_id = str(device.get('qizi_dynamic_source_exit_id') or '')
            target = self._qizi_find_exit(target_id) if target_id else None
            if not old_id or not target:
                continue
            if device.get('exit_id') != '__QIZI_BLOCK__':
                continue
            origin, source = self._qizi_dynamic_last_position(uid)
            self._qizi_auto_route_pending[uid] = dict(
                ip=ip, old_exit_id=old_id, old_ids=[old_id],
                target_exit_id=target_id,
                target_public_ip=str(target.get('public_ip') or ''),
                start_coord=origin, start_source=source if origin else '',
                next_try=0, created=time.monotonic(), last_wait_reason='',
                was_allowed=False)
            group = self._qizi_auto_route_groups.setdefault(
                old_id, dict(target_id=target_id, uids=set(), completed=set()))
            group['uids'].add(uid)
            self.log_operation(f'{ip}：恢复尚未开始的动态IP寻路任务，等待MMDB及Wi-Fi状态')

    def _qizi_dynamic_boot_diagnostic(self):
        """运行时版本自检，确认已经加载新动态模块及可自动轮询的 API。"""
        available = [p for p in getattr(self, 'exits', [])
                     if isinstance(p.get('dynamic_api'), dict)
                     and p['dynamic_api'].get('enabled')
                     and p['dynamic_api'].get('url')]
        self.log_operation(
            f'动态IP自动寻路模块 v{DYNAMIC_MODULE_VERSION} 已加载；'
            f'可轮询动态API出口 {len(available)} 个；'
            '状态为：IP检测→MMDB→冻结游走坐标→规划→自动启动→首点确认')
        if not available:
            self.log_operation('未发现已启用的动态 API 轮询配置；'
                               '绑定出口改用30～35秒健康检测，发现公网 IP 变化时自动规划并开始寻路')

    def _qizi_api_health_ip_changed(self, eid, public_ip, country):
        """普通健康检测发现动态出口变化时，强制重新请求API而不提前使用未核验IP。"""
        proxy = self._qizi_find_exit(eid)
        if proxy is None or not isinstance(proxy.get('dynamic_api'), dict):
            return False
        self._qizi_api_next[eid] = 0
        for ip, uid in self._qizi_dynamic_users(eid):
            self._qizi_dynamic_block(ip, uid, '健康检测发现IP变化，等待动态API重新核验出口')
        self.log_operation(f'出口 {str(eid)[:8]}：健康检测发现公网IP '
                           f'{proxy.get("public_ip") or "未知"} → {public_ip}；'
                           '已安排动态API独立核验')
        return True

    def _qizi_health_ip_changed(self, eid, public_ip, country):
        """Turn a verified health-probe IP change into the same future-exit route flow."""
        proxy = self._qizi_find_exit(eid)
        if not proxy or proxy.get('qizi_dynamic_retired') or not self._qizi_dynamic_users(eid):
            return False
        snapshot = dict(proxy)
        parsed = {key: proxy.get(key, '') for key in ('host', 'port', 'username', 'password')}
        self.log_operation(f'出口 {str(eid)[:8]}：健康检测确认公网 IP '
                           f'{snapshot.get("public_ip") or "未知"} → {public_ip}；'
                           '已建立新未来出口并启动自动寻路判断')
        self._qizi_api_apply_result(eid, snapshot, (True, parsed, public_ip, country))
        return True

    def handle_events(self):
        """原出口健康检测可能先写 public_ip：恢复 API 旧值并让动态 API 专门裁决切换。

        不能让普通健康检测绕过自动路线，把目标出口在飞行中直接标记为新 IP。
        与原本的 handle_events 同线程调用，不读取/修改事件队列。
        """
        before = {str(p.get('id')): str(p.get('public_ip') or '')
                  for p in getattr(self, 'exits', [])
                  if isinstance(p.get('dynamic_api'), dict)
                  and p['dynamic_api'].get('enabled') and p['dynamic_api'].get('url')}
        result = super().handle_events()
        for p in list(getattr(self, 'exits', [])):
            eid = str(p.get('id') or '')
            if eid not in before:
                continue
            current = str(p.get('public_ip') or '')
            known = getattr(self, '_qizi_api_verified_public', {}).get(eid, before[eid])
            if current == known:
                continue
            # 这里观察到的是普通健康检测的变化，尚未经本次动态 API 授权切换。
            p['public_ip'] = known
            p['dynamic_api_state'] = '健康检测发现IP变化，等待动态API验证'
            try:
                # 原健康检测已落盘；同步撤销其提前写入的新 IP，避免重启后误认为切换已完成。
                self._qizi_api_saved_exits(self.exits)
            except OSError as exc:
                self.log_operation(f'动态IP {eid[:8]}：撤销未验证IP的配置保存失败：{exc}')
            self._qizi_api_next[eid] = 0
            self.log_operation(f'动态IP {eid[:8]}：普通健康检测发现 {known or "未知"} → '
                               f'{current or "未知"}；已触发动态API独立验证，未提前切换出口')
            affected = self._qizi_dynamic_users(eid)
            for ip, uid in affected:
                # 封网之前持久保存来源：exit_id 被改成禁网哨兵后，API 回调还能找回关联设备。
                with self.device_lock:
                    d = self.devices.get(ip)
                    if d is not None:
                        d['qizi_dynamic_source_exit_id'] = eid
                        d['qizi_dynamic_follow_exit_id'] = eid
                self._qizi_dynamic_block(ip, uid, '新公网IP尚未完成自动寻路，不允许提前使用新出口')
            if not affected:
                self.log_operation(f'动态IP {eid[:8]}：健康检测发现变化，但尚无绑定该出口的设备')
            self.refresh_exits()
            # 自上次代码更新到此处，下一次 _qizi_api_poll 将以恢复后的旧 IP 为基线。
        return result

    def build_ui(self):
        super().build_ui()
        self.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei')
        try:
            self.geometry('1190x620')
            self.minsize(980, 500)
            # 只有一列新字段：未来出口；不再显示已绑/放行/公网状态大列。
            header = self.rows_frame.winfo_children()[0]
            cell = tk.Frame(header, bg='#eeeeee', width=self.WIDTHS[-1])
            cell.pack(side='left', fill='y')
            cell.pack_propagate(False)
            tk.Label(cell, text='未来出口', bg='#eeeeee',
                     font=('Microsoft YaHei UI', 9, 'bold')).pack(expand=True)
            # 顶部：自动扫描、扫描状态、设备数量、更多。去掉重复“在线”。
            container = self.rows_frame.master.master.master  # canvas -> left -> paned -> container
            top = next((w for w in container.winfo_children()
                        if isinstance(w, ttk.Frame)), None)
            if top is not None:
                more_button = next((w for w in top.winfo_children()
                    if isinstance(w, ttk.Menubutton) and str(w.cget('text')) == '更多'), None)
                summary_label = next((w for w in top.winfo_children()
                    if isinstance(w, ttk.Label) and str(w.cget('textvariable')) == str(self.summary)), None)
                for w in (more_button, summary_label):
                    if w is not None: w.pack_forget()
                if summary_label is not None:
                    summary_label.pack(side='left', padx=(2, 4))
                if more_button is not None:
                    more_button.pack(side='left', padx=(2, 0))
            right = self.exit_table.master.master
            right.configure(width=218)
            # 清掉旧右侧四个操作按钮，标题与唯一“添加出口”并排。
            children = list(right.winfo_children())
            for w in children:
                if isinstance(w, ttk.Label) and str(w.cget('text')) == '出口管理':
                    w.destroy()
                elif isinstance(w, ttk.Frame) and w is not self.exit_table.master:
                    # 这里只删除原始顶部 toolbar，不能删除 Treeview 的 wrapper。
                    w.destroy()
            header_right = ttk.Frame(right)
            header_right.pack(fill='x', padx=3, pady=(1, 3), before=self.exit_table.master)
            ttk.Label(header_right, text='出口管理', font=('Microsoft YaHei UI', 10, 'bold')).pack(side='left')
            ttk.Button(header_right, text='添加出口', width=8, command=self.add_exit).pack(side='right')
            self.exit_table.heading('name', text='数量  国家 + IP')
            self.exit_table.column('name', width=195, minwidth=150)
            self.style.configure('Exit.Treeview', rowheight=24,
                                 font=('Microsoft YaHei UI', 9))
            # 旧“已绑/放行”信息和占用汇总都不再出现。
            self.exit_table.bind('<ButtonRelease-1>', self._qizi_on_exit_click, add='+')
        except Exception as exc:
            if hasattr(self, 'log_operation'):
                self.log_operation('简洁界面调整失败：' + type(exc).__name__ + ' ' + str(exc)[:150])

    def new_row(self, ip):
        row = super().new_row(ip)
        try:
            # 旧行有8个 cell；第9列由扩展 WIDTHS 创建，在自动寻路后。
            cell = row['frame'].winfo_children()[-1]
            lab = tk.Label(cell, text='', anchor='w', bg=cell.cget('bg'),
                           fg='#666666', font=('Consolas', 9), padx=3)
            lab.pack(fill='both', expand=True)
            row['qizi_future_exit'] = lab
            lab.bind('<Button-1>', lambda e, ip=ip:self.choose_row(ip), add='+')
            lab.bind('<Button-3>', lambda e, ip=ip:self.row_menu(e, ip))
        except (tk.TclError, IndexError):
            pass
        return row

    def _qizi_occupancy(self):
        from qizi_exit_usage_engine import current_counts
        return current_counts(self.exits, self._qizi_usage_devices() if hasattr(self, '_qizi_usage_devices') else self.devices, self.route_states, self.uid_for_ip, self.devices)

    def _qizi_refresh_usage(self):
        if not hasattr(self, 'exit_table') or not hasattr(self, 'devices'):
            return
        from qizi_exit_usage_engine import (current_counts, ordered_exit_indices,
                                            future_exit_for_device)
        exits = list(getattr(self, 'exits', []))
        counts = current_counts(exits, self._qizi_usage_devices() if hasattr(self, '_qizi_usage_devices') else self.devices, self.route_states, self.uid_for_ip, self.devices)
        lookup = {str(p.get('id')): p for p in exits}
        for ip, row in list(getattr(self, 'row_widgets', {}).items()):
            lab = row.get('qizi_future_exit')
            if not lab or not lab.winfo_exists():
                continue
            uid = self.uid_for_ip(ip)
            eid = future_exit_for_device(ip, self.devices, self.route_states, uid)
            task = self._qizi_auto_route_pending.get(str(uid), {}) if uid else {}
            active = self._qizi_auto_route_active.get(str(uid), {}) if uid else {}
            if active:
                # “未来出口”始终显示当前冻结目的地；最新 IP 标注待到达后确认。
                eid = active.get('latest_target_id') or active['target_id']
                prefix, suffix = '', ''
            elif task and not eid:
                eid = task['target_exit_id']
                prefix, suffix = '待规划 ', ''
            else:
                prefix, suffix = '', ''
            if eid:
                proxy = lookup.get(str(eid))
                # 简洁显示：国家 城市 IP；不再叠加飞行、等待等状态描述。
                label = self.exit_label(proxy) if proxy else ''
                lab.configure(text=label, fg='#1565c0')
            else:
                lab.configure(text='', fg='#666666')
        # 当前出口及未来出口分别计算；每个设备对同一目的地最多预占一次。
        for ip, device in self.devices.items():
            uid = str(self.uid_for_ip(ip) or '')
            pending = self._qizi_auto_route_pending.get(uid) or {}
            active = self._qizi_auto_route_active.get(uid) or {}
            paused = self._qizi_auto_route_paused.get(uid) or {}
            future = str(pending.get('target_exit_id') or active.get('latest_target_id')
                         or paused.get('latest_target_id')
                         or device.get('qizi_dynamic_follow_exit_id') or '')
            if not future or future not in counts:
                continue
            state = self.route_states.get(uid) or {}
            counted_as_current = (str(device.get('exit_id') or '') == future and
                                  ip in (self._qizi_usage_devices() if hasattr(self, '_qizi_usage_devices') else self.devices))
            counted_as_route = (bool(state.get('running')) and
                                str(state.get('target_exit_id') or '') == future)
            if not counted_as_current and not counted_as_route:
                counts[future] += 1
        for i, p in enumerate(exits):
            iid = str(i)
            if not self.exit_table.exists(iid):
                continue
            count = counts.get(str(p.get('id')), 0)
            flag = '↻ ' if p.get('dynamic_api') else ''
            values = list(self.exit_table.item(iid, 'values'))
            if values:
                values[0] = f'{count}  {flag}{self.exit_label(p)}'
                self.exit_table.item(iid, values=tuple(values))
        # 0个设备最上面，使用越多越靠下面。IID保留原索引，右键检测不会错位。
        for pos, i in enumerate(ordered_exit_indices(exits, counts)):
            self.exit_table.move(str(i), '', pos)

    def refresh_exits(self, selected=None):
        result = super().refresh_exits(selected)
        self._qizi_refresh_usage()
        return result

    def refresh_devices(self, selected_ip=None):
        result = super().refresh_devices(selected_ip)
        if hasattr(self, 'summary'):
            self.summary.set(f'设备 {len(self.devices)}')
        for ip, row in list(getattr(self,'row_widgets',{}).items()):
            name = str(self.devices.get(ip,{}).get('remark') or '').strip()
            if row.get('ip') and row['ip'].winfo_exists():
                row['ip'].configure(text=(f'{name[:12]}\n{ip}' if name else ip),
                                    font=('Microsoft YaHei UI', 8, 'bold') if name
                                    else ('Consolas', 9, 'bold'))
        self._qizi_refresh_usage()
        return result

    def _qizi_on_exit_click(self, event):
        iid = self.exit_table.identify_row(event.y)
        if not iid:
            return
        try:
            eid = self.exits[int(iid)]['id']
        except (ValueError, IndexError, KeyError):
            return
        users = [ip for ip, d in self.devices.items() if d.get('exit_id') == eid]
        if users:
            ip = users[0]
            self.choose_row(ip)
            row = self.row_widgets.get(ip)
            if row and row['frame'].winfo_exists():
                try:
                    y = row['frame'].winfo_y()
                    total = max(1, self.rows_frame.winfo_height() - self.canvas.winfo_height())
                    self.canvas.yview_moveto(min(1., max(0., y / total)))
                except (TypeError, tk.TclError):
                    pass

    def edit_device_remark(self, ip):
        from tkinter import simpledialog
        d = self.devices.get(ip)
        if d is None:
            return
        old = str(d.get('remark') or '')
        name = simpledialog.askstring('设备备注', '修改设备名称（留空则只显示 IP）：',
                                      initialvalue=old, parent=self)
        if name is None:
            return
        name = name.strip()[:48]
        if name == old:
            return
        with self.device_lock:
            if ip not in self.devices:
                return
            self.devices[ip]['remark'] = name
        # Save from installed base application; imported in outer module via injection.
        self._qizi_save_devices(self.devices)
        self.refresh_devices(ip)
        self.log_operation(f'{ip}：设备备注已修改为 {name or "（无）"}')

    def choice_values(self):
        """历史A仍显示在出口管理中，但已停用的出口不再进入网络/寻路下拉框。"""
        ids, labels = super().choice_values()
        retired = {str(p.get('id')) for p in getattr(self, 'exits', [])
                   if p.get('qizi_dynamic_retired')}
        pairs = [(eid, label) for eid, label in zip(ids, labels) if str(eid) not in retired]
        return [eid for eid, _ in pairs], [label for _, label in pairs]

    def bind_row_exit(self, ip):
        """旧A保留到确认抵达B，但旧代理不允许被再次绑定。"""
        row = getattr(self, 'row_widgets', {}).get(ip)
        if row:
            index = row['combo'].current()
            ids = row.get('ids', ())
            if 0 <= index < len(ids):
                proxy = self._qizi_find_exit(ids[index])
                if proxy and proxy.get('qizi_dynamic_retired'):
                    self.log_operation(f'{ip}：该出口已轮换，属于待删除历史出口，不允许重新绑定')
                    self.refresh_devices(ip)
                    return
        return super().bind_row_exit(ip)

    def row_menu(self, event, ip):
        self.choose_row(ip)
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label='修改设备名称 / 备注',
                         command=lambda:self.edit_device_remark(ip))
        menu.add_command(label='动态IP / 游走坐标记录',
                         command=lambda:self._qizi_dynamic_coordinate_dialog(ip))
        menu.add_separator()
        menu.add_command(label='断开 Wi-Fi 连接',command=lambda:self.disconnect_row(ip))
        menu.add_command(label='恢复真实定位',command=lambda:self.clear_row(ip))
        menu.add_command(label='设备详情 / 错误',command=lambda:self.row_details(ip))
        menu.add_command(label='手动匹配 iPhone',command=lambda:self.match_row(ip))
        menu.add_separator()
        menu.add_command(label='删除设备',command=self.delete_device)
        try:menu.tk_popup(event.x_root,event.y_root)
        finally:menu.grab_release()

    def open_dynamic_api_dialog(self, target_exit_id=None):
        """支持 API 配置 JSON 或单次响应 JSON；无 URL 时不启用定时轮询。"""
        win = tk.Toplevel(self)
        win.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei')
        win.geometry('800x640')
        win.transient(self)
        win.grab_set()
        body = ttk.Frame(win, padding=12)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='粘贴 API 配置 JSON（可含 url / headers / host_path），或粘贴一次 JSON 响应。',
                  wraplength=760).pack(anchor='w')
        text = tk.Text(body, height=11, font=('Consolas', 10))
        text.pack(fill='both', expand=True, pady=7)
        selected = next((p for p in self.exits if p.get('id') == target_exit_id), None)
        demo_config = (selected.get('dynamic_api') if selected and selected.get('dynamic_api') else {
            'url':'https://YOUR-API-URL', 'method':'GET', 'host_path':'data.ip',
            'port_path':'data.port', 'enabled':True
        })
        text.insert('1.0', json.dumps(demo_config, ensure_ascii=False, indent=2))
        rows = ttk.Frame(body)
        rows.pack(fill='x', pady=4)
        url_var = tk.StringVar()
        port_var = tk.StringVar()
        user_var = tk.StringVar()
        password_var = tk.StringVar()
        path_var = tk.StringVar()
        def field(label, variable, row, secret=False):
            ttk.Label(rows, text=label).grid(row=row, column=0, sticky='w', pady=3)
            ttk.Entry(rows, textvariable=variable, show='•' if secret else '').grid(
                row=row, column=1, sticky='ew', pady=3, padx=6)
        rows.columnconfigure(1, weight=1)
        field('API URL（可覆盖 JSON 内 url；只有提供 URL 才能自动更新）', url_var, 0)
        field('默认端口（JSON 不返回 port 时使用）', port_var, 1)
        field('代理账号（可选）', user_var, 2)
        field('代理密码（可选）', password_var, 3, True)
        field('IP JSON 路径（如 data.ip，可选）', path_var, 4)
        existing = [(p.get('id'), p) for p in self.exits]
        opts = ['新增为独立出口'] + [f'关联现有出口：{p.get("name") or p.get("host")} [{str(eid)[:7]}]'
                                for eid, p in existing]
        target_var = tk.StringVar(value=opts[0])
        ttk.Label(body, text='添加方式：').pack(anchor='w')
        choice = ttk.Combobox(body, textvariable=target_var, state='readonly', values=opts)
        initial_idx=next((i+1 for i, (eid, _) in enumerate(existing) if eid==target_exit_id), 0)
        choice.current(initial_idx)
        choice.pack(fill='x', pady=4)
        status = tk.StringVar(value='动态 API：每10～20秒检查；先验证 HTTPS 出口 IP，成功后更新。')
        ttk.Label(body, textvariable=status, foreground='#1565c0', wraplength=740).pack(anchor='w', pady=5)
        buttons = ttk.Frame(body)
        buttons.pack(fill='x', pady=7)
        submitted = {'in_progress':False}

        def submit():
            if submitted['in_progress']:
                return
            try:
                raw = json.loads(text.get('1.0', 'end'))
                if not isinstance(raw, dict):
                    raise ValueError('请粘贴 JSON 对象')
                config = dict(raw)
                if url_var.get().strip(): config['url'] = url_var.get().strip()
                if path_var.get().strip(): config['host_path'] = path_var.get().strip()
                is_auto = bool(config.get('url') or config.get('api_url'))
                if is_auto: config = normalise_api_config(config)
                current = {'port':port_var.get().strip(),
                           'username':user_var.get().strip(), 'password':password_var.get()}
                selection = choice.current()
                existing_id = existing[selection-1][0] if 1 <= selection <= len(existing) else None
            except (ValueError, TypeError) as exc:
                messagebox.showerror('输入不合法', safe_status(exc), parent=win)
                return
            submitted['in_progress'] = True
            status.set('正在后台请求 API 并通过代理验证，请稍候…' if is_auto else '正在验证 JSON 给出的代理，请稍候…')
            btn.configure(state='disabled')
            def worker():
                try:
                    payload = fetch_json(config) if is_auto else raw
                    parsed = parse_response(payload, config, current)
                    candidate = {k: parsed[k] for k in ('host','port','username','password')}
                    public, country = self._qizi_api_probe(candidate)
                    if parsed.get('expected_public_ip') and parsed['expected_public_ip'] != public:
                        raise ValueError('代理实际公网出口 IP 与 JSON 声明不一致，未切换')
                    result = ('ok', parsed, public, country, config if is_auto else None, existing_id)
                except Exception as exc:
                    result = ('fail', safe_status(exc))
                self._qizi_api_results.put(('dialog', result, win, status, btn, submitted))
            threading.Thread(target=worker, name='qizi-dynamic-api-add', daemon=True).start()
        btn = ttk.Button(buttons, text='验证后添加 / 关联', command=submit)
        btn.pack(side='right')
        ttk.Button(buttons, text='取消', command=win.destroy).pack(side='right', padx=8)

    def _exit_context_menu(self, event):
        iid = self.exit_table.identify_row(event.y) if hasattr(self, 'exit_table') else ''
        if iid:
            self.exit_table.selection_set(iid)
            self.exit_table.focus(iid)
            try:self._last_exit_index = int(iid)
            except ValueError:pass
        try:p = self.exits[int(iid)] if iid else None
        except (ValueError, IndexError):p = None
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label='修改', command=self.edit_exit,
                         state='normal' if p else 'disabled')
        menu.add_command(label='删除', command=self.delete_exit,
                         state='normal' if p else 'disabled')
        menu.add_command(label='检测', command=self.test_selected_direct,
                         state='normal' if p else 'disabled')
        menu.add_separator()
        # 动态IP仍可配置，但不占用顶部按钮位；空白处右键也可新增动态API。
        menu.add_command(label='动态IP API 配置…',
                         command=lambda:self.open_dynamic_api_dialog(p.get('id') if p else None))
        if p and p.get('dynamic_api'):
            menu.add_command(label='立即检查动态IP',
                             command=lambda:self._qizi_api_next.__setitem__(p['id'], 0))
        try:menu.tk_popup(event.x_root, event.y_root)
        finally:menu.grab_release()

    def _qizi_api_collect(self):
        if getattr(self, '_closing', False):
            return
        for _ in range(25):
            try: ev = self._qizi_api_results.get_nowait()
            except queue.Empty: break
            if ev[0] == 'dialog':
                _, result, win, status, btn, submitted = ev
                if not win.winfo_exists():
                    continue
                if result[0] == 'fail':
                    status.set('验证失败：' + result[1] + '；未修改现有出口')
                    submitted['in_progress'] = False
                    btn.configure(state='normal')
                else:
                    _, parsed, public, country, config, existing_id = result
                    if self._qizi_api_apply_new(parsed, public, country, config, existing_id):
                        win.destroy()
                    else:
                        status.set('出口配置保存失败 / 目标已删除，未修改已有出口')
                        submitted['in_progress'] = False
                        btn.configure(state='normal')
            elif ev[0] == 'auto_log':
                self.log_operation(f'{ev[1]}：自动寻路 {ev[2]}')
            elif ev[0] == 'auto_plan':
                try:
                    self._qizi_auto_plan_result(*ev[1:])
                except Exception as exc:
                    uid, ip = str(ev[1]), str(ev[2])
                    self._qizi_auto_route_planning.discard(uid)
                    task = self._qizi_auto_route_pending.get(uid)
                    if task: task['next_try'] = time.monotonic() + AUTO_ROUTE_RETRY_SECONDS
                    self.log_operation(f'{ip}：动态IP【规划结果执行异常】'
                                       f'{type(exc).__name__}: {str(exc)[:220]}；'
                                       '维持禁止网络，60秒后重试')
            elif ev[0] == 'poll':
                _, eid, snapshot, result = ev
                self._qizi_api_running.discard(eid)
                try:
                    self._qizi_api_apply_result(eid, snapshot, result)
                except Exception as exc:
                    # 单次异步回调失败不能导致整条轮询调度链停止。
                    self.log_operation('动态IP回调异常（下一轮继续检测）：'
                                       + type(exc).__name__ + ': ' + str(exc)[:140])
                    self._qizi_api_next[str(eid)] = 0
        self.after(500, self._qizi_api_collect)

    def _qizi_api_apply_new(self, parsed, public, country, config, existing_id):
        if existing_id:
            p = next((p for p in self.exits if p.get('id') == existing_id), None)
            if not p:
                self.log_operation('目标出口已删除，取消 API 关联')
                return False
            old = dict(p)
            candidate = dict(p)
        else:
            # 首次添加同一API+同一出口时复用已有记录；反复点添加不会复制一大堆IP。
            matched = next((x for x in self.exits
                if isinstance(x.get('dynamic_api'), dict)
                and config and x.get('dynamic_api') == config
                and str(x.get('public_ip') or '') == str(public)
                and all(str(x.get(k) or '') == str(parsed.get(k) or '')
                        for k in ('host','port','username','password'))), None)
            if matched is not None:
                self.log_operation('动态API+公网IP已存在，直接使用现有出口，不重复新增')
                return True
            p = None
            old = None
            candidate = dict(id=str(uuid.uuid4()), name='动态 ' + parsed['host'],
                             type='http', status='在线', country='', city='',
                             geo_lat='', geo_lon='', geo_status='等待地理识别', public_ip='')
        candidate.update({k: parsed[k] for k in ('host', 'port', 'username', 'password')})
        candidate.update(public_ip=public, country=country, status='在线', last_error='',
                         dynamic_api_last=time.strftime('%Y-%m-%d %H:%M:%S'),
                         dynamic_api_error='', dynamic_api_state='已验证' if config else '单次JSON')
        # 首次把动态 API 关联到正在使用的固定出口，如果实际公网 IP 已经变更，
        # 不能原地覆盖旧出口并跳过寻路，应进入同一条冻结路线/安全切换流程。
        if (p is not None and config and old.get('public_ip') and
                str(old.get('public_ip')) != str(public) and
                self._qizi_dynamic_users(str(p['id']))):
            original = dict(p)
            p['dynamic_api'] = config
            snapshot = dict(p)
            snapshot['_qizi_poll_started_at'] = time.monotonic()
            self._qizi_api_apply_result(p['id'], snapshot, (True, parsed, public, country))
            migrated = any(x.get('dynamic_api') == config and
                           str(x.get('public_ip')) == str(public) and x is not p
                           for x in self.exits)
            if not migrated:
                p.clear(); p.update(original)
                return False
            self.log_operation(f'动态 API 首次关联：旧公网 IP {old["public_ip"]} 与新出口 {public} 不同，已进入寻路判断')
            return True
        if config:
            candidate['dynamic_api'] = config
        else:
            candidate.pop('dynamic_api', None)
        # 先写盘成功，才同步内存和 live routes。失败时旧出口完全不变。
        prospective = [(candidate if x is p else x) for x in self.exits]
        if p is None:
            prospective.append(candidate)
        try:
            self._qizi_api_saved_exits(prospective)
        except OSError:
            self.log_operation('出口配置保存失败，旧出口保持不变')
            return False
        if p is None:
            self.exits.append(candidate)
            p = candidate
        else:
            p.clear(); p.update(candidate)
        if old and any(old.get(k) != p.get(k) for k in ('host', 'port', 'username', 'password')):
            self._qizi_api_route_change(p['id'])
        self._qizi_api_next[p['id']] = time.monotonic() + random.uniform(POLL_MIN_SECONDS, POLL_MAX_SECONDS)
        self.refresh_exits()
        self.log_operation(f'出口 {p["id"][:8]}：已验证公网 IP {public}；' +
                           ('开启随机10～20秒动态检查' if config else '单次导入，无自动检查'))
        # refresh_exits 已统一触发公网IP的 MMDB 队列，不重复加入。
        return True

    def _qizi_api_poll(self):
        if getattr(self, '_closing', False):
            return
        now = time.monotonic()
        known = {p.get('id') for p in self.exits}
        for eid in list(self._qizi_api_next):
            if eid not in known:
                self._qizi_api_next.pop(eid, None)
        for eid in list(self._qizi_egress_probe_next):
            if eid not in known:
                self._qizi_egress_probe_next.pop(eid, None)
        free = MAX_CONCURRENT_CHECKS - len(self._qizi_api_running)
        if free > 0:
            for p in list(self.exits):
                if free <= 0: break
                eid = p.get('id')
                conf = p.get('dynamic_api')
                if not isinstance(conf, dict) or not conf.get('enabled') or not conf.get('url'):
                    continue
                if eid in self._qizi_api_running or self._qizi_api_next.get(eid, 0) > now:
                    continue
                snapshot = dict(p)
                self._qizi_api_running.add(eid)
                self._qizi_api_next[eid] = now + random.uniform(POLL_MIN_SECONDS, POLL_MAX_SECONDS)
                snapshot['_qizi_poll_started_at'] = now
                free -= 1
                def worker(eid=eid, snap=snapshot):
                    try:
                        cfg = normalise_api_config(snap['dynamic_api'])
                        payload = fetch_json(cfg)
                        parsed = parse_response(payload, cfg, snap)
                        candidate = dict(snap)
                        candidate.update({k: parsed[k] for k in ('host', 'port', 'username', 'password')})
                        actual_ip, country = self._qizi_api_probe(candidate)
                        if parsed.get('expected_public_ip') and parsed['expected_public_ip'] != actual_ip:
                            raise ValueError('API 声明的出口 IP 与代理真实公网 IP 不一致')
                        result = (True, parsed, actual_ip, country)
                    except Exception as exc:
                        result = (False, safe_status(exc))
                    self._qizi_api_results.put(('poll', eid, snap, result))
                threading.Thread(target=worker, name='qizi-dynamic-api-check', daemon=True).start()
        referenced_ids = {
            str(device.get(key) or '')
            for device in self.devices.values()
            for key in ('exit_id', 'qizi_dynamic_source_exit_id', 'qizi_dynamic_follow_exit_id')
            if device.get(key)
        }
        health_capacity = min(2, max(
            0, MAX_CONCURRENT_CHECKS - len(self._qizi_api_running) -
            len(getattr(self, 'test_jobs', {}))))
        for index, proxy in enumerate(self.exits):
            if health_capacity <= 0:
                break
            eid = str(proxy.get('id') or '')
            if (eid not in referenced_ids or isinstance(proxy.get('dynamic_api'), dict)
                    or proxy.get('qizi_dynamic_retired')
                    or self._qizi_egress_probe_next.get(eid, 0) > now):
                continue
            self._qizi_egress_probe_next[eid] = now + random.uniform(
                HEALTH_MIN_SECONDS, HEALTH_MAX_SECONDS)
            self.start_direct_test(index)
            health_capacity -= 1
        self.after(1000, self._qizi_api_poll)

    @staticmethod
    def _qizi_distance_km(a, b):
        """两点大圆距离；仅用于是否有必要重新模拟移动的策略判断。"""
        lat1, lon1 = map(math.radians, a)
        lat2, lon2 = map(math.radians, b)
        h = (math.sin((lat2-lat1)/2.0)**2 + math.cos(lat1)*math.cos(lat2)*
             math.sin((lon2-lon1)/2.0)**2)
        return 12742.0176 * math.asin(min(1.0, math.sqrt(max(0.0, h))))

    @staticmethod
    def _qizi_coord(source):
        try:
            a, b = float(source['lat']), float(source['lon'])
            if math.isfinite(a) and math.isfinite(b) and -90 <= a <= 90 and -180 <= b <= 180:
                return a, b
        except (TypeError, ValueError, KeyError):
            pass
        return None

    def _qizi_exit_coord(self, proxy):
        coordinates = exit_coordinates(proxy)
        return self._qizi_coord({'lat': coordinates[0], 'lon': coordinates[1]}) if coordinates else None

    def _qizi_find_exit(self, eid):
        return next((p for p in self.exits if str(p.get('id')) == str(eid)), None)

    def _qizi_dynamic_log_once(self, uid, key, message):
        """等待条件只在变化时打印，避免淹没操作日志。"""
        old = self._qizi_dynamic_last_log.get(str(uid))
        if old != key:
            self._qizi_dynamic_last_log[str(uid)] = key
            self.log_operation(message)

    def _qizi_dynamic_users(self, eid):
        """设备被禁止网络后 exit_id 不再是旧 ID，仍须跟踪 pending / active。"""
        out = []
        for ip, device in list(self.devices.items()):
            uid = str(self.uid_for_ip(ip) or '')
            task = self._qizi_auto_route_pending.get(uid, {}) if uid else {}
            active = self._qizi_auto_route_active.get(uid, {}) if uid else {}
            paused = self._qizi_auto_route_paused.get(uid, {}) if uid else {}
            state = self.route_states.get(uid, {}) if uid else {}
            if (str(device.get('exit_id') or '') == str(eid) or
                str(device.get('qizi_dynamic_source_exit_id') or '') == str(eid) or
                str(device.get('qizi_dynamic_follow_exit_id') or '') == str(eid) or
                str(task.get('target_exit_id') or '') == str(eid) or
                str(active.get('latest_target_id') or '') == str(eid) or
                str(paused.get('latest_target_id') or '') == str(eid) or
                ((state.get('running') or state.get('paused'))
                 and str(state.get('target_exit_id') or '') == str(eid))):
                out.append((ip, uid))
        return out

    def _qizi_dynamic_snapshot_path(self):
        """沿用路线模块 DATA_DIR；用户配置与网络凭证不存入坐标快照。"""
        root = getattr(self, '_qizi_live_positions_path', None)
        return root.parent / 'qizi_dynamic_route_coordinates.json' if root else None

    def _qizi_dynamic_load_coordinates(self):
        path = self._qizi_dynamic_snapshot_path()
        if path is None or not path.is_file(): return
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(raw, dict):
                self._qizi_dynamic_coord_snapshots = {
                    str(uid): value for uid, value in raw.items() if isinstance(value, dict)}
        except (OSError, ValueError) as exc:
            self.log_operation(f'动态坐标历史载入失败：{str(exc)[:100]}')

    def _qizi_dynamic_save_coordinates(self, uid, *, ip, source=None, origin=None,
                                       target_id=None, target_ip=None, dest=None, status=None):
        """保存此次IP轮换的冻结起点与目标MMDB坐标，不修改正在游走的主坐标。"""
        uid = str(uid)
        rec = dict(self._qizi_dynamic_coord_snapshots.get(uid) or {})
        rec.update(device_ip=ip)
        if origin is not None:
            rec.update(roam_lat=origin[0], roam_lon=origin[1],
                       roam_source=source or 'unknown',
                       roam_record_time=time.strftime('%Y-%m-%d %H:%M:%S'))
        if target_id is not None: rec['target_exit_id'] = str(target_id)
        if target_ip is not None: rec['dynamic_public_ip'] = str(target_ip)
        if dest is not None:
            rec.update(dynamic_lat=dest[0], dynamic_lon=dest[1])
        elif target_id is not None:
            # 轮换后必须重新查 MMDB；绝不可保留上一次 IP 的旧目标坐标。
            rec.pop('dynamic_lat', None); rec.pop('dynamic_lon', None)
        if status is not None: rec['status'] = status
        if rec == self._qizi_dynamic_coord_snapshots.get(uid):
            return  # 秒级寻路调度无需反复写盘；IP/坐标/状态真正变化才保存
        rec['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
        self._qizi_dynamic_coord_snapshots[uid] = rec
        path = self._qizi_dynamic_snapshot_path()
        if path is None: return
        tmp = path.with_suffix(path.suffix + '.tmp')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._qizi_dynamic_coord_snapshots,
                           ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(tmp, path)
        except OSError as exc:
            self.log_operation(f'{ip}：动态IP/游走坐标快照写入失败：{str(exc)[:100]}')

    def _qizi_dynamic_coordinate_dialog(self, ip):
        uid = str(self.uid_for_ip(ip) or '')
        record = dict(self._qizi_dynamic_coord_snapshots.get(uid) or {})
        current, source = self._qizi_dynamic_last_position(uid)
        target = self._qizi_find_exit(record.get('target_exit_id'))
        dest = self._qizi_exit_coord(target)
        if current:
            record.update(current_lat=current[0], current_lon=current[1], current_source=source)
        if dest:
            record.update(dynamic_lat=dest[0], dynamic_lon=dest[1])
        win = tk.Toplevel(self)
        win.title(f'动态IP / 游走坐标记录 - {ip}')
        win.geometry('650x335')
        win.transient(self)
        body = ttk.Frame(win, padding=12); body.pack(fill='both', expand=True)
        fields = [
            ('设备', ip),
            ('最后定位记录（当前）',
             f'{record.get("current_lat", "未知")}, {record.get("current_lon", "未知")} '
             f'[{record.get("current_source", "无可用新坐标")}]'),
            ('IP变化时冻结的游走起点',
             f'{record.get("roam_lat", "未记录")}, {record.get("roam_lon", "未记录")} '
             f'[{record.get("roam_source", "未知")}]'),
            ('新动态公网 IP', record.get('dynamic_public_ip', '未记录')),
            ('目标 IP 的 MMDB 经/纬度',
             f'{record.get("dynamic_lat", "等待查询")}, {record.get("dynamic_lon", "等待查询")}'),
            ('任务状态', record.get('status', '未创建任务')),
            ('记录时间', record.get('updated', '')),
        ]
        for i, (title, value) in enumerate(fields):
            ttk.Label(body, text=title, width=25).grid(row=i, column=0, sticky='w', pady=5)
            ttk.Label(body, text=str(value), wraplength=390).grid(row=i, column=1, sticky='w', pady=5)
        ttk.Button(body, text='关闭', command=win.destroy).grid(row=len(fields), column=1, sticky='e', pady=12)

    def _qizi_dynamic_last_position(self, uid):
        """只信任近期已收到的游走/路线坐标报告，严禁将MMDB旧坐标当作设备当前位置。

        qizi_route_ui_engine 已经保存 _qizi_live_positions[uid] 的定位事件。
        没有有效事件时不假设输入框坐标=最后实际下发坐标。
        """
        uid = str(uid)
        rec = (getattr(self, '_qizi_live_positions', None) or {}).get(uid) or {}
        point = self._qizi_coord(rec)
        try: age = time.time() - float(rec.get('updated') or 0)
        except (TypeError, ValueError): age = float('inf')
        if point and -5.0 <= age <= DYNAMIC_MAX_POSITION_AGE_SECONDS:
            label = str(rec.get('mode') or '定位事件')
            return point, f'最后实际发送定位/{label}（{max(0,age):.0f}秒前）'
        # 网络变更时手机可能已经断网，最后一个游走点超过120秒仍然有价值；
        # 仅限本次运行已有记录或24小时内的持久记录，不回退到输入框/旧路线起点。
        if point and DYNAMIC_MAX_POSITION_AGE_SECONDS < age <= 86400:
            return point, f'最后一次已记录定位/{rec.get("mode") or "未知"}（{age:.0f}秒前）'
        # 正在运行路线时的 state.last_lat/last_lon 是近期发送的路线坐标。
        state = (getattr(self, 'route_states', None) or {}).get(uid) or {}
        if state.get('running'):
            point = self._qizi_coord({'lat': state.get('last_lat'), 'lon': state.get('last_lon')})
            if point: return point, '寻路发送坐标'
        return None, '没有120秒内的定位发送记录'

    def _qizi_manual_panel_origin(self, ip, uid):
        """使用与手动点击【自动寻路】时相同的起点：该设备行中的纬度/经度。"""
        row = getattr(self, 'row_widgets', {}).get(ip)
        if row:
            try:
                point = self._qizi_coord({'lat': row['lat'].get().strip(),
                                          'lon': row['lon'].get().strip()})
            except (KeyError, AttributeError, TypeError):
                point = None
            if point:
                return point, '自动寻路面板起点（与手动相同）'
        saved = (getattr(self, 'route_plans', {}) or {}).get(str(uid), {})
        point = self._qizi_coord({'lat': saved.get('start_lat'), 'lon': saved.get('start_lon')})
        if point:
            return point, '手动寻路面板保存的起点'
        return self._qizi_dynamic_last_position(uid)

    def _qizi_dynamic_origin(self, uid, task):
        """首次收到IP变更时冻结起点；永远不能改用旧IP的MMDB坐标。"""
        direct = task.get('start_coord')
        if isinstance(direct, (tuple, list)) and len(direct) == 2:
            point = self._qizi_coord({'lat': direct[0], 'lon': direct[1]})
            if point: return point
        point, source = self._qizi_dynamic_last_position(uid)
        if point:
            task['start_coord'] = point
            task['start_source'] = source
            self._qizi_dynamic_save_coordinates(uid, ip=task.get('ip') or '',
                origin=point, source=source,
                target_id=task.get('target_exit_id'), status='已锁定最后游走坐标，等待目标MMDB')
        return point

    def _qizi_dyn_consolidate_duplicates(self):
        """Consolidate *real* same-IP dynamic duplicates with a durable ID alias.

        Never merge a manually configured static exit or unrelated active API
        configurations. Keep an old IP if its address is genuinely different.
        Identity rewrites do not release a network lock or finish a route.
        """
        exits = getattr(self, 'exits', None)
        if not isinstance(exits, list) or not exits:
            return 0
        dynamic = [p for p in exits if (p.get('dynamic_api') or p.get('qizi_dynamic_retired')
                    or str(p.get('dynamic_api_state') or '').startswith('历史出口'))]
        # Build equivalence classes by family+public IP and by identical endpoint+IP.
        parent = {str(p['id']): str(p['id']) for p in dynamic if p.get('id')}
        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        def unite(a, b):
            x, y = find(a), find(b)
            if x != y: parent[y] = x
        by_family, by_endpoint = {}, {}
        for p in dynamic:
            eid = str(p.get('id') or '')
            if eid not in parent: continue
            public = str(p.get('public_ip') or '').strip()
            if not public: continue
            family = str(p.get('qizi_dynamic_family') or '')
            endpoint = (public, str(p.get('host') or '').casefold(),
                        str(p.get('port') or ''), str(p.get('username') or ''),
                        str(p.get('password') or ''))
            if family:
                key = (family, public)
                other = by_family.setdefault(key, eid)
                if other != eid: unite(other, eid)
            other = by_endpoint.setdefault(endpoint, eid)
            if other != eid:
                first = next((x for x in dynamic if str(x.get('id')) == other), None)
                # Do not merge two independently configured active API owners.
                if not (first and first.get('dynamic_api') and p.get('dynamic_api')
                        and first['dynamic_api'] != p['dynamic_api']):
                    unite(other, eid)
        groups = {}
        for p in dynamic:
            eid = str(p.get('id') or '')
            if eid in parent: groups.setdefault(find(eid), []).append(p)
        aliases, victims = {}, []
        for group in groups.values():
            if len(group) < 2: continue
            # Keep the active API owner, otherwise keep the first stable record.
            survivor = next((x for x in group if x.get('dynamic_api')), group[0])
            keep = str(survivor['id'])
            for item in group:
                old = str(item['id'])
                if old == keep: continue
                aliases[old] = keep
                victims.append(item)
                existing = survivor.setdefault('qizi_dynamic_aliases', [])
                for alias in [old] + list(item.get('qizi_dynamic_aliases') or []):
                    if alias not in existing and alias != keep: existing.append(alias)
        # Also repair links of merges already committed to disk before a crash.
        for p in dynamic:
            eid = str(p.get('id') or '')
            for old in p.get('qizi_dynamic_aliases') or []:
                if str(old) != eid and old not in parent:
                    aliases[str(old)] = eid
        if not aliases: return 0
        # Persist aliases with the canonical exit *before* rewriting any device refs.
        if victims:
            prospective = [x for x in exits if x not in victims]
            try: self._qizi_api_saved_exits(prospective)
            except OSError as exc:
                self.log_operation('动态IP重复合并保存失败，保留原记录：'+str(exc)[:120]);return 0
            exits[:] = prospective
        def resolve(value):
            key = str(value or '')
            visited = set()
            while key in aliases and key not in visited:
                visited.add(key); key = aliases[key]
            return key
        def rewrite(mapping, keys):
            changed = False
            for key in keys:
                if mapping.get(key) not in (None, ''):
                    old = str(mapping[key]); new = resolve(old)
                    if new != old:
                        mapping[key] = new;changed = True
            for key in ('old_ids', 'qizi_dynamic_old_ids'):
                if isinstance(mapping.get(key), list):
                    prev = list(mapping[key])
                    mapping[key] = list(dict.fromkeys(resolve(v) for v in prev if v))
                    changed |= prev != mapping[key]
            return changed
        modified_devices = False
        for d in getattr(self, 'devices', {}).values():
            modified_devices |= rewrite(d, ('exit_id', 'qizi_dynamic_source_exit_id',
                                            'qizi_dynamic_follow_exit_id'))
        modified_routes = False
        for state in getattr(self, 'route_states', {}).values():
            modified_routes |= rewrite(state, ('target_exit_id','qizi_dynamic_old_id',
                'qizi_dynamic_original_target_id','qizi_dynamic_latest_target_id'))
        modified_plans = False
        for plan in getattr(self, 'route_plans', {}).values():
            modified_plans |= rewrite(plan, ('target_exit_id',))
        for dictionary in (self._qizi_auto_route_pending, self._qizi_auto_route_active,
                           self._qizi_auto_route_paused):
            for item in dictionary.values():
                rewrite(item, ('old_exit_id','old_id','target_exit_id','target_id',
                               'latest_target_id','verified_target_id'))
        updated_groups = {}
        for old_id, group in self._qizi_auto_route_groups.items():
            key = resolve(old_id)
            rewrite(group, ('target_id',))
            if key in updated_groups:
                updated_groups[key]['uids'].update(group.get('uids') or ())
                updated_groups[key]['completed'].update(group.get('completed') or ())
            else: updated_groups[key] = group
        self._qizi_auto_route_groups = updated_groups
        for name in ('_qizi_api_next', '_qizi_egress_probe_next', '_qizi_api_verified_public'):
            d = getattr(self, name, None)
            if isinstance(d, dict):
                for old, new in aliases.items():
                    if old in d:
                        d.setdefault(new, d[old]); d.pop(old, None)
        for rec in self._qizi_dynamic_coord_snapshots.values():
            rewrite(rec, ('target_exit_id',))
        try:
            if modified_devices: self._qizi_save_devices(self.devices)
            if modified_routes: self._save_route_state()
            if modified_plans: self._save_route_plans()
        except (OSError, AttributeError) as exc:
            self.log_operation('重复合并后关联记录保存异常，后续将按别名修复：'+str(exc)[:120])
        if victims:
            self.log_operation(f'动态IP：已合并 {len(victims)} 条同IP同来源的冗余记录，路线/设备ID已同步迁移')
            if hasattr(self, 'exit_table'): self.refresh_exits()
        return len(victims)

    def _qizi_api_apply_result(self, eid, snapshot, result):
        p = self._qizi_find_exit(eid)
        if not p or p.get('dynamic_api') != snapshot.get('dynamic_api'):
            return  # 检测期间修改或删除了 API 配置
        if not result[0]:
            msg = result[1]
            changed = p.get('dynamic_api_error') != msg
            p['dynamic_api_error'] = msg
            p['dynamic_api_state'] = '检查失败（保留旧出口）'
            if changed:
                self.log_operation(f'动态出口 {str(eid)[:8]}：{msg}；旧代理地址保持不变')
            self._qizi_refresh_usage()
            return
        _, parsed, actual_ip, country = result
        if any(p.get(k) != snapshot.get(k) for k in ('host', 'port', 'username', 'password')):
            return  # 异步检查期间有人手动修改了代理
        endpoint_changed = any(str(p.get(k,'')) != str(parsed.get(k,''))
                               for k in ('host','port','username','password'))
        public_changed = str(p.get('public_ip') or '') != str(actual_ip)
        p['dynamic_api_error'] = ''
        p['dynamic_api_state'] = '已验证'
        p['dynamic_api_last'] = time.strftime('%Y-%m-%d %H:%M:%S')
        # 同一个公网IP只更换代理接入参数，不应生成新UUID/新寻路任务。
        if endpoint_changed and not public_changed:
            updated = dict(p)
            updated.update({k: parsed[k] for k in ('host','port','username','password')})
            try:
                self._qizi_api_saved_exits([updated if x is p else x for x in self.exits])
            except OSError as exc:
                self.log_operation('动态IP接入参数更新保存失败：'+str(exc)[:100]); return
            p.clear(); p.update(updated)
            self._qizi_api_route_change(eid)
            self.log_operation('公网IP未变，仅更新对应代理接入参数；不创建重复出口')
        if not public_changed:
            # 本轮真实HTTPS出口探测成功，健康状态与手动点击检测保持一致。
            p['status'] = '在线'
            p['last_error'] = ''
            self._qizi_api_verified_public = getattr(self, '_qizi_api_verified_public', {})
            self._qizi_api_verified_public[str(eid)] = str(actual_ip)
            self._qizi_arrival_verified(eid, snapshot, actual_ip)
            self._qizi_refresh_usage()
            return

        # 每次轮换产生独立新出口，旧路线与旧代理快照均保持不可变。
        old_public = str(p.get('public_ip') or '')
        old_host = str(p.get('host') or '')
        users = self._qizi_dynamic_users(eid)
        candidate = dict(p)
        candidate['id'] = str(uuid.uuid4())
        candidate.pop('qizi_dynamic_retired', None)
        candidate['type'] = 'http'
        candidate['name'] = '动态 ' + str(parsed['host'])
        candidate.update({k: parsed[k] for k in ('host', 'port', 'username', 'password')})
        candidate.update(public_ip=actual_ip, country=country, status='在线', last_error='',
                         geo_lat='', geo_lon='', geo_source_ip='', geo_status='等待 MMDB',
                         geo_raw_lat='', geo_raw_lon='', dynamic_api_error='',
                         dynamic_api_last=time.strftime('%Y-%m-%d %H:%M:%S'))
        # A public IP may be recycled (123 -> 456 -> 123). Never revive an old
        # retired generation, nor hijack a separately configured manual exit.
        # 同一个动态API的同一公网IP只能保留一条目标记录。
        # 只复用本动态家族或完全相同连接参数的历史记录，绝不借用其他静态出口。
        family = str(p.get('qizi_dynamic_family') or p.get('id') or '')
        candidate['qizi_dynamic_family'] = family
        candidate['city'] = ''  # 新 IP 不能沿用旧 IP 的城市。
        candidate.pop('city_zh', None)
        old_like = [x for x in self.exits if x is not p
                    and str(x.get('public_ip') or '') == str(actual_ip)
                    and (str(x.get('qizi_dynamic_family') or '') == family
                         or (x.get('qizi_dynamic_retired') and
                             all(str(x.get(k) or '') == str(candidate.get(k) or '')
                                 for k in ('host', 'port', 'username', 'password'))))]
        new_p = next((x for x in old_like if str(x.get('qizi_dynamic_family') or '') == family),
                     old_like[0] if old_like else None)
        new_id = str(new_p.get('id')) if new_p else candidate['id']
        old = dict(p)
        old['qizi_dynamic_family'] = family
        old.pop('dynamic_api', None)
        old['dynamic_api_state'] = '历史出口（到达后删除）'
        old['qizi_dynamic_retired'] = True
        old['status'] = '历史出口（停用）'
        if new_p:
            candidate = dict(new_p)
            candidate['qizi_dynamic_family'] = family
            candidate.pop('qizi_dynamic_retired', None)
            candidate['type'] = 'http'
            candidate.update({k:parsed[k] for k in ('host','port','username','password')})
            candidate.update(dynamic_api=p['dynamic_api'], public_ip=actual_ip,
                             country=country, status='在线', last_error='', dynamic_api_error='', dynamic_api_state='已验证',
                             dynamic_api_last=time.strftime('%Y-%m-%d %H:%M:%S'))
            if candidate.get('geo_source_ip') != actual_ip:
                candidate.update(geo_lat='', geo_lon='', geo_raw_lat='', geo_raw_lon='',
                                 geo_source_ip='', geo_status='等待 MMDB')
        else:
            candidate['dynamic_api_state'] = '已验证'
        prospective = [(old if x is p else candidate if x is new_p else x) for x in self.exits]
        if not new_p:
            prospective.append(candidate)
        try:
            self._qizi_api_saved_exits(prospective)
        except OSError as exc:
            self.log_operation(f'动态出口 {str(eid)[:8]}：保存失败，保留旧地址：{exc}')
            return
        p.clear(); p.update(old)
        if new_p:
            new_p.clear(); new_p.update(candidate)
        else:
            self.exits.append(candidate)
        # 修复版本升迁后的同IP陈旧副本（保留一切在途路线身份）。
        self._qizi_dyn_consolidate_duplicates()
        self._qizi_api_next.pop(eid, None)
        self._qizi_api_next[new_id] = time.monotonic() + random.uniform(POLL_MIN_SECONDS, POLL_MAX_SECONDS)
        self._qizi_api_verified_public = getattr(self, '_qizi_api_verified_public', {})
        self._qizi_api_verified_public.pop(str(eid), None)
        self._qizi_api_verified_public[str(new_id)] = str(actual_ip)

        group = self._qizi_auto_route_groups.setdefault(
            str(eid), dict(target_id=new_id, uids=set(), completed=set()))
        group['target_id'] = new_id
        for ip, uid in users:
            if not uid:
                with self.device_lock:
                    d = self.devices.get(ip)
                    if d is not None:
                        d['qizi_dynamic_source_exit_id'] = eid
                        d['qizi_dynamic_follow_exit_id'] = new_id
                self.log_operation(f'{ip}：动态出口变化但UDID暂缺；已保存待关联的新出口，信任后自动寻路')
                self._qizi_dynamic_block(ip, '', '设备未关联iPhone，不能自动寻路；已禁止旧出口网络')
                continue
            group['uids'].add(uid)
            # 跟随出口字段不用于路由，仅供中途断网/重启时找回正确关联。
            with self.device_lock:
                d = self.devices.get(ip)
                if d is not None: d['qizi_dynamic_follow_exit_id'] = new_id
            active = self._qizi_auto_route_active.get(uid)
            paused = self._qizi_auto_route_paused.get(uid)
            pending = self._qizi_auto_route_pending.get(uid)
            if active:
                # 飞行中的终点与轨迹冻结。只记住最后一次验证成功的新出口。
                active['latest_target_id'] = new_id
                active['latest_public_ip'] = actual_ip
                active.setdefault('old_ids', [active.get('old_id')])
                if eid not in active['old_ids']:
                    active['old_ids'].append(eid)
                running = self.route_states.get(uid)
                if running and running.get('qizi_dynamic_route'):
                    running['qizi_dynamic_latest_target_id'] = new_id
                    running['qizi_dynamic_old_ids'] = list(active['old_ids'])
                    self._save_route_state()
                self._qizi_dynamic_save_coordinates(uid, ip=ip, target_id=new_id,
                    target_ip=actual_ip, status='飞行中IP更新，保持原终点，抵达后复核')
                active.pop('verified_target_id', None)
                if active.get('arrived_wait'):
                    self.log_operation(f'{ip}：到达后复核发现 IP 再次变化 → {actual_ip}，等待最新位置')
                else:
                    self.log_operation(f'{ip}：飞行途中出口变为 {actual_ip}；原航线和终点不变，到达后复核')
                continue
            if paused:
                paused['latest_target_id'] = new_id
                paused['latest_public_ip'] = actual_ip
                paused.setdefault('old_ids', [paused.get('old_id')])
                if eid not in paused['old_ids']:
                    paused['old_ids'].append(eid)
                saved = self.route_states.get(uid)
                if saved and saved.get('qizi_dynamic_route'):
                    saved['qizi_dynamic_latest_target_id'] = new_id
                    saved['qizi_dynamic_old_ids'] = list(paused['old_ids'])
                    self._save_route_state()
                self._qizi_dynamic_save_coordinates(uid, ip=ip, target_id=new_id,
                    target_ip=actual_ip, status='手动暂停中，保留原航线')
                self.log_operation(f'{ip}：暂停期间出口变为 {actual_ip}，手动继续仍按原路线走，到达后复核')
                continue
            if pending:
                old_target = pending['target_exit_id']
                pending.setdefault('old_ids', [pending.get('old_exit_id')])
                if eid not in pending['old_ids']:
                    pending['old_ids'].append(eid)
                pending.update(target_exit_id=new_id, target_public_ip=actual_ip,
                               next_try=0, last_wait_reason='')
                self._qizi_dynamic_save_coordinates(uid, ip=ip, target_id=new_id,
                    target_ip=actual_ip, status='规划前再次换IP；起点保持首次冻结坐标')
                self.log_operation(f'{ip}：规划前 IP 再次变化 {old_target[:8]} → {new_id[:8]}，只使用最新目标')
                continue
            origin, origin_source = self._qizi_dynamic_last_position(uid)
            self._qizi_auto_route_pending[uid] = dict(
                ip=ip, target_exit_id=new_id, target_public_ip=actual_ip,
                old_exit_id=eid, old_ids=[eid], created=time.monotonic(), next_try=0, last_wait_reason='',
                old_public_ip=old_public, start_coord=origin,
                start_source=origin_source if origin else '',
                was_allowed=(ip in self._location_network_allowed))
            self._qizi_dynamic_save_coordinates(uid, ip=ip, source=origin_source,
                origin=origin, target_id=new_id, target_ip=actual_ip,
                status='已检测IP变化；等待MMDB定位' if origin else '等待最新游走定位记录')
            if origin:
                self.log_operation(f'{ip}：已冻结最后发送的定位坐标：'
                                   f'{origin[0]:.9f}, {origin[1]:.9f}，来源：{origin_source}')
            else:
                self.log_operation(f'{ip}：尚无最近120秒的实际定位发送记录；'
                                   '不会用旧出口MMDB坐标代替起点，等待设备上报')
            self.log_operation(f'{ip}：检测到公网出口变化 {old_public or "未知"} → {actual_ip}；'
                               '已建立自动寻路任务，等待地理识别和距离判断')
            self._qizi_dynamic_block(ip, uid, '动态出口已变化，暂时禁止网络，待位置协调完成')
        self._qizi_save_devices(self.devices)
        self.refresh_exits()  # 使用原来的 MMDB 队列获取实际公网 IP 对应的经纬度
        self.refresh_devices()  # 即刻更新“未来出口”，不等待下一次健康扫描
        self.log_operation(f'动态出口 {str(eid)[:8]}：{old_host}/{old_public or "未知"} → '
                           f'{candidate["host"]}/{actual_ip}；关联设备 {len(users)} 台')
        # 若此次轮换恰好是到达后发起的核查，允许使用这一轮 HTTPS 验证结果。
        self._qizi_arrival_verified(new_id, snapshot, actual_ip)
        if not users:
            self._qizi_dynamic_cleanup_old(eid)

    def _qizi_arrival_verified(self, verified_id, snapshot, actual_ip):
        """必须使用抵达后 *发起* 的新请求；抵达前在途的轮询结果不能放行。"""
        started = float(snapshot.get('_qizi_poll_started_at') or 0)
        for uid, active in list(self._qizi_auto_route_active.items()):
            if not active.get('arrived_wait') or active.get('latest_target_id') != verified_id:
                continue
            if started < active.get('arrived_at', float('inf')):
                continue
            proxy = self._qizi_find_exit(verified_id)
            if not proxy or str(proxy.get('public_ip') or '') != str(actual_ip):
                continue
            active['verified_target_id'] = verified_id
            self.log_operation(f'{active["ip"]}：到达后 HTTPS 出口复核成功：{actual_ip}；正在判断是否需要再次寻路')

    def _qizi_dynamic_set_near_exit(self, uid, task):
        """同区域更新出口，不重置模拟定位；仅恢复变更前已被允许的网络。"""
        ip, target_id = task['ip'], task['target_exit_id']
        if ip not in self.devices or self.uid_for_ip(ip) != uid:
            return False
        state = self.ios_states.get(uid, {})
        if not state.get('trusted') or state.get('wifi') != '连接中':
            return False
        with self.device_lock:
            if ip not in self.devices: return False
            self.devices[ip]['exit_id'] = target_id
            self.devices[ip].pop('qizi_dynamic_source_exit_id', None)
            self.devices[ip].pop('qizi_dynamic_follow_exit_id', None)
        self._qizi_save_devices(self.devices)
        if task.get('was_allowed') and state.get('location') == '定位中':
            self._location_network_allowed.add(ip)
        self._update_live_routes()
        self._qizi_auto_route_pending.pop(uid, None)
        self._qizi_dynamic_complete_groups(uid, list(task.get('old_ids') or []) +
                                           [str(task.get('old_exit_id') or '')])
        self.refresh_devices(ip)
        self.log_operation(f'{ip}：新旧位置在 {DYNAMIC_NEARBY_KM:g} 公里范围内，'
                           '无需自动寻路；保持现有模拟坐标，仅更新已验证出口')
        return True

    def _qizi_dynamic_complete_groups(self, uid, old_ids=()):
        """Only settle generations whose outgoing leg THIS device actually finished.

        Example: A flies 123->456 while B flies 456->789.  Arrival at 456
        completes A's membership of group 123, *not* group 456.  Group 456
        completes after both A and B have reached 789.  Do not mark every
        group containing a UID completed: it would retire 456 prematurely.
        """
        uid = str(uid)
        for old_id in dict.fromkeys(str(x or '') for x in (old_ids or ())):
            if not old_id:
                continue
            group = self._qizi_auto_route_groups.setdefault(
                old_id, dict(target_id='', uids=set(), completed=set()))
            group['uids'].add(uid)
            group['completed'].add(uid)
        self._qizi_dynamic_prune_retired()

    def _qizi_dynamic_prune_retired(self):
        """Remove retired exits only when absolutely no live reference remains."""
        # The normal cleanup performs all device/route/pending checks. Scanning
        # retired entries as well handles missing group bookkeeping after restart.
        candidates = set(self._qizi_auto_route_groups)
        candidates.update(str(p.get('id')) for p in self.exits
                          if p.get('qizi_dynamic_retired'))
        for eid in candidates:
            self._qizi_dynamic_cleanup_old(eid)
        self._qizi_dyn_consolidate_duplicates()

    def _qizi_auto_route_tick(self):
        """始终用最新目标判断；路线启动之后只等待冻结航线完成。"""
        if getattr(self, '_closing', False): return
        now = time.monotonic()
        # IP 切换时 UDID 可能临时不可用，恢复后补齐待执行任务。
        for ip, dev in list(self.devices.items()):
            if str(dev.get('exit_id') or '') != '__QIZI_BLOCK__': continue
            follow = str(dev.get('qizi_dynamic_follow_exit_id') or '')
            source_id = str(dev.get('qizi_dynamic_source_exit_id') or '')
            uid = str(self.uid_for_ip(ip) or '')
            if (not uid or not follow or not source_id
                or uid in self._qizi_auto_route_pending
                or uid in self._qizi_auto_route_active
                or uid in self._qizi_auto_route_paused): continue
            target = self._qizi_find_exit(follow)
            if not target: continue
            origin, label = self._qizi_dynamic_last_position(uid)
            self._qizi_auto_route_pending[uid] = dict(
                ip=ip, old_exit_id=source_id, old_ids=[source_id],
                target_exit_id=follow, target_public_ip=str(target.get('public_ip') or ''),
                start_coord=origin, start_source=label if origin else '',
                was_allowed=False, next_try=0, created=now, last_wait_reason='')
            group = self._qizi_auto_route_groups.setdefault(
                source_id, dict(target_id=follow, uids=set(), completed=set()))
            group['uids'].add(uid)
            self.log_operation(f'{ip}：UDID已恢复，补建动态公网IP自动寻路；保持禁止网络')
        for uid, active in list(self._qizi_auto_route_active.items()):
            if active.get('arrived_wait') and active.get('verified_target_id'):
                self._qizi_arrival_finish(uid)
        for uid, task in list(self._qizi_auto_route_pending.items()):
            if uid in self._qizi_auto_route_planning or now < task.get('next_try', 0):
                continue
            if self._route_is_running(uid):
                self._qizi_dynamic_log_once(uid, 'in_progress',
                    f'{task["ip"]}：已有寻路执行中，保留原定终点；新 IP 等到达后处理')
                continue
            ip = task['ip']
            if ip not in self.devices or self.uid_for_ip(ip) != uid:
                self._qizi_dynamic_log_once(uid, 'device_missing',
                    f'{ip}：动态IP自动寻路等待：设备/UDID 关联已变化')
                task['next_try'] = now + AUTO_ROUTE_RETRY_SECONDS
                continue
            saved = self.route_states.get(uid, {})
            if saved.get('paused') and not saved.get('running'):
                self._qizi_dynamic_log_once(uid, 'manual_paused',
                    f'{ip}：设备存在手动暂停的路线；不会自动覆盖断点，等待用户继续')
                continue
            state = self.ios_states.get(uid, {})
            if not state.get('trusted') or state.get('wifi') != '连接中':
                self._qizi_dynamic_log_once(uid, 'wifi_wait',
                    f'{ip}：动态IP自动寻路等待：未信任或 Wi-Fi 未连接')
                continue
            target = self._qizi_find_exit(task['target_exit_id'])
            if not target:
                self._qizi_dynamic_log_once(uid, 'target_missing',
                    f'{ip}：目标出口已被移除；任务暂停，网络保持禁止')
                task['next_try'] = now + AUTO_ROUTE_RETRY_SECONDS
                continue
            dest = self._qizi_exit_coord(target)
            if not dest:
                self._qizi_dynamic_log_once(uid, 'geo_wait:'+str(target.get('public_ip')),
                    f'{ip}：自动寻路等待目标公网IP {target.get("public_ip") or "未知"} 的 MMDB 坐标')
                continue
            if str(target.get('status') or '') != '在线':
                self._qizi_dynamic_log_once(uid, 'target_offline:'+str(target.get('public_ip')),
                    f'{ip}：B出口已建立但网络检测尚未通畅；继续等待，不启动寻路')
                continue
            origin = self._qizi_dynamic_origin(uid, task)
            if not origin:
                self._qizi_dynamic_log_once(uid, 'origin_missing',
                    f'{ip}：自动寻路缺少有效起点坐标，等待定位数据；不会改用MMDB旧坐标')
                continue
            self._qizi_dynamic_save_coordinates(uid, ip=ip,
                target_id=task['target_exit_id'], target_ip=target.get('public_ip'),
                dest=dest, status='MMDB目标已配置；准备自动规划')
            distance = self._qizi_distance_km(origin, dest)
            self._qizi_dynamic_log_once(uid, f'distance:{task["target_exit_id"]}',
                f'{ip}：自动寻路已获取目标 {target.get("public_ip")}；'
                f'起点 {origin[0]:.12f},{origin[1]:.12f}（{task.get("start_source") or "冻结坐标"}） → '
                f'终点 {dest[0]:.12f},{dest[1]:.12f}（与手动选择该出口相同的MMDB原始坐标）；'
                f'直线约 {distance:.1f} 公里')
            if not DYNAMIC_FORCE_ROUTE_ON_CHANGE and distance <= DYNAMIC_NEARBY_KM:
                if not self._qizi_dynamic_set_near_exit(uid, task):
                    task['next_try'] = now + AUTO_ROUTE_RETRY_SECONDS
                continue
            plan_saved = self.route_plans.get(uid, {})
            try:
                car = float(plan_saved.get('car_kmh') or 220)
                plane = float(plan_saved.get('plane_kmh') or 958)
                if car <= 0 or plane <= 0: raise ValueError('无效速度')
            except (TypeError, ValueError):
                car, plane = 220.0, 958.0
            mode = plan_saved.get('mode')
            if mode not in ('WiFi模式', 'USB数据线模式'):
                mode = 'WiFi模式'
            self._qizi_auto_route_planning.add(uid)
            task['next_try'] = now + AUTO_ROUTE_RETRY_SECONDS
            target_id = task['target_exit_id']
            self.log_operation(f'{ip}：动态 IP 自动寻路开始规划，已固定本次目标 {target.get("public_ip")}；'
                               f'距离约 {distance:.1f} 公里，规划完成后自动执行；'
                               f'对应出口ID={target_id[:8]}')
            def worker(u=uid, address=ip, start=origin, destination=dest,
                       speed_car=car, speed_plane=plane, route_mode=mode, tid=target_id):
                try:
                    from qizi_route_engine import build_route, precompute_timeline, timeline_to_dict
                    from pathlib import Path
                    import sys
                    root = (Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False)
                            else Path(__file__).resolve().parent)
                    plan = build_route(start, destination, speed_car, speed_plane, root,
                                       logger=lambda msg: self._qizi_api_results.put(('auto_log', address, msg)),
                                       force_flight=True)
                    timeline = timeline_to_dict(precompute_timeline(plan, speed_car, speed_plane, 1.0))
                    if not timeline: raise ValueError('路线为空')
                    data = dict(start_lat=start[0], start_lon=start[1],
                                dest_lat=destination[0], dest_lon=destination[1],
                                car_kmh=speed_car, plane_kmh=speed_plane,
                                mode=route_mode, target_exit_id=tid, timeline=timeline)
                    self._qizi_api_results.put(('auto_plan', u, address, tid, data, None))
                except Exception as exc:
                    self._qizi_api_results.put(('auto_plan', u, address, tid, None, str(exc)))
            threading.Thread(target=worker, name='qizi-api-auto-route-'+uid[-8:], daemon=True).start()
        if now - self._qizi_last_retired_prune >= 12.0:
            self._qizi_last_retired_prune = now
            self._qizi_dynamic_prune_retired()
        self.after(1000, self._qizi_auto_route_tick)

    def _qizi_auto_plan_result(self, uid, ip, target_id, data, error):
        self._qizi_auto_route_planning.discard(uid)
        task = self._qizi_auto_route_pending.get(uid)
        if not task or task['target_exit_id'] != target_id:
            self.log_operation(f'{ip}：规划期间动态 IP 已更新，放弃旧路线；下一轮规划最新目标')
            return
        if error:
            self.log_operation(f'{ip}：动态IP自动寻路规划失败：{error[:200]}；60秒后重试')
            task['next_try'] = time.monotonic() + AUTO_ROUTE_RETRY_SECONDS
            return
        target = self._qizi_find_exit(target_id)
        current_dest = self._qizi_exit_coord(target)
        try:
            planned_dest = self._qizi_coord({'lat': data.get('dest_lat'), 'lon': data.get('dest_lon')})
        except (AttributeError, TypeError):
            planned_dest = None
        if (not target or str(target.get('public_ip') or '') != str(task.get('target_public_ip') or '')
                or not current_dest or not planned_dest
                or any(not math.isclose(a, b, rel_tol=0.0, abs_tol=1e-12)
                       for a, b in zip(current_dest, planned_dest))):
            task['next_try'] = 0
            self.log_operation(f'{ip}：目标出口 IP 或其当前 MMDB 坐标在规划期间发生变化；'
                               '已丢弃该路线，按最新出口重新规划')
            return
        if self._route_is_running(uid) or self.ios_states.get(uid, {}).get('wifi') != '连接中':
            task['next_try'] = time.monotonic() + AUTO_ROUTE_RETRY_SECONDS
            self.log_operation(f'{ip}：规划完成但当前寻路/Wi-Fi 状态变化，等待重新确认')
            return
        previous = self.route_plans.get(uid)
        self.route_plans[uid] = data
        self._save_route_plans()
        self.log_operation(f'{ip}：自动寻路规划成功，共 {len(data["timeline"]):,} 个秒级轨迹点；'
                           f'预计约 {len(data["timeline"])//60} 分钟；正在自动开始执行')
        try:
            self._start_route(ip, uid, data.get('mode', 'WiFi模式'), target_exit_id=target_id)
        except Exception as exc:
            self.log_operation(f'{ip}：自动寻路启动异常：{type(exc).__name__}: {str(exc)[:120]}')
        if self._route_is_running(uid):
            # 确认调用到的是“开始执行”入口而不是“规划路线”回调。
            st = self.route_states.get(uid, {})
            if (st.get('target_exit_id') != target_id or
                    st.get('ip') not in ('', None, ip)):
                self.log_operation(f'{ip}：启动后路线状态不一致，禁止网络；'
                                   f'预计出口 {target_id[:8]}，实际 {str(st.get("target_exit_id"))[:8]}')
                task['next_try'] = time.monotonic() + AUTO_ROUTE_RETRY_SECONDS
                return
            self._qizi_auto_route_pending.pop(uid, None)
            self._qizi_auto_route_active[uid] = dict(
                target_id=target_id, latest_target_id=target_id,
                old_id=task['old_exit_id'], old_ids=list(task.get('old_ids') or [task['old_exit_id']]),
                ip=ip, frozen_dest=(data['dest_lat'], data['dest_lon']),
                was_allowed=task.get('was_allowed', False))
            st['qizi_dynamic_route'] = True
            st['qizi_dynamic_old_id'] = task['old_exit_id']
            st['qizi_dynamic_old_ids'] = list(task.get('old_ids') or [task['old_exit_id']])
            st['qizi_dynamic_original_target_id'] = target_id
            st['qizi_dynamic_latest_target_id'] = target_id
            st['qizi_dynamic_frozen_dest'] = [data['dest_lat'], data['dest_lon']]
            st['qizi_dynamic_was_allowed'] = bool(task.get('was_allowed', False))
            self._save_route_state()
            self._qizi_dynamic_save_coordinates(uid, ip=ip, target_id=target_id,
                dest=(data['dest_lat'], data['dest_lon']), status='自动寻路执行中')
            self._qizi_dynamic_block(ip, uid, '动态IP自动寻路已开始，网络保持禁止直至抵达并完成复核')
            self.log_operation(f'{ip}：动态IP自动寻路【执行中】；目的地已冻结，途中 IP 变化不会重规划')
            # A不再实际使用，但保留其配置到达B并完成HTTPS验证后才删除。
            self.log_operation(f'{ip}：旧出口A {str(task.get("old_exit_id") or "")[:8]} 已停用；'
                               '抵达B并复核成功后自动删除')
            self.refresh_devices(ip)
            self.after(6500, lambda u=str(uid), addr=str(ip):
                       self._qizi_confirm_first_route_point(u, addr, retry=0))
        else:
            if previous is None: self.route_plans.pop(uid, None)
            else: self.route_plans[uid] = previous
            self._save_route_plans()
            task['next_try'] = time.monotonic() + AUTO_ROUTE_RETRY_SECONDS
            self.log_operation(f'{ip}：自动寻路启动条件不足，原手动路线已恢复，60秒后重试')

    def _qizi_confirm_first_route_point(self, uid, ip, retry=0):
        """真实执行确认：不能用“规划成功”或 running=True 冒充发送成功。"""
        active = self._qizi_auto_route_active.get(uid)
        if not active or active.get('ip') != ip or active.get('arrived_wait'):
            return
        state = self.route_states.get(uid, {})
        if not state.get('running'):
            self.log_operation(f'{ip}：动态IP【执行中断】路线状态已停止；维持禁止网络')
            self._qizi_dynamic_block(ip, uid, '路线未正常执行，仍禁止新出口网络')
            return
        index = int(state.get('next_index') or 0)
        if index > 0:
            if not active.get('first_point_confirmed'):
                active['first_point_confirmed'] = True
                self.log_operation(f'{ip}：动态IP【首个坐标已成功加入定位发送队列】'
                                   f'第 {index} / {len((self.route_plans.get(uid) or {}).get("timeline") or [])} 点；'
                                   f'当前 {state.get("last_lat")},{state.get("last_lon")}')
            self.refresh_devices(ip)
            return
        reason = str(state.get('waiting_reason') or '')
        runtime = self.route_runtime.get(uid, {}) if hasattr(self, 'route_runtime') else {}
        self.log_operation(f'{ip}：动态IP【尚未发送首个坐标】'
                           f'等待停止={bool(runtime.get("waiting_stop"))}，'
                           f'等待原因={reason or "无"}，重试 {retry+1}/3；网络保持禁止')
        if retry >= 2:
            return  # 不接管旧 iOS worker，不伪造成功。
        if self.ios_states.get(uid, {}).get('wifi') == '连接中':
            if not runtime.get('job') and hasattr(self, '_schedule_route_tick'):
                self._schedule_route_tick(uid, 150)
        self.after(6500, lambda u=uid, addr=ip, n=retry+1:
                   self._qizi_confirm_first_route_point(u, addr, retry=n))

    def _qizi_dynamic_block(self, ip, uid, reason):
        """只禁止受影响设备网络，原有 Wi-Fi、模拟定位进程不受影响。"""
        if ip not in self.devices: return
        gateway = getattr(self, 'persistent_gateway', None)
        if gateway is not None:
            gateway.revoke_ip(ip)
        self._location_network_allowed.discard(ip)
        # 保留原网络定位起始时间/秒数，不能修改 iOS 定位或路线 60 秒计时。
        with self.device_lock:
            if ip in self.devices:
                device = self.devices[ip]
                current = str(device.get('exit_id') or '')
                if current and current not in ('__QIZI_BLOCK__', '__QIZI_LOCAL__'):
                    device.setdefault('qizi_dynamic_source_exit_id', current)
                device['exit_id'] = '__QIZI_BLOCK__'
        self._qizi_save_devices(self.devices)
        self._update_live_routes()
        self.log_operation(f'{ip}：{reason}')

    def _qizi_dynamic_cleanup_old(self, old_id):
        """Retire only a generation with no device, queued leg or route marker.

        Group bookkeeping is deliberately in addition to checks on actual
        references; group completion alone must never delete a generation
        while a second device is using it or in flight toward/from it.
        """
        old_id = str(old_id or '')
        old = self._qizi_find_exit(old_id)
        if old is None:
            self._qizi_auto_route_groups.pop(old_id, None)
            return
        if old.get('dynamic_api'):
            return  # the live generation owns the API poller
        if not old.get('qizi_dynamic_retired') and not str(old.get('status') or '').startswith('历史出口'):
            return  # never delete a manually managed exit
        if any(str(d.get(k) or '') == old_id
               for d in self.devices.values()
               for k in ('exit_id', 'qizi_dynamic_source_exit_id',
                         'qizi_dynamic_follow_exit_id')):
            return
        for task in (list(self._qizi_auto_route_pending.values()) +
                     list(self._qizi_auto_route_active.values()) +
                     list(self._qizi_auto_route_paused.values())):
            if any(str(task.get(k) or '') == old_id for k in
                   ('old_exit_id', 'old_id', 'target_exit_id', 'target_id', 'latest_target_id')):
                return
            if old_id in {str(x) for x in task.get('old_ids', ()) if x}:
                return
        for state in self.route_states.values():
            if not (state.get('running') or state.get('paused')):
                continue
            if any(str(state.get(k) or '') == old_id for k in
                   ('target_exit_id', 'qizi_dynamic_old_id',
                    'qizi_dynamic_original_target_id', 'qizi_dynamic_latest_target_id')):
                return
            if old_id in {str(x) for x in state.get('qizi_dynamic_old_ids', ()) if x}:
                return
        group = self._qizi_auto_route_groups.get(old_id)
        if group and not group['uids'].issubset(group['completed']):
            return  # some device has not yet completed THIS generation's leg
        try:
            self._qizi_api_saved_exits([p for p in self.exits if p is not old])
        except OSError as exc:
            self.log_operation(f'历史出口删除失败：{exc}')
            return
        self.exits.remove(old)
        self._qizi_auto_route_groups.pop(old_id, None)
        getattr(self, '_qizi_api_next', {}).pop(old_id, None)
        getattr(self, '_qizi_egress_probe_next', {}).pop(old_id, None)
        self.refresh_exits()
        self.log_operation(f'动态IP历史出口 {str(old.get("public_ip") or old_id)}：'
                           '所有设备已完成该段寻路且无引用，已自动删除')

    def _finish_route(self, uid):
        """到达目的地只触发复核；复核成功才调用原引擎完成方法。"""
        uid = str(uid)
        active = self._qizi_auto_route_active.get(uid)
        if active is None:
            # A manually started route may have selected this dynamic IP as its
            # FUTURE exit. If that IP rotates while the manual route is flying,
            # finish the original route at its frozen end point, but NEVER
            # switch the network to an already retired generation.
            task = self._qizi_auto_route_pending.get(uid)
            state = self.route_states.get(uid, {})
            old_target = str(state.get('target_exit_id') or '')
            old_proxy = self._qizi_find_exit(old_target) if old_target else None
            if (task and old_proxy and old_proxy.get('qizi_dynamic_retired')
                    and old_target != str(task.get('target_exit_id') or '')):
                plan = self.route_plans.get(uid, {})
                reached = (self._qizi_coord({'lat': state.get('last_lat'), 'lon': state.get('last_lon')})
                           or self._qizi_coord({'lat': plan.get('dest_lat'), 'lon': plan.get('dest_lon')}))
                state['target_exit_id'] = '__QIZI_BLOCK__'
                result = super()._finish_route(uid)
                if reached:
                    task['start_coord'] = reached
                    task['start_source'] = '已完成的原手动航线终点'
                task['old_exit_id'] = old_target
                task['old_ids'] = list(dict.fromkeys(
                    [old_target] + [str(x) for x in task.get('old_ids', ()) if x]))
                task['next_try'] = 0
                with self.device_lock:
                    d = self.devices.get(task['ip'])
                    if d is not None:
                        d['qizi_dynamic_source_exit_id'] = old_target
                        d['qizi_dynamic_follow_exit_id'] = task['target_exit_id']
                self._qizi_save_devices(self.devices)
                self._qizi_dynamic_block(task['ip'], uid,
                    '原手动航线已抵达旧未来出口；旧IP已退役，改从抵达坐标自动寻路到最新IP')
                return result
            return super()._finish_route(uid)
        if not active.get('arrived_wait'):
            active['arrived_wait'] = True
            active['arrived_at'] = time.monotonic()
            active.pop('verified_target_id', None)
            # A->B is a fully completed positional leg even when B->C is
            # already pending. Settle A and release it immediately at B,
            # independently of whether the new C proxy can be rechecked now.
            # C remains BLOCKED until the next actual flight arrives and its
            # post-arrival HTTPS verification succeeds.
            if str(active.get('latest_target_id') or '') != str(active.get('target_id') or ''):
                self.log_operation(f'{active["ip"]}：本段已到达冻结目的地；'
                                   '即刻释放上一代出口并从当前位置准备下一段寻路')
                self._qizi_arrival_finish(uid, settle_leg_only=True)
                if uid not in self._qizi_auto_route_active:
                    return None
            self._qizi_dynamic_save_coordinates(uid, ip=active['ip'], status='到达原终点，正在复核最新IP')
            self.log_operation(f'{active["ip"]}：已到达本次固定目的地；暂停出口放行，'
                               '正在重新请求动态 API 并验证最新公网 IP')
            latest = self._qizi_find_exit(active.get('latest_target_id'))
            if latest and latest.get('dynamic_api'):
                self._qizi_api_next[latest['id']] = 0
            elif latest and latest.get('public_ip'):
                # A non-API target was created from a successful proxy health probe.
                # Later health-probe changes replace latest_target_id while the route
                # is running, so the current target is the newest verified egress.
                active['verified_target_id'] = str(latest['id'])
                self.log_operation(f'{active["ip"]}：到达后采用最近健康检测确认的公网 IP '
                                   f'{latest["public_ip"]}；正在判断是否需要再次寻路')
            else:
                self.log_operation(f'{active["ip"]}：找不到有效动态 API，网络继续禁止，等待修复')
        return None

    def _qizi_arrival_finish(self, uid, settle_leg_only=False):
        """Settle one frozen route leg, then either release or queue next hop.

        An IP change during flight never teleports the current route. Arrival
        at its frozen destination releases only the *previous* generations;
        the frozen destination becomes the origin for the next flight.
        """
        uid = str(uid)
        active = self._qizi_auto_route_active.get(uid)
        if not active or not active.get('arrived_wait') or active.get('finishing'):
            return
        if settle_leg_only:
            # This is *not* HTTPS verification of the latest generation. No
            # network is released at an intermediate waypoint.
            target_id = str(active.get('latest_target_id') or '')
            if target_id == str(active.get('target_id') or ''):
                return
        else:
            target_id = str(active.get('verified_target_id') or '')
            if not target_id or target_id != str(active.get('latest_target_id') or ''):
                return
        proxy = self._qizi_find_exit(target_id)
        if not proxy or not proxy.get('public_ip'):
            return
        origin = self._qizi_coord({'lat': active.get('frozen_dest', (None,None))[0],
                                   'lon': active.get('frozen_dest', (None,None))[1]}) if active.get('frozen_dest') else None
        if not origin:
            return
        dest = self._qizi_exit_coord(proxy)
        # If the *newer* generation has not yet acquired MMDB coordinates,
        # this leg can still end at its already-frozen destination.  Keep the
        # next leg pending/blocked until the geo lookup succeeds.
        same_generation = target_id == str(active.get('target_id') or '')
        if dest is None and same_generation:
            self._qizi_dynamic_log_once(uid, 'arrival_geo:'+target_id,
                f'{active["ip"]}：终点出口仍缺少已核实地理坐标，保持禁止网络')
            return
        distance = self._qizi_distance_km(origin, dest) if dest else None
        # Once the public IP really rotated, a nearby city / MMDB point must
        # not suppress the requested second flight. Only an identical point
        # can be completed without movement after fresh verification.
        near = (not settle_leg_only and
                (same_generation or (distance is not None and distance <= 1e-9)))
        ip = active['ip']
        active['finishing'] = True
        state = self.route_states.get(uid)
        if not isinstance(state, dict):
            active['finishing'] = False
            self.log_operation(f'{ip}：无法读取寻路状态；保留禁止网络，等待人工检查')
            return
        old_ids = list(dict.fromkeys(str(x) for x in
            list(active.get('old_ids') or []) + [active.get('old_id')] if x))
        current_leg_destination_id = str(active.get('target_id') or '')
        # A -> B, during flight B -> C: only A is completed on arrival at B.
        # The previous generation marker belongs to this leg, while B itself
        # remains reserved as the source of the future B -> C leg.
        reached_index = old_ids.index(current_leg_destination_id) if current_leg_destination_id in old_ids else len(old_ids)
        finished_generations = old_ids[:reached_index]
        remaining_generations = old_ids[reached_index:]
        if not near and current_leg_destination_id not in remaining_generations:
            remaining_generations.insert(0, current_leg_destination_id)
        # Before calling the original stop, choose its network target.
        state['target_exit_id'] = target_id if near else '__QIZI_BLOCK__'
        self.log_operation(f'{ip}：本段已到达 {current_leg_destination_id[:8]}；'
                           f'最新出口 {proxy.get("public_ip")}，距离'
                           f'{(f"约 {distance:.1f} 公里" if distance is not None else "等待MMDB坐标")}；'
                           + ('本次全部到达' if near else '随后启动下一段寻路'))
        try:
            super()._finish_route(uid)
        except Exception as exc:
            active['finishing'] = False
            self.log_operation(f'{ip}：本段路线收尾失败：{type(exc).__name__}: {str(exc)[:150]}')
            return
        self._qizi_auto_route_active.pop(uid, None)
        if near:
            # The latest verified IP is now reached. No more outstanding leg
            # for this UID: remove all historical references before pruning.
            with self.device_lock:
                d = self.devices.get(ip)
                if d is not None:
                    d.pop('qizi_dynamic_source_exit_id', None)
                    d.pop('qizi_dynamic_follow_exit_id', None)
            self._qizi_save_devices(self.devices)
            self._qizi_dynamic_complete_groups(uid, old_ids)
            self._qizi_dynamic_save_coordinates(uid, ip=ip, target_id=target_id,
                target_ip=proxy.get('public_ip'), dest=dest or origin, status='自动寻路全部完成')
            self.log_operation(f'{ip}：动态IP自动寻路【全部完成】，已绑定核实后的最新出口')
        else:
            # Save the new source/follow IDs *before* pruning retired exits.
            # Previous code kept setdefault(source=A) even after arrival at B,
            # so A could never be deleted until reaching C (or a restart).
            self._qizi_auto_route_pending[uid] = dict(
                ip=ip, old_exit_id=current_leg_destination_id,
                old_ids=remaining_generations,
                old_public_ip='', target_exit_id=target_id,
                target_public_ip=str(proxy.get('public_ip') or ''),
                start_coord=origin, start_source='前一段实际到达终点',
                created=time.monotonic(), next_try=0, last_wait_reason='',
                was_allowed=active.get('was_allowed', False))
            with self.device_lock:
                d = self.devices.get(ip)
                if d is not None:
                    d['qizi_dynamic_source_exit_id'] = current_leg_destination_id
                    d['qizi_dynamic_follow_exit_id'] = target_id
            self._qizi_save_devices(self.devices)
            self._qizi_dynamic_block(ip, uid, '已到达本段目的地，按最新动态IP准备下一段寻路')
            self._qizi_dynamic_save_coordinates(uid, ip=ip, origin=origin,
                source='前一段实际到达终点', target_id=target_id,
                target_ip=proxy.get('public_ip'), dest=dest, status='等待下一段自动寻路')
            self._qizi_dynamic_complete_groups(uid, finished_generations)
            self.log_operation(f'{ip}：下一段从当前到达坐标出发，'
                               f'目的地 {proxy.get("public_ip")}；'
                               '上一段历史IP若已无人引用，会立即回收')

    def _start_route(self, ip, uid, mode, status_var=None, resume=False, target_exit_id=None):
        """手动恢复动态IP航线仍追踪到达复核，不影响普通手动寻路。"""
        uid = str(uid)
        paused = self._qizi_auto_route_paused.get(uid)
        result = super()._start_route(
            ip, uid, mode, status_var, resume=resume, target_exit_id=target_exit_id)
        if paused and self._route_is_running(uid):
            self._qizi_auto_route_paused.pop(uid, None)
            paused.pop('arrived_wait', None)
            paused.pop('arrived_at', None)
            paused.pop('verified_target_id', None)
            paused.pop('finishing', None)
            self._qizi_auto_route_active[uid] = paused
            self.log_operation(f'{ip}：继续此前暂停的动态IP航线，终点仍然冻结，到达后重新核对 IP')
            self._qizi_dynamic_block(ip, uid, '动态IP路线恢复执行，继续保持网络禁止')
        return result

    def _stop_route(self, uid, manual=True, status_var=None):
        """手动停止时保留原有断点机制，但不提前使用未到达的动态出口。"""
        uid = str(uid)
        active = self._qizi_auto_route_active.get(uid)
        if active and manual:
            state = self.route_states.get(uid)
            if isinstance(state, dict):
                state['target_exit_id'] = '__QIZI_BLOCK__'
                self.log_operation(f'{active["ip"]}：动态IP自动寻路手动停止，已禁止网络；不会自动重新规划')
        result = super()._stop_route(uid, manual=manual, status_var=status_var)
        if active and manual:
            self._qizi_auto_route_active.pop(uid, None)
            self._qizi_auto_route_paused[uid] = active
            self._qizi_dynamic_block(active['ip'], uid, '模拟寻路中途停止，保留原有暂停/断点机制')
        return result

    def _qizi_api_route_change(self, eid):
        """仅撤销使用此出口的旧代理隧道；再发布新路由。"""
        gateway = getattr(self, 'persistent_gateway', None)
        if gateway is not None:
            users = [ip for ip, d in list(self.devices.items()) if d.get('exit_id') == eid]
            for ip in users:
                gateway.revoke_ip(ip)
        self._update_live_routes()
