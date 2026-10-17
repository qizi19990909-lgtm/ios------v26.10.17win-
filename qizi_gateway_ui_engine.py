# -*- coding: utf-8 -*-
"""Compact aligned titlebar, per-cell wheel forwarding, strict PC proxy presence.

Keep physical proxy authentication and Apple Wi-Fi worker logic in the original
controller; inject just the ARP scanner/dependencies from its parent module.
"""
import tkinter as tk
from tkinter import ttk

def make_gateway_ui_mixin(context):
    scan_hotspot_devices = context['scan_hotspot_devices']
    select_lan_interface = context['select_this_pc_lan_interface']
    normalize_mac = context['normalize_mac']
    valid_device_mac = context['valid_device_mac']
    
    class QiziGatewayUiMixin:
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei')
            self.bind_all('<MouseWheel>', self._qizi_mouse_wheel, add='+')
            self.bind_all('<Button-4>', self._qizi_mouse_wheel, add='+')
            self.bind_all('<Button-5>', self._qizi_mouse_wheel, add='+')
            self.log_operation('设备列表仅显示本机代理访问设备及本地已配置设备；代理空闲不会断开 Wi-Fi。')
            # Traffic can precede ARP/neighbour-table discovery. The old code
            # remembered the IP but never created its blocked row until restart.
            self.after(500, self._qizi_gateway_live_discovery)

        def build_ui(self):
            super().build_ui()
            self.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei')
            try:
                # The original title bar belongs to container, while the right
                # exit list lives in the Panedwindow below. Put both headings in
                # the same top bar; preserve right pane width and all row widths.
                right = self.exit_table.master.master
                wrapper = self.exit_table.master
                top = next((w for w in self.winfo_children()[0].winfo_children()
                            if isinstance(w, ttk.Frame)), None)
                if top is None:
                    raise RuntimeError('顶部容器不存在')
                for w in list(right.winfo_children()):
                    if w is wrapper:
                        continue
                    if isinstance(w, ttk.Frame) and any(
                            isinstance(c, ttk.Label) and str(c.cget('text')) == '出口管理'
                            for c in w.winfo_children()):
                        w.destroy()
                    elif isinstance(w, ttk.Label) and 'exit_summary' in str(w.cget('textvariable')):
                        w.pack_forget()
                title_bar = ttk.Frame(top, width=218, height=27)
                title_bar.pack(side='right', padx=(3,2))
                title_bar.pack_propagate(False)
                ttk.Label(title_bar, text='出口管理', font=('Microsoft YaHei UI',11,'bold')).pack(side='left')
                ttk.Button(title_bar,text='添加出口',width=8,command=self.add_exit).pack(side='right')
                self._qizi_right_topbar = title_bar
                scan_label = next((w for w in top.winfo_children()
                                   if isinstance(w,ttk.Label) and str(w.cget('textvariable')) == str(self.scan_status)),None)
                # Pack widgets directly to the right of the scanning results.
                if scan_label is not None:
                    summary_label = next((w for w in top.winfo_children()
                        if isinstance(w,ttk.Label) and str(w.cget('textvariable')) == str(self.summary)),None)
                    more_button = next((w for w in top.winfo_children()
                        if isinstance(w,ttk.Menubutton) and str(w.cget('text')) == '更多'),None)
                    anchor=scan_label
                    for w in (summary_label, more_button):
                        if w is not None:
                            w.pack_forget()
                            w.pack(side='left',after=anchor,padx=(3,1))
                            anchor=w
                if hasattr(self,'exit_summary'):
                    for w in right.winfo_children():
                        if isinstance(w,ttk.Label) and str(w.cget('textvariable')) == str(self.exit_summary):
                            w.pack_forget()
            except Exception as exc:
                self.log_operation('顶栏布局调整失败：'+str(exc)[:180])

        def _qizi_inside_canvas(self, widget):
            try:
                name = str(self.canvas)
                while widget is not None:
                    if str(widget) == name:
                        return True
                    parent = widget.winfo_parent()
                    if not parent:
                        return False
                    widget = widget.nametowidget(parent)
            except (tk.TclError, KeyError):
                return False
            return False

        def _qizi_mouse_wheel(self,event):
            if not getattr(self,'canvas',None) or not self._qizi_inside_canvas(event.widget):
                return None
            if getattr(event,'num',None)==4:
                steps=-1
            elif getattr(event,'num',None)==5:
                steps=1
            else:
                delta=int(getattr(event,'delta',0) or 0)
                if not delta:
                    return None
                steps=-max(1,min(4,abs(delta)//120)) if delta>0 else max(1,min(4,abs(delta)//120))
            self.canvas.yview_scroll(steps,'units')
            return 'break'  # Do not accidentally change the selected exit combobox.

        def _qizi_gateway_live_discovery(self):
            """Promote *verified proxy visitors* to blocked rows on the Tk thread.

            A real HTTP request is required: ARP/Bonjour alone cannot enlist
            another PC's phones. MAC can be populated by a later network scan.
            """
            if getattr(self, '_closing', False):
                return
            try:
                import ipaddress
                now = __import__('time').monotonic()
                interface = getattr(self, '_lan_interface', None) or {}
                gateway = getattr(self, 'persistent_gateway', None)
                local = {str(interface.get('ip') or ''),
                         str(interface.get('gateway') or ''),
                         str(getattr(gateway, 'host', '') or '')}
                seen = (getattr(self, '_lan_gateway_seen_ips', set()) or set()).copy()
                added = []
                for ip in seen:
                    try:
                        address = ipaddress.ip_address(ip)
                        if (address.version != 4 or address.is_global or address.is_loopback
                                or address.is_link_local or address.is_multicast
                                or address.is_unspecified or ip in local):
                            continue
                    except ValueError:
                        continue
                    if ip in self.devices:
                        continue
                    # Do not infer the MAC or inherit another phone's exit.
                    # Until real MAC verification the newly observed client is BLOCKED.
                    with self.device_lock:
                        if ip in self.devices:
                            continue
                        self.devices[ip] = dict(ip=ip, mac='', interface=interface.get('alias',''),
                            remark='', exit_id='__QIZI_BLOCK__', manual=False,
                            online=True, last_seen=__import__('time').time())
                    added.append(ip)
                    if hasattr(self, '_qizi_presence_seen'):
                        self._qizi_presence_seen[ip] = now
                if added:
                    self._qizi_save_devices(self.devices)
                    self.refresh_devices()
                    self.log_operation('无需重启：已实时识别 ' + str(len(added)) +
                        ' 台实际访问本机代理的新设备（默认禁止网络，等待MAC补全）')
                    # A normal scan fills real MAC/identity without restarting.
                    if not getattr(self, 'scanning', False):
                        self.scan_devices()
            except Exception as exc:
                self.log_operation('实时识别代理客户端失败：'+type(exc).__name__+' '+str(exc)[:120])
            finally:
                if not getattr(self, '_closing', False):
                    self.after(1200, self._qizi_gateway_live_discovery)

        def on_listener_ready(self,gateway):
            return super().on_listener_ready(gateway)

        def _qizi_proxy_ip(self):
            gateway=getattr(self,'persistent_gateway',None)
            host=getattr(gateway,'host','') if gateway is not None else ''
            interface=getattr(self,'_lan_interface',{}) or {}
            return str(host or interface.get('ip',''))

        def _qizi_usage_devices(self):
            return {ip: device for ip, device in self.devices.items()
                    if device.get('online')}

        def refresh_devices(self,selected_ip=None):
            return super().refresh_devices(selected_ip)

        def scan_worker(self):
            """Show saved devices and clients that reached this PC's proxy."""
            try:
                primary=select_lan_interface()
                interfaces=[primary] if primary else []
                self._lan_interface=primary
                self._hotspot_interfaces_cache=list(interfaces)
                if not interfaces:raise RuntimeError('没有识别到本机活动局域网接口')
                found,info=scan_hotspot_devices(interfaces)
                own={str(x.get('ip') or '') for x in interfaces}
                gateways={str(x.get('gateway') or '') for x in interfaces}
                seen_ips=set(getattr(self,'_lan_gateway_seen_ips',set()) or set())
                seen_macs=set(getattr(self,'_lan_gateway_seen_macs',set()) or set())
                with self.device_lock:
                    saved_ips=set(self.devices)
                    saved_macs={normalize_mac(d.get('mac','')) for d in self.devices.values()
                                if valid_device_mac(d.get('mac',''))}
                allowed_ips=seen_ips|saved_ips
                allowed_macs=seen_macs|saved_macs
                found=[item for item in found
                       if str(item.get('ip') or '') not in own|gateways
                       and (str(item.get('ip') or '') in allowed_ips
                            or normalize_mac(item.get('mac','')) in allowed_macs)]
                # The first request may have arrived before ARP knew the MAC.
                # Update the provisional row once the scanner verifies it.
                for item in found:
                    ip=str(item.get('ip') or '')
                    mac=normalize_mac(item.get('mac',''))
                    if ip in seen_ips and valid_device_mac(mac):
                        with self.device_lock:
                            old=self.devices.get(ip)
                            needs_update=bool(old is None or not valid_device_mac(old.get('mac','')))
                        if needs_update:
                            self.events.put(('device_ip_learned_v9',None,ip,mac,
                                str(item.get('interface') or ''),'__QIZI_BLOCK__','',False))
                self.events.put(('scan_ready',found,info,interfaces))
            except Exception as exc:
                self.events.put(('devices_scan_error',f'{type(exc).__name__}: {exc}'))

        def _qizi_allowed_discovery_ips(self):
            with self.device_lock:
                configured=set(self.devices)
            return configured | set(getattr(self,'_lan_gateway_seen_ips',set()) or set())

        def poll_ios(self):
            result=super().poll_ios()
            allowed=self._qizi_allowed_discovery_ips()
            self.discovered={uid:data for uid,data in self.discovered.items()
                             if self.discovery_ip(uid,data) in allowed}
            return result

        def resolve_discovery(self):
            old=self.discovered
            allowed=self._qizi_allowed_discovery_ips()
            self.discovered={uid:data for uid,data in old.items()
                             if self.discovery_ip(uid,data) in allowed}
            try:
                return super().resolve_discovery()
            finally:
                self.discovered=old

        def _maybe_auto_restore_wifi(self):
            old=self.discovered
            allowed=self._qizi_allowed_discovery_ips()
            self.discovered={uid:data for uid,data in old.items()
                             if self.discovery_ip(uid,data) in allowed}
            try:
                return super()._maybe_auto_restore_wifi()
            finally:
                self.discovered=old

        def _maybe_auto_start_location(self,ip,uid=None,reason='Wi-Fi 已连接'):
            return super()._maybe_auto_start_location(ip,uid,reason)

        def connect_row(self,ip):
            return super().connect_row(ip)

        def trust_row(self,ip):
            return super().trust_row(ip)

    return QiziGatewayUiMixin
