# -*- coding: utf-8 -*-
"""v3.10.21: 明确区分暂停（续飞）与停止（结束任务）。

置于 QiziReliabilityMixin 外层；不改 USB/Wi-Fi/DVT 工作线程和动态IP探测。
暂停利用旧 iOS 工作者的 op=hold，每秒持续重发同一坐标；程序重启
后先恢复 hold，再自动从记录的 next_index 继续未完成的航线。
停止彻底清理路线及动态后续任务，但把最后一次位置独立持久化。
"""
from __future__ import annotations

import time
from pathlib import Path
from tkinter import ttk

from qizi_route_engine import load_json_dict, save_route_plans
from qizi_geo_engine import exit_coordinates


class QiziRouteControlMixin:
    def __init__(self, *args, **kwargs):
        self._qizi_stop_hold_file = None
        self._qizi_stopped_positions = {}
        self._qizi_resume_after_boot = set()
        self._qizi_hold_issued = {}
        self._qizi_resume_boot_scheduled = set()
        super().__init__(*args, **kwargs)
        self._qizi_stop_hold_file = Path(self._qizi_live_positions_path).parent / 'qizi_stopped_route_positions.json'
        self._qizi_stopped_positions = load_json_dict(self._qizi_stop_hold_file)
        self._qizi_resume_after_boot = {
            str(uid) for uid, state in self.route_states.items()
            if isinstance(state, dict) and state.get('paused') and state.get('qizi_pause_hold')
            and (self.route_plans.get(str(uid)) or {}).get('timeline')
        }
        # Already-finished routes are not resumed. Their GPS hold is persisted
        # separately from the route_state used by the existing resume engine.
        for uid, data in list(self._qizi_stopped_positions.items()):
            pos = self._qizi_safe_point(data.get('lat'), data.get('lon')) if isinstance(data, dict) else None
            if pos and not self.route_states.get(uid, {}).get('running'):
                self._qizi_route_hold[uid] = pos
                self._qizi_write_route_coordinate(uid, pos, save=False)
        self.after(1700, self._qizi_hold_pulse)
        self.log_operation('V3.10.21 暂停/停止已独立：暂停原地持续发送，重启自动续飞；停止彻底结束路线并保持最后位置')

    def _qizi_safe_point(self, lat, lon):
        return self._qizi_valid_route_point(lat, lon)

    def _qizi_save_stopped_positions(self):
        if self._qizi_stop_hold_file is not None:
            save_route_plans(self._qizi_stop_hold_file, self._qizi_stopped_positions)

    def _qizi_send_fixed_hold(self, uid, pos):
        """One op=hold arms the existing worker's own 1Hz repeated DVT updates."""
        uid = str(uid)
        if self.ios_states.get(uid, {}).get('wifi') != '连接中':
            return False
        if self.ios.send(uid, dict(op='hold', lat=pos[0], lon=pos[1], interval=1)):
            self.ios_states.setdefault(uid, {})['location'] = '准备发送'
            worker = getattr(self.ios, 'workers', {}).get(uid)
            generation = self.ios_states.get(uid, {}).get('generation')
            self._qizi_hold_issued[uid] = (id(worker), generation, pos)
            return True
        return False

    def _qizi_pause_route(self, uid, status_var=None):
        uid = str(uid)
        state = self.route_states.get(uid, {})
        timeline = (self.route_plans.get(uid) or {}).get('timeline') or []
        if state.get('paused') and state.get('qizi_pause_hold'):
            if status_var is not None:
                status_var.set('当前已经暂停；最后一次坐标仍每秒保持发送。')
            return True
        if not state.get('running') or not timeline:
            if status_var is not None:
                status_var.set('当前没有正在飞行的路线，无法暂停。')
            return False
        ip = str(state.get('ip') or self._ip_for_uid(uid) or '')
        pos = self._qizi_route_position(uid, prefer_ack=True)
        if pos is None:
            if status_var is not None:
                status_var.set('尚无成功发送的航线坐标，暂不暂停。')
            return False
        # Stop the *route timer*, not the DVT position sender. The last position
        # is immediately armed as a stationary hold, with no next route point.
        runtime = self.route_runtime.setdefault(uid, {})
        job = runtime.pop('job', None)
        if job is not None:
            try: self.after_cancel(job)
            except Exception: pass
        sender = runtime.pop('usb_sender', None)
        if sender is not None:
            try: sender.stop()
            except Exception: pass
        confirmed_index = state.get('last_confirmed_index')
        if confirmed_index is not None and self._qizi_recent_route_ack(uid):
            state['next_index'] = max(0, min(int(state.get('next_index', 0)), int(confirmed_index)))
        # Dynamic routes must remain blocked until the actual final verification.
        try:
            self._route_force_network_block(uid, state)
        except Exception as exc:
            self.log_operation(f'{ip}：暂停时网络保护检查失败：{type(exc).__name__}')
        state.update(running=False, paused=True, qizi_pause_hold=True,
                     last_lat=pos[0], last_lon=pos[1],
                     waiting_reason='用户已暂停；每秒保持最后定位，重启后自动继续',
                     updated=time.time())
        self._qizi_route_hold[uid] = pos
        self._qizi_write_route_coordinate(uid, pos, save=True)
        self._qizi_store_live_coordinate(uid, *pos, 'route_hold', False)
        self._qizi_set_intent(uid, True)
        self._save_route_state()
        self._qizi_save_checkpoint(uid, force=True)
        self._qizi_send_fixed_hold(uid, pos)
        # Do NOT place this in _qizi_resume_after_boot; in the CURRENT process,
        # paused means paused until operator presses Continue. The next process
        # populates _qizi_resume_after_boot from the durable paused state.
        if status_var is not None:
            status_var.set('已暂停：最后一次坐标持续发送；点击继续飞行，或下次启动自动续飞。')
        self.log_operation(f'{ip}：已暂停寻路，冻结坐标 {pos[0]:.10f},{pos[1]:.10f}，下一点尚未发送')
        self.refresh_devices(ip)
        return True

    def _start_route(self, ip, uid, mode, status_var=None, resume=False, target_exit_id=None):
        if self.route_states.get(str(uid), {}).get('qizi_pause_hold') and self.route_states.get(str(uid), {}).get('paused'):
            # Both the row's Continue button and the original dialog's Start
            # resume without issuing the old pause-as-STOP command.
            resume = True
        result = super()._start_route(ip, uid, mode, status_var,
                                     resume=resume, target_exit_id=target_exit_id)
        uid = str(uid)
        if self.route_states.get(uid, {}).get('running'):
            # New flight supersedes any independently saved STOP-position hold.
            if uid in self._qizi_stopped_positions:
                self._qizi_stopped_positions.pop(uid, None)
                self._qizi_save_stopped_positions()
            self._qizi_hold_issued.pop(uid, None)
            self._qizi_route_hold.pop(uid, None)
        return result

    def _qizi_resume_paused(self, uid, status_var=None):
        uid = str(uid)
        state = self.route_states.get(uid, {})
        if not (state.get('paused') and (self.route_plans.get(uid) or {}).get('timeline')):
            return False
        ip = str(state.get('ip') or self._ip_for_uid(uid) or '')
        ios_state = self.ios_states.get(uid, {})
        if not ip or not ios_state.get('trusted') or ios_state.get('wifi') != '连接中':
            if status_var is not None:
                status_var.set('等待设备重新连接并完成信任后继续飞行；期间坐标记录不会丢失。')
            return False
        # Existing RouteUi / Dynamic MRO restores the exact frozen destination,
        # the remaining index, and dynamic IP generation bookkeeping.
        self._qizi_resume_boot_scheduled.discard(uid)
        self._start_route(ip, uid, state.get('mode', 'WiFi模式'), status_var,
                          resume=True, target_exit_id=state.get('target_exit_id'))
        if self.route_states.get(uid, {}).get('running'):
            self._qizi_resume_after_boot.discard(uid)
            self._qizi_hold_issued.pop(uid, None)
            self._qizi_route_hold.pop(uid, None)
            self.log_operation(f'{ip}：已从暂停检查点继续飞行')
            return True
        return False

    def _qizi_hold_pulse(self):
        if getattr(self, '_closing', False):
            return
        for uid, state in list(getattr(self, 'route_states', {}).items()):
            if state.get('paused') and state.get('qizi_pause_hold'):
                self._qizi_check_hold(uid, self._qizi_route_position(uid))
        for uid, saved in list(self._qizi_stopped_positions.items()):
            if uid in self.route_states and (self.route_states[uid].get('running') or self.route_states[uid].get('paused')):
                continue
            pos = self._qizi_safe_point(saved.get('lat'), saved.get('lon')) if isinstance(saved, dict) else None
            self._qizi_check_hold(uid, pos)
        self.after(2500, self._qizi_hold_pulse)

    def _qizi_check_hold(self, uid, pos):
        uid = str(uid)
        if pos is None or self.ios_states.get(uid, {}).get('wifi') != '连接中':
            self._qizi_hold_issued.pop(uid, None)
            return
        if self._qizi_location_intent.get(uid) is False:
            return  # Explicit restore-real-location overrides any recorded hold.
        worker = getattr(self.ios, 'workers', {}).get(uid)
        token = (id(worker), self.ios_states.get(uid, {}).get('generation'), pos)
        old = self._qizi_hold_issued.get(uid)
        if old != token:
            self._qizi_send_fixed_hold(uid, pos)
        # The Wi-Fi worker repeats hold at 1Hz on its own; no per-tick GUI send.

    def _resume_saved_routes(self):
        # The original implementation schedules its next call every 2 seconds.
        # Convert ONLY pauses discovered at boot into a two-phase hold + resume.
        for uid in list(getattr(self, '_qizi_resume_after_boot', ())):
            st = self.route_states.get(uid, {})
            if not st.get('qizi_pause_hold') or not st.get('paused') or not (self.route_plans.get(uid) or {}).get('timeline'):
                self._qizi_resume_after_boot.discard(uid)
                continue
            if uid in self._qizi_resume_boot_scheduled:
                continue
            ios = self.ios_states.get(uid, {})
            if ios.get('wifi') != '连接中' or not ios.get('trusted'):
                continue
            pos = self._qizi_route_position(uid)
            if pos is None:
                continue
            if self._qizi_send_fixed_hold(uid, pos):
                self._qizi_resume_boot_scheduled.add(uid)
                self.after(1300, lambda u=uid: self._qizi_resume_boot_worker(u))
        return super()._resume_saved_routes()

    def _qizi_resume_boot_worker(self, uid):
        self._qizi_resume_boot_scheduled.discard(uid)
        if uid not in self._qizi_resume_after_boot:
            return
        self._qizi_resume_paused(uid)

    def _stop_route(self, uid, manual=True, status_var=None):
        # Finish-route / automatic waypoint handoff MUST preserve the existing
        # dynamic multi-generation arrival and verification mechanism.
        if not manual:
            return super()._stop_route(uid, manual=False, status_var=status_var)
        uid = str(uid)
        state = self.route_states.get(uid, {})
        ip = str(state.get('ip') or self._ip_for_uid(uid) or '')
        was_route = bool(state.get('running') or state.get('paused'))
        was_pending = uid in getattr(self, '_qizi_auto_route_pending', {})
        pos = self._qizi_route_position(uid, prefer_ack=True) if was_route else None
        self._qizi_resume_after_boot.discard(uid)
        self._qizi_resume_boot_scheduled.discard(uid)
        self._qizi_hold_issued.pop(uid, None)
        if state.get('running'):
            # Stopping half way must never connect the unvisited FUTURE exit.
            state['target_exit_id'] = '__QIZI_BLOCK__'
            super()._stop_route(uid, manual=True, status_var=status_var)
        elif not was_route:
            # The dynamic layer knows how to cancel unplanned/phantom tasks.
            super()._stop_route(uid, manual=True, status_var=status_var)
        # Explicit STOP means no paused route, no future/resume reservation.
        self._qizi_auto_route_active.pop(uid, None)
        self._qizi_auto_route_paused.pop(uid, None)
        if uid in self._qizi_auto_route_pending:
            self._qizi_cancel_pending(uid, '用户停止，取消所有后续寻路', prune=False)
        self._qizi_clear_stale_device_links(ip)
        for eid, group in list(self._qizi_auto_route_groups.items()):
            group['uids'].discard(uid)
            group['completed'].discard(uid)
            if not group['uids']:
                self._qizi_auto_route_groups.pop(eid, None)
        self._qizi_discard_route_checkpoint(uid)  # also removes saved route plan
        if pos is not None:
            self._qizi_stopped_positions[uid] = {'lat': str(pos[0]), 'lon': str(pos[1]), 'updated': time.time()}
            self._qizi_save_stopped_positions()
            self._qizi_route_hold[uid] = pos
            self._qizi_write_route_coordinate(uid, pos, save=True)
            self._qizi_store_live_coordinate(uid, *pos, 'route_hold', False)
            self._qizi_set_intent(uid, True)
            self._qizi_send_fixed_hold(uid, pos)
            self._qizi_sync_panel_coordinates(uid)
            self.log_operation(f'{ip}：已停止并结束整个寻路任务；当前定位固定在 {pos[0]:.10f},{pos[1]:.10f}，不会自动续飞')
        else:
            self.log_operation(f'{ip or uid}：已停止/取消寻路，没有有效已发送坐标可保留')
        self._qizi_dynamic_prune_retired()
        if status_var is not None:
            status_var.set('任务已结束（不再续飞）；最后定位已保留，换出口后改用新出口坐标。')
        self.refresh_devices(ip or None)
        return bool(was_route or was_pending)

    def _maybe_auto_start_location(self, ip, uid=None, reason='Wi-Fi 已连接'):
        uid = str(uid or self.uid_for_ip(ip) or '')
        saved = self._qizi_stopped_positions.get(uid)
        if saved and not self.route_states.get(uid, {}).get('running') and not self.route_states.get(uid, {}).get('paused'):
            if self._qizi_location_intent.get(uid) is False:
                return False
            point = self._qizi_safe_point(saved.get('lat'), saved.get('lon'))
            if point and self._qizi_send_fixed_hold(uid, point):
                self._qizi_route_hold[uid] = point
                return True
            return False
        return super()._maybe_auto_start_location(ip, uid, reason)

    def _qizi_apply_exit_coordinates(self, ip, eid, reason='出口切换'):
        """For a STOPPED device, replace the fixed point with verified GeoIP.

        Do not call the ordinary 'start' walk operation: STOP means a stationary
        hold, even after the operator changes to another exit. This hook also
        handles a GeoIP result that arrives *later* than the combobox choice.
        """
        uid = str(self.uid_for_ip(ip) or '')
        proxy = self._qizi_find_exit(eid) if eid else None
        point_text = exit_coordinates(proxy) if proxy else None
        st = self.route_states.get(uid, {})
        if (uid in self._qizi_stopped_positions and point_text
                and not st.get('running') and not st.get('paused')
                and str(self.devices.get(ip, {}).get('exit_id') or '') == str(eid)):
            point = self._qizi_safe_point(*point_text)
            if point is None:
                return False
            old = self.coordinates.get(uid, {})
            try: interval = max(1, min(60, float(old.get('interval') or 1)))
            except (TypeError, ValueError): interval = 1
            self.coordinates[uid] = dict(lat=str(point[0]), lon=str(point[1]), interval=interval)
            self.coordinates.pop('ip:' + ip, None)
            self.save_ios()
            row = self.row_widgets.get(ip)
            if row:
                row['lat'].set(str(point[0]))
                row['lon'].set(str(point[1]))
                row['key'] = uid
            self._qizi_route_hold[uid] = point
            self._qizi_store_live_coordinate(uid, *point, 'route_hold', False)
            self._qizi_stopped_positions[uid] = {
                'lat': str(point[0]), 'lon': str(point[1]), 'updated': time.time()}
            self._qizi_save_stopped_positions()
            self.devices[ip]['qizi_last_auto_exit'] = eid
            self.devices[ip]['qizi_last_auto_lat'] = str(point[0])
            self.devices[ip]['qizi_last_auto_lon'] = str(point[1])
            self._qizi_save_devices(self.devices)
            self._qizi_hold_issued.pop(uid, None)
            self._qizi_send_fixed_hold(uid, point)
            self.log_operation(f'{ip}：{reason}，已使用新出口核实的公网IP坐标并持续保持：'
                               f'{point[0]:.10f},{point[1]:.10f}')
            return True
        return super()._qizi_apply_exit_coordinates(ip, eid, reason)

    def bind_row_exit(self, ip):
        row = self.row_widgets.get(ip)
        chosen = None
        if row:
            idx = row['combo'].current()
            if 0 <= idx < len(row.get('ids', ())):
                chosen = str(row['ids'][idx])
        before = str(self.devices.get(ip, {}).get('exit_id') or '')
        # The dynamic layer cancels active / paused flight when a new exit is
        # explicitly chosen. Our STOP then saves the final held point.
        result = super().bind_row_exit(ip)
        after = str(self.devices.get(ip, {}).get('exit_id') or '')
        if chosen is not None and chosen != before and after == chosen:
            proxy = self._qizi_find_exit(after)
            if proxy and exit_coordinates(proxy):
                uid = str(self.uid_for_ip(ip) or '')
                saved = self._qizi_stopped_positions.get(uid, {})
                dest = self._qizi_safe_point(*exit_coordinates(proxy))
                old = self._qizi_safe_point(saved.get('lat'), saved.get('lon')) if saved else None
                if not saved or dest != old:
                    self._qizi_apply_exit_coordinates(ip, after, reason='手动修改出口')
        return result

    def delete_device(self):
        ip = getattr(self, 'selected_ip', None)
        uid = str(self.uid_for_ip(ip) or '') if ip else ''
        result = super().delete_device()
        if uid and ip not in self.devices:
            self._qizi_stopped_positions.pop(uid, None)
            self._qizi_save_stopped_positions()
            self._qizi_resume_after_boot.discard(uid)
            self._qizi_resume_boot_scheduled.discard(uid)
            self._qizi_hold_issued.pop(uid, None)
            self._qizi_route_hold.pop(uid, None)
        return result

    def clear_row(self, ip):
        uid = str(self.uid_for_ip(ip) or '')
        # Restoring real GPS overrides even a PAUSED flight. Otherwise its
        # persisted auto-resume flag could relaunch simulation on next startup.
        st = self.route_states.get(uid, {}) if uid else {}
        if uid and (st.get('running') or st.get('paused') or uid in self._qizi_auto_route_pending):
            self._stop_route(uid, manual=True)
        if uid:
            self._qizi_stopped_positions.pop(uid, None)
            self._qizi_save_stopped_positions()
            self._qizi_route_hold.pop(uid, None)
            self._qizi_hold_issued.pop(uid, None)
        return super().clear_row(ip)

    def new_row(self, ip):
        # 行内不再创建冗余的“暂”按钮；路线弹窗中继续保留完整暂停/续飞操作。
        return super().new_row(ip)

    def _qizi_toggle_pause(self, ip):
        uid = str(self.uid_for_ip(ip) or '')
        state = self.route_states.get(uid, {})
        if state.get('running'):
            return self._qizi_pause_route(uid)
        if state.get('paused') and state.get('qizi_pause_hold'):
            return self._qizi_resume_paused(uid)
        return False

    def open_route_dialog(self, ip):
        result = super().open_route_dialog(ip)
        uid = str(self.uid_for_ip(ip) or '')
        win = self.route_dialogs.get(uid)
        if win is None or not win.winfo_exists():
            return result
        try:
            body = win.winfo_children()[0]
            bar = next(c for c in body.grid_slaves(row=10, column=0)
                       if isinstance(c, ttk.Frame))
            if not hasattr(win, '_qizi_pause_button'):
                start_btn = next((w for w in bar.winfo_children()
                                  if isinstance(w, ttk.Button) and w.cget('text') == '开始执行'), None)
                btn = ttk.Button(bar, text='暂停',
                                 command=lambda u=uid: self._qizi_dialog_pause(u))
                btn.pack(side='left', after=start_btn, padx=6)
                win._qizi_pause_button = btn
            state = self.route_states.get(uid, {})
            if state.get('paused') and state.get('qizi_pause_hold'):
                win._qizi_pause_button.configure(text='继续飞行')
            else:
                win._qizi_pause_button.configure(text='暂停')
        except (AttributeError, StopIteration, ValueError):
            pass
        return result

    def _qizi_dialog_pause(self, uid):
        win = self.route_dialogs.get(uid)
        state = self.route_states.get(uid, {})
        if state.get('paused') and state.get('qizi_pause_hold'):
            self._qizi_resume_paused(uid)
        else:
            self._qizi_pause_route(uid)
        if win is not None and win.winfo_exists() and hasattr(win, '_qizi_pause_button'):
            st = self.route_states.get(uid, {})
            paused = bool(st.get('paused'))
            win._qizi_pause_button.configure(text='继续飞行' if paused else '暂停')
            try:
                body = win.winfo_children()[0]
                for label in body.grid_slaves(row=9, column=0):
                    if isinstance(label, ttk.Label):
                        name = label.cget('textvariable')
                        if name:
                            win.setvar(name, '暂停中：最后坐标持续发送，下次启动自动续飞。' if paused
                                       else '已继续执行剩余航线。')
                            break
            except Exception: pass

    def refresh_devices(self, selected_ip=None):
        result = super().refresh_devices(selected_ip)
        for ip, row in list(self.row_widgets.items()):
            btn = row.get('qizi_pause_btn')
            if btn is None:
                continue
            uid = str(self.uid_for_ip(ip) or '')
            state = self.route_states.get(uid, {})
            running = bool(state.get('running'))
            paused = bool(state.get('paused') and state.get('qizi_pause_hold'))
            if running:
                btn.configure(text='暂停', state='normal')
            elif paused:
                btn.configure(text='继续', state='normal' if self.ios_states.get(uid, {}).get('wifi') == '连接中' else 'disabled')
                row['route'].configure(text='已暂停', fg=self.BLUE)
            else:
                btn.configure(text='暂停', state='disabled')
        return result
