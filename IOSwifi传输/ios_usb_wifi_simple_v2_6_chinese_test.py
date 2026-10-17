# -*- coding: utf-8 -*-
"""Standalone iPhone USB live-view tester for Windows / pymobiledevice3.
Run: py -3.13 ios_usb_live_test.py
No administrator privilege needed: uses pymobiledevice3 --userspace.
"""
import json
import atexit
from pathlib import Path
import re
import ipaddress
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
import urllib.request
import urllib.error
from tkinter import ttk, messagebox

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
NEW_PROCESS_GROUP = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
CONFIG_DIR = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'QiziIOSWifiViewer'
CONFIG_FILE = CONFIG_DIR / 'devices.json'

def load_config():
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {'devices': {}, 'last_uid': ''}
    except (OSError, ValueError):
        return {'devices': {}, 'last_uid': ''}

def save_config(data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, CONFIG_FILE)



def _scheme_a_worker(uid: str, port: int, strategy: str) -> None:
    """Single-purpose video child. All transport changes are process-local (RAM only)."""
    import logging
    os.environ['PYMOBILEDEVICE3_UDID'] = uid
    # A stale tunneld setting should never silently override our userspace selection.
    os.environ.pop('PYMOBILEDEVICE3_TUNNEL', None)
    os.environ.pop('PYMOBILEDEVICE3_NATIVE', None)
    os.environ['PYMOBILEDEVICE3_USERSPACE'] = '1'
    os.environ['PYMOBILEDEVICE3_DEFAULT_FALLBACK'] = 'userspace'
    os.environ['PYMOBILEDEVICE3_USERSPACE_TCP_RELAY'] = '1'
    print(f'[方案A子进程] 严格指定目标 UDID={uid}；模式={strategy}；网页端口={port}', flush=True)
    print('[方案A子进程] screen_stream.py 和其余已安装库文件完全不修改。', flush=True)
    # Import PMD entrypoint first: on Windows it installs the selector event-loop policy.
    from pymobiledevice3.__main__ import main
    if strategy == 'remote':
        from pymobiledevice3.remote import userspace_tunnel, tunnel_service
        if not hasattr(userspace_tunnel, '_create_no_root_tunnel_provider'):
            raise RuntimeError('当前安装的 pymobiledevice3 没有已知的 userspace provider 接口；停止测试，不修改库文件。')

        async def selected_wifi_provider(serial=None, autopair=True, remotepairing_fallback=True):
            # The upstream default first opens CoreDeviceProxy via USB. For WLAN-only
            # userspace we instead select the exact already-paired RemotePairing device.
            if str(serial).lower() != uid.lower():
                raise RuntimeError(f'设备匹配保护：拒绝非目标设备 {serial!r}')
            print('[方案A子进程] 正在通过 Bonjour + RemotePairing 连接已配对的目标 Wi-Fi iPhone。', flush=True)
            providers = await tunnel_service.get_remote_pairing_tunnel_services(udid=uid)
            if not providers:
                raise RuntimeError('指定 UDID 的 RemotePairing 服务不可用；请确认 Wi-Fi 同网段、信任和 RemotePairing。')
            # The provider list is already filtered by UDID. Never fall back to another device.
            for extra in providers[1:]:
                await extra.close()
            print('[方案A子进程] 已找到匹配 RemotePairing 服务；开始同进程 userspace TCP+UDP 隧道。', flush=True)
            return providers[0], None

        # This assignment affects ONLY this newly spawned worker, never the installed
        # source files and never other Python processes or connected devices.
        userspace_tunnel._create_no_root_tunnel_provider = selected_wifi_provider
    elif strategy != 'usbmux':
        raise ValueError(f'未知方案A传输模式：{strategy}')
    else:
        print('[方案A子进程] 使用 Apple Mobile Device Service 的无线 usbmux 连接建立同进程隧道。', flush=True)
    sys.argv = [
        'pymobiledevice3', 'developer', 'core-device', 'display', 'serve-web',
        '--display-id', '1', '--bind', '127.0.0.1', '--http-port', str(port),
        '--no-audio', '--userspace'
    ]
    result = main()
    if result is not None:
        raise SystemExit(int(result))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('iPhone USB / Wi-Fi 画面控制 · 七子')
        self.geometry('900x625')
        self.minsize(640, 450)
        self.events = queue.Queue()
        self.devices = {}
        self.device_label = tk.StringVar(value='正在识别 USB iPhone…')
        self.status = tk.StringVar(value='请用数据线连接并解锁 iPhone。')
        self.scheme_a_uid = None
        self.scheme_a_busy = False
        self.scheme_a_strategy = tk.StringVar(value='remote')
        self.scheme_a_device_label = tk.StringVar(value='尚未绑定 Wi-Fi 手机')
        self.port = tk.StringVar(value='8080')
        self.owned = set()
        self.owned_lock = threading.Lock()
        self.proc = None
        self.running_uid = None
        self.stream_mode = None
        self.active_port = None
        self.starting = False
        self.busy_scan = False
        self.busy_setup = False
        self.closing = False
        self.http_ready = False
        self.stream_ready = False
        self.browser_opened = False
        self.video_diag_token = 0
        self.wifi_proc = None
        self.wifi_busy = False
        self._build()
        self.after(150, self._poll)
        self.after(350, self.scan)
        self.after(5000, self._auto_scan)
        self.protocol('WM_DELETE_WINDOW', self._close)

    def _build(self):
        outer = ttk.Frame(self, padding=(18, 16, 18, 12))
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='iPhone USB / Wi-Fi 实时画面',
                  font=('Microsoft YaHei UI', 17, 'bold')).pack(anchor='w')
        ttk.Label(outer, text='保留你已验证成功的 RemotePairing 无线画面和触控方式',
                  foreground='#555555').pack(anchor='w', pady=(3, 12))

        device = ttk.LabelFrame(outer, text='当前设备（自动识别 USB 唯一 UDID）', padding=(12, 8))
        device.pack(fill='x', pady=(0, 12))
        ttk.Label(device, textvariable=self.device_label, wraplength=760).pack(anchor='w')
        ttk.Label(device, textvariable=self.scheme_a_device_label,
                  foreground='#146734').pack(anchor='w', pady=(4, 0))

        actions = ttk.Frame(outer)
        actions.pack(fill='x', pady=(0, 4))
        self.prepare_btn = ttk.Button(actions, text='① 一键信任并连接 Wi-Fi',
                                     command=self.scheme_a_prepare)
        self.prepare_btn.pack(side='left', padx=(0, 8), ipadx=8, ipady=6)
        self.wifi_btn = ttk.Button(actions, text='② Wi-Fi 实时画面',
                                  command=self.scheme_a_start)
        self.wifi_btn.pack(side='left', padx=(0, 8), ipadx=8, ipady=6)

        paste_area = ttk.LabelFrame(outer, text='Wi-Fi 键盘与中文输入测试', padding=(10, 8))
        paste_area.pack(fill='x', pady=(8, 5))
        self.paste_text = tk.StringVar(value='你好世界123')
        ttk.Entry(paste_area, textvariable=self.paste_text, width=46).pack(side='left', fill='x', expand=True, padx=(0, 8))
        ttk.Button(paste_area, text='复制到手机剪贴板', command=self.test_copy).pack(side='left', padx=(0, 6))
        ttk.Button(paste_area, text='模拟 ⌘+V 粘贴', command=self.test_paste).pack(side='left', padx=(0, 6))
        ttk.Button(paste_area, text='直接键盘输入', command=self.test_direct_type).pack(side='left', padx=(0, 6))
        ttk.Button(paste_area, text='中文自动输入', command=self.test_chinese_auto).pack(side='left')

        other = ttk.Frame(outer)
        other.pack(fill='x', pady=(6, 11))
        ttk.Button(other, text='USB 实时画面', command=self.start).pack(
            side='left', padx=(0, 8), ipadx=10, ipady=3)
        ttk.Button(other, text='停止画面', command=self.stop).pack(
            side='left', ipadx=10, ipady=3)

        ttk.Label(outer, text='使用方法：先插 USB 点击①；拔掉数据线后点击②。USB 画面可直接点击“USB 实时画面”。',
                  wraplength=750, foreground='#555555').pack(anchor='w', pady=(0, 10))
        ttk.Label(outer, textvariable=self.status, foreground='#17643a',
                  wraplength=770).pack(anchor='w', pady=(0, 10))
        ttk.Label(outer, text='运行日志').pack(anchor='w')
        area = ttk.Frame(outer)
        area.pack(fill='both', expand=True, pady=(4, 0))
        self.logbox = tk.Text(area, height=9, wrap='word', state='disabled', font=('Consolas', 9))
        scroll = ttk.Scrollbar(area, orient='vertical', command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=scroll.set)
        self.logbox.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')

    def _clipboard_request(self, path, payload=None):
        if self.stream_mode != 'wifi' or not self.proc or self.proc.poll() is not None or not self.active_port:
            raise RuntimeError('请先启动 Wi-Fi 实时画面，并确认网页画面已出现。')
        url = f'http://127.0.0.1:{self.active_port}{path}'
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'},
                                     method='GET' if data is None else 'POST')
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = resp.read(16384)
            if resp.status != 200:
                raise RuntimeError(f'{path} HTTP {resp.status}')
            return body

    def test_copy(self):
        value = self.paste_text.get()
        if not value:
            messagebox.showwarning('粘贴测试', '请先输入要测试的文字。')
            return
        threading.Thread(target=self._paste_worker, args=(value, False), daemon=True).start()

    def test_paste(self):
        value = self.paste_text.get()
        if not value:
            messagebox.showwarning('粘贴测试', '请先输入要测试的文字。')
            return
        threading.Thread(target=self._paste_worker, args=(value, True), daemon=True).start()

    def _paste_worker(self, value, send_keys):
        try:
            self.log(f'[键盘测试] 正在通过 Wi-Fi 写入手机剪贴板，长度={len(value)}')
            self._clipboard_request('/clipboard', {'text': value})
            # Verify actual device pasteboard, not just HTTP success.
            result = json.loads(self._clipboard_request('/clipboard'))
            actual = result.get('text')
            if actual != value:
                self.log(f'[键盘测试] 手机剪贴板回读不一致：{actual!r}')
                return
            self.log('[键盘测试] 手机剪贴板写入及回读一致。')
            if not send_keys:
                self.log('[键盘测试] 复制完成。可在 iPhone 文本框内长按选择“粘贴”。')
                return
            self.log('[键盘测试] 请先在网页画面中点击手机的输入框，确保光标已出现。')
            # HID Keyboard Usage Tables: Left GUI (Command)=0xE3; V=0x19.
            # iOS external hardware keyboards typically use Command+V to paste.
            try:
                self._clipboard_request('/key', {'usages': [0xE3, 0x19]})
                time.sleep(0.12)
            finally:
                self._clipboard_request('/key', {'usages': []})
            self.log('[键盘测试] 已发送物理键盘 Command+V（HID），请观察手机输入框是否出现文字。')
        except Exception as exc:
            self.log('[键盘测试] 失败：' + repr(exc))

    def test_direct_type(self):
        value = self.paste_text.get()
        if not value:
            messagebox.showwarning('键盘输入', '请输入测试文字，例如 hello123。')
            return
        # USB HID keyboard uses US layout keycodes, not Unicode text injection.
        if any(ord(ch) > 127 for ch in value):
            messagebox.showwarning('键盘输入', '直接 HID 输入先测试英文、数字及 ASCII 符号。中文请暂时使用剪贴板。')
            return
        threading.Thread(target=self._direct_type_worker, args=(value,), daemon=True).start()

    def test_chinese_auto(self):
        value = self.paste_text.get()
        if not value:
            messagebox.showwarning('中文测试', '请输入要测试的中文，例如：你好世界123。')
            return
        threading.Thread(target=self._chinese_auto_worker, args=(value,), daemon=True).start()

    def _chinese_auto_worker(self, value):
        # HID Usage Page 0x07 has no direct Unicode character representation.
        # Automatically paste Chinese after verifying the device clipboard.
        try:
            self.log(f'[中文自动输入] 准备发送 {len(value)} 个字符；请先点选 iPhone 输入框。')
            self._clipboard_request('/clipboard', {'text': value})
            result = json.loads(self._clipboard_request('/clipboard'))
            if result.get('text') != value:
                self.log('[中文自动输入] 失败：iPhone 剪贴板回读不一致。')
                return
            self.log('[中文自动输入] 剪贴板回读一致，自动发送 Command+V。')
            try:
                self._clipboard_request('/key', {'usages': [0xE3, 0x19]})
                time.sleep(0.16)
            finally:
                self._clipboard_request('/key', {'usages': []})
            self.log('[中文自动输入] 粘贴按键已发送，请观察手机输入框实际内容。')
            self.log('[中文自动输入] 该测试使用自动粘贴，并非直接 Unicode HID 输入。')
        except Exception as exc:
            self.log('[中文自动输入] 失败：' + repr(exc))

    def _direct_type_worker(self, value):
        # USB HID Keyboard/Keypad Usage Page 0x07, US keyboard layout.
        shifted = '!@#$%^&*()_+{}|:\"<>?~'
        plain = '1234567890-=[]\\;\',./`'
        special = {' ': (0x2c, False), '\n': (0x28, False), '\t': (0x2b, False)}
        special.update({chr(97+i): (0x04+i, False) for i in range(26)})
        special.update({chr(65+i): (0x04+i, True) for i in range(26)})
        codes = [0x1e,0x1f,0x20,0x21,0x22,0x23,0x24,0x25,0x26,0x27,
                 0x2d,0x2e,0x2f,0x30,0x31,0x33,0x34,0x36,0x37,0x38,0x35]
        for char, code in zip(plain, codes): special[char] = (code, False)
        for char, code in zip(shifted, codes): special[char] = (code, True)
        try:
            self.log('[直接输入] 请先点击 iPhone 画面中的输入框。开始发送逐键 HID（不使用剪贴板）。')
            for ch in value:
                if ch not in special:
                    raise ValueError(f'当前未映射字符: {ch!r}')
                code, shift = special[ch]
                usages = ([0xE1] if shift else []) + [code]
                try:
                    self._clipboard_request('/key', {'usages': usages})
                    time.sleep(0.055)
                finally:
                    self._clipboard_request('/key', {'usages': []})
                time.sleep(0.035)
            self.log(f'[直接输入] 已发送 {len(value)} 个 HID 字符；请核对手机输入框实际显示内容。')
        except Exception as exc:
            self.log('[直接输入] 失败：' + repr(exc))

    def _selected(self):
        # Single-device test: never silently select one among multiple USB iPhones.
        if len(self.devices) == 1:
            return next(iter(self.devices))
        return None

    def _auto_scan(self):
        if not self.closing:
            self.scan()
            self.after(5000, self._auto_scan)

    def _valid_port(self):
        # UI deliberately hides port settings. _pick_port chooses an unused port automatically.
        return 8080


    def log(self, msg):
        self.events.put(('log', str(msg)))


    def _append(self, msg):
        self.logbox.configure(state='normal')
        self.logbox.insert('end', f'[{time.strftime("%H:%M:%S")}] {msg}\n')
        self.logbox.see('end')
        self.logbox.configure(state='disabled')


    def _track(self, p):
        with self.owned_lock: self.owned.add(p)
        return p


    def _untrack(self, p):
        with self.owned_lock: self.owned.discard(p)


    def _pick_port(self):
        requested = self._valid_port()
        if requested is None: return None
        for candidate in range(requested, min(requested + 200, 65536)):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                try:
                    sock.bind(('127.0.0.1', candidate))
                    if candidate != requested:
                        self.log(f'端口 {requested} 已占用，本次使用 {candidate}；配置仍保留首选端口 {requested}。')
                    return candidate
                except OSError: pass
        messagebox.showerror('端口错误', '连续 200 个本地端口都不可用。')
        return None


    def scheme_a_prepare(self):
        """Lock one USB UDID; enable Wi-Fi support only on that target device."""
        uid = self._selected()
        if not uid or uid not in self.devices:
            messagebox.showwarning('方案A', '请先通过 USB 连接并解锁一台 iPhone，等待自动识别。')
            return
        if self.scheme_a_busy or self.starting or self.busy_setup:
            messagebox.showwarning('方案A', '正在执行其他操作，请稍后再试。')
            return
        if self.proc and self.proc.poll() is None:
            messagebox.showwarning('方案A', '请先停止本程序启动的 USB/无线实时画面。')
            return
        if self.wifi_proc and self.wifi_proc.poll() is None:
            messagebox.showwarning('方案A', '请先点击「断开 Wi-Fi 隧道」，避免旧外部 RSD 隧道干扰。')
            return
        self.scheme_a_busy = True
        self.status.set('方案A：通过 USB 验证并绑定目标 UDID……')
        self.log('[方案A] 请求绑定 USB 唯一设备：' + uid)
        threading.Thread(target=self._scheme_a_prepare_worker, args=(uid,), daemon=True).start()


    def _scheme_a_prepare_worker(self, uid):
        try:
            env = os.environ.copy()
            env['PYMOBILEDEVICE3_UDID'] = uid
            base = self._python()
            def run(args, timeout=35):
                return subprocess.run(base+args, capture_output=True, text=True,
                    encoding='utf-8', errors='replace', timeout=timeout,
                    creationflags=CREATE_NO_WINDOW, env=env)
            usb_result = run(['usbmux','list'],20)
            if usb_result.returncode:
                raise RuntimeError('USB 检查失败：' + (usb_result.stderr or usb_result.stdout)[-600:])
            matches = [d for d in json.loads(usb_result.stdout) if
                       (d.get('UniqueDeviceID') or d.get('Identifier')) == uid and
                       str(d.get('ConnectionType','')).upper() == 'USB']
            if len(matches) != 1:
                raise RuntimeError('目标 UDID 没有通过 USB 检测到，停止操作，不会误选其他设备。')
            info = run(['lockdown','info','--udid',uid],35)
            if info.returncode:
                raise RuntimeError('目标设备尚未完成 USB 信任：' + (info.stderr or info.stdout)[-800:])
            wifi_on = run(['lockdown','wifi-connections','on','--udid',uid],35)
            if wifi_on.returncode:
                raise RuntimeError('打开目标 Wi-Fi Connections 失败：' + (wifi_on.stderr or wifi_on.stdout)[-850:])
            # For Wi-Fi RemotePairing, the existing paired record is used.
            # Do not re-pair or delete other records in this testing phase.
            self.events.put(('scheme_a_prepared', (uid, matches[0].get('DeviceName','iPhone'),
                            matches[0].get('ProductVersion',''))))
        except Exception as exc:
            self.events.put(('scheme_a_prepare_fail',str(exc)))


    def scheme_a_start(self):
        uid = self.scheme_a_uid
        if not uid:
            messagebox.showwarning('方案A', '先插入 USB，点击「① 一键信任并连接 Wi-Fi」。')
            return
        if self.scheme_a_busy or self.starting:
            messagebox.showinfo('方案A', '当前有一个操作正在执行。')
            return
        if self.proc and self.proc.poll() is None:
            messagebox.showwarning('方案A', '请先点击「停止画面」，再切换至 Wi-Fi。')
            return
        if self.wifi_proc and self.wifi_proc.poll() is None:
            messagebox.showwarning('方案A', '已有另一个无线隧道运行，请关闭旧版程序后再试。')
            return
        port = self._pick_port()
        if port is None: return
        self.scheme_a_busy = True
        self.status.set('方案A：核对 Wi-Fi 设备与 USB UDID，准备同进程视频传输……')
        self.log('[方案A] 启动前核对：只能连接 USB 曾识别的 UDID=' + uid)
        threading.Thread(target=self._scheme_a_preflight, args=(uid,port,'remote'), daemon=True).start()


    def _scheme_a_preflight(self, uid, port, requested_strategy):
        try:
            env = os.environ.copy()
            env['PYMOBILEDEVICE3_UDID'] = uid
            def run(args, timeout):
                return subprocess.run(self._python()+args,capture_output=True,text=True,
                    encoding='utf-8',errors='replace',timeout=timeout,
                    creationflags=CREATE_NO_WINDOW,env=env)
            r = run(['usbmux','list'],25)
            if r.returncode:
                raise RuntimeError('usbmux 列表失败：' + (r.stderr or r.stdout)[-650:])
            devs = json.loads(r.stdout)
            matched = [d for d in devs if str(d.get('UniqueDeviceID') or d.get('Identifier','')).lower() == uid.lower()]
            if any(str(d.get('ConnectionType','')).upper() == 'USB' for d in matched):
                raise RuntimeError('目标手机仍通过 USB 接入；请先拔线，避免实际在测 USB 视频。')
            wifi_usbmux = any(str(d.get('ConnectionType','')).upper() != 'USB' for d in matched)
            self.log('[方案A] 目标已拔线；无线上 usbmux 识别=' + ('成功' if wifi_usbmux else '未发现（可走 RemotePairing）'))
            r = run(['remote','browse'],40)
            if r.returncode:
                raise RuntimeError('Wi-Fi RemotePairing 设备发现失败：' + (r.stderr or r.stdout)[-650:])
            devices = json.loads(r.stdout).get('wifi',[])
            selected = [d for d in devices if str(d.get('identifier','')).lower() == uid.lower()]
            if not selected:
                raise RuntimeError('Wi-Fi RemotePairing 未发现 USB 已锁定 UDID。为防止误连，取消测试。')
            strategy = requested_strategy
            if strategy == 'auto':
                strategy = 'usbmux' if wifi_usbmux else 'remote'
            if strategy == 'usbmux' and not wifi_usbmux:
                raise RuntimeError('已选择无线 usbmux 模式，但 usbmux list 未列出目标的网络连接；请切换 RemotePairing。')
            ip = selected[0].get('address','（未报告地址）')
            self.log(f'[方案A] Wi-Fi RemotePairing 精确匹配：UDID={uid}，设备IP={ip}，userspace={strategy}')
            self.events.put(('scheme_a_preflight_ok',(uid,port,strategy)))
        except Exception as exc:
            self.events.put(('scheme_a_preflight_fail',str(exc)))


    def _scheme_a_launch(self, uid, port, strategy):
        try:
            if self.closing or uid != self.scheme_a_uid:
                raise RuntimeError('目标 UID 改变或界面正在关闭，取消。')
            env=os.environ.copy()
            env['PYMOBILEDEVICE3_UDID']=uid
            env.pop('PYMOBILEDEVICE3_TUNNEL', None)
            env.pop('PYMOBILEDEVICE3_NATIVE', None)
            env['PYMOBILEDEVICE3_USERSPACE']='1'
            cmd=[sys.executable,os.path.abspath(__file__),'--qz-wifi-worker',uid,str(port),strategy]
            self.log('[方案A] 视频子进程在自身内部创建 Wi-Fi userspace Tunnel；不向 --rsd 传旧隧道地址。')
            self.log('[方案A] 启动：' + ' '.join(cmd))
            p=self._track(subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding='utf-8',errors='replace',bufsize=1,
                creationflags=CREATE_NO_WINDOW | NEW_PROCESS_GROUP,env=env))
            if self.closing:
                self._stop_process(p)
                return
            self.events.put(('launched',(p,uid,port,'wifi')))
        except Exception as exc:
            self.events.put(('launch_error','方案A：'+str(exc)))


    def _python(self):
        # Use the same Python interpreter that runs this UI, avoiding py launcher ambiguity.
        return [sys.executable, '-m', 'pymobiledevice3']


    def scan(self):
        if self.busy_scan or self.closing or self.wifi_busy or (self.proc and self.proc.poll() is None):
            return
        self.busy_scan = True
        def worker():
            try:
                result = subprocess.run(self._python() + ['usbmux', 'list'], capture_output=True, text=True,
                                        encoding='utf-8', errors='replace', timeout=18,
                                        creationflags=CREATE_NO_WINDOW)
                if result.returncode:
                    raise RuntimeError((result.stderr or result.stdout)[-1200:])
                data = json.loads(result.stdout)
                devices = {str(d.get('UniqueDeviceID') or d.get('Identifier')): d for d in data
                           if d.get('ConnectionType') == 'USB' and (d.get('UniqueDeviceID') or d.get('Identifier'))}
                self.events.put(('devices', devices))
            except Exception as exc:
                self.events.put(('scan_error', str(exc)))
            finally:
                self.events.put(('scan_done', None))
        threading.Thread(target=worker, daemon=True).start()


    def start(self):
        uid = self._selected()
        if not uid or uid not in self.devices:
            messagebox.showwarning('未选择设备', '请先在 USB 设备列表中选择一台 iPhone。')
            return
        if self.busy_setup:
            messagebox.showinfo('初始化中', '请先等待信任或开发者模式操作结束。')
            return
        if self.starting:
            messagebox.showinfo('正在检测', '正在检查开发者服务，请稍候。')
            return
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo('正在运行', '请先停止当前画面，再切换设备。')
            return
        port = self._pick_port()
        if port is None: return
        # The --userspace provider may ignore UDID env var in some PMD versions.
        # For safety, require one USB device until explicit provider selection is verified.
        if len(self.devices) != 1:
            messagebox.showwarning('多台 USB 设备', '当前检测到多台 USB iPhone。\n此版本的 --userspace 不能可靠地通过 --udid 指定设备。\n为防止误连，请仅保留测试手机的 USB 数据线，再刷新设备。\n不会影响其他设备的 Wi-Fi 连接。')
            return
        self.starting = True
        self.status.set('正在核对 USB 设备并启动视频服务……')
        threading.Thread(target=self._preflight_and_launch, args=(uid, port), daemon=True).start()


    def _preflight_and_launch(self, uid, port):
        env = os.environ.copy()
        env['PYMOBILEDEVICE3_UDID'] = uid
        base = self._python() + ['developer', 'core-device']
        try:
            # Check the exact USB identity before the implicit userspace selection.
            res = subprocess.run(self._python() + ['usbmux', 'list'], capture_output=True,
                                 text=True, encoding='utf-8', errors='replace', timeout=18,
                                 creationflags=CREATE_NO_WINDOW, env=env)
            if res.returncode:
                raise RuntimeError('USB 扫描失败：' + (res.stderr or res.stdout)[-500:])
            found = [d for d in json.loads(res.stdout) if d.get('ConnectionType') == 'USB']
            if len(found) != 1 or (found[0].get('UniqueDeviceID') or found[0].get('Identifier')) != uid:
                raise RuntimeError('USB 设备数量或 UDID 已改变；为避免误连已取消启动。')
            # Do not run extra developer commands before serve-web.
            # Each --userspace invocation opens its own tunnel; the previously
            # successful manual test launched serve-web directly.
            self.events.put(('log', f'动态识别当前 USB 设备：{uid}；不使用固定 UDID；直接启动 serve-web。'))
            cmd = base + ['display', 'serve-web', '--display-id', '1',
                          '--bind', '127.0.0.1', '--http-port', str(port),
                          '--no-audio', '--userspace']
            proc = self._track(subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                                    errors='replace', bufsize=1, creationflags=CREATE_NO_WINDOW | NEW_PROCESS_GROUP, env=env))
            if self.closing:
                self._stop_process(proc)
                return
            self.events.put(('launched', (proc, uid, port, 'usb')))
        except Exception as exc:
            self.events.put(('launch_error', str(exc)))


    def _read_process(self, process):
        try:
            failed = False
            for line in process.stdout:
                s = line.rstrip()
                if s: self.events.put(('log', s))
                if 'AAC-ELD decode requires macOS' in s:
                    self.events.put(('log', '提示：Windows 不支持此音频解码器；该错误不能单独证明视频黑屏原因。'))
                if 'Open http://127.0.0.1:' in s and 'Ctrl-C to stop' in s:
                    self.events.put(('stream_ready', process))
                if ('No such service: com.apple.coredevice.displayservice' in s or
                    'No such service: com.apple.coredevice.hid.universalhidservice' in s):
                    if not failed:
                        failed = True
                        self.events.put(('service_error', (process, s)))
        except Exception as exc:
            self.events.put(('log', f'读取日志失败：{exc}'))
        finally:
            try: code = process.wait(timeout=2)
            except Exception: code = '未知'
            self._untrack(process)
            self.events.put(('exited', (process, code)))


    def _wait_for_http(self, process, port):
        for _ in range(80):
            if process.poll() is not None: return
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.4)
                if s.connect_ex(('127.0.0.1', port)) == 0:
                    self.events.put(('http_listening', (process, port)))
                    return
            time.sleep(0.5)
        self.events.put(('log', '等待网页端口超时；检查日志中 USB 隧道错误。'))


    def open_browser(self):
        port = self.active_port
        if port is not None:
            webbrowser.open(f'http://127.0.0.1:{port}/')


    def stop(self):
        p = self.proc
        if not p: return
        self.proc = None
        self.running_uid = None
        self.active_port = None
        self.stream_mode = None
        self.status.set('已停止本程序的视频服务。')
        self.http_ready = False
        self.stream_ready = False
        self.video_diag_token += 1
        if p.poll() is None:
            self.log(f'正在停止本程序创建的 PID={p.pid}')
            self._stop_process(p)


    def _stop_process(self, p):
        if p is None: return
        try:
            if p.poll() is None:
                if os.name == 'nt':
                    # Kill ONLY this process tree; serve-web/userspace may create children
                    # which survive plain Popen.terminate() and retain the HTTP port.
                    subprocess.run(['taskkill', '/PID', str(p.pid), '/T', '/F'],
                                   capture_output=True, timeout=12, creationflags=CREATE_NO_WINDOW)
                else:
                    p.terminate()
                try: p.wait(timeout=5)
                except subprocess.TimeoutExpired: p.kill(); p.wait(timeout=3)
        except Exception as exc:
            self.events.put(('log', f'停止进程失败 PID={p.pid}：{exc}'))
        finally:
            self._untrack(p)


    def _poll(self):
        if self.closing:
            return
        try:
            for _ in range(80):
                kind, payload = self.events.get_nowait()
                if kind == 'log':
                    self._append(payload)
                elif kind == 'devices':
                    self.devices = payload
                    if len(payload) == 1:
                        uid, d = next(iter(payload.items()))
                        self.device_label.set(f"USB 已连接：{d.get('DeviceName', 'iPhone')} / iOS {d.get('ProductVersion', '?')} / UDID {uid}")
                    elif len(payload) > 1:
                        self.device_label.set('检测到多台 USB 手机。当前为单设备测试，请仅保留要测试的手机 USB 连接。')
                    elif self.scheme_a_uid:
                        self.device_label.set('USB 已拔出；保留已锁定 UDID：' + self.scheme_a_uid)
                    else:
                        self.device_label.set('未发现 USB 手机，请连接并解锁 iPhone。')
                elif kind == 'scan_error':
                    self._append('设备扫描失败：' + payload)
                    self.status.set('无法识别 USB 手机，请检查 Apple 驱动。')
                elif kind == 'scan_done':
                    self.busy_scan = False
                elif kind == 'scheme_a_prepared':
                    uid, name, version = payload
                    self.scheme_a_busy = False
                    self.scheme_a_uid = uid
                    self.scheme_a_device_label.set(f'Wi-Fi 目标已锁定：{name} / iOS {version} / UDID …{uid[-10:]}')
                    self._append('[Wi-Fi] 已通过 USB 验证信任、锁定 UDID 并启用 Wi-Fi Connections：' + uid)
                    self.status.set('无线准备成功。现在拔掉 USB，点击「② Wi-Fi 实时画面」。')
                elif kind == 'scheme_a_prepare_fail':
                    self.scheme_a_busy = False
                    self._append('[Wi-Fi] USB 准备失败：' + payload)
                    self.status.set('准备失败，请解锁手机、确认信任提示后重试。')
                elif kind == 'scheme_a_preflight_ok':
                    uid, port, strategy = payload
                    self.scheme_a_busy = False
                    if (not self.closing and uid == self.scheme_a_uid and
                            not self.starting and not self.proc):
                        self.starting = True
                        self.status.set('已核对 Wi-Fi 唯一 UDID；正在建立 RemotePairing 视频连接……')
                        threading.Thread(target=self._scheme_a_launch,
                                         args=(uid, port, 'remote'), daemon=True).start()
                    else:
                        self._append('[Wi-Fi] 测试状态已改变，取消启动。')
                elif kind == 'scheme_a_preflight_fail':
                    self.scheme_a_busy = False
                    self._append('[Wi-Fi] 启动失败：' + payload)
                    self.status.set('无线连接未通过验证，请查看下方日志。')
                elif kind == 'launched':
                    p, uid, port, mode = payload
                    self.starting = False
                    if self.closing:
                        self._stop_process(p)
                        continue
                    self.proc = p
                    self.active_port = port
                    self.stream_mode = mode
                    self.running_uid = uid
                    self.http_ready = False
                    self.stream_ready = False
                    self.browser_opened = False
                    self.status.set('视频服务已启动，等待浏览器画面……')
                    self._append(f'视频进程 PID={p.pid}，方式={mode.upper()}，端口={port}')
                    threading.Thread(target=self._read_process, args=(p,), daemon=True).start()
                    threading.Thread(target=self._wait_for_http, args=(p,port), daemon=True).start()
                elif kind == 'launch_error':
                    self.starting = False
                    self._append('视频启动失败：' + payload)
                    self.status.set('视频启动失败，请查看日志。')
                    messagebox.showerror('视频启动失败', payload[-1200:])
                elif kind == 'service_error':
                    p, err = payload
                    if p is self.proc:
                        self._append('视频服务缺失：' + err)
                        self.stop()
                elif kind == 'http_listening':
                    p, port = payload
                    if p is self.proc:
                        self.http_ready = True
                        self._open_when_ready(p,port)
                elif kind == 'stream_ready':
                    p = payload
                    if p is self.proc:
                        self.stream_ready = True
                        self._open_when_ready(p,self.active_port)
                elif kind == 'exited':
                    p, code = payload
                    self._append(f'视频进程已退出 PID={p.pid}，退出码={code}')
                    if p is self.proc:
                        self.proc = None
                        self.active_port = None
                        self.stream_mode = None
                        self.running_uid = None
                        self.status.set('画面已停止；可以重新启动 USB 或 Wi-Fi 画面。')
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def _open_when_ready(self, p, port):
        if (p is self.proc and p.poll() is None and self.http_ready
                and self.stream_ready and not self.browser_opened):
            self.browser_opened = True
            mode = 'Wi-Fi RemotePairing' if self.stream_mode == 'wifi' else 'USB'
            self.status.set(f'{mode} 画面服务运行中；浏览器地址 http://127.0.0.1:{port}/')
            self._append(f'{mode} 视频服务已启动，正在打开浏览器。')
            webbrowser.open(f'http://127.0.0.1:{port}/?session={int(time.time())}')

    def _close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno('退出', '退出时将停止本程序创建的视频进程。确定退出？'):
                return
        self.closing = True
        with self.owned_lock:
            owned = list(self.owned)
        for p in owned:
            self._stop_process(p)
        self.proc = None
        self.destroy()

if __name__ == '__main__':
    if len(sys.argv) == 5 and sys.argv[1] == '--qz-wifi-worker':
        uid = sys.argv[2]
        if not re.fullmatch(r'[0-9A-Za-z-]{12,80}', uid):
            raise SystemExit('不合法的设备 UDID')
        try:
            _scheme_a_worker(uid, int(sys.argv[3]), sys.argv[4])
        except Exception as exc:
            import traceback
            print('[RemotePairing] 建立视频失败：' + repr(exc), flush=True)
            traceback.print_exc()
            raise SystemExit(1)
    else:
        App().mainloop()
