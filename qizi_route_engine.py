# -*- coding: utf-8 -*-
"""七子多设备管理器：自动寻路规划与秒级轨迹模块。

规划阶段：真实汽车道路(OSRM/OSM) -> 出发机场 -> 大圆飞行 -> 到达机场 -> 真实汽车道路。
执行阶段不再访问网络，只播放已经冻结的秒级坐标。
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import time
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

AIRPORT_URL = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airports.csv"
OSRM_BASE = "https://router.project-osrm.org"
USER_AGENT = "QiziAutoRoute/2.9"
EARTH_R = 6371008.8


def http_get(url: str, timeout: float = 35.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def haversine_m(a, b) -> float:
    lat1, lon1 = map(math.radians, a); lat2, lon2 = map(math.radians, b)
    dlat, dlon = lat2-lat1, lon2-lon1
    h = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return 2*EARTH_R*math.asin(min(1.0, math.sqrt(h)))


def cumulative_lengths(points):
    c=[0.0]
    for i in range(1,len(points)):
        c.append(c[-1]+haversine_m(points[i-1],points[i]))
    return c


def point_at_distance(points,cumulative,target_m):
    if not points: raise ValueError("空路线")
    if len(points)==1 or target_m<=0:return points[0]
    if target_m>=cumulative[-1]:return points[-1]
    lo,hi=0,len(cumulative)-1
    while lo+1<hi:
        mid=(lo+hi)//2
        if cumulative[mid]<=target_m:lo=mid
        else:hi=mid
    span=max(1e-9,cumulative[hi]-cumulative[lo]);t=(target_m-cumulative[lo])/span
    return (points[lo][0]+(points[hi][0]-points[lo][0])*t,
            points[lo][1]+(points[hi][1]-points[lo][1])*t)


def great_circle_points(a,b,max_step_m=18000.0):
    lat1,lon1=map(math.radians,a);lat2,lon2=map(math.radians,b)
    def vec(lat,lon):return (math.cos(lat)*math.cos(lon),math.cos(lat)*math.sin(lon),math.sin(lat))
    v1,v2=vec(lat1,lon1),vec(lat2,lon2)
    dot=max(-1.0,min(1.0,sum(x*y for x,y in zip(v1,v2))))
    omega=math.acos(dot);dist=EARTH_R*omega
    count=max(2,min(1600,int(math.ceil(dist/max_step_m))+1))
    if omega<1e-10:return [a,b]
    s=math.sin(omega);out=[]
    for i in range(count):
        t=i/(count-1);s1=math.sin((1-t)*omega)/s;s2=math.sin(t*omega)/s
        x=s1*v1[0]+s2*v2[0];y=s1*v1[1]+s2*v2[1];z=s1*v1[2]+s2*v2[2]
        out.append((math.degrees(math.atan2(z,math.hypot(x,y))),math.degrees(math.atan2(y,x))))
    return out


@dataclass
class Airport:
    name:str;iata:str;ident:str;lat:float;lon:float;country:str;kind:str
    @property
    def coord(self):return (self.lat,self.lon)
    @property
    def label(self):return f"{self.name} ({self.iata or self.ident})"

@dataclass
class RouteLeg:
    kind:str;title:str;points:list;speed_kmh:float;source:str
    def __post_init__(self):
        self.points=[(float(a),float(b)) for a,b in self.points]
        self.cumulative=cumulative_lengths(self.points)
        self.distance_m=self.cumulative[-1] if self.cumulative else 0.0

@dataclass
class RoutePlan:
    start:tuple;destination:tuple;legs:list;origin_airport:Airport|None=None;dest_airport:Airport|None=None
    @property
    def distance_m(self):return sum(x.distance_m for x in self.legs)

@dataclass
class TimelinePoint:
    second:int;lat:float;lon:float;leg_index:int;kind:str;stage:str;speed_kmh:float;virtual_seconds:float


class AirportDatabase:
    def __init__(self,data_dir,logger=lambda _x:None):
        self.data_dir=Path(data_dir);self.cache=self.data_dir/'qizi_airports_cache.csv';self.logger=logger;self.airports=None
    def load(self):
        if self.airports is not None:return self.airports
        data=None
        if self.cache.exists():
            try:
                if time.time()-self.cache.stat().st_mtime<7*86400:data=self.cache.read_bytes()
            except OSError:pass
        if data is None:
            self.logger('正在获取机场数据…');data=http_get(AIRPORT_URL,40)
            try:self.cache.write_bytes(data)
            except OSError:pass
        rows=[]
        for r in csv.DictReader(io.StringIO(data.decode('utf-8-sig','replace'))):
            if r.get('scheduled_service')!='yes' or r.get('type') not in ('large_airport','medium_airport') or not (r.get('iata_code') or '').strip():continue
            try:lat,lon=float(r['latitude_deg']),float(r['longitude_deg'])
            except Exception:continue
            rows.append(Airport((r.get('name') or 'Airport').strip(),(r.get('iata_code') or '').strip(),
                (r.get('ident') or '').strip(),lat,lon,(r.get('iso_country') or '').strip(),(r.get('type') or '').strip()))
        if not rows:raise RuntimeError('机场数据为空')
        self.airports=rows;return rows
    def nearest(self,coord):
        airports=self.load();large=[a for a in airports if a.kind=='large_airport']
        if large:
            best=min(large,key=lambda a:haversine_m(coord,a.coord))
            if haversine_m(coord,best.coord)<=500000:return best
        return min(airports,key=lambda a:haversine_m(coord,a.coord))


def osrm_drive(a,b):
    coords=f"{a[1]:.7f},{a[0]:.7f};{b[1]:.7f},{b[0]:.7f}"
    url=f"{OSRM_BASE}/route/v1/driving/{coords}?overview=full&geometries=geojson&steps=true&alternatives=false"
    payload=json.loads(http_get(url,35).decode('utf-8','replace'))
    if payload.get('code')!='Ok' or not payload.get('routes'):
        raise RuntimeError('OSRM 未找到真实可驾驶道路：'+str(payload.get('message') or payload.get('code') or '未知错误'))
    geometry=(payload['routes'][0].get('geometry') or {}).get('coordinates') or []
    if len(geometry)<2:raise RuntimeError('OSRM 返回的道路 geometry 为空')
    return [(float(lat),float(lon)) for lon,lat in geometry]


def drive_real_route(a,b,logger=lambda _x:None):
    if haversine_m(a,b)<25:return [a,b],'短距离连接'
    try:pts=osrm_drive(a,b)
    except Exception as exc:raise RuntimeError('真实道路规划失败；未使用直线回退：'+str(exc)) from exc
    logger(f'真实道路：{len(pts):,} 个道路点')
    return pts,'OSRM/OSM 真实道路'


def build_route(start,dest,car_kmh,plane_kmh,data_dir,logger=lambda _x:None,force_flight=True):
    start=(float(start[0]),float(start[1]));dest=(float(dest[0]),float(dest[1]))
    if not (-90<=start[0]<=90 and -180<=start[1]<=180 and -90<=dest[0]<=90 and -180<=dest[1]<=180):raise ValueError('经纬度超出范围')
    db=AirportDatabase(data_dir,logger)
    if not force_flight and haversine_m(start,dest)<450000:
        pts,src=drive_real_route(start,dest,logger);return RoutePlan(start,dest,[RouteLeg('drive','汽车：起点 → 目的地',pts,float(car_kmh),src)])
    a1,a2=db.nearest(start),db.nearest(dest)
    if a1.ident==a2.ident:
        pts,src=drive_real_route(start,dest,logger);return RoutePlan(start,dest,[RouteLeg('drive','汽车：起点 → 目的地',pts,float(car_kmh),src)],a1,a2)
    logger('出发机场：'+a1.label);logger('到达机场：'+a2.label)
    road1,src1=drive_real_route(start,a1.coord,logger);road2,src2=drive_real_route(a2.coord,dest,logger)
    flight=great_circle_points(a1.coord,a2.coord)
    return RoutePlan(start,dest,[RouteLeg('drive',f'汽车：起点 → {a1.label}',road1,float(car_kmh),src1),
        RouteLeg('flight',f'飞机：{a1.label} → {a2.label}',flight,float(plane_kmh),'大圆航线'),
        RouteLeg('drive',f'汽车：{a2.label} → 目的地',road2,float(car_kmh),src2)],a1,a2)


def precompute_timeline(plan,car_kmh,plane_kmh,multiplier=1.0):
    multiplier=float(multiplier)
    if multiplier<=0:raise ValueError('速度倍率必须大于0')
    timeline=[TimelinePoint(0,float(plan.start[0]),float(plan.start[1]),0,'start','起点',0.0,0.0)]
    second=0;virtual_elapsed=0.0;estimated=1
    for leg in plan.legs:
        speed=float(car_kmh if leg.kind=='drive' else plane_kmh);meters=max(.01,speed/3.6*multiplier)
        estimated+=max(1,int(math.ceil(leg.distance_m/meters)))
    if estimated>300000:raise ValueError(f'当前速度需要生成约 {estimated:,} 个秒级坐标，过多')
    for leg_index,leg in enumerate(plan.legs):
        speed=float(car_kmh if leg.kind=='drive' else plane_kmh);meters=max(.01,speed/3.6*multiplier)
        steps=max(1,int(math.ceil(leg.distance_m/meters)))
        for i in range(1,steps+1):
            lat,lon=point_at_distance(leg.points,leg.cumulative,min(leg.distance_m,i*meters));second+=1;virtual_elapsed+=multiplier
            timeline.append(TimelinePoint(second,float(lat),float(lon),leg_index,leg.kind,leg.title,speed,virtual_elapsed))
    last=timeline[-1]
    if haversine_m((last.lat,last.lon),plan.destination)>.25:
        second+=1;timeline.append(TimelinePoint(second,float(plan.destination[0]),float(plan.destination[1]),max(0,len(plan.legs)-1),'destination','最终目的地',0.0,virtual_elapsed))
    else:
        timeline[-1]=TimelinePoint(last.second,float(plan.destination[0]),float(plan.destination[1]),last.leg_index,last.kind,last.stage,last.speed_kmh,last.virtual_seconds)
    return timeline


def timeline_to_dict(timeline):
    return [[p.second,p.lat,p.lon,p.leg_index,p.kind,p.stage,p.speed_kmh,p.virtual_seconds] for p in timeline]

def timeline_from_dict(rows):
    return [TimelinePoint(int(r[0]),float(r[1]),float(r[2]),int(r[3]),str(r[4]),str(r[5]),float(r[6]),float(r[7])) for r in rows]

def save_route_plans(path,plans):
    path=Path(path);tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(plans,ensure_ascii=False,separators=(',',':')),encoding='utf-8');os.replace(tmp,path)

def load_json_dict(path):
    path=Path(path)
    try:
        value=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        return value if isinstance(value,dict) else {}
    except Exception:return {}
