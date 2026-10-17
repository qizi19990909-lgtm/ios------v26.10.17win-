# -*- coding: utf-8 -*-
"""顶部“更多”右侧的设备信任重置功能；Tk 控件与 PowerShell 后台执行解耦。"""
from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

from qizi_trust_reset_engine import usb_candidates, reset_windows_pairing


def make_trust_reset_ui_mixin(data_dir):
    data_dir = Path(data_dir)
    class QiziTrustResetUiMixin:
        def __init__(self, *args, **kwargs):
            self._qizi_reset_queue = queue.Queue()
            self._qizi_reset_busy = False
            self._qizi_reset_button = None
            self._qizi_reset_pending_path = data_dir / 'qizi_trust_reset_pending.json'
            try:
                from qizi_route_engine import load_json_dict
                self._qizi_reset_pending = {
                    str(uid) for uid, flag in load_json_dict(self._qizi_reset_pending_path).items() if flag
                }
            except Exception:
                self._qizi_reset_pending = set()
            super().__init__(*args, **kwargs)
            # Route UI restores historical trust badges from old device links.
            # A reset must remain visibly untrusted after restarting this app.
            self._qizi_reset_apply_pending_badges()
            self.after(200, self._qizi_reset_collect)

        def _qizi_reset_save_pending(self):
            from qizi_route_engine import save_route_plans
            save_route_plans(self._qizi_reset_pending_path,
                             {uid: True for uid in sorted(self._qizi_reset_pending)})

        def _qizi_reset_apply_pending_badges(self):
            trust_set = getattr(self, '_qizi_known_trust', None)
            for uid in self._qizi_reset_pending:
                if trust_set is not None:
                    trust_set.discard(uid)
                self.ios_states.setdefault(uid, {})['trusted'] = False

        def poll_ios(self):
            result = super().poll_ios()
            restored = {uid for uid in self._qizi_reset_pending
                        if self.ios_states.get(uid, {}).get('trusted') is True}
            if restored:
                self._qizi_reset_pending.difference_update(restored)
                try:
                    self._qizi_reset_save_pending()
                except OSError as exc:
                    self.log_operation('已重新信任，但待重置信任状态保存失败：' + str(exc)[:120])
                for uid in restored:
                    self.log_operation(uid + '：新的 USB 信任已验证，已清除重置待确认标记')
            return result

        def build_ui(self):
            super().build_ui()
            # Look up the actual '更多' menubutton after the original + gateway + dynamic UI
            # have finished repacking it. Insert after that widget, not in the exit pane.
            def locate(widget):
                for child in widget.winfo_children():
                    if isinstance(child, ttk.Menubutton) and str(child.cget('text')) == '更多':
                        return child
                    found = locate(child)
                    if found is not None:
                        return found
                return None
            more = locate(self)
            if more is None:
                self.log_operation('重置信任按钮添加失败：未找到顶部“更多”')
                return
            btn = ttk.Button(more.master, text='重置设备信任', command=self._qizi_reset_click)
            btn.pack(side='left', after=more, padx=(5, 2))
            self._qizi_reset_button = btn

        def _qizi_reset_busy_set(self, busy):
            self._qizi_reset_busy = bool(busy)
            btn = self._qizi_reset_button
            if btn is not None and btn.winfo_exists():
                btn.configure(state='disabled' if busy else 'normal')

        def _qizi_reset_click(self):
            if self._qizi_reset_busy:
                return
            if getattr(self, '_closing', False):
                return
            # Respect the application's existing authorization gate.
            if hasattr(self, '_license_valid') and not self._license_valid():
                self.show_license_dialog()
                return
            self._qizi_reset_busy_set(True)
            self.log_operation('设备信任重置：正在扫描电脑 USB 直连的 iPhone…')

            def work():
                try:
                    # Read actual USBMUX contacts now; do not trust cached WiFi devices,
                    # manually entered UDIDs, or cached historical trust mappings.
                    rows = usb_candidates(json.loads(self.ios.run(['usbmux', 'list'], 25)))
                    self._qizi_reset_queue.put(('scanned', rows))
                except Exception as exc:
                    self._qizi_reset_queue.put(('error', 'USB 扫描失败：' + str(exc)[:240]))
            threading.Thread(target=work, name='qizi-reset-usb-scan', daemon=True).start()

        def _qizi_reset_show_devices(self, rows):
            if not rows:
                self._qizi_reset_busy_set(False)
                messagebox.showwarning('重置设备信任',
                    '没有检测到通过 USB 数据线接入的 iPhone。\n\n请插线、解锁手机，并确认 Apple 驱动正常后重试。',
                    parent=self)
                return
            if len(rows) == 1:
                self._qizi_reset_confirm(rows[0])
                return
            # For multiple phones require explicit selection: never pick the first one.
            win = tk.Toplevel(self)
            win.title('重置设备信任 - 选择 USB iPhone')
            win.geometry('650x185')
            win.resizable(False, False)
            win.transient(self)
            win.grab_set()
            frm = ttk.Frame(win, padding=12)
            frm.pack(fill='both', expand=True)
            ttk.Label(frm, text=f'发现 {len(rows)} 台 USB iPhone，只会处理选中这一台的配对记录：').pack(anchor='w', pady=(0, 9))
            items = [f"{item['name']}  |  {item['udid']}" for item in rows]
            combo = ttk.Combobox(frm, values=items, state='readonly', width=78)
            combo.pack(fill='x')
            ttk.Label(frm, text='不会自动选择第一台，请先核对 UDID。', foreground='#9c5c00').pack(anchor='w', pady=8)
            buttons = ttk.Frame(frm)
            buttons.pack(fill='x')
            def cancel():
                self._qizi_reset_busy_set(False)
                win.destroy()
            def accept():
                idx = combo.current()
                if not 0 <= idx < len(rows):
                    return messagebox.showwarning('重置设备信任', '请先选择要重置的 iPhone。', parent=win)
                selected = rows[idx]
                win.destroy()
                self._qizi_reset_confirm(selected)
            ttk.Button(buttons, text='取消', command=cancel).pack(side='right', padx=(6, 0))
            ttk.Button(buttons, text='下一步', command=accept).pack(side='right')
            win.protocol('WM_DELETE_WINDOW', cancel)

        def _qizi_reset_confirm(self, item):
            udid = item['udid']
            approved = messagebox.askyesno(
                '确认重置设备信任',
                f"已检测到 USB 连接设备：\n{item['name']}\nUDID：{udid}\n\n"
                '将依次执行：\n'
                '1. 备份并移除这一台手机的 .plist / .plist.tmp 配对记录\n'
                '2. 重启 Apple Mobile Device Service（可能影响其他已连接 iPhone）\n'
                '3. 检查 Apple 服务是否恢复 Running\n\n'
                '需要 Windows 管理员授权。完成后请重新插线，并在 iPhone 上点击“信任”。\n\n'
                '是否确认执行？',
                parent=self)
            if not approved:
                self._qizi_reset_busy_set(False)
                return
            self.log_operation(f'{udid}：已确认重置信任；正在重新核实 USB 设备并运行 Windows PowerShell…')
            def work():
                try:
                    rows = usb_candidates(json.loads(self.ios.run(['usbmux', 'list'], 25)))
                    if not any(x['udid'] == udid for x in rows):
                        raise RuntimeError('执行前 USB 设备已经断开或 UDID 不匹配，已取消操作')
                    report = reset_windows_pairing(udid)
                    self._qizi_reset_queue.put(('done', report))
                except Exception as exc:
                    self._qizi_reset_queue.put(('error', str(exc)[:350]))
            threading.Thread(target=work, name='qizi-reset-pair-'+udid[-8:], daemon=True).start()

        def _qizi_reset_collect(self):
            if getattr(self, '_closing', False):
                return
            for _ in range(15):
                try: evt = self._qizi_reset_queue.get_nowait()
                except queue.Empty: break
                kind, payload = evt
                if kind == 'scanned':
                    self._qizi_reset_show_devices(payload)
                elif kind == 'error':
                    self._qizi_reset_busy_set(False)
                    self.log_operation('重置设备信任失败：' + payload)
                    messagebox.showerror('重置设备信任失败', payload, parent=self)
                elif kind == 'done':
                    self._qizi_reset_busy_set(False)
                    uid = str(payload['udid'])
                    # Drop only the UI's old remembered trust badge. No network/route/
                    # device bindings are deleted by this feature.
                    try:
                        self._qizi_reset_pending.add(uid)
                        self._qizi_reset_save_pending()
                        self.ios_states.setdefault(uid, {})['trusted'] = False
                        trust_set = getattr(self, '_qizi_known_trust', None)
                        if trust_set is not None:
                            trust_set.discard(uid)
                            path = getattr(self, '_qizi_trust_cache_path', None)
                            if path is not None:
                                from qizi_route_engine import save_route_plans
                                save_route_plans(path, {item: True for item in sorted(trust_set)})
                        self.refresh_devices()
                    except Exception as exc:
                        self.log_operation('配对已重置，刷新本地信任显示异常：' + str(exc)[:180])
                    removed = payload.get('removed') or []
                    backup = str(payload.get('backup_dir') or '')
                    self.log_operation(f'{uid}：配对文件已处理 {len(removed)} 个；Apple 服务 ' +
                                       str(payload.get('service_status') or '未知') + '；备份 ' + backup)
                    messagebox.showinfo('设备信任重置完成',
                        f'目标 UDID：{uid}\n'
                        f'已清除配对文件数：{len(removed)}\n'
                        f"Apple 服务：{payload.get('service_status', '未知')}\n"
                        f'备份路径：{backup}\n\n'
                        'PowerShell 已自动退出。\n'
                        '请拔下这台 iPhone 并重新连接电脑，解锁手机后确认“信任”，然后在管理器里点击信任。',
                        parent=self)
                    try: self.ios.usb_scan()
                    except Exception: pass
            if self.winfo_exists():
                self.after(200, self._qizi_reset_collect)

    return QiziTrustResetUiMixin
