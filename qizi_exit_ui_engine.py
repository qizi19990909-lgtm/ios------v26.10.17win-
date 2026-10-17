# -*- coding: utf-8 -*-
"""批量出口 GUI / Wi-Fi 手动断开联动：主程序只负责加载。"""
import json,threading,uuid,queue,time,tkinter as tk
from pathlib import Path
from tkinter import ttk,messagebox,filedialog
from qizi_geo_engine import resolve_ip
from qizi_exit_import_engine import qizi_parse_proxy_lines
from qizi_dynamic_ui_engine import QiziDynamicMixin
from qizi_geo_source_engine import discover_mmdb
from qizi_gateway_ui_engine import make_gateway_ui_mixin
from qizi_route_ui_engine import make_route_ui_mixin
from qizi_trust_reset_ui_engine import make_trust_reset_ui_mixin
from qizi_reliability_ui_engine import QiziReliabilityMixin

def make_app_class(BaseApp, context):
    # 依赖注入旧程序的配置和保存函数，避免复制整个主程序。
    global DATA_DIR, EXITS_FILE, ExitDialog, save_json, save_devices
    DATA_DIR=context['DATA_DIR']
    EXITS_FILE=context['EXITS_FILE']
    ExitDialog=context['ExitDialog']
    save_json=context['save_json']
    save_devices=context['save_devices']
    GatewayUiMixin = make_gateway_ui_mixin(context)
    RouteUiMixin = make_route_ui_mixin(DATA_DIR)
    TrustResetUiMixin = make_trust_reset_ui_mixin(DATA_DIR)
    class QiziBulkGeoApp(TrustResetUiMixin, RouteUiMixin, GatewayUiMixin, QiziDynamicMixin, BaseApp):
        WIDTHS = tuple(BaseApp.WIDTHS) + (158,)
        """继承原有控制器；不改变原始网络网关、授权或定位发送逻辑。"""
        def __init__(self,*args,**kwargs):
            self._qizi_geo_running=False
            self._qizi_geo_ui_queue=queue.Queue()
            self._qizi_geo_waiting=[]
            self._qizi_geo_requested={}
            self._qizi_geo_retry_after={}
            self._qizi_new_exit_check_queue=[]
            self._qizi_new_exit_check_active=False
            self._qizi_new_exit_check_started={}
            self._qizi_save_devices=save_devices
            self._qizi_api_probe=context['test_http_exit']
            self._qizi_api_saved_exits=lambda data: save_json(EXITS_FILE,data)
            self._qizi_wifi_manual_off=set()
            self._qizi_geo_file=DATA_DIR / 'qizi_geo_settings.json'
            self._qizi_geo_db=''
            try:
                self._qizi_geo_db=json.loads(self._qizi_geo_file.read_text(encoding='utf-8')).get('mmdb','')
            except (OSError,ValueError,TypeError):pass
            self._qizi_geo_db=discover_mmdb(DATA_DIR, self._qizi_geo_db)
            super().__init__(*args,**kwargs)
            # 九列设备区新增“未来出口”，适度扩大窗口使右侧列表仍然可见。
            self.geometry('1260x620')
            self.minsize(1050,500)
            if self._qizi_geo_db:
                self.log_operation('已自动发现本地 MMDB：'+Path(self._qizi_geo_db).name)
            else:
                self.log_operation('程序主目录未发现 MMDB；出口可正常添加，坐标识别等待数据库。')
            # UI 主线程定时读取工作线程的结果；工作线程绝不调用 Tkinter。
            self.after(100,self._qizi_drain_geo_queue)
            # 旧出口若没有坐标 / IP 已变化，启动后也补齐识别。
            self.after(2000,self._qizi_scan_unlocated_exits)
            self.after(60000,self._qizi_retry_geo_tick)
            for ip,d in self.devices.items():
                if d.get('qizi_wifi_manual_off'):
                    uid=self.uid_for_ip(ip)
                    if uid:self._qizi_wifi_manual_off.add(str(uid))

        def _qizi_set_mmdb(self,db):
            self._qizi_geo_db=db
            try:
                self._qizi_geo_file.parent.mkdir(parents=True,exist_ok=True)
                self._qizi_geo_file.write_text(json.dumps({'mmdb':db},ensure_ascii=False,indent=2),encoding='utf-8')
            except OSError as exc:self.log_operation('MMDB 路径保存失败：'+str(exc))

        def add_exit(self):
            """一个紧凑的弹窗内切换批量/手动；手动模式不再另起 ExitDialog。"""
            current=getattr(self,'_qizi_add_window',None)
            if current and current.winfo_exists():
                current.lift();return
            win=tk.Toplevel(self)
            self._qizi_add_window=win
            win.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei')
            win.geometry('515x333');win.resizable(False,False)
            win.transient(self)
            book=ttk.Notebook(win)
            book.pack(fill='both',expand=True,padx=9,pady=(9,3))
            bulk=ttk.Frame(book,padding=8)
            manual=ttk.Frame(book,padding=8)
            book.add(bulk,text='批量添加')
            book.add(manual,text='手动输入')
            ttk.Label(bulk,text='支持 地址:端口:账号:密码、字段块、curl -x、代理链接；可粘贴多条').pack(anchor='w')
            inputbox=tk.Text(bulk,height=8,font=('Consolas',9),wrap='none')
            inputbox.pack(fill='both',expand=True,pady=(5,2))
            manual_vars={}
            for r,(key,title) in enumerate([
                ('name','出口名称（可选）'),('host','IP / 服务器'),('port','端口'),
                ('username','用户名'),('password','密码')]):
                ttk.Label(manual,text=title).grid(row=r,column=0,sticky='w',pady=5)
                var=tk.StringVar();manual_vars[key]=var
                ttk.Entry(manual,textvariable=var,width=34,
                          show='*' if key=='password' else '').grid(row=r,column=1,sticky='ew',padx=9,pady=5)
            manual.columnconfigure(1,weight=1)
            # 手动页也支持直接把完整代理字符串粘贴进服务器输入框。
            def smart_paste(event=None):
                widget = event.widget
                try:
                    clipboard = win.clipboard_get()
                except tk.TclError:
                    return None
                if not clipboard.strip():
                    return None
                records, errors = qizi_parse_proxy_lines(clipboard)
                if len(records) != 1 or errors:
                    return None  # 普通主机名/不完整文本仍使用 Tk 默认粘贴
                rec = records[0]
                for key in ('host','port','username','password'):
                    manual_vars[key].set(str(rec[key]))
                state.set('已自动识别服务器、端口、账号和密码')
                return 'break'
            host_entry = next((w for w in manual.grid_slaves(row=1,column=1) if isinstance(w,ttk.Entry)),None)
            if host_entry:
                host_entry.bind('<<Paste>>',smart_paste)
                host_entry.bind('<Control-v>',smart_paste)

            state=tk.StringVar(value='MMDB：程序目录自动发现；默认离线识别')
            ttk.Label(win,textvariable=state,foreground='#666666',wraplength=495).pack(
                fill='x',padx=12,pady=(1,2))
            buttons=ttk.Frame(win)
            buttons.pack(fill='x',padx=12,pady=(0,9))
            def submit():
                try:
                    if book.index(book.select())==0:
                        records,errors=qizi_parse_proxy_lines(inputbox.get('1.0','end'))
                        if errors:
                            raise ValueError('\n'.join(errors[:8]))
                    else:
                        data={k:v.get().strip() for k,v in manual_vars.items()}
                        host=data['host']
                        port=int(data['port'])
                        if not host or not 1<=port<=65535:
                            raise ValueError('请填写有效的服务器地址和端口')
                        records=[dict(host=host,port=port,username=data['username'],
                                      password=data['password'],name=data['name'] or host)]
                    if not records:
                        raise ValueError('请先输入至少一个出口')
                except (ValueError,TypeError) as exc:
                    messagebox.showwarning('输入错误',str(exc),parent=win)
                    return
                db=discover_mmdb(DATA_DIR,self._qizi_geo_db)
                if db and db!=self._qizi_geo_db:
                    self._qizi_set_mmdb(db)
                known={(str(p.get('host')),int(p.get('port',0)),str(p.get('username','')))
                       for p in self.exits}
                added=[]
                for item in records:
                    key=(item['host'],item['port'],item.get('username',''))
                    if key in known:continue
                    p=dict(item,id=str(uuid.uuid4()),name=item.get('name') or item['host'],
                           type=item.get('type','http'),status='待检测',public_ip='',country='',city='',
                           geo_raw_lat='',geo_raw_lon='',geo_lat='',geo_lon='',
                           geo_status='等待 MMDB')
                    self.exits.append(p);added.append(p);known.add(key)
                if not added:
                    messagebox.showinfo('提示','这些出口已存在，未重复添加',parent=win)
                    return
                save_json(EXITS_FILE,self.exits)
                self.refresh_exits()
                self.request_gateway_reconcile('新增出口')
                self.log_operation(f'已添加 {len(added)} 条出口，正在检测网络')
                win.destroy()
                if not db:
                    self.log_operation('未发现 MMDB：请将 .mmdb 放入程序主目录；未来预留服务器查询接口')
                # Kick off detection on the running instance. On large imports,
                # don't flood the four-worker health pool with hundreds of jobs.
                # Index is unstable after deletes/sorts, therefore queue exit IDs.
                for p in added:
                    self._qizi_new_exit_check_queue.append(str(p['id']))
                if not self._qizi_new_exit_check_active:
                    self._qizi_new_exit_check_active=True
                    self.after(60,self._qizi_new_exit_check_tick)
            ttk.Button(buttons,text='取消',command=win.destroy).pack(side='right')
            ttk.Button(buttons,text='添加并检测',command=submit).pack(side='right',padx=6)
            win.bind('<Escape>',lambda e:win.destroy())

        def _qizi_retry_geo_tick(self):
            """自动重试失败的 MMDB 查询，避免新 IP 因一次查询失败永久卡住。"""
            if getattr(self,'_closing',False):return
            self._qizi_scan_unlocated_exits()
            self.after(60000,self._qizi_retry_geo_tick)

        def _qizi_scan_unlocated_exits(self):
            """只使用最新、已核实的公网 IP 定位；避免代理服务器地址的旧坐标。"""
            if getattr(self,'_closing',False):return
            db=discover_mmdb(DATA_DIR,self._qizi_geo_db)
            if not db:return
            self._qizi_geo_db=db
            waiting=[]
            for p in list(getattr(self,'exits',[])):
                eid=str(p.get('id',''))
                # 出口公网 IP 未检测出时先等，避免误把代理接入IP当成公网出口。
                public=str(p.get('public_ip') or '').strip()
                if not eid or not public:continue
                if self._qizi_geo_retry_after.get(eid,0)>time.monotonic():continue
                if (str(p.get('geo_source_ip') or '')==public and
                        p.get('geo_lat') not in (None,'') and p.get('geo_lon') not in (None,'')):continue
                if self._qizi_geo_requested.get(eid)==public:continue
                self._qizi_geo_requested[eid]=public
                waiting.append(p)
            if waiting:self._qizi_start_geo(waiting,db)

        def refresh_exits(self,selected=None):
            result=super().refresh_exits(selected)
            self._qizi_scan_unlocated_exits()
            return result

        def _qizi_test_by_id(self,eid):
            for i,p in enumerate(self.exits):
                if p.get('id')==eid:
                    self.start_direct_test(i)
                    return True
            return False

        def _qizi_new_exit_check_tick(self):
            """Process freshly added exits without waiting for another startup."""
            if getattr(self,'_closing',False):
                self._qizi_new_exit_check_active=False
                return
            import time as _time
            now=_time.monotonic()
            # A health worker normally finishes within seconds, but don't mark
            # success until the existing HTTPS result event actually arrives.
            active=set(getattr(self,'test_jobs',{}))
            for eid,started in list(self._qizi_new_exit_check_started.items()):
                if eid not in active:
                    self._qizi_new_exit_check_started.pop(eid,None)
                elif now-started>20:
                    # Stuck job: leave it under the existing worker's ownership;
                    # don't corrupt test_jobs or launch a duplicate.
                    self._qizi_new_exit_check_started.pop(eid,None)
                    self.log_operation('新增出口检测等待超时：请右键重检 '+str(eid)[:8])
            available=max(0, 3-len(active))
            while available and self._qizi_new_exit_check_queue:
                eid=self._qizi_new_exit_check_queue.pop(0)
                if eid in active:
                    continue
                if self._qizi_test_by_id(eid):
                    self._qizi_new_exit_check_started[eid]=now
                    available-=1
            if self._qizi_new_exit_check_queue or self._qizi_new_exit_check_started:
                self.after(800,self._qizi_new_exit_check_tick)
            else:
                self._qizi_new_exit_check_active=False

        def _qizi_start_geo(self,items,db):
            ids=[p['id'] for p in items]
            if self._qizi_geo_running:
                self._qizi_geo_waiting.append((list(items),db))
                return
            self._qizi_geo_running=True
            def worker():
                try:
                    path=db
                    for eid in ids:
                        p=next((p for p in self.exits if p.get('id')==eid),None)
                        if p is None:continue
                        try:
                            source_ip=p.get('public_ip') or p['host']
                            result=resolve_ip(path,source_ip)
                            if not result:raise ValueError('数据库中没有该 IP')
                            country=result.get('国家/地区','');city=result.get('城市','')
                            rawlat=result.get('纬度','');rawlon=result.get('经度','')
                            candidate=result.get('candidate')
                            update=dict(_qizi_source_ip=source_ip,geo_source_ip=source_ip,country=country,city=city,geo_raw_lat=str(rawlat),geo_raw_lon=str(rawlon),
                                geo_lat=candidate['lat_text'] if candidate else str(rawlat),
                                geo_lon=candidate['lon_text'] if candidate else str(rawlon),
                                geo_status=candidate['method'] if candidate else '没有经纬度',
                                geo_accuracy_radius_m=(float(result['精度半径(km)'])*1000 if result.get('精度半径(km)') not in ('',None) else None),
                                geo_candidate_reference_m=candidate['radius_m'] if candidate else None,
                                geo_distance_m=candidate['offset_m'] if candidate else 0.0)
                            self._qizi_geo_ui_queue.put(('result',(eid,update)))
                        except Exception as exc:
                            failed_source=locals().get('source_ip','')
                            self._qizi_geo_ui_queue.put(('result',(eid,{
                                '_qizi_source_ip':failed_source,'geo_source_ip':failed_source,
                                'geo_status':'查询失败：'+str(exc)[:180]})))
                except Exception as exc:
                    self._qizi_geo_ui_queue.put(('log','地理位置数据库读取失败：'+str(exc)[:180]))
                finally:
                    self._qizi_geo_ui_queue.put(('finished',None))
            threading.Thread(target=worker,name='qizi-bulk-geo',daemon=True).start()

        def _qizi_drain_geo_queue(self):
            """仅在 Tk 主线程执行；销毁窗口时不再安排轮询。"""
            try:
                while True:
                    kind, payload = self._qizi_geo_ui_queue.get_nowait()
                    if kind == 'result':
                        eid, update = payload
                        self._qizi_geo_result(eid, update)
                    elif kind == 'log':
                        self.log_operation(payload)
                    elif kind == 'finished':
                        self._qizi_geo_finished()
            except queue.Empty:
                pass
            if self.winfo_exists():
                self.after(100, self._qizi_drain_geo_queue)

        def _qizi_geo_finished(self):
            self._qizi_geo_running=False
            if self._qizi_geo_waiting:
                items,db=self._qizi_geo_waiting.pop(0)
                self._qizi_start_geo(items,db)

        def _qizi_geo_result(self,eid,update):
            p=next((p for p in self.exits if p.get('id')==eid),None)
            if p is None:return
            expected=update.pop('_qizi_source_ip',None)
            if expected and (p.get('public_ip') or p.get('host')) != expected:
                return  # 动态 IP 已更新，避免旧 MMDB 回调覆盖新坐标
            if not update.get('geo_lat') or not update.get('geo_lon'):
                # 查询失败/缺少地理坐标不得标记为已定位；60 秒后自动重查。
                update['geo_source_ip']=''
                update['geo_lat']=''
                update['geo_lon']=''
                self._qizi_geo_requested.pop(eid,None)
                self._qizi_geo_retry_after[eid]=time.monotonic()+60
            else:
                self._qizi_geo_retry_after.pop(eid,None)
            p.update(update)
            if p.get('city') and p.get('name')==p.get('host'):
                p['name']=f'{p.get("country","")} {p.get("city","")} {p["host"]}'
            save_json(EXITS_FILE,self.exits)
            self.refresh_exits()
            self.log_operation(f'{p["host"]}：地理位置 {p.get("city") or "未知"}；{p.get("geo_status","")}')
            # MMDB 在绑定出口之后才返回时，也要回填当前已绑定设备。
            for ip, device in list(self.devices.items()):
                if device.get('exit_id') == eid:
                    self._qizi_apply_exit_coordinates(ip, eid, reason='出口地理位置查询完成')

        def disconnect_row(self,ip):
            uid=self.uid_for_ip(ip)
            if uid:
                self._qizi_wifi_manual_off.add(str(uid))
                self._auto_attempted_uids.add(str(uid))
                self.pending_actions.pop(ip,None)
                if ip in self.devices:
                    self.devices[ip]['qizi_wifi_manual_off']=True
                    save_devices(self.devices)
            return super().disconnect_row(ip)

        def connect_row(self,ip):
            uid=self.uid_for_ip(ip)
            if uid:self._qizi_wifi_manual_off.discard(str(uid))
            if ip in self.devices:
                self.devices[ip].pop('qizi_wifi_manual_off',None)
                save_devices(self.devices)
            return super().connect_row(ip)

        def _maybe_auto_restore_wifi(self):
            # 自动恢复仍然保留，但手动断开的设备在本次程序运行期间不再恢复。
            for uid in self._qizi_wifi_manual_off:self._auto_attempted_uids.add(uid)
            return super()._maybe_auto_restore_wifi()

        def _qizi_apply_exit_coordinates(self, ip, eid, reason='出口切换'):
            """只更新已绑定出口对应的设备坐标，不向设备发送定位命令。"""
            if not eid or self.devices.get(ip, {}).get('exit_id') != eid:
                return False
            p = next((x for x in self.exits if x.get('id') == eid), None)
            if not p or not p.get('geo_lat') or not p.get('geo_lon'):
                self.log_operation(f'{ip}：出口暂无候选坐标，等待 MMDB 查询完成')
                return False
            uid = self.uid_for_ip(ip)
            state = self.ios_states.get(uid, {}) if uid else {}
            if state.get('location') in ('定位中', '准备发送', '正在停止', '正在清除', '正在恢复') or (uid and self._route_is_running(uid)):
                self.log_operation(f'{ip}：定位/寻路正在运行，未覆盖当前坐标')
                return False
            row = self.row_widgets.get(ip)
            if not row:
                return False
            lat, lon = str(p['geo_lat']), str(p['geo_lon'])
            # 先写入持久坐标，再刷新可视输入框；防止 refresh_devices 在 key 变化时覆盖输入。
            key = uid or 'ip:' + ip
            self.coordinates[key] = dict(lat=lat, lon=lon, interval=1)
            if uid:
                self.coordinates.pop('ip:' + ip, None)
            self.save_ios()
            row['key'] = key
            row['lat'].set(lat)
            row['lon'].set(lon)
            self.devices[ip]['qizi_last_auto_exit'] = eid
            self.devices[ip]['qizi_last_auto_lat'] = lat
            self.devices[ip]['qizi_last_auto_lon'] = lon
            save_devices(self.devices)
            self.log_operation(f'{ip}：{reason}，已填写经纬度：{lat}, {lon}')
            return True

        def bind_row_exit(self,ip):
            # 基类可能同步刷新设备行，所以应在基类绑定完成后读取最终 exit_id。
            previous = self.devices.get(ip, {}).get('exit_id', '')
            result = super().bind_row_exit(ip)
            current = self.devices.get(ip, {}).get('exit_id', '')
            if current and (current != previous or not self.row_widgets.get(ip, {}).get('lat').get().strip()):
                self._qizi_apply_exit_coordinates(ip, current)
            return result


    class QiziReliableApp(QiziReliabilityMixin, QiziBulkGeoApp):
        """Final cooperative reliability layer for the existing app."""
        pass

    return QiziReliableApp
