# -*- coding: utf-8 -*-
"""七子多设备管理器：自动寻路使用的精确定位发送辅助。

Wi-Fi 精确坐标由主程序现有持久 DVT worker 负责；本模块只负责 USB 数据线
模式的官方 pymobiledevice3 DVT set/clear 命令，并限制未结束子进程数量。
"""
from __future__ import annotations

import os
import subprocess
import threading
import time


class UsbRouteLocationSender:
    def __init__(self, interpreter, udid, on_result=None, max_processes=3):
        self.interpreter = [str(x) for x in interpreter]
        self.udid = str(udid)
        self.on_result = on_result
        self.max_processes = max(1, int(max_processes))
        self.lock = threading.RLock()
        self.processes = []
        self.ignored_pids = set()
        self.closed = False
        self.flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW") else 0

    def _emit(self, ok, message):
        if self.on_result:
            try:
                self.on_result(bool(ok), str(message))
            except Exception:
                pass

    def _reap(self):
        with self.lock:
            alive = []
            for proc in self.processes:
                if proc.poll() is None:
                    alive.append(proc)
                else:
                    try:
                        out = proc.stdout.read() if proc.stdout else ""
                    except Exception:
                        out = ""
                    if proc.returncode not in (0, None) and proc.pid not in self.ignored_pids:
                        self._emit(False, (out or f"USB定位命令退出码 {proc.returncode}")[-900:])
                    self.ignored_pids.discard(proc.pid)
            self.processes = alive

    def send(self, lat, lon):
        with self.lock:
            if self.closed:
                return False
        self._reap()
        env = os.environ.copy()
        env["PYMOBILEDEVICE3_UDID"] = self.udid
        cmd = self.interpreter + [
            "-m", "pymobiledevice3", "developer", "dvt", "simulate-location", "set",
            "--", str(float(lat)), str(float(lon)),
        ]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=self.flags, env=env,
            )
        except Exception as exc:
            self._emit(False, f"USB定位启动失败：{type(exc).__name__}: {exc}")
            return False
        with self.lock:
            self.processes.append(proc)
            while len(self.processes) > self.max_processes:
                old = self.processes.pop(0)
                if old.poll() is None:
                    self.ignored_pids.add(old.pid)
                    try:
                        old.terminate()
                        old.wait(timeout=1.0)
                    except Exception:
                        try: old.kill()
                        except Exception: pass
        threading.Thread(target=self._wait_one, args=(proc,), daemon=True).start()
        return True

    def _wait_one(self, proc):
        try:
            out, _ = proc.communicate(timeout=12)
            if proc.pid in self.ignored_pids:
                self.ignored_pids.discard(proc.pid)
                return
            if proc.returncode == 0:
                self._emit(True, "USB定位命令成功")
            else:
                self._emit(False, (out or f"USB定位命令退出码 {proc.returncode}")[-900:])
        except subprocess.TimeoutExpired:
            self._emit(False, "USB定位命令超时")
        except Exception as exc:
            self._emit(False, f"USB定位命令异常：{type(exc).__name__}: {exc}")

    def stop(self):
        with self.lock:
            self.closed = True
            procs = list(self.processes)
            self.processes.clear()
        for proc in procs:
            if proc.poll() is None:
                self.ignored_pids.add(proc.pid)
                try:
                    proc.terminate(); proc.wait(timeout=1.0)
                except Exception:
                    try: proc.kill()
                    except Exception: pass


def usb_clear_location(interpreter, udid, timeout=20):
    env = os.environ.copy()
    env["PYMOBILEDEVICE3_UDID"] = str(udid)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    result = subprocess.run(
        [str(x) for x in interpreter] + [
            "-m", "pymobiledevice3", "developer", "dvt", "simulate-location", "clear"
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=flags, env=env,
    )
    if result.returncode:
        raise RuntimeError((result.stdout or result.stderr or "USB清除模拟定位失败")[-1000:])
    return True
