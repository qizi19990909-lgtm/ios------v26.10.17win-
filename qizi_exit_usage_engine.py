# -*- coding: utf-8 -*-
"""简洁出口数量：当前使用量 + 运行中路线的未来出口预占量。"""

def current_counts(exits, devices, states=None, uid_for_ip=None, all_devices=None):
    valid={str(item.get('id','')) for item in exits}
    counts={eid:0 for eid in valid}
    for ip,dev in devices.items():
        current=str(dev.get('exit_id') or '')
        if current in counts:counts[current]+=1
    # 未来出口是已保存的路线预占用：即使设备暂时断联、隐藏也不能被再次分配。
    # 当前使用量只统计本机活跃设备，和未来预占量采用不同的条件。
    if states is None or uid_for_ip is None:return counts
    for ip,dev in (all_devices if all_devices is not None else devices).items():
        uid=uid_for_ip(ip)
        st=(states.get(str(uid)) or {}) if uid else {}
        if not st.get('running') or (st.get('ip') and st['ip']!=ip):continue
        future=str(st.get('target_exit_id') or '')
        current=str(dev.get('exit_id') or '')
        if future in counts and (ip not in devices or future!=current):
            counts[future]+=1
    return counts

def ordered_exit_indices(exits, counts):
    return sorted(range(len(exits)),key=lambda i:(counts.get(str(exits[i].get('id','')),0),i))

def future_exit_for_device(ip,devices,states,uid):
    if not uid:return ''
    state=states.get(str(uid)) or {}
    if not state.get('running') or (state.get('ip') and state['ip']!=ip):return ''
    return str(state.get('target_exit_id') or '')
