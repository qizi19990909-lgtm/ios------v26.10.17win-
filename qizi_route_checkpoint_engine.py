# -*- coding: utf-8 -*-
"""自动寻路剩余时间和检查点纯逻辑（不接触 Tkinter/代理）。"""
import math
import time

def remaining_seconds(total_points, next_index):
    return max(0,int(total_points)-max(0,int(next_index)))

def remaining_minutes(total_points,next_index):
    return int(math.ceil(remaining_seconds(total_points,next_index)/60.0))

def countdown_text(total_points,next_index):
    minutes=remaining_minutes(total_points,next_index)
    return f'{minutes//60}:{minutes%60:02d}'

def eta_chinese(total_points,next_index=0):
    minutes=remaining_minutes(total_points,next_index)
    return f'{minutes//60}小时{minutes%60:02d}分'

def build_checkpoint(state,coordinate=None,initial_coordinate=None):
    coord=coordinate or {}
    return {
        'uid':str(state.get('uid') or ''),
        'ip':str(state.get('ip') or ''),
        'running':bool(state.get('running')),
        'paused':bool(state.get('paused')),
        'next_index':max(0,int(state.get('next_index',0) or 0)),
        'target_exit_id':str(state.get('target_exit_id') or ''),
        'last_lat':coord.get('lat',state.get('last_lat')),
        'last_lon':coord.get('lon',state.get('last_lon')),
        'stage':str(state.get('stage') or ''),
        'waiting_reason':str(state.get('waiting_reason') or ''),
        'initial_coordinate': initial_coordinate,
        'updated':time.time(),
    }
