# -*- coding: utf-8 -*-
"""七子多设备管理器：出口健康检测调度器。

只负责“什么时候检测哪些出口”，实际 HTTP 检测仍复用主程序已经验证的
start_direct_test/direct_test_worker，避免改动现有代理协议和状态显示。
"""
from __future__ import annotations

import time
import random


class ExitHealthScheduler:
    """Stagger exit probes instead of bursting every exit at once.

    - normal_interval: 一个出口正常情况下多久检测一次。
    - batch_size: 每次调度最多放入多少个出口。
    - start_spacing: 新出口初始检测错开，避免启动瞬间几十/几百请求并发。
    """
    def __init__(self, normal_interval=90.0, offline_interval=60.0,
                 batch_size=3, start_spacing=1.5):
        self.normal_interval = float(normal_interval)
        self.offline_interval = float(offline_interval)
        self.batch_size = max(1, int(batch_size))
        self.start_spacing = max(0.1, float(start_spacing))
        self.next_due = {}
        self._seeded = False
        # Per-exit randomized jitter avoids synchronized waves from many PCs.
        self._jitter = (0.87, 1.13)

    @staticmethod
    def _id(proxy, index):
        return str(proxy.get("id") or f"index:{index}")

    def _seed(self, exits, now):
        cursor = float(now)
        for index, proxy in enumerate(exits):
            eid = self._id(proxy, index)
            self.next_due.setdefault(eid, cursor)
            cursor += self.start_spacing
        self._seeded = True

    def due_indices(self, exits, now=None):
        now = time.monotonic() if now is None else float(now)
        if not self._seeded:
            self._seed(exits, now)
        valid = {self._id(p, i) for i, p in enumerate(exits)}
        for stale in list(self.next_due):
            if stale not in valid:
                self.next_due.pop(stale, None)
        candidates = []
        for index, proxy in enumerate(exits):
            eid = self._id(proxy, index)
            # New exits added *after* the initial seed should be checked soon,
            # rather than waiting index * spacing (hundreds of seconds for
            # large exit lists). The batch limit still prevents a burst.
            self.next_due.setdefault(eid, now + self.start_spacing)
            if self.next_due[eid] <= now:
                candidates.append((self.next_due[eid], index, eid, proxy))
        candidates.sort(key=lambda x: x[0])
        chosen = candidates[:self.batch_size]
        for _due, _index, eid, proxy in chosen:
            status = str(proxy.get("status", ""))
            interval = self.offline_interval if status.startswith("断线") else self.normal_interval
            self.next_due[eid] = now + interval * random.uniform(*self._jitter)
        return [item[1] for item in chosen]

    def mark_now(self, exits, index, delay=0.0):
        if 0 <= int(index) < len(exits):
            eid = self._id(exits[int(index)], int(index))
            self.next_due[eid] = time.monotonic() + max(0.0, float(delay))

    def mark_result(self, proxy, index=0, ok=True):
        eid = self._id(proxy, index)
        self.next_due[eid] = time.monotonic() + (
            self.normal_interval if ok else self.offline_interval
        ) * random.uniform(*self._jitter)

    def reset(self):
        self.next_due.clear()
        self._seeded = False
