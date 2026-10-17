# -*- coding: utf-8 -*-
"""V2.9.29.9: 自动寻路倒计时、当前位置快照、断电续行、历史信任显示。"""
import json
import math
import time
from pathlib import Path
from qizi_route_checkpoint_engine import countdown_text, eta_chinese, build_checkpoint
from qizi_route_engine import save_route_plans, load_json_dict


def make_route_ui_mixin(data_dir):
    folder=Path(data_dir)

    class QiziRouteUiMixin:
        def __init__(self,*args,**kwargs):
            self._qizi_route_checkpoint_path=folder/'qizi_route_position.json'
            self._qizi_route_positions=load_json_dict(self._qizi_route_checkpoint_path)
            self._qizi_trust_cache_path=folder/'qizi_trusted_uids.json'
            raw=load_json_dict(self._qizi_trust_cache_path)
            self._qizi_known_trust={str(uid) for uid,valid in raw.items() if valid}
            self._qizi_checkpoint_last_write={}
            self._qizi_live_positions_path=folder/'qizi_device_live_positions.json'
            self._qizi_live_positions=load_json_dict(self._qizi_live_positions_path)
            self._qizi_live_last_write={}
            super().__init__(*args,**kwargs)
            self._qizi_restore_configured_coordinates()
            # Legacy associations were written after manual association/trust.
            # They are evidence of a remembered pairing, NOT a fresh verification.
            self._qizi_known_trust.update(str(x) for x in self.links.values() if x)
            self._qizi_restore_trust_badges()
            self._qizi_timer_job=self.after(1000,self._qizi_route_ui_pulse)
            self.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei')

        def _qizi_restore_trust_badges(self):
            for uid in self._qizi_known_trust:
                self.ios_states.setdefault(uid,{})['trusted']=True

        def _qizi_capture_verified_trust(self):
            found={str(uid) for uid,state in self.ios_states.items() if state.get('trusted')}
            new=found-self._qizi_known_trust
            if new:
                self._qizi_known_trust.update(new)
                try:
                    save_route_plans(self._qizi_trust_cache_path,{uid:True for uid in sorted(self._qizi_known_trust)})
                except OSError as exc:
                    self.log_operation('历史信任记录写入失败：'+str(exc)[:110])

        def poll_ios(self):
            result=super().poll_ios()
            self._qizi_capture_verified_trust()
            return result

        def refresh_devices(self,selected_ip=None):
            # Scan and proxy-offline are not pair/untrust operations.
            self._qizi_restore_trust_badges()
            result=super().refresh_devices(selected_ip)
            self._qizi_update_countdown_buttons()
            return result

        def _qizi_update_countdown_buttons(self):
            for ip,row in list(getattr(self,'row_widgets',{}).items()):
                uid=self.uid_for_ip(ip)
                state=self.route_states.get(str(uid),{}) if uid else {}
                if not state.get('running'):continue
                plan=self.route_plans.get(str(uid),{})
                timeline=plan.get('timeline') or []
                if not timeline:continue
                remaining=countdown_text(len(timeline),state.get('next_index',0))
                try:
                    if row['route'].winfo_exists():
                        row['route'].configure(text=remaining,fg=self.GREEN,state='normal')
                except Exception:pass

        def _qizi_track_device_coordinate(self,uid,data):
            """来自工作线程的定位事件已在 poll_ios 主线程中消费。"""
            uid=str(uid)
            if data.get('lat') is None or data.get('lon') is None:return
            self._qizi_store_live_coordinate(
                uid,data.get('lat'),data.get('lon'),
                str(data.get('movement') or 'unknown'),bool(data.get('confirmed')),
            )

        def _qizi_store_live_coordinate(self,uid,lat,lon,mode,confirmed):
            uid=str(uid)
            try:
                lat=float(lat);lon=float(lon)
                if not (-90<=lat<=90 and -180<=lon<=180):return
            except (TypeError,ValueError):return
            old=self._qizi_live_positions.get(uid,{})
            configured=self.coordinates.get(uid,{})
            interval=old.get('interval',configured.get('interval',1))
            try:interval=max(1,min(60,float(interval or 1)))
            except (TypeError,ValueError):interval=1
            self._qizi_live_positions[uid]={
                'lat':str(lat),'lon':str(lon),'mode':mode,
                'interval':interval,'confirmed':bool(confirmed),
                'updated':time.time(),
            }
            now=time.monotonic()
            if now-self._qizi_live_last_write.get(uid,0)<5:return
            try:
                save_route_plans(self._qizi_live_positions_path,self._qizi_live_positions)
                self._qizi_live_last_write[uid]=now
            except OSError as exc:self.log_operation('实时坐标保存失败：'+str(exc)[:100])

        def _qizi_live_coordinate(self,uid):
            position=self._qizi_live_positions.get(str(uid),{})
            try:
                lat=float(position.get('lat'));lon=float(position.get('lon'))
                interval=float(position.get('interval',1) or 1)
                if not (-90<=lat<=90 and -180<=lon<=180 and 1<=interval<=60):return None
            except (TypeError,ValueError):return None
            return dict(lat=str(lat),lon=str(lon),interval=interval)

        def _configured_coordinate(self,ip,uid=None):
            uid=str(uid or self.uid_for_ip(ip) or '')
            live=self._qizi_live_coordinate(uid)
            if live:return live
            return super()._configured_coordinate(ip,uid or None)

        def _qizi_restore_configured_coordinates(self):
            changed=False
            for uid,checkpoint in self._qizi_route_positions.items():
                if uid not in self.route_states or not isinstance(checkpoint,dict):continue
                original=checkpoint.get('initial_coordinate')
                if not isinstance(original,dict):continue
                current=self.coordinates.get(uid)
                state=self.route_states.get(uid,{})
                if not isinstance(current,dict) or state.get('last_lat') is None or state.get('last_lon') is None:
                    continue
                try:
                    was_route_coordinate=(
                        math.isclose(float(current.get('lat')),float(state['last_lat']),abs_tol=1e-9)
                        and math.isclose(float(current.get('lon')),float(state['last_lon']),abs_tol=1e-9)
                    )
                    lat=float(original.get('lat'));lon=float(original.get('lon'))
                    interval=float(original.get('interval',1) or 1)
                    if not (-90<=lat<=90 and -180<=lon<=180 and 1<=interval<=60):continue
                except (TypeError,ValueError):continue
                if not was_route_coordinate:continue
                self.coordinates[uid]=dict(lat=str(lat),lon=str(lon),interval=interval)
                changed=True
            if changed:
                self.save_ios()
                self.refresh_devices()

        def _apply_route_coordinate(self,ip,uid,lat,lon):
            self._qizi_store_live_coordinate(uid,lat,lon,'route',False)
            return super()._apply_route_coordinate(ip,uid,lat,lon)

        def close(self):
            try:save_route_plans(self._qizi_live_positions_path,self._qizi_live_positions)
            except OSError as exc:self.log_operation('实时坐标保存失败：'+str(exc)[:100])
            return super().close()

        def _qizi_route_ui_pulse(self):
            if getattr(self,'_closing',False):return
            self._qizi_update_countdown_buttons()
            self._qizi_timer_job=self.after(1000,self._qizi_route_ui_pulse)

        def _qizi_save_checkpoint(self,uid,force=False):
            uid=str(uid)
            state=self.route_states.get(uid)
            if not state:return
            now=time.monotonic()
            if not force and now-self._qizi_checkpoint_last_write.get(uid,0)<5:return
            state=dict(state,uid=uid)
            pos=self._qizi_route_positions.get(uid,{})
            coord=dict(self.coordinates.get(uid,{}) or {})
            if state.get('last_lat') is not None and state.get('last_lon') is not None:
                # Checkpoint must describe the *last route point*, not the static
                # origin stored in the user-editable coordinate settings.
                coord.update(lat=state['last_lat'],lon=state['last_lon'])
            original=pos.get('initial_coordinate')
            self._qizi_route_positions[uid]=build_checkpoint(state,coord,original)
            try:
                save_route_plans(self._qizi_route_checkpoint_path,self._qizi_route_positions)
                self._qizi_checkpoint_last_write[uid]=now
            except OSError as exc:self.log_operation('寻路检查点保存失败：'+str(exc)[:120])

        def _start_route(self,ip,uid,mode,status_var=None,resume=False,target_exit_id=None):
            uid=str(uid)
            st=self.route_states.get(uid,{})
            was_paused=bool(st.get('paused') and not st.get('running'))
            if was_paused and not resume:
                resume=True
                # Main location may have resumed random walk after Stop; stop it
                # before continuing from the saved next_index.
                self.ios.send(uid,{'op':'stop'})
                self.ios_states.setdefault(uid,{})['location']='正在停止'
            if not resume and not st.get('running'):
                self._qizi_route_positions[uid]={
                    'initial_coordinate':dict(self.coordinates.get(uid,{}) or {}),
                    'ip':ip,'updated':time.time(),
                }
            result=super()._start_route(ip,uid,mode,status_var,resume=resume,target_exit_id=target_exit_id)
            active=self.route_states.get(uid,{})
            if active.get('running'):
                active.pop('paused',None)
                gateway=getattr(self,'persistent_gateway',None)
                if gateway is not None and not active.get('origin_gateway'):
                    active['origin_gateway']={
                        'host':str(self._qizi_proxy_ip()),
                        'port':int(getattr(gateway,'port',0) or 0),
                    }
                    self._save_route_state()
                if was_paused:
                    runtime=self.route_runtime.setdefault(uid,{})
                    runtime['waiting_stop']=True
                    runtime['stop_deadline']=time.monotonic()+3.5
                self._qizi_save_checkpoint(uid,force=True)
                # Click Start -> the route window closes automatically.
                if not resume or was_paused:
                    dialog=self.route_dialogs.pop(uid,None)
                    if dialog is not None:
                        try:
                            if dialog.winfo_exists():dialog.destroy()
                        except Exception:pass
                self._qizi_refresh_usage()
                self._qizi_update_countdown_buttons()
            return result

        def _route_tick(self,uid):
            uid=str(uid)
            before=self.route_states.get(uid,{}).get('next_index',0)
            result=super()._route_tick(uid)
            if uid in self.route_states:
                if self.route_states[uid].get('next_index',0)!=before:
                    self._qizi_save_checkpoint(uid)
                self._qizi_update_countdown_buttons()
            return result

        def _stop_route(self,uid,manual=True,status_var=None):
            uid=str(uid)
            snapshot=dict(self.route_states.get(uid,{}) or {})
            if manual and snapshot:
                self._qizi_save_checkpoint(uid,force=True)
            result=super()._stop_route(uid,manual=manual,status_var=status_var)
            if manual and snapshot:
                # Base correctly restores destination exit + main location.
                # Only its deletion of progress is overridden.
                snapshot.update(running=False,paused=True,paused_at=time.time(),
                                waiting_reason='手动暂停；可从上次坐标继续')
                self.route_states[uid]=snapshot
                self._save_route_state()
                self._qizi_save_checkpoint(uid,force=True)
                self.log_operation((snapshot.get('ip') or uid)+'：已保存路线检查点，剩余 '+
                    eta_chinese(len((self.route_plans.get(uid) or {}).get('timeline') or []),snapshot.get('next_index',0)))
            self._qizi_refresh_usage()
            return result

        def _qizi_on_new_route_plan(self,uid):
            uid=str(uid)
            state=self.route_states.get(uid,{})
            if state.get('paused'):
                self.route_states.pop(uid,None)
                self._save_route_state()
                self._qizi_route_positions.pop(uid,None)
                try:save_route_plans(self._qizi_route_checkpoint_path,self._qizi_route_positions)
                except OSError as exc:self.log_operation('重规划检查点清理失败：'+str(exc)[:110])

        def _finish_route(self,uid):
            uid=str(uid)
            result=super()._finish_route(uid)
            self._qizi_route_positions.pop(uid,None)
            try:save_route_plans(self._qizi_route_checkpoint_path,self._qizi_route_positions)
            except OSError as exc:self.log_operation('已到达，但清理寻路检查点失败：'+str(exc)[:110])
            self._qizi_refresh_usage()
            return result


    return QiziRouteUiMixin
