# -*- coding: utf-8 -*-
"""独立 IP 坐标候选测试工具，不启动代理端口，不触碰多设备管理器配置。"""
import ipaddress
import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk, messagebox
from qizi_geo_engine import resolve_ip

HERE=Path(__file__).resolve().parent
class GeoTest(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('多设备管理器-v3.10.17 有问题联系TG:Laosermei')
        self.geometry('760x610')
        self.db=tk.StringVar()
        self.ip=tk.StringVar(value='8.8.8.8')
        self.status=tk.StringVar(value='请选择 MMDB 数据库；不需要关闭旧版管理器')
        self.radius_override=tk.StringVar()
        root=ttk.Frame(self,padding=16);root.pack(fill='both',expand=True)
        ttk.Label(root,text='MMDB 数据库（本地离线）').pack(anchor='w')
        r=ttk.Frame(root);r.pack(fill='x',pady=7)
        ttk.Entry(r,textvariable=self.db).pack(side='left',fill='x',expand=True)
        ttk.Button(r,text='选择数据库',command=self.choose).pack(side='left',padx=6)
        ttk.Label(root,text='输入 IP 地址（也可粘贴 IP:端口:账号:密码，只读取 IP）').pack(anchor='w',pady=(12,0))
        r=ttk.Frame(root);r.pack(fill='x',pady=7)
        ent=ttk.Entry(r,textvariable=self.ip);ent.pack(side='left',fill='x',expand=True)
        ent.bind('<Return>',lambda e:self.query())
        self.btn=ttk.Button(r,text='查询并生成12位坐标',command=self.query)
        self.btn.pack(side='left',padx=6)
        ttk.Label(root,text='可选：手动误差半径（米；留空使用 MMDB 提供的半径）').pack(anchor='w')
        ttk.Entry(root,textvariable=self.radius_override).pack(fill='x',pady=5)
        self.out=tk.Text(root,font=('Consolas',11),wrap='word',height=20)
        self.out.pack(fill='both',expand=True,pady=10)
        ttk.Label(root,textvariable=self.status).pack(anchor='w')
        ttk.Label(root,text='注意：输出12位小数，坐标发生小范围人为偏移；不代表IP真实位置更准。',foreground='#a35d00',wraplength=700).pack(anchor='w',pady=10)
        for folder in (HERE,HERE/'databases'):
            for p in folder.glob('*.mmdb'):
                self.db.set(str(p));return
    def choose(self):
        p=filedialog.askopenfilename(filetypes=[('MMDB','*.mmdb'),('All','*.*')]);
        if p:self.db.set(p)
    def query(self):
        raw=self.ip.get().strip().split(':')[0]
        try:
            ip=ipaddress.ip_address(raw)
            if not ip.is_global:raise ValueError('请输入公网 IP')
            if not Path(self.db.get()).is_file():raise ValueError('请选择有效 MMDB 文件')
            override=float(self.radius_override.get()) if self.radius_override.get().strip() else None
            if override is not None and (not 0<override<=1000000):raise ValueError('手动半径必须大于 0 且不超过 1000000 米')
        except ValueError as e:
            messagebox.showerror('输入错误',str(e));return
        self.btn.config(state='disabled');self.status.set('正在本地查询…')
        def worker():
            try:
                record=resolve_ip(self.db.get(),str(ip),override)
                if record is None:output='数据库无此 IP 记录'
                else:
                    c=record.get('candidate')
                    output=(f"IP：{ip}\n国家：{record['国家/地区']}\n城市：{record['城市']}\n"
                            f"原始纬度：{record['纬度']}\n原始经度：{record['经度']}\n"
                            f"数据库定位误差半径：{record['精度半径(km)']} km\n手动覆盖参考距离：{override if override is not None else '无，按经纬度显示位数估算'} 米\n\n")
                    if c:
                        output+=(f"候选纬度：{c['lat_text']}\n候选经度：{c['lon_text']}\n"
                                 f"偏移距离：{c['offset_m']:.3f} 米\n方式：{c['method']}\n")
                    output+='\n注意：这只是误差范围内的候选点，不代表 IP 的实际物理位置。'
                status='查询完成（没有占用代理端口）'
            except Exception as e:
                output=f'查询失败：{type(e).__name__}: {e}';status='查询失败'
            self.after(0,lambda:self.done(output,status))
        threading.Thread(target=worker,daemon=True).start()
    def done(self,output,status):
        self.out.delete('1.0','end');self.out.insert('end',output)
        self.status.set(status);self.btn.config(state='normal')
if __name__=='__main__':GeoTest().mainloop()
