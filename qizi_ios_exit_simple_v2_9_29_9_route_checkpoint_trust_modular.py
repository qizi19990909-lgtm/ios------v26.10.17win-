
import sys
import os
import ctypes

class OwnedProcessJob:
    """Windows kernel ownership; never select processes by name or port."""
    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [("ProcessTime", ctypes.c_longlong),
                        ("JobTime", ctypes.c_longlong), ("Flags", w.DWORD),
                        ("MinWorkingSet", ctypes.c_size_t),
                        ("MaxWorkingSet", ctypes.c_size_t),
                        ("ActiveProcesses", w.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("Priority", w.DWORD), ("Scheduling", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("ReadOps", "WriteOps", "OtherOps", "ReadBytes",
                         "WriteBytes", "OtherBytes")]
        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("IO", IO),
                        ("ProcessMemory", ctypes.c_size_t),
                        ("JobMemory", ctypes.c_size_t),
                        ("PeakProcess", ctypes.c_size_t),
                        ("PeakJob", ctypes.c_size_t)]
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        k.CreateJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int,
                                             ctypes.c_void_p, w.DWORD]
        k.SetInformationJobObject.restype = w.BOOL
        k.GetCurrentProcess.restype = w.HANDLE
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.AssignProcessToJobObject.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]
        handle = k.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            k.CloseHandle(handle)
            raise ctypes.WinError(error)
        if not k.AssignProcessToJobObject(handle, k.GetCurrentProcess()):
            error = ctypes.get_last_error()
            k.CloseHandle(handle)
            raise RuntimeError("无法建立本程序的子进程退出保护：" + str(ctypes.WinError(error)))
        # Keep this non-inheritable handle until our process exits. Closing it
        # explicitly here would also kill the current process before saving.
        self.handle = handle





# V2.6.2: one persistent RSD/DVT location helper per connected phone.
# The bundled iOS runtime owns pymobiledevice3, while the main/frozen program only
# sends small JSON commands to the helper.  This avoids launching a new Python +
# pymobiledevice3 process every second for every phone.
PERSISTENT_LOCATION_HELPER_CODE = r"""
import asyncio
import inspect
import json
import sys
import threading
import traceback


def _emit(kind, **data):
    payload = {"kind": kind}
    payload.update(data)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


async def _maybe_close(obj):
    close = getattr(obj, "close", None)
    if close is None:
        close = getattr(obj, "aclose", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def _main(host, port):
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

    loop = asyncio.get_running_loop()
    controls = asyncio.Queue(maxsize=16)
    wake = asyncio.Event()
    latest_set = {"cmd": None}
    current_set_task = {"task": None}

    def enqueue(cmd):
        op = str(cmd.get("op", ""))
        if op in ("start", "update", "once"):
            # Latest-wins: a slow DVT call must never build a long backlog of old coordinates.
            latest_set["cmd"] = cmd
        else:
            # STOP/CLEAR/SHUTDOWN cancel an in-flight set immediately when possible.
            task = current_set_task.get("task")
            if op in ("stop", "clear", "shutdown") and task is not None and not task.done():
                task.cancel()
            try:
                controls.put_nowait(cmd)
            except asyncio.QueueFull:
                # STOP/CLEAR/SHUTDOWN are safety/control commands.  Make room instead of losing them.
                try:
                    controls.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    controls.put_nowait(cmd)
                except asyncio.QueueFull:
                    pass
        wake.set()

    def reader():
        try:
            for raw in sys.stdin:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    cmd = json.loads(raw)
                except Exception:
                    continue
                try:
                    loop.call_soon_threadsafe(enqueue, cmd)
                except Exception:
                    break
        finally:
            try:
                loop.call_soon_threadsafe(enqueue, {"op": "shutdown", "generation": 10**12})
            except Exception:
                pass

    threading.Thread(target=reader, name="location-command-reader", daemon=True).start()

    rsd = RemoteServiceDiscoveryService((host, int(port)))
    await rsd.connect()
    try:
        async with DvtProvider(rsd) as dvt, LocationSimulation(dvt) as location:
            _emit("ready", backend="persistent-dvt")
            send_count = 0
            min_generation = 0

            async def do_set(cmd):
                nonlocal send_count
                lat = float(cmd["lat"])
                lon = float(cmd["lon"])
                generation = int(cmd.get("generation", 0) or 0)
                request_id = cmd.get("request_id")
                op = str(cmd.get("op", "update"))
                if generation < min_generation:
                    _emit("ack", op=op, request_id=request_id, generation=generation,
                          count=send_count, skipped=True)
                    return
                await asyncio.wait_for(location.set(lat, lon), timeout=6.0)
                send_count += 1
                _emit("ack", op=op, request_id=request_id, generation=generation,
                      count=send_count, skipped=False)

            while True:
                await wake.wait()

                # Control commands always run before any queued coordinate update.
                while True:
                    try:
                        cmd = controls.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    op = str(cmd.get("op", ""))
                    generation = int(cmd.get("generation", 0) or 0)
                    request_id = cmd.get("request_id")
                    if op == "shutdown":
                        return
                    if op == "stop":
                        min_generation = max(min_generation, generation)
                        pending = latest_set.get("cmd")
                        if pending is not None and int(pending.get("generation", 0) or 0) < min_generation:
                            latest_set["cmd"] = None
                        _emit("ack", op=op, request_id=request_id, generation=generation,
                              count=send_count)
                        continue
                    if op == "clear":
                        min_generation = max(min_generation, generation)
                        pending = latest_set.get("cmd")
                        if pending is not None and int(pending.get("generation", 0) or 0) < min_generation:
                            latest_set["cmd"] = None
                        await asyncio.wait_for(location.clear(), timeout=8.0)
                        _emit("ack", op=op, request_id=request_id, generation=generation,
                              count=send_count)
                        continue
                    if op == "ping":
                        _emit("ack", op=op, request_id=request_id, generation=generation,
                              count=send_count)
                        continue

                cmd = latest_set.get("cmd")
                latest_set["cmd"] = None
                if cmd is not None:
                    task = asyncio.create_task(do_set(cmd))
                    current_set_task["task"] = task
                    try:
                        await task
                    except asyncio.CancelledError:
                        _emit("ack", op=str(cmd.get("op", "update")),
                              request_id=cmd.get("request_id"),
                              generation=int(cmd.get("generation", 0) or 0),
                              count=send_count, skipped=True, canceled=True)
                    finally:
                        if current_set_task.get("task") is task:
                            current_set_task["task"] = None

                if controls.empty() and latest_set.get("cmd") is None:
                    wake.clear()
    finally:
        await _maybe_close(rsd)


if __name__ == "__main__":
    try:
        asyncio.run(_main(sys.argv[1], int(sys.argv[2])))
    except BaseException as exc:
        _emit("error", error=f"{type(exc).__name__}: {exc}", detail=traceback.format_exc()[-1600:])
        raise
"""

class PersistentLocationBridge:
    """Long-lived pymobiledevice3 DVT channel with ACK tracking and stale-command cancellation."""
    def __init__(self, interpreter, endpoint, launch, kill, emit, flags=0):
        import queue as _queue
        self.interpreter = list(interpreter)
        self.endpoint = endpoint
        self.launch = launch
        self.kill = kill
        self.emit = emit
        self.flags = flags
        self.process = None
        self.messages = _queue.Queue(maxsize=256)
        self.stderr_tail = ""
        self.last_target = None
        self.last_interval = None
        self.active = False
        self.request_id = 0
        self.backend = ""
        self.generation = 0
        self.pending = {}
        # Match ACK request IDs to the exact coordinates originally sent;
        # target may have changed while DVT was still processing an older set.
        self.pending_targets = {}
        self.first_set_write_at = 0.0
        self.last_set_write_at = 0.0
        self.last_set_ack_at = 0.0
        self.last_ack_request_id = 0
        self.last_ack_count = 0

    def _reader(self):
        import json as _json
        process = self.process
        if not process or not process.stdout:
            return
        for raw in process.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = _json.loads(raw)
            except Exception:
                message = {"kind": "log", "text": raw[-800:]}
            try:
                self.messages.put_nowait(message)
            except Exception:
                try:
                    self.messages.get_nowait()
                    self.messages.put_nowait(message)
                except Exception:
                    pass

    def _stderr_reader(self):
        process = self.process
        if not process or not process.stderr:
            return
        for raw in process.stderr:
            raw = raw.strip()
            if raw:
                self.stderr_tail = raw[-1200:]

    def start(self, timeout=16.0):
        import subprocess as _subprocess, threading as _threading, time as _time
        host, port = self.endpoint
        self.process = self.launch(
            self.interpreter + ["-u", "-c", PERSISTENT_LOCATION_HELPER_CODE, str(host), str(port)],
            stdin=_subprocess.PIPE,
            stdout=_subprocess.PIPE,
            stderr=_subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=self.flags,
        )
        _threading.Thread(target=self._reader, name="persistent-location-out", daemon=True).start()
        _threading.Thread(target=self._stderr_reader, name="persistent-location-err", daemon=True).start()
        deadline = _time.monotonic() + timeout
        progress_at = 0.0
        while _time.monotonic() < deadline:
            now = _time.monotonic()
            if now - progress_at >= 1.0:
                self.emit("heartbeat")
                progress_at = now
            if self.process.poll() is not None:
                break
            try:
                message = self.messages.get(timeout=0.1)
            except Exception:
                continue
            kind = message.get("kind")
            if kind == "ready":
                self.backend = message.get("backend", "persistent-dvt")
                return True
            if kind == "error":
                self.stderr_tail = str(message.get("error") or message.get("detail") or self.stderr_tail)[-1200:]
                break
        self.close()
        return False

    def _write(self, payload):
        import json as _json
        process = self.process
        if process is None or process.poll() is not None or process.stdin is None:
            raise RuntimeError("持久定位会话已退出：" + (self.stderr_tail or "无详细信息"))
        process.stdin.write(_json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def _next_id(self):
        self.request_id += 1
        return self.request_id

    def _remember_request(self, request_id, op, generation):
        import time as _time
        now = _time.monotonic()
        if op in ("start", "update", "once"):
            # The helper is latest-wins, so older unsent set requests can be forgotten here too.
            # Do NOT discard an in-flight request merely because a newer 1 Hz
            # coordinate has arrived. Its eventual DVT ACK is the only reliable
            # evidence that the phone accepted a location update. The helper
            # already collapses *unsent* coordinates to latest-wins.
            for old_id,(old_op,old_generation,old_at) in list(self.pending.items()):
                if (old_op in ("start", "update", "once") and
                        (old_generation != generation or now-old_at > 24.0)):
                    self.pending.pop(old_id, None)
                    self.pending_targets.pop(old_id, None)
        elif op in ("stop", "clear"):
            # A new generation invalidates every older coordinate request.
            for old_id,(old_op,old_generation,_old_at) in list(self.pending.items()):
                if old_op in ("start", "update", "once") and old_generation < generation:
                    self.pending.pop(old_id, None)
                    self.pending_targets.pop(old_id, None)
        self.pending[request_id] = (op, generation, now)
        if op in ("start", "update", "once"):
            if not self.first_set_write_at:
                self.first_set_write_at = now
            self.last_set_write_at = now
        return request_id

    def start_or_update(self, target, interval, force=False):
        lat, lon = float(target[0]), float(target[1])
        interval = float(interval)
        op = "update" if self.active else "start"
        request_id = self._next_id()
        self._write({"op": op, "lat": lat, "lon": lon, "interval": interval,
                     "request_id": request_id, "generation": self.generation})
        self._remember_request(request_id, op, self.generation)
        self.pending_targets[request_id] = (lat, lon)
        self.last_target = (lat, lon)
        self.last_interval = interval
        self.active = True
        return request_id

    def once(self, target):
        lat, lon = float(target[0]), float(target[1])
        request_id = self._next_id()
        self._write({"op": "once", "lat": lat, "lon": lon,
                     "request_id": request_id, "generation": self.generation})
        self._remember_request(request_id, "once", self.generation)
        self.pending_targets[request_id] = (lat, lon)
        self.last_target = (lat, lon)
        self.active = False
        return request_id

    def stop_repeat(self):
        if self.process is None or self.process.poll() is not None:
            return None
        self.generation += 1
        request_id = self._next_id()
        self._write({"op": "stop", "request_id": request_id, "generation": self.generation})
        self._remember_request(request_id, "stop", self.generation)
        self.active = False
        return request_id

    def clear(self, timeout=12.0):
        self.generation += 1
        request_id = self._next_id()
        self._write({"op": "clear", "request_id": request_id, "generation": self.generation})
        self._remember_request(request_id, "clear", self.generation)
        self.active = False
        return self.wait_ack(request_id, "clear", timeout)

    def _consume_message(self, message):
        import time as _time
        kind = message.get("kind")
        if kind == "ack":
            request_id = message.get("request_id")
            try:
                request_id = int(request_id)
            except Exception:
                pass
            pending = self.pending.pop(request_id, None)
            coordinate = self.pending_targets.pop(request_id, None)
            message['_tracked'] = pending is not None
            if coordinate is not None:
                message['_lat'], message['_lon'] = coordinate
            op = str(message.get("op") or (pending[0] if pending else ""))
            generation = int(message.get("generation", pending[1] if pending else 0) or 0)
            if op in ("start", "update", "once") and pending is not None and not message.get("skipped"):
                self.last_set_ack_at = _time.monotonic()
                self.last_ack_request_id = request_id if isinstance(request_id, int) else self.last_ack_request_id
                self.last_ack_count = int(message.get("count", self.last_ack_count) or self.last_ack_count)
            message["_op"] = op
            message["_generation"] = generation
            return message
        if kind == "error":
            self.stderr_tail = str(message.get("error") or message.get("detail") or self.stderr_tail)[-1200:]
        elif kind == "log" and message.get("text"):
            self.stderr_tail = str(message.get("text"))[-1200:]
        return message

    def check(self, raise_on_exit=True):
        process = self.process
        latest_error = ""
        drained = []
        for _ in range(64):
            try:
                message = self.messages.get_nowait()
            except Exception:
                break
            message = self._consume_message(message)
            drained.append(message)
            if message.get("kind") == "error":
                latest_error = str(message.get("error") or message.get("detail") or "")[-1200:]
        if latest_error:
            self.stderr_tail = latest_error
            if raise_on_exit:
                raise RuntimeError("持久定位服务异常：" + latest_error)
        if process is not None and process.poll() is not None and raise_on_exit:
            raise RuntimeError("持久定位会话已退出：" + (self.stderr_tail or f"退出码 {process.returncode}"))
        return drained

    def wait_ack(self, request_id, op, timeout):
        import time as _time
        deadline = _time.monotonic() + timeout
        parked = []
        try:
            while _time.monotonic() < deadline:
                self.emit("heartbeat")
                process = self.process
                if process is None or process.poll() is not None:
                    raise RuntimeError("持久定位会话已退出：" + (self.stderr_tail or "无详细信息"))
                try:
                    message = self.messages.get(timeout=0.1)
                except Exception:
                    continue
                message = self._consume_message(message)
                kind = message.get("kind")
                if kind == "ack" and message.get("request_id") == request_id and str(message.get("op")) == op:
                    return True
                if kind == "error":
                    raise RuntimeError(str(message.get("error") or message.get("detail") or "持久定位服务异常"))
                parked.append(message)
            raise RuntimeError(f"持久定位 {op} 等待超时")
        finally:
            for message in parked:
                try:
                    self.messages.put_nowait(message)
                except Exception:
                    break

    def stale(self, now=None, timeout=3.5):
        import time as _time
        if not self.active:
            return False
        now = _time.monotonic() if now is None else float(now)
        base = self.last_set_ack_at or self.first_set_write_at
        return bool(base and now - base >= float(timeout))

    def close(self):
        process = self.process
        self.process = None
        self.active = False
        if process is None:
            return
        if process.poll() is None:
            try:
                if process.stdin:
                    self.generation += 1
                    process.stdin.write('{"op":"shutdown","generation":%d}\n' % self.generation)
                    process.stdin.flush()
                process.wait(timeout=2.0)
            except Exception:
                try:
                    self.kill(process)
                except Exception:
                    pass
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe:
                try:
                    pipe.close()
                except Exception:
                    pass

def run_ios_session(udid, pmd, commands, emit, register):
    """V2.5.6: bounded random route walking; every new segment starts exactly at the previous segment endpoint."""
    import os, queue, re, subprocess, threading, time, ipaddress, random, math
    flags=0x08000000 if os.name=='nt' else 0
    def launch(args, **options):
        env=os.environ.copy()
        env['PYTHONIOENCODING']='utf-8'
        env['PYTHONUNBUFFERED']='1'
        proc=subprocess.Popen(args, env=env, **options)
        register(proc)
        return proc
    def kill(p):
        if p and p.poll() is None:
            p.terminate()
            try:p.wait(timeout=2)
            except subprocess.TimeoutExpired:p.kill();p.wait(timeout=2)
    tunnel=None
    clear=None
    locations=[]
    cli_requests={}
    location_bridge=None
    persistent_location=False
    results=queue.Queue()
    lines=queue.Queue(maxsize=256)
    active=False
    route_active=False
    hold_active=False  # paused flight: resend exactly same coordinate
    count=0
    epoch=0
    try:
        emit('state',wifi='建立中',location='未开启')
        tunnel=launch(pmd+['remote','start-tunnel','-t','wifi','--udid',udid,
            '--protocol','tcp','--script-mode'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            text=True,encoding='utf-8',errors='replace',bufsize=1,creationflags=flags)
        def read_tunnel():
            for line in tunnel.stdout:
                clean=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',line).strip()
                try:lines.put_nowait(clean)
                except queue.Full:pass
        threading.Thread(target=read_tunnel,daemon=True).start()
        end=time.monotonic()+35
        recent=[]
        endpoint=None
        progress_at=0
        while time.monotonic()<end and endpoint is None:
            if time.monotonic()-progress_at>=1:
                emit('heartbeat')
                progress_at=time.monotonic()
            try:
                cmd=commands.get_nowait()
                if cmd.get('op') in ('shutdown','disconnect'):return
            except queue.Empty:pass
            try:line=lines.get(timeout=.05)
            except queue.Empty:line=''
            if line:
                recent=(recent+[line])[-8:]
                emit('tunnel_log', text=line)
                m=re.search(r'([0-9a-fA-F:.%]+)\s+(\d{1,5})$',line)
                if m:
                    try:
                        ipaddress.ip_address(m[1].split('%')[0])
                        if 0<int(m[2])<65536:endpoint=(m[1],str(int(m[2])))
                    except ValueError:pass
            if tunnel.poll() is not None:raise RuntimeError('Tunnel 已退出：'+' | '.join(recent))
        if endpoint is None:raise RuntimeError('35秒未取得连接地址：'+' | '.join(recent))
        # Same connected criterion as the user's known-working original.
        if tunnel.poll() is not None:raise RuntimeError('取得RSD后隧道进程已退出')
        emit('state',wifi='连接中',location='未开启',trusted=True,rsd=' : '.join(endpoint),error='')

        # V2.6.2: keep one RSD/DVT/LocationSimulation service open for this device.
        # The existing Tunnel remains untouched.  If the bundled pymobiledevice3 is
        # older/incompatible, transparently fall back to the proven CLI sender.
        interpreter=list(pmd[:-2]) if len(pmd)>=3 and pmd[-2:]==['-m','pymobiledevice3'] else [pmd[0]]
        try:
            location_bridge=PersistentLocationBridge(interpreter,endpoint,launch,kill,emit,flags)
            persistent_location=location_bridge.start(timeout=16.0)
        except Exception as exc:
            persistent_location=False
            try:
                if location_bridge:location_bridge.close()
            except Exception:pass
            location_bridge=None
            emit('tunnel_log',text='持久定位初始化失败，已自动回退兼容模式：'+str(exc)[-700:])
        if persistent_location:
            emit('tunnel_log',text='低CPU持久定位会话已就绪：RSD/DVT只建立一次；只有收到真实 DVT ACK 才会显示“定位中”。')
        else:
            emit('tunnel_log',text='当前 Runtime 不支持持久定位 API，使用兼容 CLI 模式。')

        persistent_capable=bool(persistent_location)
        bridge_restarts_since_ack=0
        last_bridge_recovery_at=0.0
        LOCATION_ACK_TIMEOUT_S=12.0
        last_bridge_open_attempt_at = time.monotonic() if not persistent_location else 0.0

        target=None
        base_target=None
        interval=1.0
        send_due=0.0
        heartbeat=0

        # V2.5.6 受控随机路线参数：
        # - base_target / initial_coordinate：用户最初输入的坐标，整个定位周期内固定不变。
        # - segment_start_coordinate：当前这一段的起点；第一段=用户输入坐标，此后严格=上一段终点。
        # - segment_endpoint：当前这一段随机移动最终要到达的坐标。
        # - last_completed_endpoint：最近一段已经完成的终点；下一段规划只从这里开始。
        # - 每段位移随机 0~20 米；移动方式随机为直线、曲线、随机折线。
        # - 每段内部的时间间隔和空间推进比例都随机，但整体速度仍保持偏慢步行 0.5~1.0 m/s。
        # - 移动完成后停顿 10~30 秒；停顿时仍持续按 interval 重复发送 segment_endpoint。
        # - 以初始坐标为中心，每次启动随机生成 20~30 米“软范围”。
        #   在范围内允许一整段继续向外，所以例如当前 29 米仍可能再向外走 20 米；
        #   一旦起点已经超过软范围，下一段方向自动偏向初始点，防止长期漂移。
        WALK_DISTANCE_MIN_M=0.0
        WALK_DISTANCE_MAX_M=20.0
        WALK_SPEED_MIN_MPS=0.5
        WALK_SPEED_MAX_MPS=1.0
        WALK_SOFT_RADIUS_MIN_M=20.0
        WALK_SOFT_RADIUS_MAX_M=30.0
        WALK_PAUSE_MIN_S=10.0
        WALK_PAUSE_MAX_S=30.0
        WALK_KNOT_INTERVAL_MIN_S=0.55
        WALK_KNOT_INTERVAL_MAX_S=1.65
        WALK_PROGRESS_WEIGHT_MIN=0.45
        WALK_PROGRESS_WEIGHT_MAX=1.55
        WALK_RETURN_JITTER_DEG=22.0
        WALK_TURN_MAX_DEG=100.0
        WALK_NEW_HEADING_CHANCE=0.32
        WALK_CURVE_MAX_LATERAL_M=5.0
        WALK_RANDOM_MAX_LATERAL_M=4.0

        initial_coordinate=None
        route_points=[]
        route_index=0
        segment_heading=None
        segment_start_coordinate=None
        segment_endpoint=None
        last_completed_endpoint=None
        segment_mode='straight'
        segment_distance=0.0
        segment_speed=0.0
        segment_start_at=0.0
        segment_end_at=0.0
        segment_pause_until=0.0
        segment_pause_s=0.0
        center_soft_radius=25.0
        motion_phase='idle'

        def _offset_coordinate(lat, lon, east_m, north_m):
            meters_per_lat=111320.0
            cos_lat=max(abs(math.cos(math.radians(lat))),0.01)
            return (lat + north_m/meters_per_lat,
                    lon + east_m/(meters_per_lat*cos_lat))

        def _meters_from_center(lat, lon):
            if base_target is None:
                return 0.0,0.0,0.0
            center_lat,center_lon=base_target
            north=(float(lat)-center_lat)*111320.0
            cos_lat=max(abs(math.cos(math.radians(center_lat))),0.01)
            east=(float(lon)-center_lon)*111320.0*cos_lat
            return east,north,math.hypot(east,north)

        def _cap_to_outer_safety(lat, lon):
            """Hard safety ceiling: soft radius + one maximum 20 m segment."""
            if base_target is None:
                return float(lat),float(lon)
            east,north,distance=_meters_from_center(lat,lon)
            limit=center_soft_radius+WALK_DISTANCE_MAX_M
            if distance <= limit or distance <= 0.001:
                return float(lat),float(lon)
            scale=limit/distance
            return _offset_coordinate(base_target[0],base_target[1],east*scale,north*scale)

        def _choose_heading(start_lat, start_lon):
            """Choose a normal wandering direction, or a center-biased heading after soft-range overflow."""
            nonlocal segment_heading
            east,north,center_distance=_meters_from_center(start_lat,start_lon)
            returning=center_distance > center_soft_radius
            if returning and center_distance > 0.01:
                # 角度定义与位移一致：0=向东，pi/2=向北。越界后向中心回走，
                # 但保留少量方向抖动，避免每次都是完全笔直地折返。
                heading=math.atan2(-north,-east) + math.radians(
                    random.uniform(-WALK_RETURN_JITTER_DEG,WALK_RETURN_JITTER_DEG))
                segment_heading=heading % math.tau
            elif segment_heading is None or random.random() < WALK_NEW_HEADING_CHANCE:
                segment_heading=random.uniform(0.0,math.tau)
            else:
                segment_heading=(segment_heading + math.radians(
                    random.uniform(-WALK_TURN_MAX_DEG,WALK_TURN_MAX_DEG))) % math.tau
            return segment_heading,returning,center_distance

        def _random_lateral_profile(distance_m):
            amplitude=min(WALK_RANDOM_MAX_LATERAL_M,max(0.20,distance_m*0.30))
            return [(0.0,0.0),
                    (0.22,random.uniform(-amplitude,amplitude)),
                    (0.45,random.uniform(-amplitude,amplitude)),
                    (0.68,random.uniform(-amplitude,amplitude)),
                    (0.84,random.uniform(-amplitude,amplitude)),
                    (1.0,0.0)]

        def _profile_value(profile, progress):
            p=min(1.0,max(0.0,float(progress)))
            for idx in range(1,len(profile)):
                p1,v1=profile[idx-1]
                p2,v2=profile[idx]
                if p <= p2:
                    if p2 <= p1:
                        return v2
                    ratio=(p-p1)/(p2-p1)
                    return v1+(v2-v1)*ratio
            return profile[-1][1]

        def _path_coordinate(start_lat,start_lon,heading,distance_m,progress,mode,shape_data):
            p=min(1.0,max(0.0,float(progress)))
            along=distance_m*p
            lateral=0.0
            if mode=='curve':
                # 一条平滑弧线，起点和终点横向偏移都严格为 0。
                lateral=math.sin(math.pi*p)*shape_data
            elif mode=='random':
                # 随机折线的横向偏移在多个控制点间插值；终点仍落在计算好的终点。
                lateral=_profile_value(shape_data,p)
            east=math.cos(heading)*along - math.sin(heading)*lateral
            north=math.sin(heading)*along + math.cos(heading)*lateral
            lat,lon=_offset_coordinate(start_lat,start_lon,east,north)
            return _cap_to_outer_safety(lat,lon)

        def plan_segment(start_lat, start_lon, start_at=None):
            """Plan one 0~20 m segment with random geometry, timing and per-step progress."""
            nonlocal route_points,route_index,segment_mode,segment_distance,segment_speed
            nonlocal segment_start_at,segment_end_at,segment_start_coordinate,segment_endpoint
            nonlocal last_completed_endpoint,motion_phase,segment_pause_until,segment_pause_s

            begin=time.monotonic() if start_at is None else float(start_at)
            # V2.5.6：把本段起点显式冻结下来。这个值来自上一段已完成终点，
            # 不使用初始中心重新起步，也不使用移动中的临时插值 target。
            segment_start_coordinate=(float(start_lat),float(start_lon))
            start_lat,start_lon=segment_start_coordinate
            heading,returning,center_distance=_choose_heading(start_lat,start_lon)
            segment_mode=random.choice(('straight','curve','random'))
            segment_distance=random.uniform(WALK_DISTANCE_MIN_M,WALK_DISTANCE_MAX_M)

            # 已经越过软范围时，不能一直抽到接近 0 米而停在外面；回中心段至少走一点。
            # 其余正常路段仍完整保留 0~20 米随机范围。
            if returning:
                segment_distance=max(2.0,segment_distance)

            segment_speed=random.uniform(WALK_SPEED_MIN_MPS,WALK_SPEED_MAX_MPS)
            segment_start_at=begin
            route_points=[]
            route_index=0
            segment_pause_until=0.0
            segment_pause_s=0.0

            if segment_distance < 0.20:
                segment_endpoint=(float(start_lat),float(start_lon))
                last_completed_endpoint=segment_endpoint
                segment_end_at=begin
                motion_phase='paused'
                segment_pause_s=random.uniform(WALK_PAUSE_MIN_S,WALK_PAUSE_MAX_S)
                segment_pause_until=begin+segment_pause_s
                return

            duration=segment_distance/segment_speed
            # 先随机出移动事件之间的间隔，再统一缩放到本段目标总时长；
            # 因而每个内部移动间隔不同，但整段平均速度仍在 0.5~1.0 m/s。
            approx_step=random.uniform(0.80,1.20)
            knot_count=max(2,min(36,int(round(duration/approx_step))))
            raw_intervals=[random.uniform(WALK_KNOT_INTERVAL_MIN_S,WALK_KNOT_INTERVAL_MAX_S)
                           for _ in range(knot_count)]
            time_scale=duration/sum(raw_intervals)
            intervals=[x*time_scale for x in raw_intervals]

            # 每个移动事件推进的距离比例也随机，再归一化为最终恰好走完整段。
            progress_weights=[random.uniform(WALK_PROGRESS_WEIGHT_MIN,WALK_PROGRESS_WEIGHT_MAX)
                              for _ in range(knot_count)]
            progress_total=sum(progress_weights)
            progress_steps=[x/progress_total for x in progress_weights]

            if segment_mode=='curve':
                max_bend=min(WALK_CURVE_MAX_LATERAL_M,max(0.25,segment_distance*0.32))
                bend=random.uniform(-max_bend,max_bend)
                # 避免“曲线模式”随机到几乎看不出弯曲。
                if abs(bend) < min(0.8,max_bend):
                    bend=math.copysign(min(0.8,max_bend),bend if bend else random.choice((-1,1)))
                shape_data=bend
            elif segment_mode=='random':
                shape_data=_random_lateral_profile(segment_distance)
            else:
                shape_data=0.0

            route_points=[(begin,float(start_lat),float(start_lon),0.0)]
            elapsed=0.0
            progress=0.0
            for idx,(wait_s,progress_step) in enumerate(zip(intervals,progress_steps)):
                elapsed+=wait_s
                progress=min(1.0,progress+progress_step)
                if idx==knot_count-1:
                    elapsed=duration
                    progress=1.0
                lat,lon=_path_coordinate(start_lat,start_lon,heading,segment_distance,
                                         progress,segment_mode,shape_data)
                route_points.append((begin+elapsed,lat,lon,progress))

            segment_end_at=begin+duration
            segment_endpoint=(route_points[-1][1],route_points[-1][2])
            motion_phase='moving'

        def reset_walk(lat, lon):
            nonlocal target,base_target,initial_coordinate,route_points,route_index
            nonlocal segment_heading,segment_start_coordinate,segment_endpoint,last_completed_endpoint
            nonlocal center_soft_radius,motion_phase,send_due
            base_target=(float(lat),float(lon))
            initial_coordinate=base_target
            target=base_target
            segment_start_coordinate=base_target
            segment_endpoint=base_target
            last_completed_endpoint=base_target
            route_points=[]
            route_index=0
            segment_heading=None
            center_soft_radius=random.uniform(WALK_SOFT_RADIUS_MIN_M,WALK_SOFT_RADIUS_MAX_M)
            motion_phase='idle'
            now=time.monotonic()
            send_due=now
            plan_segment(target[0],target[1],now)

        def update_motion(now):
            """Update target from the preplanned random route; sending itself is handled separately every interval."""
            nonlocal target,route_index,motion_phase,segment_pause_until,segment_pause_s
            nonlocal last_completed_endpoint
            if motion_phase=='moving':
                if not route_points:
                    last_completed_endpoint=segment_endpoint or last_completed_endpoint or target
                    target=last_completed_endpoint
                    motion_phase='paused'
                    segment_pause_s=random.uniform(WALK_PAUSE_MIN_S,WALK_PAUSE_MAX_S)
                    segment_pause_until=now+segment_pause_s
                    return

                if now >= segment_end_at:
                    # 本段完成：最终坐标先记录，再进入停顿。后续下一段只读取这个终点。
                    last_completed_endpoint=segment_endpoint
                    target=last_completed_endpoint
                    route_index=max(0,len(route_points)-1)
                    motion_phase='paused'
                    segment_pause_s=random.uniform(WALK_PAUSE_MIN_S,WALK_PAUSE_MAX_S)
                    segment_pause_until=now+segment_pause_s
                    return

                # 找到当前时间位于哪两个随机移动节点之间。
                while route_index+1 < len(route_points) and now >= route_points[route_index+1][0]:
                    route_index+=1
                left=route_points[route_index]
                if route_index+1 >= len(route_points):
                    target=(left[1],left[2])
                    return
                right=route_points[route_index+1]
                span=max(0.0001,right[0]-left[0])
                ratio=min(1.0,max(0.0,(now-left[0])/span))
                target=(left[1]+(right[1]-left[1])*ratio,
                        left[2]+(right[2]-left[2])*ratio)
                return

            if motion_phase=='paused':
                # 停顿期间始终锁定“最近完成终点”，外层发送器继续每秒重复发送。
                if last_completed_endpoint is not None:
                    target=last_completed_endpoint
                elif segment_endpoint is not None:
                    target=segment_endpoint
                if now >= segment_pause_until:
                    # V2.5.6 无缝衔接关键：下一段起点必须严格等于上一段完成终点。
                    chain_start=last_completed_endpoint or segment_endpoint or target
                    plan_segment(chain_start[0],chain_start[1],now)
                return

            if last_completed_endpoint is not None:
                target=last_completed_endpoint
            elif segment_endpoint is not None:
                target=segment_endpoint
        def collect(proc,version):
            out,_=proc.communicate()
            results.put((proc,version,proc.returncode,(out or '')[-1200:]))
        def discard_locations():
            nonlocal epoch
            epoch+=1
            for proc,_,_ in locations:kill(proc)
            locations.clear()
            cli_requests.clear()
        def _open_persistent_bridge(timeout=12.0):
            nonlocal location_bridge,persistent_location,last_bridge_open_attempt_at
            last_bridge_open_attempt_at = time.monotonic()
            bridge=None
            try:
                bridge=PersistentLocationBridge(interpreter,endpoint,launch,kill,emit,flags)
                if not bridge.start(timeout=timeout):
                    bridge.close()
                    return False
                location_bridge=bridge
                persistent_location=True
                return True
            except Exception as exc:
                try:
                    if bridge is not None:bridge.close()
                except Exception:pass
                location_bridge=None
                persistent_location=False
                emit('tunnel_log',text='持久定位会话建立失败：'+str(exc)[-700:])
                return False

        def _recover_persistent_bridge(reason):
            nonlocal location_bridge,persistent_location,bridge_restarts_since_ack,last_bridge_recovery_at,send_due
            now=time.monotonic()
            if now-last_bridge_recovery_at<1.0:
                return persistent_location
            last_bridge_recovery_at=now
            bridge_restarts_since_ack+=1
            emit('state',wifi='连接中',location='正在恢复',error=str(reason)[-500:])
            emit('tunnel_log',text='定位 ACK 异常，正在复用现有 Tunnel 重建 DVT 会话：'+str(reason)[-700:])
            old=location_bridge
            location_bridge=None
            persistent_location=False
            if old is not None:
                try:old.close()
                except Exception:pass
            # 连续恢复两次仍没有成功 ACK 时，优先保证定位可用，退回旧 CLI 方式。
            if bridge_restarts_since_ack<=2 and _open_persistent_bridge(timeout=10.0):
                emit('tunnel_log',text='DVT 定位会话已恢复；Tunnel 未重连。')
                if (active or hold_active) and target is not None:
                    send(force=True)
                    send_due=time.monotonic()+interval
                return True
            emit('tunnel_log',text='持久定位连续恢复失败，当前连接自动切换兼容 CLI 保活；Wi-Fi Tunnel 保持不变。')
            if (active or hold_active) and target is not None:
                send(force=True)
                send_due=time.monotonic()+interval
            return False

        def _process_bridge_messages():
            nonlocal count,bridge_restarts_since_ack
            if not persistent_location or location_bridge is None:
                return
            messages=location_bridge.check()
            for message in messages:
                if message.get('kind')!='ack':
                    continue
                op=str(message.get('_op') or message.get('op') or '')
                generation=int(message.get('_generation',message.get('generation',0)) or 0)
                if (op not in ('start','update','once') or message.get('skipped')
                        or not message.get('_tracked')):
                    continue
                # ACK from a command invalidated by STOP/CLEAR must never resurrect the UI state.
                if generation!=location_bridge.generation:
                    continue
                bridge_restarts_since_ack=0
                count+=1
                if (active or route_active) and op in ('start','update'):
                    emit('sent',location='定位中',count=count,last=time.strftime('%H:%M:%S'),
                         backend='persistent',confirmed=True,
                         lat=message.get('_lat'),lon=message.get('_lon'),
                         movement='walk' if active else 'route')
                elif op=='once':
                    emit('sent',location='已发送',count=count,last=time.strftime('%H:%M:%S'),
                         backend='persistent',confirmed=True)

        def send(force=False, once=False):
            nonlocal count
            now=time.monotonic()
            if persistent_location and location_bridge is not None:
                # Do not claim success here.  The row becomes “定位中” only after helper ACK.
                if once:
                    location_bridge.once(target)
                else:
                    location_bridge.start_or_update(target,interval,force=force)
                return True

            # Compatibility fallback: retain the proven CLI behavior, but STOP now kills holders.
            locations[:]=[x for x in locations if x[0].poll() is None]
            proc=launch(pmd+['developer','dvt','simulate-location','set',
                '--rsd',*endpoint,'--',str(target[0]),str(target[1])],
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',
                errors='replace',creationflags=flags)
            locations.append((proc,epoch,now))
            cli_requests[proc] = (target, 'walk' if active else ('route' if route_active else 'once'), epoch)
            threading.Thread(target=collect,args=(proc,epoch),daemon=True).start()
            while len(locations)>3:
                oldest=locations.pop(0)
                cli_requests.pop(oldest[0], None)
                threading.Thread(target=kill,args=(oldest[0],),daemon=True).start()
            # Subprocess creation is NOT location success. Only its return
            # code can provide a minimal (CLI-level) confirmation.
            emit('state',location='准备发送',backend='cli-fallback')
            return True
        while True:
            if tunnel.poll() is not None:raise RuntimeError('Wi-Fi Tunnel 已断开')
            if persistent_location and location_bridge is not None:
                try:
                    _process_bridge_messages()
                except Exception as exc:
                    if active or route_active:
                        _recover_persistent_bridge(exc)
                    else:
                        emit('tunnel_log',text='定位 DVT 会话已退出，但 Wi-Fi Tunnel 仍保持；下次定位将自动恢复：'+str(exc)[-700:])
                        try:location_bridge.close()
                        except Exception:pass
                        location_bridge=None
                        persistent_location=False
            try:cmd=commands.get(timeout=.05)
            except queue.Empty:cmd=None
            if cmd:
                op=cmd.get('op')
                if op in ('shutdown','disconnect'):return
                if op in ('start','once','route','hold'):
                    lat,lon=float(cmd['lat']),float(cmd['lon'])
                    interval=float(cmd.get('interval',1))
                    if not(-90<=lat<=90 and -180<=lon<=180 and 1<=interval<=60):raise ValueError('经纬度或间隔错误')
                    if base_target is not None and base_target!=(lat,lon) and op!='route':discard_locations()
                    if (persistent_capable and (not persistent_location or location_bridge is None)
                            and time.monotonic()-last_bridge_open_attempt_at >= 30.0):
                        _open_persistent_bridge(timeout=10.0)
                    if op=='start':
                        # Repeated recovery/start for the SAME center must not
                        # reset the current random segment or its pause timer.
                        # A genuine new origin is deliberately re-planned.
                        if not active or base_target != (lat,lon):
                            reset_walk(lat,lon)
                        active=True;route_active=False;hold_active=False
                    else:
                        active=False;route_active=(op in ('route','hold'));hold_active=(op=='hold')
                        base_target=(lat,lon)
                        initial_coordinate=(lat,lon)
                        segment_start_coordinate=(lat,lon)
                        segment_endpoint=(lat,lon)
                        last_completed_endpoint=(lat,lon)
                        target=(lat,lon)
                    send(force=True,once=(op=='once'))
                    send_due=time.monotonic()+interval
                elif op=='stop':
                    active=False;route_active=False;hold_active=False
                    discard_locations()
                    if persistent_location and location_bridge is not None:
                        try:
                            request_id=location_bridge.stop_repeat()
                            if request_id is not None:
                                location_bridge.wait_ack(request_id,'stop',3.0)
                            emit('state',location='已停止',error='')
                        except Exception as exc:
                            # Killing the helper is safer than allowing queued old coordinates to execute after STOP.
                            emit('tunnel_log',text='停止定位 ACK 异常，已强制回收定位 Helper，Wi-Fi Tunnel 保持连接：'+str(exc)[-700:])
                            try:location_bridge.close()
                            except Exception:pass
                            location_bridge=None
                            persistent_location=False
                            emit('state',location='已停止',error='定位 Helper 已重置')
                    else:
                        emit('state',location='已停止',error='')
                elif op=='clear':
                    active=False;route_active=False;hold_active=False;discard_locations()
                    if persistent_location and location_bridge is not None:
                        try:
                            location_bridge.clear(timeout=12.0)
                            emit('state',location='已清除模拟',error='')
                        except Exception as exc:
                            emit('location_error',location='清除失败',error=str(exc)[-900:])
                    else:
                        clear=launch(pmd+['developer','dvt','simulate-location','clear',
                            '--rsd',*endpoint],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                            text=True,encoding='utf-8',errors='replace',creationflags=flags)
                        deadline=time.monotonic()+25
                        response=[]
                        def read_clear():response.append(clear.communicate()[0])
                        t=threading.Thread(target=read_clear,daemon=True);t.start()
                        while clear.poll() is None and time.monotonic()<deadline:
                            emit('heartbeat')
                            try:pending=commands.get(timeout=.2)
                            except queue.Empty:continue
                            if pending.get('op') in ('shutdown','disconnect'):
                                kill(clear);t.join(2);return
                        if clear.poll() is None:kill(clear)
                        t.join(2)
                        if clear.returncode!=0:
                            emit('location_error',location='清除失败',error=(response[0] if response else '清除超时')[-900:])
                        else:emit('state',location='已清除模拟')
            # V2.5.2: simulate-location 子进程的退出码不是手机端“定位成功/失败”回执。
            # 这里只负责回收已结束的发送进程；只要 Wi-Fi Tunnel 仍存活且 active=True，
            # 就继续按原来的 interval 发送定位。真正的停止条件仍是人工 stop/clear、
            # disconnect/shutdown，或上面的 Tunnel 存活检查失败。
            while True:
                try:proc,version,rc,error=results.get_nowait()
                except queue.Empty:break
                locations[:]=[x for x in locations if x[0] is not proc]
                info=cli_requests.pop(proc, None)
                if version != epoch or info is None:
                    continue
                point,movement,_=info
                if rc == 0:
                    count += 1
                    emit('sent',location='定位中' if (active or route_active or hold_active) else '已发送',
                         count=count,last=time.strftime('%H:%M:%S'),backend='cli-fallback',
                         confirmed=True,lat=point[0],lon=point[1],movement=movement)
                else:
                    emit('location_error',location='定位未确认',
                         error='CLI定位发送失败：'+str(error)[-600:])
            now=time.monotonic()
            if (active or route_active) and persistent_location and location_bridge is not None:
                try:
                    _process_bridge_messages()
                    if location_bridge.stale(now,LOCATION_ACK_TIMEOUT_S):
                        _recover_persistent_bridge(
                            f'{LOCATION_ACK_TIMEOUT_S:.1f}秒没有收到 DVT set 成功 ACK')
                        now=time.monotonic()
                except Exception as exc:
                    _recover_persistent_bridge(exc)
                    now=time.monotonic()
            if active:
                update_motion(now)
                # 路线推进和定位发送解耦：无论正在移动还是处于 10~30 秒停顿，
                # 都持续按用户设定周期（默认 1 秒）发送“此刻应该保持的坐标”。
                if now>=send_due:
                    send()
                    send_due=now+interval
            elif hold_active and target is not None and now >= send_due:
                # A stationary GPS position, while keeping the RSD/DVT session.
                send()
                send_due=now+interval
            if now-heartbeat>=1:emit('heartbeat');heartbeat=now
            # Always drain tunnel output so its stdout cannot block the process.
            for _ in range(40):
                try:line=lines.get_nowait()
                except queue.Empty:break
                recent=(recent+[line])[-8:]
    except Exception as exc:
        emit('error',wifi='已断开',location='不可定位',error=f'{type(exc).__name__}: {exc}')
    finally:
        if location_bridge is not None:
            try:location_bridge.close()
            except Exception:pass
        for proc,_,_ in locations:kill(proc)
        kill(clear)
        kill(tunnel)
        if tunnel and tunnel.stdout:tunnel.stdout.close()
        emit('closed',wifi='已断开',location='不可定位')


"""Qizi Exit Manager V15 — Windows 10/11, Python 3.12.

运行：python qizi_exit_manager_v14.py
依赖：python -m pip install requests
首次使用：将本文件放在旧 proxy_exits.json / device_bindings.json 旁再启动。
配置首次导入 %LOCALAPPDATA%/QiziExitManager，此后不覆盖已有配置。
部署到新电脑：复制程序和需要使用的出口配置；重新识别并绑定新手机。

启动自检：有效绑定、当前程序防火墙、端口占用、默认路由、HTTPS证书与实际网关。
缺少防火墙权限时弹出系统UAC；只处理当前可执行文件的相关阻止规则和局域网放行。
相关停用规则名单保存在数据目录 firewall-disabled-*.json，便于管理员恢复。
初始端口8899，占用/不可绑定时升序到8999，不终止占用进程；记住最终端口。
手机须使用界面显示的实际端口。旧版无单实例协议时可并存，新版不会接管旧版。
新版PY/EXE、不同文件名共用Windows全局单实例标识；重复启动仅提示运行中。

HTTPS使用CONNECT字节透传，不替换网站证书；诊断保持证书校验。
手机证书报警仍需具体网址、错误码和设备时间；电脑自检不能证明手机证书信任正常。
Requests证书包应随EXE打包；当前运行路径不依赖sing-box.exe。
自检报告和最近转发记录可从窗口复制，不记录代理账号密码或临时诊断令牌。

验证范围：本地模拟上游的HTTP、CONNECT、TLS、认证、端口避让和配置迁移测试通过。
Windows图形界面、UAC、防火墙与全局互斥锁仍需目标电脑验证。
"""

import ctypes
import errno
import hashlib
import shutil
import secrets
from collections import deque
import asyncio
import base64
import socket
import ssl
import os
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import re
import sys
import json
import queue
import subprocess
import threading
import time
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import messagebox, ttk
from urllib.parse import quote

import requests
from qizi_geo_engine import exit_coordinates

# V2.9.29.1 modular helpers: route planner / USB route sender / staggered exit health / route network lock.
from qizi_exit_engine import ExitHealthScheduler
from qizi_location_engine import UsbRouteLocationSender
from qizi_route_engine import (
    build_route as build_auto_route,
    precompute_timeline as precompute_auto_route_timeline,
    timeline_to_dict, timeline_from_dict, load_json_dict, save_route_plans,
)

# 运行目录：打包后固定使用“出口管理器.exe”所在目录；源码运行时使用脚本目录。
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent

# sing-box 必须优先从程序目录查找。
# 这样部署到其他电脑时无需安装 sing-box，也不依赖 C:\proxy_manager。
SING_BOX = APP_DIR / "sing-box.exe"

EXITS_FILE = APP_DIR / "proxy_exits.json"
DEVICES_FILE = APP_DIR / "device_bindings.json"
CONFIG_FILE = APP_DIR / "config.json"
GATEWAY_CONFIG_FILE = APP_DIR / "hotspot_gateway_config.json"
GATEWAY_LOG_FILE = APP_DIR / "hotspot_gateway.log"
GATEWAY_PORT = 8899
MAX_GATEWAY_CONNECTIONS = 4096
FIREWALL_RULE_NAME = "ProxyManager Hotspot HTTP Gateway"

LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 2080
TEST_TIMEOUT = 12
AUTO_SCAN_SECONDS = 30
AUTO_GATEWAY_RELOAD_MS = 700
EXIT_HEALTH_SECONDS = 2
IOS_AUTO_SCAN_SECONDS = 45

# ===== 列表显示高度参数（修改这里即可） =====
EXIT_LIST_ROWS = 7          # 出口列表可见行数
EXIT_ROW_HEIGHT = 20        # 出口列表每行高度（像素）
DEVICE_LIST_ROWS = 7       # 设备列表可见行数
DEVICE_ROW_HEIGHT = 20      # 设备列表每行高度（像素）

DEFAULT_EXITS = []  # No bundled accounts or credentials.



def run_hidden(cmd, timeout=15):
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=timeout,
        creationflags=flags,
    )


def powershell(script, timeout=15):
    script = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;\n" + script
    result = run_hidden(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "PowerShell 执行失败").strip())
    return result.stdout.strip()


def save_json(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_exits():
    # 没有配置文件、配置文件为空时，都按“无配置”处理，不弹任何警告。
    if not EXITS_FILE.exists():
        return []

    try:
        text = EXITS_FILE.read_text(encoding="utf-8").strip()
        if not text:
            return []

        data = json.loads(text)
        if not isinstance(data, list):
            return []

        changed = False
        for p in data:
            if not isinstance(p, dict):
                continue
            if not p.get("id"):
                p["id"] = str(uuid.uuid4())
                changed = True
            p.setdefault("type", "http")
            p.setdefault("status", "未检测")
            p.setdefault("public_ip", "")
            p.setdefault("country", "")

        if changed:
            save_json(EXITS_FILE, data)
        return data
    except Exception:
        # 配置为空、损坏或暂时无法读取时保持空列表，不弹窗。
        return []


def load_devices():
    if not DEVICES_FILE.exists():
        return {}
    try:
        data = json.loads(DEVICES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_devices(devices):
    persistent = {}
    for ip, d in devices.items():
        if d.get("manual") or d.get("exit_id") or d.get("remark") or d.get("qizi_wifi_manual_off"):
            persistent[ip] = {
                "ip": ip,
                "mac": d.get("mac", ""),
                "interface": d.get("interface", ""),
                "remark": d.get("remark", ""),
                "exit_id": d.get("exit_id", ""),
                "manual": bool(d.get("manual", False)),
                # Explicit operator disconnect survives a program restart.
                "qizi_wifi_manual_off": bool(d.get("qizi_wifi_manual_off", False)),
            }
    save_json(DEVICES_FILE, persistent)


def normalize_mac(mac):
    return str(mac or '').strip().upper().replace(':', '-')


def valid_device_mac(mac):
    mac = normalize_mac(mac)
    return bool(mac) and mac not in {'00-00-00-00-00-00', 'FF-FF-FF-FF-FF-FF'}


def gateway_firewall_ready():
    try:
        script = (
            f'$r = Get-NetFirewallRule -DisplayName "{FIREWALL_RULE_NAME}" '
            '-ErrorAction SilentlyContinue | Select-Object -First 1; '
            'if ($null -ne $r) { Write-Output "READY" }'
        )
        return powershell(script, timeout=10).strip() == "READY"
    except Exception:
        return False


def proxy_url(proxy):
    host = proxy["host"].strip()
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    port = int(proxy["port"])
    username = proxy.get("username", "")
    password = proxy.get("password", "")

    auth = ""
    if username:
        auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"

    return f"http://{auth}{host}:{port}"


def test_http_exit(proxy):
    url = proxy_url(proxy)
    proxies = {"http": url, "https": url}
    errors = []

    try:
        r = requests.get(
            "https://ipapi.co/json/",
            proxies=proxies,
            timeout=TEST_TIMEOUT,
            headers={"User-Agent": "ProxyManager/1.0"},
        )
        r.raise_for_status()
        data = r.json()
        ip = str(data.get("ip", "")).strip()
        country = data.get("country_name") or data.get("country") or data.get("country_code") or ""
        if ip:
            return ip, str(country).strip() or "未知"
    except Exception as exc:
        errors.append(f"ipapi.co: {exc}")

    try:
        r = requests.get(
            "https://api.ipify.org?format=json",
            proxies=proxies,
            timeout=TEST_TIMEOUT,
            headers={"User-Agent": "ProxyManager/1.0"},
        )
        r.raise_for_status()
        ip = str(r.json().get("ip", "")).strip()
        if not ip:
            raise RuntimeError("未返回公网 IP")
    except Exception as exc:
        errors.append(f"ipify: {exc}")
        raise RuntimeError("出口连接失败：" + " | ".join(errors))

    country = "未知"
    try:
        geo = requests.get(
            f"https://ipapi.co/{ip}/json/",
            timeout=8,
            headers={"User-Agent": "ProxyManager/1.0"},
        )
        geo.raise_for_status()
        data = geo.json()
        country = data.get("country_name") or data.get("country") or data.get("country_code") or "未知"
    except Exception:
        pass

    return ip, str(country).strip() or "未知"


def make_config(proxy):
    return {
        "log": {"level": "info", "timestamp": True},
        "inbounds": [{
            "type": "mixed",
            "tag": "local-proxy",
            "listen": LOCAL_HOST,
            "listen_port": LOCAL_PORT
        }],
        "outbounds": [{
            "type": "http",
            "tag": "selected-http-exit",
            "server": proxy["host"],
            "server_port": int(proxy["port"]),
            "username": proxy.get("username", ""),
            "password": proxy.get("password", "")
        }],
        "route": {"final": "selected-http-exit"}
    }


def _json_list(text):
    if not text:
        return []
    obj = json.loads(text)
    return obj if isinstance(obj, list) else [obj]


def get_default_interface_index():
    script = r'''
$ErrorActionPreference="Stop"
$r = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" |
     Sort-Object RouteMetric,InterfaceMetric |
     Select-Object -First 1 InterfaceIndex
if ($null -ne $r) { $r | ConvertTo-Json -Compress }
'''
    try:
        data = _json_list(powershell(script))
        if data:
            return int(data[0]["InterfaceIndex"])
    except Exception:
        pass
    return None


def get_ipv4_interfaces():
    script = r'''
$ErrorActionPreference="Stop"
Get-NetIPAddress -AddressFamily IPv4 |
Where-Object {
    $_.IPAddress -ne "127.0.0.1" -and
    $_.IPAddress -notlike "169.254.*"
} |
Select-Object InterfaceIndex,InterfaceAlias,IPAddress,PrefixLength |
ConvertTo-Json -Compress
'''
    return _json_list(powershell(script))


def choose_hotspot_interfaces():
    interfaces = get_ipv4_interfaces()
    default_if = get_default_interface_index()

    preferred = []
    fallback = []

    for item in interfaces:
        try:
            ip = ipaddress.ip_address(item["IPAddress"])
            prefix = int(item.get("PrefixLength", 24))
            alias = str(item.get("InterfaceAlias", ""))
            index = int(item.get("InterfaceIndex", 0))
        except Exception:
            continue

        if not ip.is_private:
            continue

        rec = {
            "index": index,
            "alias": alias,
            "ip": str(ip),
            "prefix": prefix,
            "network": ipaddress.ip_network(f"{ip}/{prefix}", strict=False),
        }

        low = alias.lower()
        name_match = any(x in low for x in (
            "local area connection",
            "mobile hotspot",
            "hotspot",
            "本地连接",
            "热点",
        ))

        if str(ip) == "192.168.137.1":
            preferred.insert(0, rec)
        elif name_match:
            preferred.append(rec)
        elif index != default_if:
            fallback.append(rec)

    return preferred or fallback


def get_neighbors():
    script = r'''
$ErrorActionPreference="Stop"
Get-NetNeighbor -AddressFamily IPv4 |
Where-Object {
    $_.State -ne "Unreachable" -and
    $_.State -ne "Incomplete" -and
    $_.IPAddress -ne "255.255.255.255"
} |
Select-Object InterfaceIndex,IPAddress,LinkLayerAddress,@{Name="State";Expression={$_.State.ToString()}} |
ConvertTo-Json -Compress
'''
    return _json_list(powershell(script))


def scan_hotspot_devices(hotspot_ifs=None):
    if hotspot_ifs is None:
        hotspot_ifs = choose_hotspot_interfaces()
    if not hotspot_ifs:
        return [], "没有识别到可用的热点/共享网络接口"

    neighbors = get_neighbors()
    by_index = {x["index"]: x for x in hotspot_ifs}
    own_ips = {x["ip"] for x in hotspot_ifs}

    state_rank = {
        "Reachable": 50,
        "Permanent": 50,
        "Delay": 40,
        "Probe": 40,
        "Stale": 20,
        "Unknown": 10,
    }
    best_by_mac = {}

    for n in neighbors:
        try:
            idx = int(n.get("InterfaceIndex", 0))
            ip_text = str(n.get("IPAddress", "")).strip()
            ip = ipaddress.ip_address(ip_text)
        except Exception:
            continue

        adapter = by_index.get(idx)
        if not adapter:
            continue

        network = adapter["network"]
        if ip_text in own_ips or ip.is_multicast or ip.is_loopback:
            continue
        if ip not in network:
            continue
        if ip == network.network_address or ip == network.broadcast_address:
            continue

        mac = normalize_mac(n.get("LinkLayerAddress", ""))
        if not valid_device_mac(mac):
            continue

        state = str(n.get("State", "") or "")
        item = {
            "ip": ip_text,
            "mac": mac,
            "interface": adapter["alias"],
            "state": state,
        }
        old = best_by_mac.get(mac)
        if old is None or state_rank.get(state, 0) > state_rank.get(old["state"], 0):
            best_by_mac[mac] = item

    devices = list(best_by_mac.values())
    info = "；".join(f'{x["alias"]} {x["ip"]}/{x["prefix"]}' for x in hotspot_ifs)
    return devices, info



def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def restart_as_admin():
    try:
        if getattr(sys, "frozen", False):
            executable = sys.executable
            params = ""
        else:
            executable = sys.executable
            params = f'"{Path(__file__).resolve()}"'

        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", executable, params, str(APP_DIR), 1
        )
        return rc > 32
    except Exception:
        return False


def exit_tag(exit_id):
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(exit_id))
    return "exit-" + (cleaned[:18] or uuid.uuid4().hex[:18])


def detect_hotspot_gateway():
    interfaces = choose_hotspot_interfaces()
    if not interfaces:
        raise RuntimeError("没有识别到 Windows 移动热点网卡。")

    interfaces.sort(key=lambda x: 0 if x["ip"] == "192.168.137.1" else 1)
    return interfaces[0]


def make_hotspot_gateway_config(exits, devices, listen_ip):
    exit_by_id = {p.get("id", ""): p for p in exits}

    bindings = []
    used_exit_ids = set()

    for ip, device in devices.items():
        eid = device.get("exit_id", "")
        if not eid or eid not in exit_by_id:
            continue

        try:
            parsed = ipaddress.ip_address(ip)
            if parsed.version != 4:
                continue
        except Exception:
            continue

        proxy = exit_by_id[eid]
        bindings.append((ip, eid, proxy))
        used_exit_ids.add(eid)

    if not bindings:
        raise RuntimeError("没有已绑定出口的设备。请先选择手机并点击“保存绑定”。")

    outbounds = [{"type": "direct", "tag": "direct-out"}]

    for p in exits:
        eid = p.get("id", "")
        if eid not in used_exit_ids:
            continue

        outbound = {
            "type": "http",
            "tag": exit_tag(eid),
            "server": p["host"],
            "server_port": int(p["port"]),
        }

        if p.get("username"):
            outbound["username"] = p.get("username", "")
            outbound["password"] = p.get("password", "")

        outbounds.append(outbound)

    rules = []
    for ip, eid, proxy in bindings:
        rules.append({
            "inbound": ["hotspot-http"],
            "source_ip_cidr": [f"{ip}/32"],
            "action": "route",
            "outbound": exit_tag(eid),
        })

    config = {
        "log": {"level": "info", "timestamp": True},
        "inbounds": [{
            "type": "http",
            "tag": "hotspot-http",
            "listen": listen_ip,
            "listen_port": GATEWAY_PORT,
        }],
        "outbounds": outbounds,
        "route": {
            "rules": rules,
            "final": "direct-out",
            "auto_detect_interface": True,
        }
    }

    return config, bindings


def ensure_gateway_firewall():
    if not is_admin():
        raise PermissionError("需要管理员权限才能自动放行 Windows 防火墙端口。")

    script = f'''
$ErrorActionPreference="Stop"
$rule = Get-NetFirewallRule -DisplayName "{FIREWALL_RULE_NAME}" -ErrorAction SilentlyContinue
if ($null -eq $rule) {{
    New-NetFirewallRule `
        -DisplayName "{FIREWALL_RULE_NAME}" `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort {GATEWAY_PORT} `
        -Profile Any | Out-Null
}} else {{
    Set-NetFirewallRule -DisplayName "{FIREWALL_RULE_NAME}" -Enabled True -Action Allow -Profile Any | Out-Null
    Get-NetFirewallRule -DisplayName "{FIREWALL_RULE_NAME}" |
        Get-NetFirewallPortFilter |
        Set-NetFirewallPortFilter -Protocol TCP -LocalPort {GATEWAY_PORT} | Out-Null
}}
'''
    powershell(script, timeout=20)




class ExitDialog(tk.Toplevel):
    def __init__(self, parent, title, initial=None):
        super().__init__(parent)
        self.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei')
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None
        initial = initial or {}
        self._initial = dict(initial)
        self._initial_id = initial.get("id", "")
        self.entries = {}

        body = ttk.Frame(self, padding=16)
        body.grid(sticky="nsew")

        fields = [
            ("出口名称", "name", False),
            ("服务器地址 / IP", "host", False),
            ("端口", "port", False),
            ("用户名", "username", False),
            ("密码", "password", True),
        ]

        for row, (label, key, secret) in enumerate(fields):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=5)
            entry = ttk.Entry(body, width=36, show="*" if secret else "")
            entry.grid(row=row, column=1, padx=(12, 0), pady=5)
            entry.insert(0, str(initial.get(key, "")))
            self.entries[key] = entry

        if initial.get('geo_status'):
            detail=(f"国家：{initial.get('country','')}    城市：{initial.get('city','')}\n"
                    f"原始纬度：{initial.get('geo_raw_lat','')}    原始经度：{initial.get('geo_raw_lon','')}\n"
                    f"补偿纬度：{initial.get('geo_lat','')}    补偿经度：{initial.get('geo_lon','')}\n"
                    f"匹配状态：{initial.get('geo_status','')}")
            ttk.Label(body,text=detail,wraplength=470,justify='left').grid(row=len(fields)+2,column=0,columnspan=2,sticky='w')
        ttk.Label(
            body,
            text="保存后自动测试出口，并识别公网 IP 与国家。",
            foreground="#666666",
        ).grid(row=len(fields)+3, column=0, columnspan=2, sticky="w", pady=(8, 4))

        bar = ttk.Frame(body)
        bar.grid(row=len(fields) + 4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="保存并自检", command=self.save).pack(side="right", padx=8)

        self.bind("<Return>", lambda _e: self.save())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.entries["name"].focus_set()

    def save(self):
        values = {k: (v.get() if k in ('username', 'password') else v.get().strip())
                  for k, v in self.entries.items()}

        if not values["name"] or not values["host"] or not values["port"]:
            messagebox.showwarning("信息不完整", "名称、地址和端口不能为空。", parent=self)
            return

        try:
            port = int(values["port"])
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showwarning("端口错误", "端口必须是 1 到 65535 的整数。", parent=self)
            return

        values.update({k:v for k,v in self._initial.items() if k not in values})
        values.update({
            "id": self._initial_id or str(uuid.uuid4()),
            "port": port,
            "type": "http",
            "status": "待检测",
            "public_ip": self._initial.get("public_ip", ""),
            "country": self._initial.get("country", ""),
        })
        self.result = values
        self.destroy()


class ManualDeviceDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei')
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None

        body = ttk.Frame(self, padding=16)
        body.grid()

        ttk.Label(body, text="设备 IPv4").grid(row=0, column=0, sticky="w", pady=5)
        self.ip_entry = ttk.Entry(body, width=30)
        self.ip_entry.grid(row=0, column=1, padx=(12, 0), pady=5)

        ttk.Label(body, text="备注").grid(row=1, column=0, sticky="w", pady=5)
        self.remark_entry = ttk.Entry(body, width=30)
        self.remark_entry.grid(row=1, column=1, padx=(12, 0), pady=5)

        bar = ttk.Frame(body)
        bar.grid(row=2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="添加", command=self.save).pack(side="right", padx=8)

        self.ip_entry.focus_set()

    def save(self):
        ip_text = self.ip_entry.get().strip()
        try:
            ip = ipaddress.ip_address(ip_text)
            if ip.version != 4:
                raise ValueError
        except Exception:
            messagebox.showwarning("IP 错误", "请输入正确的 IPv4 地址。", parent=self)
            return

        self.result = {
            "ip": str(ip),
            "remark": self.remark_entry.get().strip(),
        }
        self.destroy()


class ProxyManagerApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei')
        self.geometry("820x680")
        self.minsize(820, 680)
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.exits = load_exits()
        self.devices = load_devices()

        for ip, d in self.devices.items():
            d.setdefault("ip", ip)
            d.setdefault("mac", "")
            d.setdefault("interface", "")
            d.setdefault("remark", "")
            d.setdefault("exit_id", "")
            d.setdefault("manual", False)
            d["online"] = False

        self.process = None
        self.active_name = None
        self.active_mode = None
        self.gateway_ip = ""
        self.last_gateway_log = ""
        self.events = queue.Queue()
        self.testing_indexes = set()
        self.scanning = False
        self._last_auto_scan = 0
        self._gateway_reload_job = None
        self._gateway_signature = None
        self._pending_gateway_signature = None
        self._gateway_starting = False
        self._auto_elevation_requested = False
        self._closing = False

        self.build_ui()
        self.refresh_exits()
        self.refresh_devices()
        self.after(300, self.scan_devices)
        self.after(100, self.handle_events)

    def build_ui(self):
        # 两个列表分别使用独立样式，方便单独调整内部行高。
        style = ttk.Style(self)
        style.configure("Exit.Treeview", rowheight=EXIT_ROW_HEIGHT)
        style.configure("Device.Treeview", rowheight=DEVICE_ROW_HEIGHT)

        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        title = ttk.Frame(root)
        title.pack(fill="x")
        ttk.Label(
            title,
            text="多设备出口自动管理器-有问题联系✈TG：Laosermei",
            font=("Microsoft YaHei UI", 16, "bold")
        ).pack(side="left")
        self.status = tk.StringVar(value="状态：未启动")
        ttk.Label(title, textvariable=self.status).pack(side="right")

        exit_box = ttk.LabelFrame(root, text="1. HTTP 出口", padding=8)
        exit_box.pack(fill="both", expand=False, pady=(10, 6))

        exit_cols = ("name", "host", "port", "country", "public_ip", "status")
        self.exit_table = ttk.Treeview(
            exit_box, columns=exit_cols, show="headings", height=EXIT_LIST_ROWS, selectmode="browse",
            style="Exit.Treeview"
        )
        exit_headers = {
            "name": "出口名称",
            "host": "服务器地址",
            "port": "端口",
            "country": "国家",
            "public_ip": "公网 IP",
            "status": "状态",
        }
        exit_widths = {
            "name": 90, "host": 148, "port": 68,
            "country": 46, "public_ip": 88, "status": 120
        }
        for key in exit_cols:
            self.exit_table.heading(key, text=exit_headers[key])
            self.exit_table.column(key, width=exit_widths[key], anchor="center")
        self.exit_table.pack(fill="x")

        exit_buttons = ttk.Frame(exit_box)
        exit_buttons.pack(fill="x", pady=(7, 0))
        ttk.Button(exit_buttons, text="添加出口", command=self.add_exit).pack(side="left")
        ttk.Button(exit_buttons, text="编辑出口", command=self.edit_exit).pack(side="left", padx=5)
        ttk.Button(exit_buttons, text="删除出口", command=self.delete_exit).pack(side="left")
        ttk.Button(exit_buttons, text="重新检测", command=self.test_selected_direct).pack(side="left", padx=(15, 0))
        ttk.Button(exit_buttons, text="检测全部", command=self.test_all).pack(side="left", padx=5)

        self.start_btn = ttk.Button(exit_buttons, text="启动所选出口", command=self.start_selected)
        self.start_btn.pack(side="right")
        self.stop_btn = ttk.Button(exit_buttons, text="停止 sing-box", command=self.stop, state="disabled")
        self.stop_btn.pack(side="right", padx=5)

        device_box = ttk.LabelFrame(root, text="2. 电脑热点设备", padding=8)
        device_box.pack(fill="both", expand=True, pady=6)

        toolbar = ttk.Frame(device_box)
        toolbar.pack(fill="x", pady=(0, 7))

        ttk.Button(toolbar, text="立即扫描", command=self.scan_devices).pack(side="left")
        ttk.Button(toolbar, text="手动添加 IP", command=self.add_manual_device).pack(side="left", padx=5)
        ttk.Button(toolbar, text="删除设备", command=self.delete_device).pack(side="left")

        self.auto_scan_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            toolbar,
            text=f"自动扫描（{AUTO_SCAN_SECONDS} 秒）",
            variable=self.auto_scan_var
        ).pack(side="left", padx=(16, 0))

        self.hotspot_info = tk.StringVar(value="热点网卡：等待扫描")
        ttk.Label(toolbar, textvariable=self.hotspot_info, foreground="#666").pack(side="right")

        device_cols = ("ip", "mac", "interface", "remark", "online", "exit")
        self.device_table = ttk.Treeview(
            device_box, columns=device_cols, show="headings", height=DEVICE_LIST_ROWS, selectmode="browse",
            style="Device.Treeview"
        )
        device_headers = {
            "ip": "设备 IP",
            "mac": "MAC",
            "interface": "热点网卡",
            "remark": "备注",
            "online": "状态",
            "exit": "指定出口",
        }
        device_widths = {
            "ip": 116, "mac": 150, "interface": 150,
            "remark": 80, "online": 80, "exit": 180
        }
        for key in device_cols:
            self.device_table.heading(key, text=device_headers[key])
            self.device_table.column(key, width=device_widths[key], anchor="center")
        self.device_table.pack(fill="both", expand=True)
        self.device_table.bind("<<TreeviewSelect>>", lambda _e: self.load_selected_device_binding())

        bind_bar = ttk.Frame(device_box)
        bind_bar.pack(fill="x", pady=(8, 0))

        ttk.Label(bind_bar, text="所选设备出口：").pack(side="left")
        self.exit_combo = ttk.Combobox(bind_bar, state="readonly", width=28)
        self.exit_combo.pack(side="left")

        ttk.Button(bind_bar, text="保存绑定", command=self.bind_selected_device).pack(side="left", padx=6)
        ttk.Button(bind_bar, text="取消绑定", command=self.unbind_selected_device).pack(side="left")

        self.gateway_btn = ttk.Button(
            bind_bar,
            text="手动重启网关",
            command=lambda: self.request_gateway_reconcile("手动重启", force=True)
        )
        self.gateway_btn.pack(side="right")

        ttk.Label(
            bind_bar,
            text="自动模式：保存绑定立即生效｜支持多设备、多出口",
            foreground="#666"
        ).pack(side="right", padx=(0, 12))

        gateway_box = ttk.LabelFrame(root, text="3. 手机代理设置", padding=8)
        gateway_box.pack(fill="x", pady=(6, 0))

        self.gateway_info = tk.StringVar(
            value=f"自动网关：等待设备绑定。手机统一使用热点 IP，端口 {GATEWAY_PORT}。"
        )
        ttk.Label(
            gateway_box,
            textvariable=self.gateway_info,
            font=("Microsoft YaHei UI", 10, "bold")
        ).pack(side="left")

        ttk.Label(
            gateway_box,
            text="所有手机使用同一个代理地址，程序按手机 IP 自动选择出口。",
            foreground="#666"
        ).pack(side="right")

        self.detail = tk.StringVar(value="正在准备扫描热点设备……")
        ttk.Label(root, textvariable=self.detail, foreground="#555").pack(fill="x", pady=(5, 0))



    def selected_exit_index(self):
        selection = self.exit_table.selection()
        return int(selection[0]) if selection else None

    def refresh_exits(self, selected=None):
        self.exit_table.delete(*self.exit_table.get_children())
        for i, p in enumerate(self.exits):
            self.exit_table.insert(
                "", "end", iid=str(i),
                values=(
                    p.get("name", ""),
                    p.get("host", ""),
                    p.get("port", ""),
                    p.get("country", ""),
                    p.get("public_ip", ""),
                    p.get("status", "未检测"),
                )
            )

        if self.exits:
            idx = 0 if selected is None else min(selected, len(self.exits) - 1)
            self.exit_table.selection_set(str(idx))
            self.exit_table.focus(str(idx))

        names = ["（未绑定）"] + [p.get("name", "") for p in self.exits]
        self.exit_combo["values"] = names
        if not self.exit_combo.get():
            self.exit_combo.set("（未绑定）")

        self.refresh_devices()

    def add_exit(self):
        dialog = ExitDialog(self, "添加 HTTP 出口")
        self.wait_window(dialog)
        if not dialog.result:
            return

        self.exits.append(dialog.result)
        idx = len(self.exits) - 1
        save_json(EXITS_FILE, self.exits)
        self.refresh_exits(idx)
        self.start_direct_test(idx)
        self.request_gateway_reconcile("新增出口")

    def edit_exit(self):
        idx = self.selected_exit_index()
        if idx is None:
            messagebox.showinfo("提示", "请先选择一个出口。")
            return

        old = self.exits[idx]
        if self.running() and old.get("name") == self.active_name:
            messagebox.showwarning("正在运行", "请先停止当前出口，再进行编辑。")
            return

        dialog = ExitDialog(self, "编辑 HTTP 出口", old)
        self.wait_window(dialog)
        if not dialog.result:
            return

        self.exits[idx] = dialog.result
        save_json(EXITS_FILE, self.exits)
        self.refresh_exits(idx)
        self.start_direct_test(idx)
        self.request_gateway_reconcile("出口配置修改")

    def delete_exit(self):
        idx = self.selected_exit_index()
        if idx is None:
            messagebox.showinfo("提示", "请先选择一个出口。")
            return

        proxy = self.exits[idx]
        if self.running() and proxy.get("name") == self.active_name:
            messagebox.showwarning("正在运行", "当前出口正在运行，请先停止。")
            return

        if not messagebox.askyesno("确认删除", f"确定删除“{proxy.get('name', '')}”吗？"):
            return

        deleted_id = proxy.get("id", "")
        del self.exits[idx]

        for d in self.devices.values():
            if d.get("exit_id") == deleted_id:
                d["exit_id"] = ""

        save_json(EXITS_FILE, self.exits)
        save_devices(self.devices)
        self.refresh_exits(max(0, idx - 1))
        self.request_gateway_reconcile("出口删除")

    def start_direct_test(self, idx):
        if not 0 <= idx < len(self.exits) or idx in self.testing_indexes:
            return

        self.testing_indexes.add(idx)
        proxy = self.exits[idx]
        proxy["status"] = "检测中..."
        proxy["public_ip"] = ""
        proxy["country"] = ""
        save_json(EXITS_FILE, self.exits)
        self.refresh_exits(idx)
        self.detail.set(f"正在检测出口：{proxy.get('name', '')} ...")

        threading.Thread(
            target=self.direct_test_worker,
            args=(idx, proxy.copy()),
            daemon=True
        ).start()

    def direct_test_worker(self, idx, proxy):
        try:
            ip, country = test_http_exit(proxy)
            self.events.put(("direct_test_ok", idx, ip, country))
        except Exception as exc:
            self.events.put(("direct_test_error", idx, str(exc)))

    def test_selected_direct(self):
        idx = self.selected_exit_index()
        if idx is None:
            messagebox.showinfo("提示", "请先选择一个出口。")
            return
        self.start_direct_test(idx)

    def test_all(self):
        for idx in range(len(self.exits)):
            self.start_direct_test(idx)

    def selected_device_ip(self):
        selection = self.device_table.selection()
        return selection[0] if selection else None

    def exit_name_by_id(self, exit_id):
        for p in self.exits:
            if p.get("id") == exit_id:
                return p.get("name", "")
        return ""

    def exit_id_by_name(self, name):
        for p in self.exits:
            if p.get("name") == name:
                return p.get("id", "")
        return ""

    def refresh_devices(self, selected_ip=None):
        if not hasattr(self, "device_table"):
            return

        current = selected_ip or self.selected_device_ip()
        self.device_table.delete(*self.device_table.get_children())

        def ip_key(x):
            try:
                return int(ipaddress.ip_address(x))
            except Exception:
                return 0

        for ip in sorted(self.devices.keys(), key=ip_key):
            d = self.devices[ip]
            exit_name = self.exit_name_by_id(d.get("exit_id", "")) or "未绑定"
            self.device_table.insert(
                "", "end", iid=ip,
                values=(
                    ip,
                    d.get("mac", ""),
                    d.get("interface", ""),
                    d.get("remark", ""),
                    "在线" if d.get("online") else "离线",
                    exit_name,
                )
            )

        if current and self.device_table.exists(current):
            self.device_table.selection_set(current)
            self.device_table.focus(current)

    def scan_devices(self):
        if self.scanning:
            return

        self.scanning = True
        self.detail.set("正在扫描 Windows 热点设备……")
        threading.Thread(target=self.scan_worker, daemon=True).start()

    def scan_worker(self):
        try:
            found, hotspot_info = scan_hotspot_devices()
            self.events.put(("devices_scanned", found, hotspot_info))
        except Exception as exc:
            self.events.put(("devices_scan_error", str(exc)))

    def add_manual_device(self):
        dialog = ManualDeviceDialog(self)
        self.wait_window(dialog)
        if not dialog.result:
            return

        ip = dialog.result["ip"]
        d = self.devices.setdefault(ip, {})
        d.update({
            "ip": ip,
            "remark": dialog.result["remark"],
            "manual": True,
            "online": d.get("online", False),
            "mac": d.get("mac", ""),
            "interface": d.get("interface", ""),
            "exit_id": d.get("exit_id", ""),
        })
        save_devices(self.devices)
        self.refresh_devices(ip)

    def delete_device(self):
        ip = self.selected_device_ip()
        if not ip:
            messagebox.showinfo("提示", "请先选择一个设备。")
            return

        if messagebox.askyesno("确认删除", f"确定删除设备 {ip} 吗？"):
            self.devices.pop(ip, None)
            save_devices(self.devices)
            self.refresh_devices()
            self.request_gateway_reconcile("设备删除")

    def load_selected_device_binding(self):
        ip = self.selected_device_ip()
        if not ip:
            return
        d = self.devices.get(ip, {})
        name = self.exit_name_by_id(d.get("exit_id", ""))
        self.exit_combo.set(name or "（未绑定）")

    def bind_selected_device(self):
        ip = self.selected_device_ip()
        if not ip:
            messagebox.showinfo("提示", "请先选择一个设备。")
            return

        name = self.exit_combo.get().strip()
        if not name or name == "（未绑定）":
            messagebox.showinfo("提示", "请选择一个出口。")
            return

        exit_id = self.exit_id_by_name(name)
        if not exit_id:
            messagebox.showerror("绑定失败", "没有找到对应出口。")
            return

        self.devices[ip]["exit_id"] = exit_id
        save_devices(self.devices)
        self.refresh_devices(ip)
        self.detail.set(f"已保存绑定：{ip} → {name}，正在自动应用……")
        self.request_gateway_reconcile("保存绑定")

    def unbind_selected_device(self):
        ip = self.selected_device_ip()
        if not ip:
            messagebox.showinfo("提示", "请先选择一个设备。")
            return

        self.devices[ip]["exit_id"] = ""
        save_devices(self.devices)
        self.exit_combo.set("（未绑定）")
        self.refresh_devices(ip)
        self.detail.set(f"已取消设备 {ip} 的出口绑定，正在自动应用……")
        self.request_gateway_reconcile("取消绑定")

    def desired_gateway_signature(self):
        exit_by_id = {p.get("id", ""): p for p in self.exits}
        rows = []
        for ip, d in self.devices.items():
            eid = d.get("exit_id", "")
            if not eid or eid not in exit_by_id:
                continue
            try:
                parsed = ipaddress.ip_address(ip)
                if parsed.version != 4:
                    continue
            except Exception:
                continue
            p = exit_by_id[eid]
            rows.append((
                ip,
                normalize_mac(d.get("mac", "")),
                eid,
                p.get("host", ""),
                int(p.get("port", 0) or 0),
                p.get("username", ""),
                p.get("password", ""),
            ))
        return tuple(sorted(rows))

    def request_gateway_reconcile(self, reason="", force=False):
        if self._closing:
            return
        if self._gateway_reload_job is not None:
            try:
                self.after_cancel(self._gateway_reload_job)
            except Exception:
                pass
            self._gateway_reload_job = None
        delay = 50 if force else AUTO_GATEWAY_RELOAD_MS
        self._gateway_reload_job = self.after(
            delay,
            lambda: self.reconcile_gateway(reason, force)
        )

    def reconcile_gateway(self, reason="", force=False):
        self._gateway_reload_job = None
        if self._closing or self._gateway_starting:
            return

        desired = self.desired_gateway_signature()
        if not desired:
            if self.running() and self.active_mode == "gateway":
                self.stop()
            self._gateway_signature = None
            self.status.set("状态：自动网关等待设备绑定")
            self.gateway_info.set(
                f"自动网关：等待绑定｜手机 Wi-Fi 代理端口 {GATEWAY_PORT}"
            )
            return

        if (not force and self.running() and self.active_mode == "gateway"
                and desired == self._gateway_signature):
            return

        self.start_hotspot_gateway(auto=True)

    def migrate_device_bindings_by_mac(self, found):
        before = self.desired_gateway_signature()

        profiles = {}
        for old_ip, d in list(self.devices.items()):
            mac = normalize_mac(d.get("mac", ""))
            if valid_device_mac(mac) and (d.get("exit_id") or d.get("remark") or d.get("manual") or d.get("qizi_wifi_manual_off")):
                profiles[mac] = {
                    "old_ip": old_ip,
                    "exit_id": d.get("exit_id", ""),
                    "remark": d.get("remark", ""),
                    "manual": bool(d.get("manual", False)),
                    "qizi_wifi_manual_off": bool(d.get("qizi_wifi_manual_off", False)),
                }

        for d in self.devices.values():
            d["online"] = False

        active_ips = set()
        for item in found:
            ip = item["ip"]
            mac = normalize_mac(item.get("mac", ""))
            if not valid_device_mac(mac):
                continue
            active_ips.add(ip)

            existing = self.devices.get(ip)
            if existing and normalize_mac(existing.get("mac", "")) not in ("", mac):
                self.devices[ip] = {
                    "ip": ip, "mac": mac, "interface": item.get("interface", ""),
                    "remark": "", "exit_id": "", "manual": False, "online": True,
                }

            d = self.devices.setdefault(ip, {
                "ip": ip, "remark": "", "exit_id": "", "manual": False,
            })

            profile = profiles.get(mac)
            if profile:
                old_ip = profile["old_ip"]
                if old_ip != ip:
                    old = self.devices.get(old_ip)
                    if old and normalize_mac(old.get("mac", "")) == mac:
                        self.devices.pop(old_ip, None)
                d["exit_id"] = profile.get("exit_id", "")
                d["remark"] = profile.get("remark", "")
                d["manual"] = profile.get("manual", False)
                d["qizi_wifi_manual_off"] = profile.get("qizi_wifi_manual_off", False)

            d["ip"] = ip
            d["mac"] = mac
            d["interface"] = item.get("interface", "")
            d["online"] = True
            d["last_seen"] = time.time()

        for old_ip, d in list(self.devices.items()):
            if old_ip in active_ips:
                continue
            if not d.get("exit_id") and not d.get("remark") and not d.get("manual") and not d.get("qizi_wifi_manual_off"):
                self.devices.pop(old_ip, None)

        save_devices(self.devices)
        return before != self.desired_gateway_signature()

    def start_hotspot_gateway(self, auto=False):
        if not SING_BOX.exists():
            messagebox.showerror(
                "文件不存在",
                "找不到 sing-box.exe。\n\n"
                f"程序目录：\n{SING_BOX}"
            )
            return

        try:
            hotspot = detect_hotspot_gateway()
            listen_ip = hotspot["ip"]
            config, bindings = make_hotspot_gateway_config(
                self.exits, self.devices, listen_ip
            )
        except Exception as exc:
            messagebox.showerror("无法启动热点代理", str(exc))
            return

        firewall_ready = gateway_firewall_ready()
        if not firewall_ready and not is_admin():
            if auto:
                if not self._auto_elevation_requested:
                    self._auto_elevation_requested = True
                    self.status.set("状态：自动网关正在请求管理员权限...")
                    self.detail.set("首次运行需要初始化 Windows 防火墙，正在重新以管理员身份启动。")
                    if restart_as_admin():
                        self.after(500, self.destroy)
                    else:
                        self._auto_elevation_requested = False
                        self.detail.set("管理员权限请求失败，请右键以管理员身份运行一次。")
                return

            if messagebox.askyesno(
                "需要管理员权限",
                "第一次启动热点代理需要放行 Windows 防火墙端口。\n\n"
                "是否关闭当前窗口并以管理员身份重新运行？"
            ):
                if restart_as_admin():
                    self.after(300, self.destroy)
                else:
                    messagebox.showerror("提升失败", "无法以管理员权限重新启动。")
            return

        if not firewall_ready:
            try:
                ensure_gateway_firewall()
            except Exception as exc:
                if auto:
                    self.detail.set(f"自动网关防火墙初始化失败：{exc}")
                else:
                    messagebox.showerror("防火墙设置失败", str(exc))
                return

        desired_signature = self.desired_gateway_signature()
        if not desired_signature:
            return

        self._gateway_starting = True
        if self.running():
            self.kill_process()

        GATEWAY_CONFIG_FILE.write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        self.gateway_ip = listen_ip
        self.active_mode = "gateway"
        self.active_name = None
        self._pending_gateway_signature = desired_signature

        self.status.set("状态：正在启动热点代理网关...")
        self.detail.set("正在检查 sing-box 热点 HTTP 网关配置...")
        self.gateway_info.set(
            f"手机 Wi-Fi 代理：服务器 {listen_ip} ｜端口 {GATEWAY_PORT}"
        )
        self.gateway_btn.config(state="disabled")
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

        threading.Thread(
            target=self.gateway_worker,
            args=(bindings,),
            daemon=True
        ).start()

    def gateway_worker(self, bindings):
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

        try:
            checked = subprocess.run(
                [str(SING_BOX), "check", "-c", str(GATEWAY_CONFIG_FILE)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                creationflags=flags
            )

            if checked.returncode:
                raise RuntimeError(
                    (checked.stderr or checked.stdout or "热点代理配置错误").strip()
                )

            GATEWAY_LOG_FILE.write_text("", encoding="utf-8")

            self.process = subprocess.Popen(
                [str(SING_BOX), "run", "-c", str(GATEWAY_CONFIG_FILE)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore",
                bufsize=1,
                creationflags=flags
            )

            threading.Thread(
                target=self.read_gateway_output,
                args=(self.process,),
                daemon=True
            ).start()

            ready = False
            import socket

            for _ in range(25):
                if self.process.poll() is not None:
                    raise RuntimeError(
                        "sing-box 热点代理启动后立即停止。\n\n"
                        + (self.last_gateway_log or "请查看 hotspot_gateway.log")
                    )

                try:
                    with socket.create_connection((self.gateway_ip, GATEWAY_PORT), timeout=0.25):
                        ready = True
                        break
                except OSError:
                    time.sleep(0.2)

            if not ready:
                raise RuntimeError(
                    f"热点代理端口 {self.gateway_ip}:{GATEWAY_PORT} 未开始监听。"
                )

            summary = "；".join(
                f"{ip} → {proxy.get('name', '')}"
                for ip, _eid, proxy in bindings
            )

            self.events.put(("gateway_started", len(bindings), summary))

        except Exception as exc:
            self.kill_process()
            self.events.put(("gateway_error", str(exc)))

    def read_gateway_output(self, process):
        try:
            with GATEWAY_LOG_FILE.open("a", encoding="utf-8") as log:
                if process.stdout is None:
                    return

                for raw in process.stdout:
                    line = raw.rstrip()
                    if not line:
                        continue

                    self.last_gateway_log = line
                    log.write(line + "\n")
                    log.flush()

                    low = line.lower()
                    if "error" in low or "fatal" in low or "warn" in low:
                        self.events.put(("gateway_log", line))
        except Exception:
            pass

    def running(self):
        return self.process is not None and self.process.poll() is None

    def start_selected(self):
        idx = self.selected_exit_index()
        if idx is None:
            messagebox.showinfo("提示", "请先选择一个出口。")
            return

        if not SING_BOX.exists():
            messagebox.showerror(
                "文件不存在",
                "找不到 sing-box.exe。\n\n"
                f"程序目录：\n{SING_BOX}"
            )
            return

        if self.running():
            self.stop()

        self.active_mode = "single"
        proxy = self.exits[idx]
        CONFIG_FILE.write_text(
            json.dumps(make_config(proxy), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        self.status.set(f"状态：正在启动 {proxy['name']}...")
        self.detail.set("正在检查 sing-box 配置并启动...")
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

        threading.Thread(
            target=self.start_worker,
            args=(proxy,),
            daemon=True
        ).start()

    def start_worker(self, proxy):
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

        try:
            checked = subprocess.run(
                [str(SING_BOX), "check", "-c", str(CONFIG_FILE)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                creationflags=flags
            )

            if checked.returncode:
                raise RuntimeError((checked.stderr or checked.stdout or "配置错误").strip())

            self.process = subprocess.Popen(
                [str(SING_BOX), "run", "-c", str(CONFIG_FILE)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                creationflags=flags
            )
            self.active_name = proxy["name"]

            local_proxy = f"http://{LOCAL_HOST}:{LOCAL_PORT}"

            for _ in range(15):
                if self.process.poll() is not None:
                    raise RuntimeError("sing-box 启动后意外停止。")

                try:
                    r = requests.get(
                        "https://ipapi.co/json/",
                        proxies={"http": local_proxy, "https": local_proxy},
                        timeout=8,
                        headers={"User-Agent": "ProxyManager/1.0"}
                    )
                    r.raise_for_status()
                    data = r.json()
                    ip = data.get("ip", "未知")
                    country = data.get("country_name") or data.get("country") or "未知"
                    self.events.put(("started", proxy["name"], str(ip), str(country)))
                    return
                except requests.RequestException:
                    time.sleep(1)

            raise RuntimeError("sing-box 已启动，但本地出口测试超时。")

        except Exception as exc:
            self.kill_process()
            self.events.put(("error", str(exc)))

    def kill_process(self):
        if self.running():
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()

        self.process = None
        self.active_name = None
        self.active_mode = None

    def stop(self):
        self.kill_process()
        self.status.set("状态：已停止")
        self.detail.set("sing-box 已停止。Windows 热点本身仍保持正常 ICS 联网。")
        self._gateway_signature = None
        self._pending_gateway_signature = None
        self._gateway_starting = False
        self.gateway_info.set(
            "自动网关已停止；只要仍有绑定，下一次扫描/配置变化会自动恢复。"
        )
        self.start_btn.config(state="normal")
        self.gateway_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

    def handle_events(self):
        try:
            while True:
                event = self.events.get_nowait()

                if event[0] == "direct_test_ok":
                    _, idx, ip, country = event
                    self.testing_indexes.discard(idx)
                    if 0 <= idx < len(self.exits):
                        self.exits[idx]["status"] = "连接成功"
                        self.exits[idx]["public_ip"] = ip
                        self.exits[idx]["country"] = country
                        save_json(EXITS_FILE, self.exits)
                        self.refresh_exits(idx)
                        self.detail.set(f"出口检测成功｜公网 IP：{ip}｜国家：{country}")

                elif event[0] == "direct_test_error":
                    _, idx, error_text = event
                    self.testing_indexes.discard(idx)
                    if 0 <= idx < len(self.exits):
                        self.exits[idx]["status"] = "连接失败"
                        self.exits[idx]["public_ip"] = ""
                        self.exits[idx]["country"] = ""
                        save_json(EXITS_FILE, self.exits)
                        self.refresh_exits(idx)
                        self.detail.set(f"出口检测失败：{error_text}")

                elif event[0] == "devices_scanned":
                    _, found, hotspot_info = event
                    self.scanning = False

                    changed = self.migrate_device_bindings_by_mac(found)
                    self.hotspot_info.set(f"热点网卡：{hotspot_info}")
                    self.refresh_devices()
                    bound_count = sum(1 for d in self.devices.values() if d.get("exit_id"))
                    self.detail.set(
                        f"扫描完成：发现 {len(found)} 台设备｜已绑定 {bound_count} 台。"
                    )
                    self.request_gateway_reconcile(
                        "DHCP/IP变化" if changed else "自动扫描"
                    )

                elif event[0] == "devices_scan_error":
                    self.scanning = False
                    self.detail.set(f"热点设备扫描失败：{event[1]}")

                elif event[0] == "gateway_started":
                    _, count, summary = event
                    self._gateway_starting = False
                    self._gateway_signature = self._pending_gateway_signature
                    self.status.set(f"状态：自动网关运行中｜{count} 台设备")
                    self.detail.set(
                        "配置已自动生效；新增设备、修改出口、设备 IP 变化都会自动重载。"
                    )
                    self.gateway_info.set(
                        f"手机 Wi-Fi 代理：服务器 {self.gateway_ip} ｜端口 {GATEWAY_PORT} ｜ {summary}"
                    )
                    self.gateway_btn.config(state="normal")
                    self.start_btn.config(state="disabled")
                    self.stop_btn.config(state="normal")

                elif event[0] == "gateway_error":
                    self._gateway_starting = False
                    self._gateway_signature = None
                    self.status.set("状态：自动网关启动失败")
                    self.detail.set(f"热点代理失败：{event[1]}")
                    self.gateway_info.set(
                        f"日志：{GATEWAY_LOG_FILE.name}｜最后信息：{self.last_gateway_log}"
                    )
                    self.gateway_btn.config(state="normal")
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    messagebox.showerror("热点代理启动失败", event[1])

                elif event[0] == "gateway_log":
                    self.detail.set(event[1][-240:])

                elif event[0] == "started":
                    _, name, ip, country = event
                    self.status.set(f"状态：{name} 运行中")
                    self.detail.set(f"sing-box 启动成功｜公网 IP：{ip}｜国家：{country}")
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="normal")

                elif event[0] == "error":
                    self.status.set("状态：启动失败")
                    self.detail.set(f"启动失败：{event[1]}")
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    messagebox.showerror("启动失败", event[1])

        except queue.Empty:
            pass

        if self.process is not None and not self.running():
            self.process = None
            self.active_name = None
            self.status.set("状态：sing-box 已停止")
            self.start_btn.config(state="normal")
            self.gateway_btn.config(state="normal")
            self.stop_btn.config(state="disabled")

        now = time.time()
        if self.auto_scan_var.get() and not self.scanning and now - self._last_auto_scan >= AUTO_SCAN_SECONDS:
            self._last_auto_scan = now
            self.scan_devices()

        self.after(100, self.handle_events)

    def close(self):
        self._closing = True
        if self._gateway_reload_job is not None:
            try:
                self.after_cancel(self._gateway_reload_job)
            except Exception:
                pass
        save_devices(self.devices)
        self.kill_process()
        self.destroy()


def exit_fingerprint(p):
    return tuple(p.get(k, "") for k in ("id", "host", "port", "username", "password"))




def test_http_exit(proxy):
    """
    5-second health probe.
    One HTTPS request only, with a hard short timeout, so a completed online/offline
    result normally returns before the next 5-second sweep.
    """
    proxy_address = proxy_url(proxy)

    with requests.Session() as session:
        session.trust_env = False
        session.proxies = {
            "http": proxy_address,
            "https": proxy_address,
        }
        session.headers.update({
            "User-Agent": "ProxyManager-V8/1.0",
            "Connection": "close",
            "Accept": "text/plain,*/*",
        })

        try:
            response = session.get(
                "https://www.cloudflare.com/cdn-cgi/trace",
                timeout=(1.5, 3.0),
                allow_redirects=True,
            )
            response.raise_for_status()

            values = {}
            for line in response.text.splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip()

            ip = str(ipaddress.ip_address(values.get("ip", "")))
            country = (
                values.get("loc", "")
                or proxy.get("country", "")
                or "未知"
            )
            return ip, country

        except requests.exceptions.ProxyError as exc:
            raise RuntimeError("代理认证/连接失败") from exc
        except requests.exceptions.Timeout as exc:
            raise RuntimeError("检测超时") from exc
        except requests.exceptions.SSLError as exc:
            raise RuntimeError("HTTPS证书/握手失败，请运行浏览器安全诊断") from exc
        except Exception as exc:
            raise RuntimeError(type(exc).__name__) from exc


def browser_security_report(proxy, listener_ready, live_gateway=None):
    """Verify certificates using Requests' CA bundle; never suppress verification.

    A short-lived secret on loopback exercises the actual production listener.
    The secret is stripped and never changes persistent device bindings.
    """
    lines = ["浏览器安全诊断 V14", "电脑本地时间：" + time.strftime("%Y-%m-%d %H:%M:%S %z"),
             "请核对电脑和手机日期时间是否准确。",
             f"实际{GATEWAY_PORT}服务：" + ("监听中" if listener_ready else "未就绪"),
             "证书库：" + ("存在" if Path(requests.certs.where()).is_file() else "缺失（请检查打包依赖）"),
             "TLS运行库：" + ssl.OPENSSL_VERSION,
             "诊断不显示代理账号密码；不修改绑定、系统代理或证书。", ""]
    endpoints = ("https://www.cloudflare.com/cdn-cgi/trace", "https://www.microsoft.com/")
    def probe(label, address, url, headers=None):
        try:
            with requests.Session() as session:
                session.trust_env = False
                with session.get(url, proxies={"http": address, "https": address},
                                 timeout=(5, 8), allow_redirects=False, stream=True,
                                 verify=True, headers=headers) as response:
                    code = response.status_code
                    lines.append(f"{label} | {url} | HTTPS证书通过，HTTP {code}")
                    if code >= 400:
                        lines.append("  TLS已建立，但网站返回错误状态；不能据此认定网页可正常使用。")
        except requests.exceptions.SSLError as exc:
            # Only retain known certificate reason text, never raw proxy URLs.
            detail = str(exc).lower()
            if "expired" in detail:
                reason = "证书已过期或本机时间异常"
            elif "not yet valid" in detail:
                reason = "证书尚未生效或本机时间异常"
            elif "hostname" in detail or "doesn't match" in detail:
                reason = "证书域名不匹配"
            elif "issuer" in detail or "self-signed" in detail:
                reason = "证书颁发者不受信任/证书链不完整"
            else:
                reason = "证书验证或TLS握手失败"
            lines.append(f"{label} | {url} | {reason}")
        except requests.exceptions.ProxyError as exc:
            raw = str(exc)
            code = next((n for n in ("407", "403", "502", "503") if n in raw), "")
            lines.append(f"{label} | {url} | 代理连接/认证/CONNECT失败 " + code)
        except requests.exceptions.Timeout:
            lines.append(f"{label} | {url} | 连接或读取超时")
        except Exception as exc:
            lines.append(f"{label} | {url} | " + type(exc).__name__)
    address = proxy_url(proxy)
    for url in endpoints:
        probe("直接经所选出口", address, url)
    if live_gateway is not None and live_gateway.ready.is_set():
        token = secrets.token_urlsafe(32)
        with live_gateway.lock:
            live_gateway.diagnostics[token] = dict(proxy)
        try:
            for url in endpoints:
                probe("经当前实际网关", f"http://qizi-diagnostic:{token}@127.0.0.1:{live_gateway.port}", url)
        finally:
            with live_gateway.lock:
                live_gateway.diagnostics.pop(token, None)
    else:
        lines.append("实际网关未就绪，无法执行完整网关HTTPS自检。")
    lines.extend(["", "结果解释：",
                  "1. 两组均失败：优先检查出口、电脑日期时间及证书库；407表示上游认证问题。",
                  "2. 第一组正常、第二组失败：问题集中在本程序转发链路。",
                  "3. 两组均正常但手机仍报警：请提供手机提示截图、网址、手机日期时间；检查手机代理地址是否与当前电脑显示一致。",
                  "4. 此检测使用Requests证书库和电脑回环连接，不代表手机证书信任、Wi-Fi链路或实际手机访问已通过。",
                  "5. 地址栏HTTP页面的“不安全”和阻止访问的证书错误需要分别判断。",
                  "请勿通过关闭证书验证或安装未知证书来消除报警。"])
    return "\n".join(lines)


class PersistentGateway:
    """One listener, immutable per-connection routes, no direct fallback."""
    def __init__(self, events, host="0.0.0.0", port=GATEWAY_PORT, resolver=None):
        self.events, self.host, self.port = events, host, port
        self.resolver = resolver
        self.lock = threading.Lock()
        self.routes = {}
        self.diagnostics = {}
        self.auto_port = False
        self.port_history = []
        self.thread = None
        self.loop = None
        self.server = None
        self.tasks = set()
        self.task_ips = {}
        self.stop_requested = threading.Event()
        self.ready = threading.Event()
        self.error = ""

    def update(self, exits, devices):
        by_id = {p["id"]: dict(p) for p in exits}
        routes = {ip: by_id[d["exit_id"]] for ip, d in devices.items()
                  if d.get("exit_id") in by_id}
        with self.lock:
            self.routes = routes

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_requested.clear()
        self.thread = threading.Thread(target=self._thread_main, daemon=True)
        self.thread.start()

    def revoke_ip(self, ip):
        """Immediately remove one phone route and cancel its existing proxy tunnels."""
        ip=str(ip or '')
        if not ip:
            return
        with self.lock:
            self.routes.pop(ip, None)
        loop=self.loop
        if loop is None or loop.is_closed():
            return
        def cancel_ip_tasks():
            for task, task_ip in list(self.task_ips.items()):
                if task_ip == ip and task is not asyncio.current_task():
                    task.cancel()
        try:
            loop.call_soon_threadsafe(cancel_ip_tasks)
        except RuntimeError:
            pass

    def stop(self, timeout=6):
        """Do not report stopped until the listener and client sockets are gone."""
        self.stop_requested.set()
        loop = self.loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(lambda: None)
            except RuntimeError:
                pass
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                raise RuntimeError("代理连接仍在清理，请稍后重试")

    def _thread_main(self):
        try:
            asyncio.run(self._serve())
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.events.put(("listener_failed", self, self.error))
        finally:
            self.ready.clear()
            self.events.put(("listener_stopped", self))

    async def _serve(self):
        self.loop = asyncio.get_running_loop()
        if self.auto_port:
            # Bind and retain the actual socket: there is no probe/close/rebind race.
            listener, skipped = reserve_gateway_socket(self.host, self.port)
            self.port_history = skipped
            try:
                self.server = await asyncio.start_server(self._accept, sock=listener,
                                                         limit=65536)
            except BaseException:
                listener.close()
                raise
        else:
            self.server = await asyncio.start_server(self._accept, self.host, self.port,
                                                      limit=65536)
        self.port = self.server.sockets[0].getsockname()[1]
        if self.auto_port:
            global GATEWAY_PORT
            GATEWAY_PORT = self.port
        self.ready.set()
        self.events.put(("listener_ready", self))
        try:
            while not self.stop_requested.is_set():
                await asyncio.sleep(0.1)
        finally:
            self.ready.clear()
            self.server.close()
            # Python 3.12 wait_closed can wait for accepted transports too.
            # Cancel handlers FIRST so their finally blocks close client sockets.
            pending = list(self.tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await self.server.wait_closed()

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        if len(self.tasks) >= MAX_GATEWAY_CONNECTIONS:
            writer.close()
            return
        self.tasks.add(task)
        upstream = None
        replied = False
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else ""
        self.task_ips[task] = ip
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 20)
            diagnostic_token = ""
            if ip == "127.0.0.1":
                for header in raw.split(b"\r\n")[1:]:
                    key, _, value = header.partition(b":")
                    if key.lower() == b"proxy-authorization":
                        scheme, _, encoded = value.strip().partition(b" ")
                        if scheme.lower() == b"basic":
                            try:
                                user, _, secret = base64.b64decode(encoded, validate=True).decode().partition(":")
                                if user == "qizi-diagnostic":
                                    diagnostic_token = secret
                            except (ValueError, UnicodeError):
                                pass
            with self.lock:
                proxy = (self.diagnostics.get(diagnostic_token) if diagnostic_token else None)
                if proxy is None:
                    proxy = self.routes.get(ip)

            # V9: a phone can receive a new DHCP address after Wi-Fi reconnect.
            # Resolve that IP back to the device MAC on the very first request,
            # then cache the route without restarting the 8899 listener.
            if proxy is None and self.resolver is not None:
                try:
                    proxy = await asyncio.wait_for(
                        asyncio.to_thread(self.resolver, ip),
                        timeout=2.5,
                    )
                except Exception:
                    proxy = None
                if proxy is not None:
                    proxy = dict(proxy)
                    with self.lock:
                        self.routes[ip] = proxy
                    self.events.put(("traffic", ip, proxy.get("id", ""), "新IP已自动学习"))

            if proxy is None:
                await self._error(writer, 403, "Device not bound")
                self.events.put(("traffic", ip, "", "拒绝：无法识别设备或设备未绑定出口"))
                return
            lines = raw[:-4].split(b"\r\n")
            method, target, version = lines[0].decode("ascii").split()
            if version not in ("HTTP/1.0", "HTTP/1.1"):
                raise ValueError("unsupported HTTP version")
            headers = []
            for line in lines[1:]:
                key, sep, value = line.partition(b":")
                if not sep or key.strip() != key:
                    raise ValueError("invalid header")
                if key.lower() not in (b"proxy-authorization", b"proxy-connection",
                                       b"connection"):
                    headers.append(line)
            if method == "CONNECT":
                from urllib.parse import urlsplit
                parsed = urlsplit("//" + target)
                if not parsed.hostname or not parsed.port or parsed.username:
                    raise ValueError("invalid CONNECT target")
                outgoing = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n".encode("ascii")
            else:
                from urllib.parse import urlsplit
                parsed = urlsplit(target)
                if parsed.scheme != "http" or not parsed.hostname or parsed.username:
                    raise ValueError("use CONNECT for HTTPS")
                outgoing = lines[0] + b"\r\n" + b"\r\n".join(headers) + b"\r\nConnection: close\r\n"
            if proxy.get("username"):
                credentials = (proxy["username"] + ":" + proxy.get("password", "")).encode("utf-8")
                outgoing += b"Proxy-Authorization: Basic " + base64.b64encode(credentials) + b"\r\n"
            outgoing += b"\r\n"
            up_reader, upstream = await asyncio.wait_for(
                asyncio.open_connection(proxy["host"], int(proxy["port"]), limit=65536), 10)
            for stream in (writer, upstream):
                transport_socket = stream.get_extra_info("socket")
                if transport_socket is not None:
                    try:
                        transport_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    except OSError:
                        pass
            upstream.write(outgoing)
            await upstream.drain()
            if method == "CONNECT":
                response = await asyncio.wait_for(up_reader.readuntil(b"\r\n\r\n"), 15)
                status = response.split(b"\r\n", 1)[0].split()
                if (len(status) < 2 or status[0] not in (b"HTTP/1.0", b"HTTP/1.1")
                        or len(status[1]) != 3 or not status[1].isdigit()
                        or not 200 <= int(status[1]) < 300):
                    code = status[1].decode("ascii", "replace") if len(status) > 1 else "?"
                    await self._error(writer, 502, "Upstream CONNECT failed")
                    replied = True
                    self.events.put(("traffic", ip, proxy["id"],
                                        "上游拒绝 CONNECT，状态 " + code))
                    return
                # Once tunnel success is queued, never inject plaintext HTTP errors.
                replied = True
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            replied = True
            self.events.put(("traffic", ip, proxy["id"], "已转发"))
            last_activity = [time.monotonic()]
            async def pump(source, destination):
                while True:
                    try:
                        data = await asyncio.wait_for(source.read(65536), 60)
                    except asyncio.TimeoutError:
                        if time.monotonic() - last_activity[0] >= 300:
                            raise
                        continue
                    if data:
                        last_activity[0] = time.monotonic()
                    if not data:
                        if destination.can_write_eof():
                            destination.write_eof()
                            await destination.drain()
                        return
                    destination.write(data)
                    await destination.drain()
            sending = asyncio.create_task(pump(reader, upstream))
            receiving = asyncio.create_task(pump(up_reader, writer))
            try:
                # Upstream EOF ends the response; client half-close must not discard it.
                done, _ = await asyncio.wait((sending, receiving),
                                             return_when=asyncio.FIRST_COMPLETED)
                for finished in done:
                    finished.result()
                if receiving not in done:
                    await receiving
            finally:
                sending.cancel()
                receiving.cancel()
                await asyncio.gather(sending, receiving, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not replied:
                try:
                    await self._error(writer, 502, "Proxy request failed")
                except Exception:
                    pass
            self.events.put(("traffic", ip, "", "转发失败：" + type(exc).__name__ + (f" (系统码 {exc.errno})" if isinstance(exc, OSError) and exc.errno else "")))
        finally:
            for stream in (upstream, writer):
                if stream:
                    stream.close()
                    try:
                        await asyncio.wait_for(stream.wait_closed(), 2)
                    except Exception:
                        pass
            self.tasks.discard(task)
            self.task_ips.pop(task, None)

    @staticmethod
    async def _error(writer, status, message):
        body = (message + "\n").encode("ascii")
        writer.write((f"HTTP/1.1 {status} {message}\r\nConnection: close\r\n"
                      f"Content-Length: {len(body)}\r\n\r\n").encode("ascii") + body)
        await writer.drain()


def current_hotspot_interfaces():
    """Use active Wi-Fi Direct adapter identity, not SSID or guessed VPN interfaces."""
    script = r"""
$ErrorActionPreference="Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$adapters = @(Get-NetAdapter -IncludeHidden | Where-Object {
    $_.Status -eq "Up" -and $_.InterfaceDescription -like "*Wi-Fi Direct*"
})
$rows = @(foreach ($adapter in $adapters) {
    Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike "169.254.*" } |
    Select-Object InterfaceIndex,InterfaceAlias,IPAddress,PrefixLength
})
ConvertTo-Json -InputObject $rows -Compress
"""
    result = []
    for entry in _json_list(powershell(script)):
        address = ipaddress.IPv4Address(entry["IPAddress"])
        result.append({"index": int(entry["InterfaceIndex"]),
                       "alias": entry["InterfaceAlias"], "ip": str(address),
                       "prefix": int(entry["PrefixLength"]),
                       "network": ipaddress.ip_network(
                           f"{address}/{entry['PrefixLength']}", strict=False)})
    return result


# Retain V6's configuration files and device discovery/migration helpers.
choose_hotspot_interfaces = current_hotspot_interfaces



class StableProxyManager(ProxyManagerApp):
    """
    V8:
    - One sing-box HTTP gateway bound to 0.0.0.0:8899.
    - Multiple phone source IPs can use different HTTP outbounds simultaneously.
    - Device binding changes automatically rebuild/restart sing-box.
    - Every exit is checked in background every 5 seconds.
    """

    def __init__(self):
        self._binding_selection = None
        self.service_enabled = True
        self.test_jobs = {}
        # V2.5.10: per-exit consecutive health-check failure streak.
        # One successful probe resets the streak immediately; only 3 consecutive
        # failures are allowed to turn an exit red/offline.
        self._exit_fail_streak = {}
        self.test_pool = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="exit-health"
        )
        self.firewall_pending = False
        self._gateway_reload_job = None
        self._gateway_restart_lock = threading.Lock()
        self._last_health_sweep = 0.0
        self._last_gateway_start = 0.0
        self._gateway_log_thread = None
        self._gateway_generation = 0
        super().__init__()

        self.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei')
        self.after(250, self.start_selected)
        self.after(500, self.test_all)

    def build_ui(self):
        super().build_ui()

        # 出口在线/断线颜色
        self.exit_table.tag_configure(
            "exit_online",
            foreground="#00A000"
        )

        self.exit_table.tag_configure(
            "exit_offline",
            foreground="#E00000"
        )

        self.start_btn.configure(
            text="重启代理网关",
            command=lambda: self.request_gateway_reconcile("手动重启", force=True)
        )
        self.stop_btn.configure(text="停止代理网关")
        self.gateway_btn.configure(text="检查防火墙", command=self.check_firewall)

        def relabel(widget):
            if isinstance(widget, ttk.Label):
                value = str(widget.cget("text"))
                if value.startswith("网络出口管理器"):
                    widget.configure(text="出口与热点设备管理器 V8")
                elif value.startswith("自动模式："):
                    widget.configure(text="保存绑定后自动生效｜出口每5秒自检")
                elif value.startswith("所有手机使用"):
                    widget.configure(text=f"手机统一使用程序显示的代理IP + {GATEWAY_PORT}")
            for child in widget.winfo_children():
                relabel(child)

        relabel(self)
        self.device_table.heading("online", text="设备状态")
        self.device_table.heading("exit", text="指定出口")

        self.gateway_info.set(
            f"正在识别 Windows 移动热点地址；代理端口固定为 {GATEWAY_PORT}"
        )

    # ---------------- Exit health monitor ----------------

    def start_direct_test(self, idx):
        if not 0 <= idx < len(self.exits):
            return

        proxy = dict(self.exits[idx])
        eid = proxy.get("id", "")
        fingerprint = exit_fingerprint(proxy)

        # Do not start duplicate probes for the same unchanged exit.
        if self.test_jobs.get(eid) == fingerprint:
            return

        self.test_jobs[eid] = fingerprint
        self.exits[idx]["status"] = "检测中…"
        self.refresh_exits(idx)

        self.test_pool.submit(
            self.direct_test_worker,
            eid,
            proxy,
            fingerprint
        )

    def direct_test_worker(self, eid, proxy, fingerprint):
        started = time.monotonic()
        try:
            ip, country = test_http_exit(proxy)
            elapsed = time.monotonic() - started
            self.events.put((
                "exit_health",
                eid,
                fingerprint,
                True,
                ip,
                country,
                "",
                elapsed,
                time.strftime("%H:%M:%S"),
            ))
        except Exception as exc:
            elapsed = time.monotonic() - started
            self.events.put((
                "exit_health",
                eid,
                fingerprint,
                False,
                "",
                "",
                str(exc),
                elapsed,
                time.strftime("%H:%M:%S"),
            ))

    def test_all(self):
        for idx in range(len(self.exits)):
            self.start_direct_test(idx)

    # ---------------- Device/output binding ----------------

    def refresh_exits(self, selected=None):
        if selected is None:
            selected = self.selected_exit_index()

        # Call base renderer.
        ProxyManagerApp.refresh_exits(self, selected)

        self.combo_ids = [""] + [p.get("id", "") for p in self.exits]
        self.exit_combo["values"] = ["（未绑定）"] + [
            f"{p.get('name', '')} [{p.get('id', '')[:8]}]"
            for p in self.exits
        ]

        self._binding_selection = None
        self.load_selected_device_binding()

    def load_selected_device_binding(self):
        if not hasattr(self, "combo_ids"):
            return
        ip = self.selected_device_ip()
        if not ip:
            self.exit_combo.current(0)
            return
        if ip == self._binding_selection:
            return

        self._binding_selection = ip
        eid = self.devices.get(ip, {}).get("exit_id", "")
        if eid in self.combo_ids:
            self.exit_combo.current(self.combo_ids.index(eid))
        else:
            self.exit_combo.current(0)

    def bind_selected_device(self):
        ip = self.selected_device_ip()
        index = self.exit_combo.current()

        if not ip or index <= 0:
            messagebox.showinfo("提示", "请选择设备和出口。")
            return

        self.devices[ip]["exit_id"] = self.combo_ids[index]
        save_devices(self.devices)

        self.refresh_devices(ip)
        self.detail.set(
            f"{ip} 的出口绑定已保存，正在自动更新代理网关……"
        )
        self.request_gateway_reconcile("设备绑定")

    def unbind_selected_device(self):
        ip = self.selected_device_ip()
        if not ip:
            messagebox.showinfo("提示", "请先选择设备。")
            return

        self.devices[ip]["exit_id"] = ""
        save_devices(self.devices)
        self.refresh_devices(ip)
        self.request_gateway_reconcile("取消绑定")

    def refresh_devices(self, selected_ip=None):
        ProxyManagerApp.refresh_devices(self, selected_ip)

    # ---------------- Hotspot discovery ----------------

    def scan_worker(self):
        try:
            interfaces = current_hotspot_interfaces()
            found, info = scan_hotspot_devices(interfaces)
            self.events.put(("scan_ready", found, info, interfaces))
        except Exception as exc:
            self.events.put((
                "devices_scan_error",
                f"{type(exc).__name__}: {exc}"
            ))

    # ---------------- Firewall ----------------

    def check_firewall(self):
        if self.firewall_pending:
            return

        self.firewall_pending = True

        def worker():
            try:
                if is_admin():
                    ensure_gateway_firewall()
                    message = f"Windows 防火墙 TCP/{GATEWAY_PORT} 已放行（全部网络配置文件）"
                elif gateway_firewall_ready():
                    message = f"已发现 {GATEWAY_PORT} 防火墙规则"
                else:
                    message = f"{GATEWAY_PORT} 尚未放行：请右键以管理员身份运行本程序一次"
                self.events.put(("firewall_done", message))
            except Exception as exc:
                self.events.put((
                    "firewall_done",
                    "防火墙检查失败：" + str(exc)
                ))

        threading.Thread(target=worker, daemon=True).start()

    # ---------------- sing-box gateway ----------------

    def gateway_bindings(self):
        by_id = {p.get("id", ""): p for p in self.exits}
        result = []

        for ip, device in self.devices.items():
            eid = device.get("exit_id", "")
            proxy = by_id.get(eid)
            if not proxy:
                continue

            try:
                address = ipaddress.ip_address(ip)
                if address.version != 4:
                    continue
            except Exception:
                continue

            result.append((ip, eid, proxy))

        return result

    def build_gateway_config(self):
        bindings = self.gateway_bindings()
        if not bindings:
            return None, []

        used_ids = {eid for _ip, eid, _proxy in bindings}

        outbounds = [{"type": "direct", "tag": "direct-out"}]

        for proxy in self.exits:
            eid = proxy.get("id", "")
            if eid not in used_ids:
                continue

            outbound = {
                "type": "http",
                "tag": exit_tag(eid),
                "server": proxy["host"],
                "server_port": int(proxy["port"]),
            }

            if proxy.get("username"):
                outbound["username"] = proxy.get("username", "")
                outbound["password"] = proxy.get("password", "")

            outbounds.append(outbound)

        rules = []
        for ip, eid, _proxy in bindings:
            rules.append({
                "inbound": ["hotspot-http"],
                "source_ip_cidr": [f"{ip}/32"],
                "action": "route",
                "outbound": exit_tag(eid),
            })

        # If a phone changes IP and the binding did not migrate yet,
        # do not silently leak that proxied request to the local direct route.
        rules.append({
            "inbound": ["hotspot-http"],
            "action": "reject",
        })

        config = {
            "log": {
                "level": "info",
                "timestamp": True,
            },
            "inbounds": [{
                "type": "http",
                "tag": "hotspot-http",
                # Listen on every Windows interface.
                # Hotspot rename/recreation no longer invalidates the listener.
                "listen": "0.0.0.0",
                "listen_port": GATEWAY_PORT,
            }],
            "outbounds": outbounds,
            "route": {
                "rules": rules,
                "final": "direct-out",
                "auto_detect_interface": True,
            },
        }

        return config, bindings

    def running(self):
        return self.process is not None and self.process.poll() is None

    def start_selected(self):
        self.service_enabled = True
        self.request_gateway_reconcile("程序启动", force=True)
        self.check_firewall()

    def start_hotspot_gateway(self, auto=False):
        self.request_gateway_reconcile("启动", force=True)

    def request_gateway_reconcile(self, reason="", force=False):
        if self._closing:
            return

        if self._gateway_reload_job is not None:
            try:
                self.after_cancel(self._gateway_reload_job)
            except Exception:
                pass

        delay = 50 if force else 350
        self._gateway_reload_job = self.after(
            delay,
            lambda: self.reconcile_gateway(reason, force)
        )

    def reconcile_gateway(self, reason="", force=False):
        self._gateway_reload_job = None
        if self._closing or not self.service_enabled:
            return

        config, bindings = self.build_gateway_config()

        if not bindings:
            if self.running():
                self.kill_process()
            self.status.set("状态：等待设备绑定")
            self.detail.set(
                "代理网关等待至少一台设备绑定出口。"
            )
            return

        # Serialize restarts so rapid binding changes collapse into one restart.
        generation = self._gateway_generation + 1
        self._gateway_generation = generation

        self.status.set("状态：正在更新代理网关…")

        threading.Thread(
            target=self._gateway_restart_worker,
            args=(generation, config, bindings, reason),
            daemon=True
        ).start()

    def _gateway_restart_worker(self, generation, config, bindings, reason):
        with self._gateway_restart_lock:
            if self._closing or generation != self._gateway_generation:
                return

            flags = (
                subprocess.CREATE_NO_WINDOW
                if hasattr(subprocess, "CREATE_NO_WINDOW")
                else 0
            )

            try:
                GATEWAY_CONFIG_FILE.write_text(
                    json.dumps(config, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )

                checked = subprocess.run(
                    [str(SING_BOX), "check", "-c", str(GATEWAY_CONFIG_FILE)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    creationflags=flags,
                    timeout=15,
                )

                if checked.returncode:
                    raise RuntimeError(
                        (checked.stderr or checked.stdout or "sing-box 配置错误").strip()
                    )

                old_process = self.process

                if old_process is not None and old_process.poll() is None:
                    old_process.terminate()
                    try:
                        old_process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        old_process.kill()
                        old_process.wait(timeout=2)

                if self._closing or generation != self._gateway_generation:
                    return

                GATEWAY_LOG_FILE.write_text("", encoding="utf-8")

                process = subprocess.Popen(
                    [str(SING_BOX), "run", "-c", str(GATEWAY_CONFIG_FILE)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    bufsize=1,
                    creationflags=flags,
                )

                self.process = process
                self.active_mode = "gateway"
                self.active_name = None
                self._last_gateway_start = time.monotonic()

                threading.Thread(
                    target=self._read_gateway_output,
                    args=(process,),
                    daemon=True
                ).start()

                # Verify 8899 is actually listening.
                ready = False
                for _ in range(30):
                    if process.poll() is not None:
                        raise RuntimeError(
                            "sing-box 启动后立即退出：" +
                            (self.last_gateway_log or "无日志")
                        )
                    try:
                        with socket.create_connection(
                            ("127.0.0.1", GATEWAY_PORT),
                            timeout=0.2
                        ):
                            ready = True
                            break
                    except OSError:
                        time.sleep(0.15)

                if not ready:
                    raise RuntimeError(f"本机 TCP/{GATEWAY_PORT} 未开始监听")

                summary = "；".join(
                    f"{ip}→{proxy.get('name', '')}"
                    for ip, _eid, proxy in bindings
                )

                self.events.put((
                    "gateway_ready_v8",
                    process,
                    summary,
                    reason,
                ))

            except Exception as exc:
                self.events.put((
                    "gateway_failed_v8",
                    str(exc),
                ))

    def _read_gateway_output(self, process):
        try:
            with GATEWAY_LOG_FILE.open("a", encoding="utf-8") as log:
                if process.stdout is None:
                    return

                for raw in process.stdout:
                    line = raw.rstrip()
                    if not line:
                        continue

                    self.last_gateway_log = line
                    log.write(line + "\n")
                    log.flush()

                    lower = line.lower()
                    if "error" in lower or "fatal" in lower:
                        self.events.put(("gateway_log_v8", line))

        except Exception:
            pass

    def kill_process(self):
        process = self.process
        self.process = None
        self.active_mode = None
        self.active_name = None

        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except Exception:
                    pass

    def stop(self):
        self.service_enabled = False
        self._gateway_generation += 1
        self.kill_process()
        self.status.set("状态：代理网关已停止")
        self.detail.set(
            f"已停止 {GATEWAY_PORT} 代理服务；手机仍设置代理时将无法通过该代理访问网络。"
        )
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    # ---------------- Event loop ----------------

    def handle_events(self):
        for _ in range(200):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break

            kind = event[0]

            if kind == "exit_health":
                (
                    _,
                    eid,
                    fingerprint,
                    ok,
                    public_ip,
                    country,
                    error,
                    elapsed,
                    checked_at,
                ) = event

                if self.test_jobs.get(eid) != fingerprint:
                    continue

                self.test_jobs.pop(eid, None)

                proxy = next(
                    (p for p in self.exits if p.get("id") == eid),
                    None
                )

                if proxy is None or exit_fingerprint(proxy) != fingerprint:
                    continue

                if ok:
                    proxy["status"] = "在线"
                    proxy["public_ip"] = public_ip
                    if country and country != "未知":
                        proxy["country"] = country
                    proxy["last_error"] = ""
                else:
                    proxy["status"] = "断线"
                    proxy["last_error"] = error

                proxy["last_check"] = checked_at
                proxy["last_latency_ms"] = int(elapsed * 1000)

                save_json(EXITS_FILE, self.exits)
                self.refresh_exits()

            elif kind == "scan_ready":
                _, found, info, interfaces = event
                self.scanning = False

                changed = self.migrate_device_bindings_by_mac(found)

                self.hotspot_info.set("热点网卡：" + info)

                addresses = " 或 ".join(
                    item["ip"] for item in interfaces
                )

                if addresses:
                    self.gateway_info.set(
                        f"手机 Wi-Fi 手动代理：服务器 {addresses} ｜端口 {GATEWAY_PORT}"
                    )
                else:
                    self.gateway_info.set(
                        "未检测到 Windows 移动热点地址"
                    )

                self.refresh_devices()

                if changed:
                    self.detail.set(
                        "检测到设备 IP 变化，已按 MAC 迁移绑定并自动更新代理网关。"
                    )
                    self.request_gateway_reconcile("设备IP变化")
                elif self.service_enabled and not self.running() and self.gateway_bindings():
                    self.request_gateway_reconcile("自动恢复")

            elif kind == "devices_scan_error":
                self.scanning = False
                self.hotspot_info.set("热点扫描失败：" + event[1])

            elif kind == "firewall_done":
                self.firewall_pending = False
                self.detail.set(event[1])

            elif kind == "gateway_ready_v8":
                _, process, summary, reason = event

                if process is not self.process:
                    continue

                self.status.set(
                    f"状态：代理网关运行中｜{GATEWAY_PORT}｜{len(self.gateway_bindings())} 台设备"
                )
                self.detail.set(
                    f"代理配置已生效：{summary}"
                )
                self.start_btn.configure(state="normal")
                self.stop_btn.configure(state="normal")

            elif kind == "gateway_failed_v8":
                self.status.set("状态：代理网关启动失败")
                self.detail.set(event[1])
                self.start_btn.configure(state="normal")
                self.stop_btn.configure(state="disabled")

            elif kind == "gateway_log_v8":
                line = event[1]
                self.detail.set(line[-260:])

            # Ignore legacy event types from unused code paths.

        now = time.monotonic()

        # Every 5 seconds start one independent worker per exit.
        # Existing unfinished probe for that exact exit is not duplicated.
        if now - self._last_health_sweep >= EXIT_HEALTH_SECONDS:
            self._last_health_sweep = now
            self.test_all()

        # Device discovery every 5 seconds.
        if (
            self.auto_scan_var.get()
            and not self.scanning
            and now - self._last_auto_scan >= AUTO_SCAN_SECONDS
        ):
            self._last_auto_scan = now
            self.scan_devices()

        # Auto-recover the sing-box gateway if it dies unexpectedly.
        if (
            self.service_enabled
            and self.gateway_bindings()
            and not self.running()
            and now - self._last_gateway_start > 2.0
        ):
            self.request_gateway_reconcile("进程自动恢复")

        if not self._closing:
            self.after(100, self.handle_events)

    def close(self):
        self._closing = True
        self.service_enabled = False
        self._gateway_generation += 1

        if self._gateway_reload_job is not None:
            try:
                self.after_cancel(self._gateway_reload_job)
            except Exception:
                pass

        save_devices(self.devices)
        self.kill_process()
        self.test_pool.shutdown(
            wait=False,
            cancel_futures=True
        )
        self.destroy()



class V10ProxyManager(StableProxyManager):
    """
    V10 persistent routing model:
    - TCP/8899 is owned by one in-process PersistentGateway and stays up.
    - Device identity is MAC-first; IPv4 is treated as a temporary DHCP address.
    - A previously unknown source IP is resolved to MAC on its first proxy request.
    - Binding/exit changes update the live route table; no sing-box restart is required.
    """

    def __init__(self):
        self.device_lock = threading.RLock()
        self._hotspot_interfaces_cache = []
        self._last_traffic_text = ""
        self._selected_device_mac = ""
        self.persistent_gateway = None
        super().__init__()

        # StableProxyManager schedules startup after its constructor returns, so the
        # persistent listener is ready before that scheduled callback runs.
        self.persistent_gateway = PersistentGateway(
            self.events,
            host="0.0.0.0",
            port=GATEWAY_PORT,
            resolver=self.resolve_proxy_for_ip,
        )
        self.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei')
        self._exit_blink_visible = True
        self.after(550, self._blink_exit_status)

    def build_ui(self):
        super().build_ui()
        self.start_btn.configure(
            text="启动/刷新代理服务",
            command=self.start_selected,
        )
        self.stop_btn.configure(text="停止代理服务")
        self.gateway_btn.configure(text="检查防火墙", command=self.check_firewall)
        ttk.Button(self.gateway_btn.master, text="浏览器安全诊断",
                   command=self.browser_security_diagnostic).pack(side="left", padx=5)

        def relabel_v9(widget):
            if isinstance(widget, ttk.Label):
                value = str(widget.cget("text"))
                if value.startswith("出口管理器"):
                    widget.configure(text="出口管理器 V11")
                elif value.startswith("保存绑定后自动生效"):
                    widget.configure(text=f"MAC识别设备｜IP变化即时学习｜{GATEWAY_PORT}不重启")
                elif value.startswith("手机统一使用程序显示"):
                    widget.configure(text=f"手机只需设置一次代理地址 + {GATEWAY_PORT}")
            for child in widget.winfo_children():
                relabel_v9(child)

        relabel_v9(self)
        self.gateway_info.set(
            f"正在识别 Windows 移动热点地址；{GATEWAY_PORT} 为常驻代理服务"
        )

    def browser_security_diagnostic(self):
        idx = self.selected_exit_index()
        if idx is None or not 0 <= idx < len(self.exits):
            messagebox.showinfo("浏览器安全诊断", "请先在上方选择手机绑定的出口。")
            return
        existing = getattr(self, "_security_window", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        proxy = dict(self.exits[idx])
        window = tk.Toplevel(self)
        self._security_window = window
        window.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei')
        window.geometry("820x580")
        output = tk.Text(window, wrap="word", padx=12, pady=12)
        output.pack(fill="both", expand=True)
        output.insert("end", "正在检测，请稍候；每个网络步骤都有超时。\n")
        output.configure(state="disabled")
        def copy_report():
            self.clipboard_clear()
            self.clipboard_append(output.get("1.0", "end-1c"))
        ttk.Button(window, text="复制诊断结果", command=copy_report).pack(pady=8)
        results = queue.Queue()
        gateway = self.persistent_gateway
        listener_ready = bool(gateway and gateway.ready.is_set())
        def worker():
            try:
                results.put(browser_security_report(proxy, listener_ready, gateway))
            except Exception as exc:
                results.put("诊断未完成：" + type(exc).__name__)
        threading.Thread(target=worker, daemon=True).start()
        def poll():
            if self._closing or not window.winfo_exists():
                return
            try:
                report = results.get_nowait()
            except queue.Empty:
                self.after(200, poll)
                return
            output.configure(state="normal")
            output.delete("1.0", "end")
            output.insert("end", report)
            output.configure(state="disabled")
        self.after(200, poll)

    # ---------------- V11 exit status lamp ----------------

    def start_direct_test(self, idx):
        """Probe silently: keep the previous result visible until the new result arrives."""
        if not 0 <= idx < len(self.exits):
            return
        proxy = dict(self.exits[idx])
        eid = proxy.get("id", "")
        fingerprint = exit_fingerprint(proxy)
        if self.test_jobs.get(eid) == fingerprint:
            return
        self.test_jobs[eid] = fingerprint
        # Deliberately do NOT display "检测中". The row changes only when a probe finishes.
        self.test_pool.submit(self.direct_test_worker, eid, proxy, fingerprint)

    def _exit_is_online(self, proxy):
        status = str(proxy.get("status", ""))
        return status.startswith("在线") or status.startswith("连接成功")


    def _exit_status_text(self, proxy):
        online = self._exit_is_online(proxy)
        visible = getattr(self, "_exit_blink_visible", True)

        if visible:
            return "● 在线" if online else "● 断线"

        return "  在线" if online else "  断线"


    def _blink_exit_status(self):
        if self._closing:
            return

        self._exit_blink_visible = not getattr(
            self, "_exit_blink_visible", True
        )

        if hasattr(self, "exit_table"):
            for i, proxy in enumerate(self.exits):
                iid = str(i)

                if not self.exit_table.exists(iid):
                    continue

                values = list(
                    self.exit_table.item(iid, "values")
                )

                if len(values) >= 6:
                    values[5] = self._exit_status_text(proxy)

                    online = self._exit_is_online(proxy)

                    tag_name = (
                        "exit_online"
                        if online
                        else "exit_offline"
                    )

                    self.exit_table.item(
                        iid,
                        values=values,
                        tags=(tag_name,)
                    )

        self.after(550, self._blink_exit_status)

    # ---------------- Stable/non-destructive UI refresh ----------------

    def refresh_exits(self, selected=None):
        """Refresh exit row values in-place without stealing selection/focus."""
        if not hasattr(self, "exit_table"):
            return

        current_selection = self.exit_table.selection()
        current_focus = self.exit_table.focus()
        current_combo = self.exit_combo.current() if hasattr(self, "exit_combo") else -1
        current_combo_text = self.exit_combo.get() if hasattr(self, "exit_combo") else ""

        wanted = {str(i) for i in range(len(self.exits))}
        existing = set(self.exit_table.get_children())
        for iid in existing - wanted:
            self.exit_table.delete(iid)

        for i, proxy in enumerate(self.exits):
            iid = str(i)
            values = (
                proxy.get("name", ""), proxy.get("host", ""), proxy.get("port", ""),
                proxy.get("country", ""), proxy.get("public_ip", ""),
                self._exit_status_text(proxy),
            )
            if self.exit_table.exists(iid):
                self.exit_table.item(iid, values=values)
            else:
                self.exit_table.insert("", "end", iid=iid, values=values)

        self.combo_ids = [""] + [p.get("id", "") for p in self.exits]
        combo_values = ["（未绑定）"] + [
            f"{p.get('name', '')} [{p.get('id', '')[:8]}]" for p in self.exits
        ]
        if hasattr(self, "exit_combo"):
            self.exit_combo["values"] = combo_values
            # Never overwrite what the user is currently choosing during an automatic refresh.
            if 0 <= current_combo < len(combo_values):
                self.exit_combo.current(current_combo)
            elif current_combo_text in combo_values:
                self.exit_combo.set(current_combo_text)
            elif not self.exit_combo.get():
                self.exit_combo.current(0)

        valid_selection = [iid for iid in current_selection if self.exit_table.exists(iid)]
        if valid_selection:
            self.exit_table.selection_set(valid_selection)
        elif selected is not None and self.exit_table.exists(str(selected)):
            self.exit_table.selection_set(str(selected))
        if current_focus and self.exit_table.exists(current_focus):
            self.exit_table.focus(current_focus)

    def refresh_devices(self, selected_ip=None):
        """Update only device rows; preserve selected device and combobox choice."""
        if not hasattr(self, "device_table"):
            return

        current_ip = self.selected_device_ip()
        current_combo = self.exit_combo.current() if hasattr(self, "exit_combo") else -1
        current_combo_text = self.exit_combo.get() if hasattr(self, "exit_combo") else ""

        selected_mac = self._selected_device_mac
        if current_ip and current_ip in self.devices:
            selected_mac = normalize_mac(self.devices[current_ip].get("mac", ""))
            if valid_device_mac(selected_mac):
                self._selected_device_mac = selected_mac

        def ip_key(value):
            try:
                return int(ipaddress.ip_address(value))
            except Exception:
                return 0

        wanted = set(self.devices.keys())
        existing = set(self.device_table.get_children())
        for iid in existing - wanted:
            self.device_table.delete(iid)

        for ip in sorted(self.devices.keys(), key=ip_key):
            d = self.devices[ip]
            exit_name = self.exit_name_by_id(d.get("exit_id", "")) or "未绑定"
            state = d.get("display_state") or ("在线" if d.get("online") else "离线")
            values = (
                ip, d.get("mac", ""), d.get("interface", ""), d.get("remark", ""),
                state, exit_name,
            )
            if self.device_table.exists(ip):
                self.device_table.item(ip, values=values)
            else:
                self.device_table.insert("", "end", iid=ip, values=values)

        target = None
        if selected_ip and self.device_table.exists(selected_ip):
            target = selected_ip
        elif current_ip and self.device_table.exists(current_ip):
            target = current_ip
        elif valid_device_mac(selected_mac):
            target = next((ip for ip, d in self.devices.items()
                           if normalize_mac(d.get("mac", "")) == selected_mac), None)

        if target and self.device_table.exists(target):
            self.device_table.selection_set(target)
            self.device_table.focus(target)
            self._binding_selection = target

        # Automatic row refresh must never change a user's current dropdown choice.
        if hasattr(self, "exit_combo"):
            values = list(self.exit_combo["values"])
            if 0 <= current_combo < len(values):
                self.exit_combo.current(current_combo)
            elif current_combo_text in values:
                self.exit_combo.set(current_combo_text)

    def migrate_device_bindings_by_mac(self, found):
        """Use LAN neighbor reachability or the iOS Wi-Fi control channel for presence."""
        state_by_mac = {
            normalize_mac(item.get("mac", "")): str(item.get("state", "") or "")
            for item in found if valid_device_mac(item.get("mac", ""))
        }
        changed = super().migrate_device_bindings_by_mac(found)
        strong_states = {"Reachable", "Permanent", "Delay", "Probe"}

        for ip, device in self.devices.items():
            mac = normalize_mac(device.get("mac", ""))
            neighbour_state = state_by_mac.get(mac, "")
            uid = self.uid_for_ip(ip)
            wifi_connected = bool(
                uid and self.ios_states.get(uid, {}).get("wifi") == "连接中"
            )
            if neighbour_state in strong_states or wifi_connected:
                device["online"] = True
                device["display_state"] = "在线"
            elif neighbour_state == "Stale":
                # STALE only means Windows still has an ARP cache entry; it does not prove
                # the phone is currently connected, so do not falsely label it online.
                device["online"] = False
                device["display_state"] = "待确认"
            else:
                device["online"] = False
                device["display_state"] = "离线"
        return changed

    # ---------------- Live persistent gateway ----------------

    def running(self):
        gateway = self.persistent_gateway
        return bool(
            gateway
            and gateway.thread
            and gateway.thread.is_alive()
            and gateway.ready.is_set()
        )

    def _update_live_routes(self):
        gateway = self.persistent_gateway
        if gateway is not None:
            with self.device_lock:
                gateway.update(self.exits, self.devices)

    def start_selected(self):
        if self._closing:
            return
        self.service_enabled = True
        self._update_live_routes()

        gateway = self.persistent_gateway
        if gateway is None:
            return

        if not gateway.thread or not gateway.thread.is_alive():
            gateway.error = ""
            self._last_gateway_start = time.monotonic()
            gateway.start()
            self.status.set("状态：正在启动常驻代理网关…")
        else:
            self.status.set(
                f"状态：代理网关运行中｜{GATEWAY_PORT}｜{len(self.gateway_bindings())} 台已绑定设备"
            )
            self.detail.set(f"实时路由表已刷新，无需重启 {GATEWAY_PORT}。")

        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="normal")
        self.check_firewall()

    def request_gateway_reconcile(self, reason="", force=False):
        if self._closing:
            return
        self._update_live_routes()
        if self.service_enabled:
            gateway = self.persistent_gateway
            if gateway is not None and (not gateway.thread or not gateway.thread.is_alive()):
                self._last_gateway_start = time.monotonic()
                gateway.start()
                self.status.set("状态：正在恢复常驻代理网关…")
            elif gateway is not None and gateway.ready.is_set():
                self.status.set(
                    f"状态：代理网关运行中｜{GATEWAY_PORT}｜{len(self.gateway_bindings())} 台已绑定设备"
                )
                if reason:
                    self.detail.set(f"{reason}：实时路由已更新，不需要重启代理网关。")

    def reconcile_gateway(self, reason="", force=False):
        self.request_gateway_reconcile(reason, force)

    def start_hotspot_gateway(self, auto=False):
        self.start_selected()

    def kill_process(self):
        # V9 no longer owns a sing-box gateway process. Keep these legacy fields
        # empty because inherited UI/configuration code still references them.
        self.process = None
        self.active_mode = None
        self.active_name = None

    def stop(self):
        self.service_enabled = False
        gateway = self.persistent_gateway
        if gateway is not None:
            gateway.stop()
        self.status.set("状态：代理网关已停止")
        self.detail.set(
            f"已停止 {GATEWAY_PORT} 代理服务；再次启动后会直接恢复现有 MAC/出口绑定。"
        )
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    # ---------------- MAC-first IP learning ----------------

    def _hotspot_snapshot(self):
        cached = list(self._hotspot_interfaces_cache)
        if cached:
            return cached
        try:
            cached = current_hotspot_interfaces()
        except Exception:
            cached = []
        if cached:
            self._hotspot_interfaces_cache = list(cached)
        return cached

    def _neighbor_for_ip(self, ip_text):
        """Return the hotspot neighbour row for one current IPv4, if Windows knows it."""
        try:
            address = ipaddress.ip_address(ip_text)
            if address.version != 4:
                return None
        except Exception:
            return None

        interfaces = self._hotspot_snapshot()
        if not interfaces:
            return None

        allowed_indexes = {item["index"] for item in interfaces}
        if not any(address in item["network"] for item in interfaces):
            return None

        # The accepted TCP connection normally causes Windows to have a fresh
        # neighbour entry already. Retry briefly to cover the ARP/ND timing edge.
        for attempt in range(4):
            try:
                for row in get_neighbors():
                    try:
                        if int(row.get("InterfaceIndex", 0)) not in allowed_indexes:
                            continue
                    except Exception:
                        continue
                    if str(row.get("IPAddress", "")).strip() != ip_text:
                        continue
                    mac = normalize_mac(row.get("LinkLayerAddress", ""))
                    if not valid_device_mac(mac):
                        continue
                    return {
                        "ip": ip_text,
                        "mac": mac,
                        "interface": next(
                            (item.get("alias", "") for item in interfaces
                             if item.get("index") == int(row.get("InterfaceIndex", 0))),
                            "",
                        ),
                        "state": str(row.get("State", "") or ""),
                    }
            except Exception:
                pass
            if attempt < 3:
                time.sleep(0.12)
        return None

    def resolve_proxy_for_ip(self, new_ip):
        """
        Called from the 8899 worker thread for an unknown source IP.
        It does no Tk work. It only resolves IP->MAC->saved device profile and
        returns the already-bound exit so the *current* request can continue.
        """
        row = self._neighbor_for_ip(new_ip)
        if not row:
            return None

        mac = normalize_mac(row.get("mac", ""))
        if not valid_device_mac(mac):
            return None

        with self.device_lock:
            profile_ip = None
            profile = None
            for old_ip, device in self.devices.items():
                if normalize_mac(device.get("mac", "")) != mac:
                    continue
                if not device.get("exit_id"):
                    continue
                profile_ip = old_ip
                profile = dict(device)
                break

            if not profile:
                return None

            eid = profile.get("exit_id", "")
            proxy = next(
                (dict(p) for p in self.exits if p.get("id") == eid),
                None,
            )
            if proxy is None:
                return None

        # UI/device persistence is performed on the Tk thread. The current proxy
        # request does not wait for that bookkeeping; it can route immediately.
        self.events.put((
            "device_ip_learned_v9",
            profile_ip,
            new_ip,
            mac,
            row.get("interface", ""),
            eid,
            profile.get("remark", ""),
            bool(profile.get("manual", False)),
        ))
        return proxy

    def _apply_learned_ip(self, old_ip, new_ip, mac, interface, eid, remark, manual):
        with self.device_lock:
            manual_wifi_off = any(
                d.get('qizi_wifi_manual_off') for candidate, d in self.devices.items()
                if candidate == old_ip or (valid_device_mac(mac) and normalize_mac(d.get('mac','')) == mac)
            )
            existing_new = self.devices.get(new_ip, {})
            if existing_new and normalize_mac(existing_new.get("mac", "")) not in ("", mac):
                # Do not steal an address that currently belongs to another MAC.
                return False

            # Remove stale addresses for the same physical/private MAC. The device
            # profile survives, but IPv4 is now just a replaceable attribute.
            for candidate_ip, device in list(self.devices.items()):
                if candidate_ip == new_ip:
                    continue
                if normalize_mac(device.get("mac", "")) == mac:
                    self.devices.pop(candidate_ip, None)

            device = self.devices.setdefault(new_ip, {})
            device.update({
                "ip": new_ip,
                "mac": mac,
                "interface": interface,
                "remark": remark or device.get("remark", ""),
                "exit_id": eid,
                "manual": bool(manual),
                "qizi_wifi_manual_off": bool(manual_wifi_off),
                "online": True,
                "last_seen": time.time(),
            })
            save_devices(self.devices)

        self._update_live_routes()
        return True

    # ---------------- Discovery ----------------

    def scan_worker(self):
        try:
            interfaces = current_hotspot_interfaces()
            self._hotspot_interfaces_cache = list(interfaces)
            found, info = scan_hotspot_devices(interfaces)
            self.events.put(("scan_ready", found, info, interfaces))
        except Exception as exc:
            self.events.put((
                "devices_scan_error",
                f"{type(exc).__name__}: {exc}"
            ))

    # ---------------- V9 event loop ----------------

    def handle_events(self):
        for _ in range(300):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break

            kind = event[0]

            if kind == "exit_health":
                (
                    _, eid, fingerprint, ok, public_ip, country, error,
                    elapsed, checked_at,
                ) = event
                if self.test_jobs.get(eid) != fingerprint:
                    continue
                self.test_jobs.pop(eid, None)
                proxy = next((p for p in self.exits if p.get("id") == eid), None)
                if proxy is None or exit_fingerprint(proxy) != fingerprint:
                    continue
                if ok:
                    # Any successful check wins immediately: green + reset streak.
                    self._exit_fail_streak[eid] = 0
                    proxy["status"] = "在线"
                    # 动态 API 出口的公网 IP 不得被健康检测直接覆盖。
                    # 统一交给动态模块创建新出口、寻路，抵达后再切换。
                    dynamic_changed = False
                    if (proxy.get('dynamic_api') and proxy.get('public_ip')
                            and str(proxy.get('public_ip')) != str(public_ip)):
                        handler = getattr(self, '_qizi_api_health_ip_changed', None)
                        if callable(handler):
                            dynamic_changed = bool(handler(eid, public_ip, country))
                        else:
                            self.log_operation('动态出口检测到IP变化，但动态迁移处理器缺失；拒绝直接覆盖')
                            dynamic_changed = True
                    elif (proxy.get('public_ip')
                          and str(proxy.get('public_ip')) != str(public_ip)):
                        handler = getattr(self, '_qizi_health_ip_changed', None)
                        if callable(handler):
                            dynamic_changed = bool(handler(eid, public_ip, country))
                    if not dynamic_changed:
                        proxy["public_ip"] = public_ip
                    if not dynamic_changed and country and country != "未知":
                        proxy["country"] = country
                    proxy["last_error"] = ""
                    proxy["health_fail_streak"] = 0
                else:
                    # Debounce transient timeout/network jitter. Only the third
                    # consecutive failure is considered a real offline state.
                    failures = int(self._exit_fail_streak.get(eid, 0) or 0) + 1
                    self._exit_fail_streak[eid] = failures
                    proxy["health_fail_streak"] = failures
                    proxy["last_error"] = error
                    previous_status = str(proxy.get("status", ""))
                    if failures >= 3:
                        proxy["status"] = "断线"
                    elif previous_status.startswith("在线") or previous_status.startswith("连接成功"):
                        # Preserve the prior green state during failure 1/3 and 2/3.
                        proxy["status"] = "在线"
                    elif previous_status.startswith("断线"):
                        # Already confirmed offline from an earlier 3-failure streak.
                        proxy["status"] = "断线"
                    else:
                        # Brand-new/unknown exit: don't paint it red before 3 failures.
                        proxy["status"] = f"待确认({failures}/3)"
                proxy["last_check"] = checked_at
                proxy["last_latency_ms"] = int(elapsed * 1000)
                save_json(EXITS_FILE, self.exits)
                self.refresh_exits()
                self._update_live_routes()

            elif kind == "scan_ready":
                _, found, info, interfaces = event
                self.scanning = False
                self._hotspot_interfaces_cache = list(interfaces)
                with self.device_lock:
                    changed = self.migrate_device_bindings_by_mac(found)
                self.hotspot_info.set("热点网卡：" + info)
                addresses = " 或 ".join(item["ip"] for item in interfaces)
                if addresses:
                    self.gateway_info.set(
                        f"手机 Wi-Fi 手动代理：服务器 {addresses} ｜端口 {GATEWAY_PORT}"
                    )
                else:
                    self.gateway_info.set("未检测到 Windows 移动热点地址")
                self.refresh_devices()
                self._update_live_routes()
                if changed:
                    self.detail.set(f"设备 IP 已变化：已按 MAC 自动迁移，{GATEWAY_PORT} 无需重启。")
                elif self.service_enabled and not self.running():
                    self.start_selected()

            elif kind == "device_ip_learned_v9":
                _, old_ip, new_ip, mac, interface, eid, remark, manual = event
                if self._apply_learned_ip(old_ip, new_ip, mac, interface, eid, remark, manual):
                    self.refresh_devices(new_ip)
                    exit_name = self.exit_name_by_id(eid) or "已绑定出口"
                    self.detail.set(
                        f"已即时学习设备新 IP：{old_ip or '?'} → {new_ip}｜{exit_name}｜无需重启网关"
                    )

            elif kind == "listener_ready":
                _, gateway = event
                if gateway is not self.persistent_gateway:
                    continue
                self.status.set(
                    f"状态：代理网关运行中｜{GATEWAY_PORT}｜{len(self.gateway_bindings())} 台已绑定设备"
                )
                self.detail.set(f"{GATEWAY_PORT} 常驻代理已就绪；手机 IP 改变时会按 MAC 即时学习。")
                if hasattr(self, "on_listener_ready"):
                    self.on_listener_ready(gateway)
                self.start_btn.configure(state="normal")
                self.stop_btn.configure(state="normal")

            elif kind == "listener_failed":
                _, gateway, error = event
                if gateway is not self.persistent_gateway:
                    continue
                self.status.set("状态：代理网关启动失败")
                self.detail.set(error)
                if hasattr(self, "record_diagnostic"):
                    self.record_diagnostic("监听失败", error)
                self.start_btn.configure(state="normal")
                self.stop_btn.configure(state="disabled")

            elif kind == "listener_stopped":
                _, gateway = event
                if gateway is not self.persistent_gateway:
                    continue
                if hasattr(self, '_qizi_on_listener_stopped'):
                    self._qizi_on_listener_stopped()
                if self.service_enabled and not self._closing:
                    # The periodic watchdog retries with a small backoff.
                    self.detail.set(f"{GATEWAY_PORT} 监听已停止，程序将自动恢复。")
                else:
                    self.status.set("状态：代理网关已停止")

            elif kind == "traffic":
                _, ip, eid, message = event
                if hasattr(self, '_lan_gateway_seen_ips') and ip not in ('127.0.0.1','::1'):
                    self._lan_gateway_seen_ips.add(ip)
                if hasattr(self, "record_diagnostic"):
                    self.record_diagnostic(ip, message)
                # Any request that reaches this PC's gateway proves the device is online.
                # A blocked device still remains online; "禁止网络" is a policy state, not offline.
                if ip not in ('127.0.0.1','::1'):
                    device = self.devices.get(ip)
                    if device is not None:
                        mac = normalize_mac(device.get("mac", ""))
                        if valid_device_mac(mac):
                            device["online"] = True
                            device["display_state"] = "在线"
                # Avoid repainting the UI for every packet/connection. Show only
                # learning/failure information; normal forwarding is intentionally quiet.
                if "学习" in message or "拒绝" in message or "失败" in message:
                    text = f"{ip}｜{message}"
                    if text != self._last_traffic_text:
                        self._last_traffic_text = text
                        self.detail.set(text)

            elif kind == "devices_scan_error":
                self.scanning = False
                self.hotspot_info.set("热点扫描失败：" + event[1])

            elif kind == "firewall_done":
                self.firewall_pending = False
                # Do not overwrite a useful IP-learning message unless firewall failed.
                if "失败" in event[1] or "尚未放行" in event[1]:
                    self.detail.set(event[1])

            # Ignore legacy sing-box event types; V9 does not run that gateway.

        now = time.monotonic()

        if now - self._last_health_sweep >= EXIT_HEALTH_SECONDS:
            self._last_health_sweep = now
            self.test_all()

        if (
            self.auto_scan_var.get()
            and not self.scanning
            and now - self._last_auto_scan >= AUTO_SCAN_SECONDS
        ):
            self._last_auto_scan = now
            self.scan_devices()

        if (
            self.service_enabled
            and not self.running()
            and now - self._last_gateway_start > 2.0
        ):
            gateway = self.persistent_gateway
            if gateway is not None and (not gateway.thread or not gateway.thread.is_alive()):
                self.start_selected()

        if not self._closing:
            self.after(100, self.handle_events)

    def close(self):
        self._closing = True
        self.service_enabled = False
        # Cancel every Tk callback before it can restart the gateway or tests.
        for callback in self.tk.call("after", "info"):
            self.after_cancel(callback)
        errors = []
        try:
            save_devices(self.devices)
        except Exception as exc:
            errors.append("保存设备配置失败：" + str(exc))
        try:
            gateway = self.persistent_gateway
            if gateway is not None:
                gateway.stop()
        except Exception as exc:
            errors.append(str(exc))
        finally:
            self.test_pool.shutdown(wait=False, cancel_futures=True)
        if errors:
            messagebox.showwarning("退出清理", "\n".join(errors) +
                                   "\n程序将退出并释放本进程的全部网络连接。")
        self.destroy()




# ---------------- V14 deployment and diagnostics ----------------

def reserve_gateway_socket(host, first_port, last_port=8999):
    """Reserve a real exclusive listener; never terminate a port owner."""
    skipped = []
    for port in range(first_port, last_port + 1):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind((host, port))
            listener.listen(128)
            listener.setblocking(False)
            return listener, skipped
        except OSError as exc:
            listener.close()
            if exc.errno not in (errno.EADDRINUSE, errno.EACCES, 10048, 10013):
                raise
            skipped.append(port)
    raise RuntimeError(f"{first_port}–{last_port} 无可用端口；未终止任何占用进程")


class SingleInstance:
    """Stable machine-wide identity across filenames, Python and packaged builds."""
    def __init__(self):
        self.handle = None

    def acquire(self):
        if os.name != "nt":
            import fcntl
            self.handle = open('/tmp/qizi-exit-manager-v14.lock', 'a')
            try:
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                self.handle.close()
                self.handle = None
                return False
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel.CloseHandle.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        self.handle = self.kernel.CreateMutexW(None, False, "Global\\QiziExitManager.Singleton.v14")
        error = ctypes.get_last_error()
        if error == 183:
            self.close()
            return False
        if not self.handle:
            if error == 5:
                # An instance launched by another account/elevation can deny access.
                return False
            raise ctypes.WinError(error)
        return True

    def close(self):
        if self.handle:
            if os.name == "nt":
                self.kernel.CloseHandle(self.handle)
            else:
                self.handle.close()
            self.handle = None


def configure_runtime():
    global EXITS_FILE, DEVICES_FILE, CONFIG_FILE, GATEWAY_CONFIG_FILE, GATEWAY_LOG_FILE
    global DATA_DIR, RUNTIME_FILE, GATEWAY_PORT, MIGRATION_NOTES
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "QiziExitManager"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MIGRATION_NOTES = []
    for filename, kind in (("proxy_exits.json", list), ("device_bindings.json", dict)):
        source, destination = APP_DIR / filename, DATA_DIR / filename
        if source.is_file() and not destination.exists():
            value = json.loads(source.read_text(encoding="utf-8-sig"))
            if not isinstance(value, kind):
                raise ValueError(f"旧配置格式无效：{source}；没有覆盖或清空")
            shutil.copy2(source, destination)
            MIGRATION_NOTES.append(f"已首次导入：{source}")
    EXITS_FILE = DATA_DIR / "proxy_exits.json"
    DEVICES_FILE = DATA_DIR / "device_bindings.json"
    # Fail visibly instead of silently discarding a damaged user configuration.
    for path, kind in ((EXITS_FILE, list), (DEVICES_FILE, dict)):
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(value, kind):
                raise ValueError(f"配置格式无效：{path}；请恢复备份")
            if kind is list and any(not isinstance(item, dict) or not item.get("host")
                                    or not item.get("port") for item in value):
                raise ValueError(f"出口配置缺少服务器或端口：{path}")
            if kind is dict and any(not isinstance(item, dict) for item in value.values()):
                raise ValueError(f"设备绑定内容无效：{path}")
    CONFIG_FILE = DATA_DIR / "config.json"
    GATEWAY_CONFIG_FILE = DATA_DIR / "hotspot_gateway_config.json"
    GATEWAY_LOG_FILE = DATA_DIR / "gateway_v14.log"
    RUNTIME_FILE = DATA_DIR / "runtime_v14.json"
    # Always reclaim the default port after a clean shutdown. Old fallback
    # ports in runtime_v14.json are informational, not a permanent preference.
    GATEWAY_PORT = 8899



def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def firewall_inspection_script(port):
    app = ps_quote(Path(sys.executable).resolve())
    return r'''
$ErrorActionPreference = 'Stop'
$app = APP_VALUE
$port = PORT_VALUE
function CoversPort($values) {
    foreach ($v in @($values)) {
        if ($v -eq 'Any' -or $v -eq "$port") { return $true }
        if ($v -match '^(\d+)-(\d+)$') {
            if ($port -ge [int]$Matches[1] -and $port -le [int]$Matches[2]) { return $true }
        }
    }
    return $false
}
$rows = @()
foreach ($rule in @(Get-NetFirewallRule -PolicyStore ActiveStore -Enabled True -Direction Inbound)) {
    $af = $rule | Get-NetFirewallApplicationFilter
    if ($af.Program -ine $app -and $af.Program -ne 'Any') { continue }
    $pf = $rule | Get-NetFirewallPortFilter
    if ($pf.Protocol -notin @('TCP','6','Any','256')) { continue }
    if (-not (CoversPort $pf.LocalPort)) { continue }
    $rows += [pscustomobject]@{
        Name=$rule.Name; Action=[string]$rule.Action; Profile=[string]$rule.Profile
        Program=$af.Program; DisplayName=$rule.DisplayName
    }
}
[pscustomobject]@{
    Rules=@($rows)
    Profiles=@(Get-NetConnectionProfile | Select-Object InterfaceAlias,NetworkCategory)
} | ConvertTo-Json -Depth 5 -Compress
'''.replace('APP_VALUE', app).replace('PORT_VALUE', str(int(port)))


def inspect_firewall(port):
    if os.name != "nt":
        return {"Rules": [], "Profiles": [], "unsupported": True}
    return json.loads(powershell(firewall_inspection_script(port), timeout=30))


def firewall_rule_name(port):
    digest = hashlib.sha256(str(Path(sys.executable).resolve()).lower().encode()).hexdigest()[:12]
    return f"QiziV14-{digest}-{port}"


def firewall_repair_script(port):
    # Disable only explicit rules for this executable and relevant TCP port.
    # Broad system/enterprise block rules are reported, never changed.
    inspection = firewall_inspection_script(port)
    inspection = inspection[:inspection.index('[pscustomobject]@{\n    Rules=')]
    return inspection + r'''
$blocked = @($rows | Where-Object {$_.Action -eq 'Block' -and $_.Program -ieq $app})
if ($blocked.Count -gt 0) {
    $backup = Join-Path BACKUP_DIR ('firewall-disabled-' + [guid]::NewGuid().ToString() + '.json')
    ConvertTo-Json -InputObject @($blocked) -Depth 5 | Set-Content -LiteralPath $backup -Encoding UTF8
    foreach ($b in $blocked) { Disable-NetFirewallRule -Name $b.Name -ErrorAction Stop }
}
$name = RULE_NAME
Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -Name $name -DisplayName 'Qizi V14 hotspot proxy' `
    -Program $app -Direction Inbound -Action Allow -Enabled True `
    -Protocol TCP -LocalPort $port -RemoteAddress LocalSubnet -Profile Any | Out-Null
'''.replace('BACKUP_DIR', ps_quote(DATA_DIR)).replace('RULE_NAME', ps_quote(firewall_rule_name(port)))


def run_elevated_powershell(script):
    # UAC processes can escape normal Job inheritance. The elevated supervisor
    # keeps a handle to this exact parent and stops its own repair child on exit.
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    supervisor = r'''$ErrorActionPreference='Stop'
$owner = Get-Process -Id OWNER_PID -ErrorAction Stop
$null = $owner.Handle
if ($owner.StartTime.ToUniversalTime().Ticks -ne OWNER_TICKS) { exit 1 }
$worker = $null
try {
    if ($owner.HasExited) { exit 1 }
    $worker = Start-Process powershell.exe -WindowStyle Hidden -PassThru -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand','SCRIPT_BASE64')
    while (-not $worker.WaitForExit(200)) {
        if ($owner.HasExited) { $worker.Kill(); $worker.WaitForExit(); exit 1 }
    }
    exit $worker.ExitCode
} finally {
    if ($worker -and -not $worker.HasExited) { $worker.Kill(); $worker.WaitForExit() }
}
'''
    ticks = powershell(f"(Get-Process -Id {os.getpid()}).StartTime.ToUniversalTime().Ticks").strip()
    supervisor = supervisor.replace('OWNER_PID', str(os.getpid())).replace(
        'OWNER_TICKS', str(int(ticks))).replace('SCRIPT_BASE64', encoded)
    payload = base64.b64encode(supervisor.encode('utf-16-le')).decode('ascii')
    outer = f'''$ErrorActionPreference='Stop'
$p = Start-Process -FilePath powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand','{payload}')
if ($p.ExitCode -ne 0) {{ throw "防火墙修复退出码 $($p.ExitCode)" }}
'''
    powershell(outer, timeout=180)



class DeploymentProxyManager(V10ProxyManager):
    def __init__(self):
        self.deployment_events = queue.Queue()
        self.diagnostic_busy = False
        self.repair_busy = False
        self.diagnostic_log = deque(maxlen=150)
        self.last_report = "启动自检尚未完成。"
        self.firewall_summary = "待检查"
        self._ready_checked_port = None
        super().__init__()
        self.persistent_gateway.auto_port = True
        self.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei')
        self.after(150, self.poll_deployment)

    def build_ui(self):
        super().build_ui()
        self.gateway_btn.configure(text="修复防火墙", command=self.repair_firewall)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=12, pady=5)
        ttk.Button(bar, text="重新自检", command=self.run_self_check).pack(side="left")
        ttk.Button(bar, text="查看自检报告", command=self.show_report).pack(side="left", padx=5)
        self.deployment_status = tk.StringVar(value="自检：等待网关启动")
        ttk.Label(bar, textvariable=self.deployment_status).pack(side="left", padx=5)
        # 沿用主界面与副本一致的 820x680，不在派生界面中覆盖窗口尺寸。

    def check_firewall(self):
        # The inherited start method calls this before the listener owns a port.
        # Perform repair only from on_listener_ready, using the actual bound port.
        pass

    def on_listener_ready(self, gateway):
        if self._ready_checked_port == gateway.port:
            return
        self._ready_checked_port = gateway.port
        try:
            save_json(RUNTIME_FILE, {"port": gateway.port})
        except OSError:
            self.record_diagnostic("配置", "当前端口无法持久保存；本次监听仍然有效")
        self.gateway_info.set(f"手机代理端口：{gateway.port}；局域网地址识别中")
        if gateway.port_history:
            self.record_diagnostic("端口避让", "已跳过 " + ','.join(map(str, gateway.port_history)) +
                                   f"；使用 {gateway.port}，未终止任何进程")
            messagebox.showinfo("代理端口已顺延", f"原端口被占用或不可绑定。\n当前端口：{gateway.port}\n"
                                "请将手机代理端口改为此端口。旧程序未被关闭。")
        self.repair_firewall()

    def record_diagnostic(self, source, message):
        text = f"{time.strftime('%H:%M:%S')} {source} | {message}"
        self.diagnostic_log.append(text)
        try:
            if GATEWAY_LOG_FILE.exists() and GATEWAY_LOG_FILE.stat().st_size > 1024 * 1024:
                GATEWAY_LOG_FILE.replace(GATEWAY_LOG_FILE.with_suffix('.previous.log'))
            with GATEWAY_LOG_FILE.open('a', encoding='utf-8') as stream:
                stream.write(text + '\n')
        except OSError:
            pass

    def repair_firewall(self):
        gateway = self.persistent_gateway
        if self.repair_busy or gateway is None or not gateway.ready.is_set():
            return
        self.repair_busy = True
        port = gateway.port
        self.deployment_status.set("自检：检查防火墙；需要时请求管理员授权")
        def worker():
            try:
                state = inspect_firewall(port)
                if state.get('unsupported'):
                    raise RuntimeError("当前系统不是Windows，未执行防火墙修复")
                rows = state.get("Rules") or []
                own_rule = any(r['Name'] == firewall_rule_name(port) and r['Action'] == 'Allow'
                               for r in rows)
                executable = str(Path(sys.executable).resolve()).lower()
                own_block = any(r['Action'] == 'Block' and r['Program'].lower() == executable
                                for r in rows)
                if not own_rule or own_block:
                    script = firewall_repair_script(port)
                    if is_admin():
                        powershell(script, timeout=45)
                    else:
                        run_elevated_powershell(script)
                state = inspect_firewall(port)
                blocks = [r for r in state.get('Rules', []) if r['Action'] == 'Block']
                if blocks:
                    summary = "仍有可能拦截当前端口的规则：" + ', '.join(r['DisplayName'] for r in blocks)
                elif not any(r['Name'] == firewall_rule_name(port) and r['Action'] == 'Allow'
                             for r in state.get('Rules', [])):
                    summary = "失败：未在有效策略中确认专用放行规则"
                else:
                    summary = f"已确认当前程序 TCP/{port} 局域网放行规则（不等于手机链路已验证）"
                self.deployment_events.put(('repair_done', summary))
            except Exception as exc:
                # Do not print command text or credentials.
                self.deployment_events.put(('repair_done', '修复未完成：' + type(exc).__name__ +
                    '；请点击“修复防火墙”并允许管理员授权，企业策略可能需管理员处理。'))
        threading.Thread(target=worker, daemon=True).start()

    def run_self_check(self):
        if self.diagnostic_busy:
            return
        self.diagnostic_busy = True
        self.deployment_status.set("自检进行中：网络 / 绑定 / 实际网关 HTTPS")
        proxies = [dict(p) for p in self.exits]
        devices = {ip: dict(d) for ip, d in self.devices.items()}
        gateway = self.persistent_gateway
        firewall = self.firewall_summary
        interfaces = list(self._hotspot_interfaces_cache)
        def worker():
            lines = ["出口管理器 V14 启动自检", time.strftime('%Y-%m-%d %H:%M:%S %z'),
                     f"程序：{sys.executable}", f"配置目录：{DATA_DIR}",
                     f"当前代理端口：{gateway.port}", "防火墙：" + firewall]
            lines.extend(MIGRATION_NOTES)
            lines.append("热点地址：" + (', '.join(i['ip'] for i in interfaces) or '尚未识别，请开启热点后重新自检'))
            lines.append("实际监听：" + ('已就绪' if gateway.ready.is_set() else '未就绪'))
            ids = {p.get('id') for p in proxies}
            bound = set()
            configured = 0
            for ip, d in devices.items():
                eid = d.get('exit_id')
                if eid == '__QIZI_BLOCK__':
                    configured += 1
                    lines.append(f"控制 {ip}：禁止网络（代理请求拒绝）")
                elif eid == '__QIZI_LOCAL__':
                    configured += 1
                    lines.append(f"控制 {ip}：本地出口（经电脑当前本地网络）")
                elif eid:
                    valid = eid in ids
                    lines.append(f"绑定 {ip}：" + ('指定出口存在' if valid else '错误：出口编号不存在'))
                    if valid:
                        configured += 1
                        bound.add(eid)
            if not configured:
                lines.append("尚无已配置设备；新发现设备默认禁止网络。")
            if not proxies:
                lines.append("没有出口配置；请添加出口，或将旧配置放在程序旁再首次启动。")
            try:
                if os.name == 'nt':
                    network = powershell('''Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' |
Select-Object InterfaceAlias,NextHop,RouteMetric | Format-Table -AutoSize | Out-String''', timeout=15)
                    lines.extend(['默认路由（未修改网络）：', network.strip()])
                    ports = ','.join(map(str, gateway.port_history + [gateway.port]))
                    owners = powershell(f"""Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
Where-Object {{$_.LocalPort -in @({ports})}} | ForEach-Object {{
    $connection = $_
    $processInfo = Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue
    [pscustomobject]@{{Port=$connection.LocalPort; Address=$connection.LocalAddress;
        PID=$connection.OwningProcess; Process=$processInfo.ProcessName}}
}} | Format-Table -AutoSize | Out-String""", timeout=15)
                    lines.extend(['相关端口占用（只读）：', owners.strip()])
                targets = [p for p in proxies if p.get('id') in bound] or proxies[:1]
                for p in targets:
                    lines.extend(['', '出口：' + str(p.get('name', '')),
                                  browser_security_report(p, gateway.ready.is_set(), gateway)])
            except Exception as exc:
                lines.append('自检步骤异常：' + type(exc).__name__)
            self.deployment_events.put(('report', '\n'.join(lines)))
        threading.Thread(target=worker, daemon=True).start()

    def poll_deployment(self):
        if self._closing:
            return
        for _ in range(30):
            try:
                kind, value = self.deployment_events.get_nowait()
            except queue.Empty:
                break
            if kind == 'repair_done':
                self.repair_busy = False
                self.firewall_summary = value
                self.record_diagnostic('防火墙', value)
                self.run_self_check()
            elif kind == 'report':
                self.diagnostic_busy = False
                self.last_report = value
                try:
                    (DATA_DIR / 'self_check_v14.txt').write_text(value, encoding='utf-8')
                except OSError:
                    self.record_diagnostic('报告', '无法保存自检报告')
                self.deployment_status.set("自检完成，请查看报告（监听正常不代表手机已联网）")
                self.refresh_report_window()
        self.after(200, self.poll_deployment)

    def show_report(self):
        window = getattr(self, '_deployment_window', None)
        if window is not None and window.winfo_exists():
            window.lift()
            self.refresh_report_window()
            return
        window = self._deployment_window = tk.Toplevel(self)
        window.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei')
        window.geometry('900x650')
        from tkinter.scrolledtext import ScrolledText
        self.report_text = ScrolledText(window, wrap='word')
        self.report_text.pack(fill='both', expand=True)
        def copy():
            self.clipboard_clear()
            self.clipboard_append(self.report_text.get('1.0', 'end-1c'))
        ttk.Button(window, text='复制报告', command=copy).pack(pady=5)
        ttk.Button(window, text='刷新记录', command=self.refresh_report_window).pack(pady=3)
        self.refresh_report_window()

    def close(self):
        # V15: cancel all callbacks, close listener/client tunnels, stop workers,
        # verify the actual proxy port is released, then destroy the window.
        errors = _hard_app_close(self)
        if errors and os.name == "nt":
            try:
                ctypes.windll.user32.MessageBoxW(None, "退出清理发现：\n" + "\n".join(errors), "退出清理", 0x30)
            except Exception:
                pass

    def refresh_report_window(self):
        widget = getattr(self, 'report_text', None)
        if widget is None or not widget.winfo_exists():
            return
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('end', self.last_report + '\n\n最近转发/错误记录：\n' + '\n'.join(self.diagnostic_log))
        widget.configure(state='disabled')


class OwnedProcessJob:
    """Windows kernel ownership; never select processes by name or port."""
    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [("ProcessTime", ctypes.c_longlong),
                        ("JobTime", ctypes.c_longlong), ("Flags", w.DWORD),
                        ("MinWorkingSet", ctypes.c_size_t),
                        ("MaxWorkingSet", ctypes.c_size_t),
                        ("ActiveProcesses", w.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("Priority", w.DWORD), ("Scheduling", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("ReadOps", "WriteOps", "OtherOps", "ReadBytes",
                         "WriteBytes", "OtherBytes")]
        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("IO", IO),
                        ("ProcessMemory", ctypes.c_size_t),
                        ("JobMemory", ctypes.c_size_t),
                        ("PeakProcess", ctypes.c_size_t),
                        ("PeakJob", ctypes.c_size_t)]
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        k.CreateJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int,
                                             ctypes.c_void_p, w.DWORD]
        k.SetInformationJobObject.restype = w.BOOL
        k.GetCurrentProcess.restype = w.HANDLE
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.AssignProcessToJobObject.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]
        handle = k.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            k.CloseHandle(handle)
            raise ctypes.WinError(error)
        if not k.AssignProcessToJobObject(handle, k.GetCurrentProcess()):
            error = ctypes.get_last_error()
            k.CloseHandle(handle)
            raise RuntimeError("无法建立本程序的子进程退出保护：" + str(ctypes.WinError(error)))
        # Keep this non-inheritable handle until our process exits. Closing it
        # explicitly here would also kill the current process before saving.
        self.handle = handle




def _force_release_gateway(gateway, timeout=8.0):
    """Hard shutdown of every socket owned by the in-process gateway."""
    if gateway is None:
        return []
    errors = []
    try:
        gateway.stop(timeout=timeout)
    except Exception as exc:
        errors.append(str(exc))
        loop = getattr(gateway, "loop", None)
        if loop is not None and not loop.is_closed():
            try:
                def emergency_cancel():
                    server = getattr(gateway, "server", None)
                    if server is not None:
                        server.close()
                    for task in list(getattr(gateway, "tasks", ())):
                        task.cancel()
                loop.call_soon_threadsafe(emergency_cancel)
            except Exception as inner:
                errors.append("紧急关闭失败：" + str(inner))
        thread = getattr(gateway, "thread", None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)
    return errors


def _write_shutdown_state(port, state, detail=""):
    try:
        save_json(RUNTIME_FILE, {"pid": os.getpid(), "port": int(port), "state": state,
                  "time": time.strftime("%Y-%m-%d %H:%M:%S"), "detail": detail[:1000]})
    except Exception:
        pass


def _verify_port_released(port):
    """Verify our old listener is gone. Never kill a foreign port owner."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(("0.0.0.0", int(port)))
        return True, ""
    except OSError as exc:
        owner = ""
        if os.name == "nt":
            try:
                ps = ("Get-NetTCPConnection -State Listen -LocalPort %d -ErrorAction SilentlyContinue | "
                      "ForEach-Object { $p=Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue; "
                      "\"PID=$($_.OwningProcess) $($p.ProcessName) $($_.LocalAddress):$($_.LocalPort)\" }") % int(port)
                owner = powershell(ps, timeout=8)
            except Exception:
                pass
        return False, (owner or str(exc))
    finally:
        probe.close()


def _hard_app_close(app):
    """Single authoritative exit path for normal close and abnormal mainloop exit."""
    if app is None or getattr(app, "_hard_close_done", False):
        return []
    app._hard_close_done = True
    app._closing = True
    app.service_enabled = False
    try:
        for callback in app.tk.call("after", "info"):
            try: app.after_cancel(callback)
            except Exception: pass
    except Exception:
        pass
    try: save_devices(app.devices)
    except Exception: pass
    gateway = getattr(app, "persistent_gateway", None)
    old_port = getattr(gateway, "port", GATEWAY_PORT) if gateway else GATEWAY_PORT
    errors = _force_release_gateway(gateway)
    pool = getattr(app, "test_pool", None)
    if pool is not None:
        try: pool.shutdown(wait=False, cancel_futures=True)
        except Exception as exc: errors.append("检测线程池关闭失败：" + str(exc))
    ok, detail = _verify_port_released(old_port)
    if not ok:
        errors.append(f"TCP/{old_port} 仍被监听：{detail}")
    _write_shutdown_state(old_port, "clean" if ok else "port_still_busy", "；".join(errors))
    try: app.destroy()
    except Exception: pass
    return errors

def main():
    instance = SingleInstance()
    app = None
    exit_code = 0
    try:
        if not instance.acquire():
            if os.name == 'nt':
                ctypes.windll.user32.MessageBoxW(None, "出口管理器已运行，请使用已打开的窗口。\n本次启动已退出。",
                                               "程序运行中", 0x40)
            return
        owned_job = OwnedProcessJob()
        configure_runtime()
        app = DeploymentProxyManager()
        app.mainloop()
    except Exception as exc:
        exit_code = 1
        text = "启动失败，未终止任何其他进程。\n" + str(exc)
        if os.name == 'nt':
            ctypes.windll.user32.MessageBoxW(None, text, "出口管理器", 0x10)
        else:
            raise
    finally:
        # Also cover mainloop errors / Ctrl+C. Normal window close already saved.
        if app is not None:
            try:
                _hard_app_close(app)
            except Exception:
                exit_code = 1
        try:
            instance.close()
        except Exception:
            exit_code = 1
        # OS teardown remains the final safety net for background workers.
        # ThreadPoolExecutor(wait=False) alone cannot make Python exit.
        for stream in (sys.stdout, sys.stderr):
            if stream is not None:
                try:
                    stream.flush()
                except Exception:
                    pass
        os._exit(exit_code)



# ================= Integrated controller V1 =================
from tkinter import simpledialog, filedialog

IOS_FLAGS = 0x08000000 if os.name == 'nt' else 0

# V2.5.8：iOS 子系统使用程序自带的 Python 3.13 x64 Runtime。
# 打包后的目标电脑不需要安装 Python，也不读取系统 PATH 中的 py/python。
IOS_RUNTIME_DIR = APP_DIR / 'ios_runtime'
IOS_RUNTIME_PYTHON = IOS_RUNTIME_DIR / ('python.exe' if os.name == 'nt' else 'bin/python3')

def bundled_ios_interpreter():
    """Return the private iOS Python runtime command.

    Frozen/installed builds MUST use the bundled runtime so behavior is identical
    on machines with no Python installed.  Source mode prefers the same runtime
    when present, then falls back to the developer's Python 3.13 launcher.
    """
    if IOS_RUNTIME_PYTHON.is_file():
        return [str(IOS_RUNTIME_PYTHON)]
    if getattr(sys, 'frozen', False):
        raise RuntimeError(
            '缺少内置 iOS 运行环境：' + str(IOS_RUNTIME_PYTHON) +
            '\n请重新安装完整程序。目标电脑不需要单独安装 Python。'
        )
    return ['py', '-3.13'] if os.name == 'nt' else [sys.executable]

def normalize_ios_interpreter(saved=None):
    """Never let an installed build fall back to a machine-wide Python."""
    if getattr(sys, 'frozen', False) or IOS_RUNTIME_PYTHON.is_file():
        return bundled_ios_interpreter()
    if isinstance(saved, (list, tuple)) and saved:
        return [str(x) for x in saved]
    return bundled_ios_interpreter()

def stop_owned_process(process):
    """Only handles captured from our own Popen calls are eligible."""
    if process is None or process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       capture_output=True, timeout=8, creationflags=IOS_FLAGS)
    else:
        process.terminate()
    try: process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)

class IOSController:
    def __init__(self, interpreter):
        self.interpreter = list(interpreter)
        self.events = queue.Queue()
        self.lock = threading.RLock()
        self.workers = {}
        self.short_processes = set()
        self.busy = set()
        self.closing = False
        self.generation = 0

    def run(self, args, timeout=30, target_udid=None):
        env=os.environ.copy()
        if target_udid:
            # pymobiledevice3 common selector. Using the env var avoids CLI option
            # placement differences between command groups and is safe for multi-device use.
            env['PYMOBILEDEVICE3_UDID']=str(target_udid)
        with self.lock:
            if self.closing: raise RuntimeError('程序正在关闭')
            process = subprocess.Popen(self.interpreter + ['-u', '-m', 'pymobiledevice3'] + args,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding='utf-8', errors='replace', creationflags=IOS_FLAGS, env=env)
            self.short_processes.add(process)
        try:
            try: out, err = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                stop_owned_process(process)
                raise RuntimeError(f'命令等待超过 {timeout} 秒')
            if process.returncode:
                raise RuntimeError((err or out or f'退出码 {process.returncode}')[-1800:])
            return out
        finally:
            for pipe in (process.stdout,process.stderr):
                if pipe:pipe.close()
            with self.lock: self.short_processes.discard(process)

    def job(self, key, fn):
        with self.lock:
            if self.closing or key in self.busy: return False
            self.busy.add(key)
        def work():
            try: fn()
            except Exception as exc: self.events.put(('job_error', key, str(exc)))
            finally:
                with self.lock: self.busy.discard(key)
        threading.Thread(target=work, name='ios-' + key, daemon=True).start()
        return True

    def scan(self):
        def work():
            data = json.loads(self.run(['remote', 'browse'], 30))
            if not isinstance(data, dict) or not isinstance(data.get('wifi', []), list):
                raise RuntimeError('remote browse 返回格式不兼容')
            devices = {}
            for d in data.get('wifi', []):
                uid = str(d.get('identifier') or '')
                if uid: devices[uid] = d
            self.events.put(('discovered', devices))
        self.job('scan', work)

    def usb_scan(self):
        def work():
            data = json.loads(self.run(['usbmux', 'list'], 25))
            if not isinstance(data, list): raise RuntimeError('USB 列表格式不兼容')
            self.events.put(('usb', [d for d in data if d.get('ConnectionType') == 'USB']))
        self.job('usb', work)

    def trust(self, uid):
        with self.lock:
            if uid in self.workers: return False
        def progress(message):
            self.events.put(('trust_progress', uid, message))
        def dev_status():
            try:
                raw=self.run(['amfi','developer-mode-status'],30,target_udid=uid)
            except Exception as exc:
                return None,str(exc)
            low=str(raw or '').strip().lower()
            # Current pymobiledevice3 prints a boolean-like status. Keep the parser
            # tolerant so older/newer builds do not break this workflow.
            if 'false' in low or 'disabled' in low or low in ('0','no'):
                return False,raw
            if 'true' in low or 'enabled' in low or low in ('1','yes'):
                return True,raw
            return None,raw
        def work():
            meta=dict(developer_ready=None,ddi_ready=None,remote_pair_ready=None,warnings=[])
            # Explicit UDID is mandatory even if there is only one USB device.
            data = json.loads(self.run(['lockdown', 'info', '--udid', uid], 45))
            actual = data.get('UniqueDeviceID') or data.get('Identifier')
            if str(actual).lower().replace('-', '') != uid.lower().replace('-', ''):
                raise RuntimeError('USB 返回的 UDID 不一致，未启用 Wi-Fi')

            progress('USB 信任正常，正在开启 Wi-Fi connections…')
            self.run(['lockdown', 'wifi-connections', 'on', '--udid', uid], 30)

            # New iOS/PMD builds keep a separate RemotePairing identity for trusted
            # Wi-Fi RSD tunnels. Pair it while USB is present. Older PMD builds may
            # not expose this command, so an unsupported-command error is non-fatal.
            progress('正在准备 Wi-Fi Tunnel 配对…')
            try:
                self.run(['lockdown','remotepairing','--pair'],75,target_udid=uid)
                meta['remote_pair_ready']=True
            except Exception as exc:
                low=str(exc).lower()
                if ('no such command' in low or 'no such option' in low or
                        'invalid value for' in low or 'unknown command' in low):
                    meta['remote_pair_ready']=None
                else:
                    meta['remote_pair_ready']=False
                    meta['warnings'].append('RemotePairing：'+str(exc)[-500:])

            progress('正在检查手机开发者模式…')
            enabled,status_detail=dev_status()
            if enabled is False:
                progress('开发者模式未开启，正在尝试自动开启；手机可能会自动重启…')
                try:
                    # pymobiledevice3 AMFI enables Developer Mode and, when allowed by
                    # iOS, waits for the reboot/reconnect and confirms the post-reboot prompt.
                    self.run(['amfi','enable-developer-mode'],240,target_udid=uid)
                    meta['developer_ready']=True
                    progress('开发者模式已开启，正在等待 USB 服务恢复…')
                    # Re-validate after the reboot and re-assert Wi-Fi lockdown access.
                    last_error=None
                    for _ in range(12):
                        try:
                            self.run(['lockdown','info','--udid',uid],20)
                            last_error=None
                            break
                        except Exception as exc:
                            last_error=exc;time.sleep(2)
                    if last_error is not None:
                        raise RuntimeError('手机重启后 USB 尚未恢复：'+str(last_error))
                    self.run(['lockdown','wifi-connections','on','--udid',uid],30)
                except Exception as exc:
                    low=str(exc).lower()
                    if 'passcode' in low or 'password' in low:
                        meta['developer_ready']=False
                        meta['warnings'].append(
                            '开发者模式未开启：手机设有锁屏密码，iOS 不允许电脑自动开启。'
                            '请在手机 设置 → 隐私与安全性 → 开发者模式 打开并按提示重启；'
                            '完成后保持 USB 连接，再点一次“开发模式”。')
                    else:
                        meta['developer_ready']=False
                        meta['warnings'].append('自动开启开发者模式失败：'+str(exc)[-700:])
            elif enabled is True:
                meta['developer_ready']=True
            else:
                # Do not break an otherwise-valid trust flow if an older PMD build
                # cannot query the state; the later Tunnel attempt still gives a real result.
                meta['developer_ready']=None
                if status_detail:
                    meta['warnings'].append('无法确认开发者模式状态：'+str(status_detail)[-500:])

            # Location simulation needs the DeveloperDiskImage once per phone boot.
            # Mount it here while USB is definitely present; failure is reported but
            # does not erase the successful USB trust record.
            if meta['developer_ready'] is True:
                progress('正在准备开发者组件（DeveloperDiskImage）…')
                try:
                    self.run(['mounter','auto-mount'],240,target_udid=uid)
                    meta['ddi_ready']=True
                except Exception as exc:
                    meta['ddi_ready']=False
                    meta['warnings'].append('开发者组件挂载失败：'+str(exc)[-700:])

            # Re-assert this at the end because Developer Mode enabling can reboot the phone.
            try:
                self.run(['lockdown','wifi-connections','on','--udid',uid],30)
            except Exception as exc:
                meta['warnings'].append('重新开启 Wi-Fi connections 失败：'+str(exc)[-500:])

            self.events.put(('trusted', uid, meta))
            self.scan()
            try:self.identity_scan()
            except Exception:pass
        return self.job('trust:' + uid, work)

    def connect(self, uid):
        with self.lock:
            if self.closing or uid in self.workers or 'trust:' + uid in self.busy: return False
            self.generation += 1
            generation = self.generation
            record = dict(generation=generation, process=None, last=time.monotonic(),
                          out=queue.Queue(maxsize=32), stopping=False)
            self.workers[uid] = record
        def work():
            process = None
            try:
                with self.lock:
                    if self.closing or record['stopping']: return
                    process = subprocess.Popen(self.interpreter + ['-u', str(Path(__file__).resolve()),
                        '--ios-worker', uid], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                        bufsize=1, creationflags=IOS_FLAGS)
                    record['process'] = process
                def writer():
                    try:
                        while process.poll() is None:
                            try: cmd = record['out'].get(timeout=.3)
                            except queue.Empty: continue
                            process.stdin.write(json.dumps(cmd) + '\n')
                            process.stdin.flush()
                    except (OSError, ValueError): pass
                def stderr_reader():
                    for line in process.stderr:
                        # Keep last failure context, do not flood the GUI every tick.
                        record['stderr'] = line.strip()[-600:]
                threading.Thread(target=writer, daemon=True).start()
                threading.Thread(target=stderr_reader, daemon=True).start()
                for line in process.stdout:
                    try: message = json.loads(line)
                    except ValueError: continue
                    if message.get('udid') != uid: continue
                    record['last'] = time.monotonic()
                    self.events.put(('worker', uid, generation, message))
                process.wait()
            except Exception as exc:
                self.events.put(('worker', uid, generation, dict(kind='error', error=str(exc))))
            finally:
                if process and process.poll() is None: stop_owned_process(process)
                if process:
                    for pipe in (process.stdin,process.stdout,process.stderr):
                        if pipe:
                            try:pipe.close()
                            except OSError:pass
                with self.lock:
                    if self.workers.get(uid) is record: self.workers.pop(uid, None)
                self.events.put(('worker', uid, generation,
                    dict(kind='ended', wifi='已断开', location='不可定位', error=record.get('stderr', ''))))
        threading.Thread(target=work, name='iphone-' + uid[-8:], daemon=True).start()
        return generation

    def send(self, uid, command):
        with self.lock:
            record = self.workers.get(uid)
            if not record or record['stopping']: return False
            op=str(command.get('op','')) if isinstance(command,dict) else ''
            if op in ('stop','clear'):
                # STOP/CLEAR supersede any not-yet-consumed START/ONCE command.
                # This prevents a stale start from running immediately after the user clicks “定位中” to stop.
                while True:
                    try:record['out'].get_nowait()
                    except queue.Empty:break
            try: record['out'].put_nowait(command)
            except queue.Full: return False
            return True

    def disconnect(self, uid):
        with self.lock:
            record = self.workers.get(uid)
            if not record or record['stopping']: return
            record['stopping'] = True
            try: record['out'].put_nowait({'op': 'shutdown'})
            except queue.Full: pass
        def end():
            process = record.get('process')
            if process:
                try: process.wait(timeout=4)
                except subprocess.TimeoutExpired: stop_owned_process(process)
        threading.Thread(target=end, daemon=True).start()

    def shutdown(self):
        with self.lock:
            self.closing = True
            uids = list(self.workers)
            short = list(self.short_processes)
        for uid in uids: self.disconnect(uid)
        for p in short:
            try: stop_owned_process(p)
            except Exception: pass
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            with self.lock:
                if not self.workers: break
            time.sleep(.05)
        with self.lock: pending = [r.get('process') for r in self.workers.values()]
        for p in pending:
            try: stop_owned_process(p)
            except Exception: pass


def configure_integrated_runtime():
    """V2.9.29.1: keep program/config/state files beside the main executable.

    Existing %LOCALAPPDATA%/QiziIOSIntegrated files are imported once when the
    same file does not already exist beside the program. No extra data folder is created.
    """
    global EXITS_FILE, DEVICES_FILE, CONFIG_FILE, GATEWAY_CONFIG_FILE, GATEWAY_LOG_FILE
    global DATA_DIR, RUNTIME_FILE, GATEWAY_PORT, MIGRATION_NOTES
    old_parent = Path(os.environ.get('LOCALAPPDATA', str(Path.home())))
    old_integrated = old_parent / 'QiziIOSIntegrated'
    old_exit = old_parent / 'QiziExitManager'
    DATA_DIR = APP_DIR
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MIGRATION_NOTES = []
    for name, kind in [('proxy_exits.json', list), ('device_bindings.json', dict),
                       ('ios_devices.json', dict), ('qizi_route_plans.json', dict),
                       ('qizi_route_state.json', dict)]:
        destination = DATA_DIR / name
        if not destination.exists():
            for source in (old_integrated / name, old_exit / name):
                if source.exists():
                    try:
                        data = json.loads(source.read_text(encoding='utf-8-sig'))
                        if isinstance(data, kind):
                            save_json(destination, data)
                            MIGRATION_NOTES.append(f'已导入 {name}')
                            break
                    except Exception:
                        pass
        if not destination.exists() and kind is dict and name in ('qizi_route_plans.json','qizi_route_state.json'):
            save_json(destination, {})
    EXITS_FILE, DEVICES_FILE = DATA_DIR / 'proxy_exits.json', DATA_DIR / 'device_bindings.json'
    CONFIG_FILE = DATA_DIR / 'config.json'
    GATEWAY_CONFIG_FILE = DATA_DIR / 'hotspot_gateway_config.json'
    GATEWAY_LOG_FILE = DATA_DIR / 'gateway.log'
    RUNTIME_FILE = DATA_DIR / 'runtime.json'
    GATEWAY_PORT = 8899


# V2.5.9: country/region labels used by the compact exit panel.
# Cloudflare health checks usually return a 2-letter ISO code, so keep this
# local/offline and do not add another network dependency just for display.
COUNTRY_ZH = {
    "US":"美国","GB":"英国","CA":"加拿大","AU":"澳大利亚","NZ":"新西兰",
    "JP":"日本","KR":"韩国","SG":"新加坡","HK":"中国香港","MO":"中国澳门","TW":"中国台湾",
    "CN":"中国","MY":"马来西亚","TH":"泰国","VN":"越南","PH":"菲律宾","ID":"印度尼西亚",
    "IN":"印度","PK":"巴基斯坦","BD":"孟加拉国","LK":"斯里兰卡","KH":"柬埔寨","LA":"老挝",
    "MM":"缅甸","NP":"尼泊尔","AE":"阿联酋","SA":"沙特阿拉伯","QA":"卡塔尔","IL":"以色列",
    "TR":"土耳其","DE":"德国","FR":"法国","IT":"意大利","ES":"西班牙","PT":"葡萄牙",
    "NL":"荷兰","BE":"比利时","CH":"瑞士","AT":"奥地利","IE":"爱尔兰","SE":"瑞典",
    "NO":"挪威","FI":"芬兰","DK":"丹麦","IS":"冰岛","PL":"波兰","CZ":"捷克",
    "HU":"匈牙利","RO":"罗马尼亚","BG":"保加利亚","GR":"希腊","UA":"乌克兰","RU":"俄罗斯",
    "BR":"巴西","AR":"阿根廷","CL":"智利","CO":"哥伦比亚","PE":"秘鲁","MX":"墨西哥",
    "ZA":"南非","EG":"埃及","NG":"尼日利亚","KE":"肯尼亚","MA":"摩洛哥","GH":"加纳"
}
COUNTRY_NAME_ZH = {
    "united states":"美国","united states of america":"美国","united kingdom":"英国",
    "canada":"加拿大","australia":"澳大利亚","new zealand":"新西兰","japan":"日本",
    "south korea":"韩国","korea, republic of":"韩国","singapore":"新加坡","hong kong":"中国香港",
    "macao":"中国澳门","macau":"中国澳门","taiwan":"中国台湾","china":"中国",
    "malaysia":"马来西亚","thailand":"泰国","vietnam":"越南","philippines":"菲律宾",
    "indonesia":"印度尼西亚","india":"印度","united arab emirates":"阿联酋","germany":"德国",
    "france":"法国","italy":"意大利","spain":"西班牙","netherlands":"荷兰","switzerland":"瑞士",
    "sweden":"瑞典","norway":"挪威","finland":"芬兰","denmark":"丹麦","poland":"波兰",
    "russia":"俄罗斯","brazil":"巴西","argentina":"阿根廷","mexico":"墨西哥","south africa":"南非"
}

def country_with_chinese(value):
    raw=str(value or '').strip()
    if not raw:
        return '未知'
    code=raw.upper()
    zh=COUNTRY_ZH.get(code)
    if zh:
        return f"{raw} {zh}"
    zh=COUNTRY_NAME_ZH.get(raw.lower())
    if zh and zh not in raw:
        return f"{raw}（{zh}）"
    return raw


class IntegratedApp(DeploymentProxyManager):
    def __init__(self):
        self.ios_file = DATA_DIR / 'ios_devices.json'
        self.ios_config = json.loads(self.ios_file.read_text(encoding='utf-8')) if self.ios_file.exists() else {}
        self.links = self.ios_config.get('links', {})
        self.coordinates = self.ios_config.get('coordinates', {})
        self.ios_states = {}
        self.discovered = {}
        self.discovery_at = 0
        self.usb_devices = []
        self.ios_logs = deque(maxlen=500)
        self.ios_ready = False
        self.last_ios_scan = 0
        self.shutdown_done = threading.Event()
        interpreter = normalize_ios_interpreter(self.ios_config.get('interpreter'))
        self.ios = IOSController(interpreter)
        super().__init__()
        self.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei')
        self.geometry('1180x620')
        self.minsize(1000, 500)
        self.after(300, self.check_ios_environment)
        self.after(100, self.poll_ios)

    def build_ui(self):
        style = ttk.Style(self)
        if 'vista' in style.theme_names(): style.theme_use('vista')
        style.configure('TButton', padding=(4, 1))
        style.configure('Device.Treeview', rowheight=24, font=('Microsoft YaHei UI', 9))
        style.configure('Exit.Treeview', rowheight=23, font=('Microsoft YaHei UI', 9))
        root = ttk.Frame(self, padding=5)
        root.pack(fill='both', expand=True)
        bar = ttk.Frame(root)
        bar.pack(fill='x', pady=(0, 4))
        def button(parent, text, fn):
            b = ttk.Button(parent, text=text, command=fn)
            b.pack(side='left', padx=2)
            return b
        button(bar, '扫描全部', self.scan_all)
        button(bar, '添加IP', self.add_manual_device)
        button(bar, 'USB信任/关联', self.usb_dialog)
        button(bar, '关联iOS设备', self.link_dialog)
        button(bar, '删除设备', self.delete_device)
        self.auto_scan_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text='自动扫描', variable=self.auto_scan_var).pack(side='left', padx=4)
        button(bar, '运行环境', self.environment_dialog)
        button(bar, '日志', self.show_ios_log)
        self.status = tk.StringVar(value='代理未启动')
        ttk.Label(root, textvariable=self.status).pack(fill='x')
        main = ttk.Panedwindow(root, orient='horizontal')
        main.pack(fill='both', expand=True, pady=3)
        left, right = ttk.Frame(main), ttk.Frame(main, width=260)
        main.add(left, weight=5)
        main.add(right, weight=1)
        self.summary = tk.StringVar(value='设备')
        ttk.Label(left, textvariable=self.summary).pack(anchor='w')
        tablewrap = ttk.Frame(left)
        tablewrap.pack(fill='both', expand=True)
        cols = ('ip', 'exit', 'trust', 'wifi', 'lon', 'lat', 'location', 'last', 'count')
        self.device_table = ttk.Treeview(tablewrap, columns=cols, show='headings', selectmode='extended',
                                        style='Device.Treeview')
        specs = [('ip','设备IP',125), ('exit','出口（双击选择）',155), ('trust','信任',65),
                 ('wifi','Wi-Fi连接',80), ('lon','经度',90), ('lat','纬度',90),
                 ('location','定位',88), ('last','最近发送',75), ('count','次数',55)]
        for key, title, width in specs:
            self.device_table.heading(key, text=title)
            self.device_table.column(key, width=width, minwidth=45, stretch=False, anchor='center')
        sy = ttk.Scrollbar(tablewrap, orient='vertical', command=self.device_table.yview)
        sx = ttk.Scrollbar(tablewrap, orient='horizontal', command=self.device_table.xview)
        self.device_table.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.device_table.grid(row=0, column=0, sticky='nsew')
        sy.grid(row=0, column=1, sticky='ns')
        sx.grid(row=1, column=0, sticky='ew')
        tablewrap.rowconfigure(0, weight=1)
        tablewrap.columnconfigure(0, weight=1)
        self.device_table.tag_configure('online', foreground='#14752c')
        self.device_table.tag_configure('offline', foreground='#ba2626')
        self.device_table.bind('<<TreeviewSelect>>', self.selection_changed)
        self.device_table.bind('<Double-1>', self.edit_cell)
        actions = ttk.Frame(left)
        actions.pack(fill='x', pady=3)
        button(actions, '连接所选', lambda:self.ios_action('connect'))
        button(actions, '断开', lambda:self.ios_action('disconnect'))
        button(actions, '开启定位', lambda:self.ios_action('start'))
        button(actions, '单次发送', lambda:self.ios_action('once'))
        button(actions, '停止保持', lambda:self.ios_action('stop'))
        button(actions, '恢复真实定位', lambda:self.ios_action('clear'))
        settings = ttk.Frame(left)
        settings.pack(fill='x', pady=2)
        self.lon_var, self.lat_var = tk.StringVar(value='139.6503'), tk.StringVar(value='35.6762')
        self.interval_var = tk.StringVar(value='1')
        for label, var, width in [('经度',self.lon_var,11),('纬度',self.lat_var,11),('间隔秒',self.interval_var,4)]:
            ttk.Label(settings,text=label).pack(side='left')
            ttk.Entry(settings,textvariable=var,width=width).pack(side='left',padx=3)
        button(settings, '应用到所选', self.save_coordinates)
        ttk.Label(left,text='Ctrl/Shift 多选批量操作；双击出口、经纬度编辑；停止保持不清除模拟位置。').pack(anchor='w')
        ttk.Label(right, text='出口管理 · 每5秒后台检测').pack(anchor='w')
        exitbar = ttk.Frame(right)
        exitbar.pack(fill='x', pady=3)
        for title, fn in [('添加',self.add_exit),('编辑',self.edit_exit),('删除',self.delete_exit),('检测',self.test_all)]:
            b=ttk.Button(exitbar,text=title,width=5,command=fn);b.pack(side='left',padx=1)
        # Keep engine-compatible backing columns, display only compact country/IP and state.
        self.exit_table = ttk.Treeview(right, columns=('name','host','port','country','public_ip','status'),
            displaycolumns=('name','status'), show='headings', style='Exit.Treeview', selectmode='browse')
        self.exit_table.heading('name',text='归属地（中文） + IP')
        self.exit_table.heading('status',text='状态')
        self.exit_table.column('name',width=185,minwidth=120)
        self.exit_table.column('status',width=55,minwidth=50,stretch=False)
        exit_scroll=ttk.Scrollbar(right,orient='vertical',command=self.exit_table.yview)
        self.exit_table.configure(yscrollcommand=exit_scroll.set)
        exit_scroll.pack(side='right',fill='y')
        self.exit_table.pack(fill='both',expand=True)
        self.exit_table.tag_configure('exit_online',foreground='#14752c')
        self.exit_table.tag_configure('exit_offline',foreground='#ba2626')
        self.exit_table.bind('<Double-1>',lambda e:self.edit_exit())
        servicebar=ttk.Frame(right);servicebar.pack(fill='x',pady=3)
        self.start_btn = button(servicebar,'启动/刷新代理',self.start_selected)
        self.stop_btn = button(servicebar,'停止代理',self.stop)
        # Hidden compatibility widget for legacy dialog/engine binding helpers.
        self.exit_combo = ttk.Combobox(root,state='readonly')
        self.gateway_info = tk.StringVar(value='正在识别热点代理地址')
        self.hotspot_info = tk.StringVar(value='热点扫描准备中')
        self.detail = tk.StringVar(value='等待设备发现')
        self.deployment_status = tk.StringVar(value='自检等待启动')
        self.ios_summary = tk.StringVar(value='iOS环境检测中')
        ttk.Label(root,textvariable=self.gateway_info).pack(fill='x')
        ttk.Label(root,textvariable=self.hotspot_info).pack(fill='x')
        ttk.Label(root,textvariable=self.ios_summary).pack(fill='x')
        diag=ttk.Frame(root);diag.pack(fill='x',pady=2)
        self.gateway_btn=button(diag,'修复防火墙',self.repair_firewall)
        button(diag,'网络自检',self.run_self_check)
        button(diag,'自检报告',self.show_report)
        button(diag,'浏览器诊断',self.browser_security_diagnostic)
        ttk.Label(diag,textvariable=self.deployment_status).pack(side='left',padx=3)
        ttk.Label(root,textvariable=self.detail,wraplength=1100).pack(fill='x')

    def _blink_exit_status(self):
        # No flashing rows in the integrated table.
        return

    def exit_label(self,p):
        return f"{p.get('country') or '未知'} {p.get('public_ip') or p.get('host','')}"

    def refresh_exits(self, selected=None):
        V10ProxyManager.refresh_exits(self,selected)
        for i,p in enumerate(self.exits):
            tag='exit_online' if self._exit_is_online(p) else 'exit_offline'
            self.exit_table.item(str(i),values=(self.exit_label(p),p.get('host',''),p.get('port',''),
                p.get('country',''),p.get('public_ip',''), '在线' if tag=='exit_online' else '断线'),tags=(tag,))
        self.refresh_devices()

    def uid_for_ip(self, ip):
        d=self.devices.get(ip,{})
        mac=normalize_mac(d.get('mac',''))
        by_mac=self.links.get('mac:'+mac) if valid_device_mac(mac) else None
        if by_mac:return by_mac
        uid=self.links.get('ip:'+ip)
        # IP-only associations are valid only while fresh discovery confirms them.
        if uid and time.monotonic()-self.discovery_at < 45:
            if self.discovered.get(uid,{}).get('address')==ip:return uid
        return None

    def refresh_devices(self, selected_ip=None):
        if not hasattr(self,'device_table'): return
        chosen=self.device_table.selection()
        wanted=set(self.devices)
        for key in set(self.device_table.get_children())-wanted:self.device_table.delete(key)
        for ip,d in self.devices.items():
            uid=self.uid_for_ip(ip)
            state=self.ios_states.get(uid,{})
            coord=self.coordinates.get(uid or 'ip:'+ip,{'lon':'139.6503','lat':'35.6762','interval':1})
            p=next((p for p in self.exits if p.get('id')==d.get('exit_id')),None)
            exit_text=self.exit_label(p) if p else '未绑定'
            if p: exit_text=('● ' if self._exit_is_online(p) else '× ')+exit_text
            trust='已信任' if state.get('trusted') else ('待验证' if uid else '未关联')
            values=(ip,exit_text,trust,state.get('wifi','未连接'),coord['lon'],coord['lat'],
                    state.get('location','未开启'),state.get('last','-'),state.get('count',0))
            tags=('online' if d.get('online') else 'offline',)
            if self.device_table.exists(ip):self.device_table.item(ip,values=values,tags=tags)
            else:self.device_table.insert('','end',iid=ip,values=values,tags=tags)
        keep=[ip for ip in chosen if self.device_table.exists(ip)]
        if selected_ip and self.device_table.exists(selected_ip):keep=[selected_ip]
        if keep and tuple(keep)!=self.device_table.selection():self.device_table.selection_set(keep)
        self.summary.set(f"设备 {len(self.devices)} · 在线 {sum(bool(d.get('online')) for d in self.devices.values())}"
                         f" · Wi-Fi {sum(s.get('wifi')=='连接中' for s in self.ios_states.values())}"
                         f" · 定位 {sum(s.get('location')=='定位中' for s in self.ios_states.values())}")

    def selection_changed(self,event=None):
        ip=self.selected_device_ip()
        if not ip:return
        uid=self.uid_for_ip(ip)
        coord=self.coordinates.get(uid or 'ip:'+ip,{'lon':'139.6503','lat':'35.6762','interval':1})
        self.lon_var.set(coord['lon']);self.lat_var.set(coord['lat']);self.interval_var.set(str(coord.get('interval',1)))
        self.load_selected_device_binding()
        state=self.ios_states.get(uid,{})
        self.detail.set(f"{ip} · UDID {uid or '未关联'} · {state.get('error') or state.get('rsd') or '就绪'}")

    def save_ios(self):
        save_json(self.ios_file,dict(links=self.links,coordinates=self.coordinates,interpreter=self.ios.interpreter))

    def save_coordinates(self):
        try:
            lat,lon,sec=float(self.lat_var.get()),float(self.lon_var.get()),float(self.interval_var.get())
            if not(-90<=lat<=90 and -180<=lon<=180 and 1<=sec<=60):raise ValueError('范围无效')
            ips=self.device_table.selection()
            if not ips:raise ValueError('请先选择设备')
            for ip in ips:
                uid=self.uid_for_ip(ip)
                self.coordinates[uid or 'ip:'+ip]=dict(lat=str(lat),lon=str(lon),interval=sec)
            self.save_ios();self.refresh_devices()
            self.detail.set('坐标已保存。正在定位的设备需再次点击“开启定位”应用新坐标。')
        except ValueError as exc:messagebox.showerror('坐标',str(exc),parent=self)

    def edit_cell(self,event):
        ip=self.device_table.identify_row(event.y)
        col=self.device_table.identify_column(event.x)
        if not ip:return
        self.device_table.selection_set(ip)
        if col=='#2':
            win=tk.Toplevel(self);win.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei');win.geometry('360x110')
            ids=['']+[p['id'] for p in self.exits]
            labels=['未绑定']+[self.exit_label(p)+' ['+p['id'][:6]+']' for p in self.exits]
            combo=ttk.Combobox(win,state='readonly',values=labels,width=44);combo.pack(padx=10,pady=10)
            eid=self.devices[ip].get('exit_id','');combo.current(ids.index(eid) if eid in ids else 0)
            def apply():
                if ip not in self.devices:return win.destroy()
                chosen=ids[combo.current()]
                if chosen and not any(p.get('id')==chosen for p in self.exits):return
                with self.device_lock:self.devices[ip]['exit_id']=chosen
                save_devices(self.devices);self._update_live_routes();self.refresh_devices();win.destroy()
            ttk.Button(win,text='保存',command=apply).pack()
        elif col in ('#5','#6'):
            key='lon' if col=='#5' else 'lat'
            uid=self.uid_for_ip(ip) or 'ip:'+ip
            coord=self.coordinates.setdefault(uid,dict(lon='139.6503',lat='35.6762',interval=1))
            value=simpledialog.askfloat('坐标','经度' if key=='lon' else '纬度',initialvalue=float(coord[key]),
                minvalue=-180 if key=='lon' else -90,maxvalue=180 if key=='lon' else 90,parent=self)
            if value is not None:
                coord[key]=str(value);self.save_ios();self.refresh_devices();self.selection_changed()
        elif col=='#3':self.usb_dialog()
        elif col=='#4':self.ios_action('connect')
        elif col=='#7':self.ios_action('start')

    def log_ios(self,text):
        self.ios_logs.append(time.strftime('%H:%M:%S')+' '+text)
        self.detail.set(text[:500])

    def scan_all(self):
        self.scan_devices()
        if self.ios_ready:self.ios.scan()

    def check_ios_environment(self):
        interpreter=list(self.ios.interpreter)
        def work():
            try:
                result=self.ios.run(['version'],20)
                self.ios.events.put(('environment',True,' '.join(interpreter)+' · '+result.strip()[-100:]))
            except Exception as exc:self.ios.events.put(('environment',False,str(exc)))
        self.ios.job('environment',work)

    def environment_dialog(self):
        win=tk.Toplevel(self);win.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei');win.geometry('620x230')
        ttk.Label(win,text='安装版固定使用程序自带 Python 3.13 x64；目标电脑无需安装 Python。').pack(anchor='w',padx=8,pady=8)
        value=tk.StringVar(value=' '.join(self.ios.interpreter))
        entry=ttk.Entry(win,textvariable=value,width=78);entry.pack(padx=8,fill='x')
        def browse():
            path=filedialog.askopenfilename(parent=win,title='选择已安装 pymobiledevice3 的 python.exe',filetypes=[('Python','*.exe'),('所有文件','*')])
            if path:value.set(path)
        ttk.Button(win,text='选择 python.exe',command=browse).pack(pady=6)
        ttk.Label(win,text='安装版路径固定为 ios_runtime\\python.exe；源码调试时仍可指定 Python 3.13。\n内置 Runtime 应包含 pymobiledevice3 及其全部依赖。').pack()
        def apply():
            if self.ios.workers or self.ios.busy:
                return messagebox.showinfo('环境','请先断开全部 iOS 连接并等待扫描/信任完成。',parent=win)
            if getattr(sys, 'frozen', False):
                self.ios.interpreter=bundled_ios_interpreter()
            else:
                raw=value.get().strip().strip('"')
                interpreter=raw.split() if raw.startswith('py ') else [raw]
                self.ios.interpreter=normalize_ios_interpreter(interpreter)
            self.ios_ready=False;self.save_ios();self.check_ios_environment();win.destroy()
        ttk.Button(win,text='保存并检测',command=apply).pack(pady=6)

    def usb_dialog(self):
        win=tk.Toplevel(self);win.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei');win.geometry('740x300')
        ttk.Label(win,text='插线并解锁手机，在手机上确认“信任”。列表只显示 USB 设备，每次操作明确指定 UDID。').pack(anchor='w',padx=8,pady=6)
        table=ttk.Treeview(win,columns=('name','uid'),show='headings',height=6)
        table.heading('name',text='设备名称');table.heading('uid',text='UDID')
        table.column('name',width=160);table.column('uid',width=410)
        table.pack(fill='both',expand=True,padx=8)
        self.usb_window_table=table
        for d in self.usb_devices:
            uid=d.get('UniqueDeviceID') or d.get('Identifier')
            if uid:table.insert('','end',iid=uid,values=(d.get('DeviceName','iPhone'),uid))
        bar=ttk.Frame(win);bar.pack(fill='x',pady=6)
        ttk.Button(bar,text='刷新USB设备',command=self.ios.usb_scan).pack(side='left',padx=8)
        def trust():
            for uid in table.selection():
                if self.ios.trust(uid):
                    self.ios_states.setdefault(uid,{}).setdefault('trusted',False)
                    self.log_ios(uid+'：正在验证 USB 信任并开启 Wi-Fi，注意手机提示')
        ttk.Button(bar,text='信任并开启Wi-Fi',command=trust).pack(side='left')
        ttk.Label(win,text='信任成功后关闭本窗口，再扫描 iOS；按发现的 Wi-Fi IP 自动关联。无法对应时使用“关联iOS设备”。').pack(pady=6)
        self.ios.usb_scan()

    def attach_uid(self,ip,uid):
        # Only one row can control a UDID; mapping by MAC survives DHCP changes.
        old=self.uid_for_ip(ip)
        if old and old!=uid and old in self.ios.workers:raise ValueError('请先断开原设备再关联')
        for other in self.devices:
            if other!=ip and self.uid_for_ip(other)==uid:
                raise ValueError(f'该 iPhone 已关联 {other}，请先移除旧关联或等待热点IP迁移')
        d=self.devices[ip];mac=normalize_mac(d.get('mac',''))
        key='mac:'+mac if valid_device_mac(mac) else 'ip:'+ip
        self.links[key]=uid
        d['manual']=True  # Keep iOS-associated rows across hotspot scan gaps.
        if valid_device_mac(mac):self.links.pop('ip:'+ip,None)
        pending=self.coordinates.pop('ip:'+ip,None)
        if pending and uid not in self.coordinates:self.coordinates[uid]=pending
        self.save_ios()
        save_devices(self.devices)

    def link_dialog(self):
        ip=self.selected_device_ip()
        if not ip:return messagebox.showinfo('关联','请先选一个设备IP。',parent=self)
        win=tk.Toplevel(self);win.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei');win.geometry('740x180')
        ttk.Label(win,text='请选择与该IP对应的 iPhone。仅按可核对的IP/UDID关联，不按列表顺序猜测。').pack(pady=8)
        uids=list(self.discovered)
        labels=[f"{self.discovered[u].get('address','?')} | {u}" for u in uids]
        combo=ttk.Combobox(win,state='readonly',values=labels,width=85);combo.pack(padx=8)
        def apply():
            if combo.current()<0:return
            uid=uids[combo.current()]
            address=self.discovered[uid].get('address','')
            if address and ':' not in address and address!=ip:
                return messagebox.showerror('关联不一致',f'发现的 IPv4 是 {address}，不是 {ip}。请刷新扫描。',parent=win)
            try:self.attach_uid(ip,uid)
            except ValueError as exc:return messagebox.showerror('关联',str(exc),parent=win)
            self.refresh_devices();win.destroy()
        ttk.Button(win,text='确认关联',command=apply).pack(pady=8)
        def unlink():
            uid=self.uid_for_ip(ip)
            if uid in self.ios.workers:return messagebox.showinfo('关联','请先断开该设备。',parent=win)
            mac=normalize_mac(self.devices.get(ip,{}).get('mac',''))
            self.links.pop('ip:'+ip,None);self.links.pop('mac:'+mac,None)
            self.save_ios();self.refresh_devices();win.destroy()
        ttk.Button(win,text='移除保存的关联',command=unlink).pack()

    def ios_action(self,op):
        if not self.ios_ready:return messagebox.showinfo('iOS','请先在“运行环境”检测可用的 pymobiledevice3。',parent=self)
        selected=self.device_table.selection()
        if not selected:return messagebox.showinfo('设备','请先选择设备，可 Ctrl/Shift 多选。',parent=self)
        handled=set()
        for ip in selected:
            uid=self.uid_for_ip(ip)
            if not uid:
                self.log_ios(ip+'：未关联 iOS UDID，请先扫描/关联');continue
            if uid in handled:continue
            handled.add(uid)
            state=self.ios_states.setdefault(uid,{})
            if op=='connect':
                generation=self.ios.connect(uid)
                if generation:
                    state.update(generation=generation,wifi='建立中',location='未开启',error='')
                continue
            if op=='disconnect':
                self.ios.disconnect(uid);state.update(wifi='断开中',location='正在停止');continue
            if state.get('wifi')!='连接中':
                self.log_ios(ip+'：Wi-Fi 未建立，不能发送定位');continue
            command=dict(op=op)
            if op in ('start','once'):
                coord=self.coordinates.get(uid,dict(lat='35.6762',lon='139.6503',interval=1))
                command.update(coord)
            if self.ios.send(uid,command):
                state['location']={'start':'准备发送','once':'准备发送','stop':'正在停止','clear':'正在清除'}[op]
            else:self.log_ios(ip+'：设备任务队列忙或已断开，请稍后重试')
        self.refresh_devices()

    def delete_device(self):
        selected=self.device_table.selection()
        if not selected:return
        for ip in selected:
            uid=self.uid_for_ip(ip)
            if uid in self.ios.workers:
                messagebox.showinfo('删除','请先断开所选设备的 iOS 连接，再删除。',parent=self);return
        with self.device_lock:
            for ip in selected:
                d=self.devices.pop(ip,{})
                self.links.pop('ip:'+ip,None)
                self.links.pop('mac:'+normalize_mac(d.get('mac','')),None)
        save_devices(self.devices);self.save_ios();self._update_live_routes();self.refresh_devices()

    def show_ios_log(self):
        win=tk.Toplevel(self);win.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei');win.geometry('900x460')
        text=tk.Text(win,wrap='word');text.pack(fill='both',expand=True)
        def refresh():
            if not win.winfo_exists():return
            text.config(state='normal');text.delete('1.0','end')
            text.insert('end','\n'.join(self.ios_logs));text.config(state='disabled');text.see('end')
            win.after(1000,refresh)
        refresh()

    def poll_ios(self):
        if self._closing:return
        changed=False
        for _ in range(400):
            try:event=self.ios.events.get_nowait()
            except queue.Empty:break
            kind=event[0]
            if kind=='environment':
                self.ios_ready=event[1]
                self.ios_summary.set(('iOS环境正常：' if event[1] else 'iOS环境不可用：')+event[2][:230])
                if event[1]:self.ios.scan()
            elif kind=='discovered':
                self.discovered=event[1];self.discovery_at=time.monotonic()
                for uid,data in self.discovered.items():
                    ip=data.get('address','')
                    if ip in self.devices:
                        try:self.attach_uid(ip,uid)
                        except ValueError as exc:self.log_ios(str(exc))
                self.ios_summary.set(f'iOS发现 {len(self.discovered)} 台 · 独立工作进程 {len(self.ios.workers)} · 定位默认1秒')
                changed=True
            elif kind=='usb':
                self.usb_devices=event[1]
                table=getattr(self,'usb_window_table',None)
                if table is not None and table.winfo_exists():
                    table.delete(*table.get_children())
                    for d in self.usb_devices:
                        uid=d.get('UniqueDeviceID') or d.get('Identifier')
                        if uid:table.insert('','end',iid=uid,values=(d.get('DeviceName','iPhone'),uid))
            elif kind=='trusted':
                self.ios_states.setdefault(event[1],{})['trusted']=True
                self.log_ios(event[1]+'：USB 信任验证通过，已开启 Wi-Fi 连接');changed=True
            elif kind=='job_error':
                self.log_ios(event[1]+'：'+event[2]);self.ios_summary.set(event[1]+'：'+event[2][:160])
            elif kind=='worker':
                _,uid,generation,data=event
                state=self.ios_states.setdefault(uid,{})
                if state.get('generation')!=generation:continue
                if data['kind']=='heartbeat':continue
                if data['kind']=='tunnel_log':
                    self.log_ios(uid+'：'+data.get('text',''))
                    continue
                state.update({k:v for k,v in data.items() if k not in ('kind','udid')})
                if data.get('wifi')=='连接中':
                    for ip in self.devices:
                        if self.uid_for_ip(ip)==uid:self.row_messages.pop(ip,None)
                if data['kind'] in ('error','ended','closed'):
                    state.update(wifi='已断开',location='不可定位')
                if data['kind']=='error':self.log_ios(uid+'：'+data.get('error','连接异常'))
                elif data['kind']=='state':self.log_ios(uid+'：'+data.get('wifi',data.get('location','状态已更新')))
                changed=True
        now=time.monotonic()
        if self.ios_ready and self.auto_scan_var.get() and now-self.last_ios_scan>=IOS_AUTO_SCAN_SECONDS:
            self.last_ios_scan=now;self.ios.scan()
        with self.ios.lock:records=list(self.ios.workers.items())
        for uid,record in records:
            state=self.ios_states.get(uid,{})
            limit=65 if state.get('wifi')=='建立中' else 20
            if not record['stopping'] and now-record['last']>limit:
                self.ios.disconnect(uid)
                state.update(wifi='已断开',location='不可定位',error='工作进程通信超时，已停止')
                self.log_ios(uid+'：工作进程通信超时，正在回收');changed=True
        if changed:self.refresh_devices()
        self.after(150,self.poll_ios)

    def close(self):
        if self._closing:return
        self._closing=True;self.service_enabled=False
        try:self.save_ios();save_devices(self.devices)
        except Exception as exc:messagebox.showwarning('配置保存失败',str(exc),parent=self)
        self.status.set('正在停止全部设备工作进程并释放代理连接……')
        for callback in self.tk.call('after','info'):
            try:self.after_cancel(callback)
            except Exception:pass
        def work():
            try:
                self.ios.shutdown()
                _force_release_gateway(self.persistent_gateway)
                self.test_pool.shutdown(wait=False,cancel_futures=True)
            finally:self.shutdown_done.set()
        threading.Thread(target=work,daemon=True).start()
        self.wait_shutdown()

    def wait_shutdown(self):
        if self.shutdown_done.is_set():self.destroy()
        else:self.after(100,self.wait_shutdown)


def integrated_main():
    instance=SingleInstance()
    app=None
    code=0
    try:
        # Share legacy singleton to avoid two gateway applications competing for phone traffic.
        if not instance.acquire():
            if os.name=='nt':ctypes.windll.user32.MessageBoxW(None,'出口管理器或整合程序已运行，请先关闭旧程序。','程序运行中',0x40)
            return
        owned_job=OwnedProcessJob()
        configure_integrated_runtime()
        app=IntegratedApp()
        app.mainloop()
    except Exception as exc:
        code=1
        if os.name=='nt':ctypes.windll.user32.MessageBoxW(None,str(exc),'整合程序启动失败',0x10)
        else:raise
    finally:
        try: clear_direct_block_rules()
        except Exception: pass
        if app is not None:
            try:
                app.ios.shutdown()
                _hard_app_close(app)
            except Exception:code=1
        instance.close()
        for stream in (sys.stdout,sys.stderr):
            if stream:
                try:stream.flush()
                except Exception:pass
        os._exit(code)


class SimpleIOSController(IOSController):
    def connect(self, uid):
        with self.lock:
            if self.closing or uid in self.workers or 'trust:'+uid in self.busy:return False
            self.generation+=1
            generation=self.generation
            record=dict(generation=generation, process=None, owned=set(), last=time.monotonic(),
                        out=queue.Queue(maxsize=32), stopping=False, done=threading.Event())
            self.workers[uid]=record
            pmd=list(self.interpreter)+['-m','pymobiledevice3']
        def register(proc):
            with self.lock:
                record['owned']={p for p in record['owned'] if p.poll() is None}
                record['owned'].add(proc)
                cancelled=self.closing or record['stopping']
            if cancelled:stop_owned_process(proc)
        def emit(kind, **data):
            record['last']=time.monotonic()
            if kind=='error':record['error']=data.get('error','')
            self.events.put(('worker',uid,generation,dict(kind=kind,udid=uid,**data)))
        def work():
            try:
                if not record['stopping']:
                    run_ios_session(uid,pmd,record['out'],emit,register)
            except Exception as exc:emit('error',wifi='已断开',location='不可定位',error=str(exc))
            finally:
                with self.lock:owned=list(record['owned'])
                for proc in owned:
                    try:stop_owned_process(proc)
                    except Exception:pass
                with self.lock:
                    if self.workers.get(uid) is record:self.workers.pop(uid,None)
                emit('ended',wifi='已断开',location='不可定位',error=record.get('error',''))
                record['done'].set()
        record['thread']=threading.Thread(target=work,name='wifi-'+uid[-8:],daemon=True)
        record['thread'].start()
        return generation

    def disconnect(self, uid):
        with self.lock:
            record=self.workers.get(uid)
            if not record or record['stopping']:return
            record['stopping']=True
            # Cancellation supersedes any commands that have not yet started.
            while True:
                try:record['out'].get_nowait()
                except queue.Empty:break
            record['out'].put_nowait({'op':'shutdown'})
        def reap():
            if record['done'].wait(4):return
            with self.lock:owned=list(record['owned'])
            for proc in owned:
                try:stop_owned_process(proc)
                except Exception:pass
        threading.Thread(target=reap,daemon=True).start()

    def shutdown(self):
        with self.lock:
            self.closing=True
            records=list(self.workers.items())
            short=list(self.short_processes)
        for uid,_ in records:self.disconnect(uid)
        for proc in short:
            try:stop_owned_process(proc)
            except Exception:pass
        deadline=time.monotonic()+8
        for _,record in records:record['done'].wait(max(0,deadline-time.monotonic()))
        for _,record in records:
            with self.lock:owned=list(record['owned'])
            for proc in owned:
                try:stop_owned_process(proc)
                except Exception:pass

    def identity_scan(self):
        def work():
            value=json.loads(self.run(['bonjour','mobdev2','--timeout','5'],20))
            if isinstance(value,list):self.events.put(('network_identities',value))
        self.job('network-identities',work)



# ===== V2.6.0 离线授权 + 热点客户端直连阻断 =====
LICENSE_PUBLIC_KEY_B64 = "3Yd+Gi/fHsfF5i4Z7jzK1fVS7K1mWVt3EAH+MmnPkx0="
LICENSE_PREFIX = "QZL1"
LICENSE_FILE_NAME = "qizi_license.dat"

def qizi_license_file():
    # V2.9.28.1: license lives beside the main executable, matching all other config.
    return APP_DIR / LICENSE_FILE_NAME
DIRECT_BLOCK_GROUP = "Qizi Hotspot Direct Block"


def _license_b64d(text):
    return base64.urlsafe_b64decode(str(text) + "=" * (-len(str(text)) % 4))


def qizi_machine_code():
    """稳定机器码：优先 Windows MachineGuid，失败时退回主板 UUID/计算机名。"""
    raw = ""
    if os.name == "nt":
        try:
            import winreg
            access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography", 0, access) as key:
                raw = str(winreg.QueryValueEx(key, "MachineGuid")[0]).strip()
        except Exception:
            pass
    if not raw:
        try:
            raw = powershell("(Get-CimInstance Win32_ComputerSystemProduct).UUID", timeout=8).strip()
        except Exception:
            raw = os.environ.get("COMPUTERNAME", "UNKNOWN-PC")
    digest = hashlib.sha256(("QIZI-LICENSE-V1|" + raw).encode("utf-8", "ignore")).hexdigest().upper()[:32]
    return "QZ-" + "-".join(digest[i:i+4] for i in range(0, 32, 4))


def verify_qizi_license(code, machine=None):
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise RuntimeError("授权组件缺失：请安装 cryptography 后重新打包") from exc
    parts = str(code or "").strip().split(".")
    if len(parts) != 3 or parts[0] != LICENSE_PREFIX:
        raise ValueError("激活码格式不正确")
    raw = _license_b64d(parts[1]); sig = _license_b64d(parts[2])
    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(LICENSE_PUBLIC_KEY_B64))
    pub.verify(sig, raw)
    data = json.loads(raw.decode("utf-8"))
    machine = machine or qizi_machine_code()
    if str(data.get("machine", "")).upper() != machine.upper():
        raise ValueError("此激活码不属于当前电脑")
    expires = int(data.get("expires", 0) or 0)
    if expires and int(time.time()) >= expires:
        raise ValueError("授权已到期")
    return data


def load_qizi_license():
    candidates=[qizi_license_file(),
                Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "QiziIOSIntegrated" / LICENSE_FILE_NAME]
    for candidate in candidates:
        try:
            code=candidate.read_text(encoding="utf-8").strip()
            data=verify_qizi_license(code)
            if candidate != qizi_license_file():
                try: save_qizi_license(code)
                except Exception: pass
            return code,data
        except Exception:
            continue
    return "", None


def save_qizi_license(code):
    license_file = qizi_license_file()
    license_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = license_file.with_suffix(".tmp")
    tmp.write_text(str(code).strip(), encoding="utf-8")
    os.replace(tmp, license_file)


def _direct_block_rule_name(ip):
    return "Qizi Direct Block " + str(ip).replace(":", "_").replace("%", "_")


def apply_direct_block_ips(ips):
    """阻止热点客户端以自身源 IPv4 直接经 ICS 出网；访问本机代理端口属于入站，不受此规则影响。"""
    if os.name != "nt":
        return
    clean=[]
    for ip in ips:
        try:
            addr=ipaddress.ip_address(str(ip))
            if addr.version==4 and addr.is_private:
                clean.append(str(addr))
        except ValueError:
            pass
    # 用一个 PowerShell 调用同步整组规则，避免扫描时重复创建。
    quoted=",".join("'"+x.replace("'","''")+"'" for x in sorted(set(clean)))
    script=f"""$ErrorActionPreference='Stop'
$group='{DIRECT_BLOCK_GROUP}'
$want=@({quoted})
$existing=Get-NetFirewallRule -Group $group -ErrorAction SilentlyContinue
foreach($r in @($existing)){{
  $af=$r | Get-NetFirewallAddressFilter
  $lip=[string]$af.LocalAddress
  if($want -notcontains $lip){{Remove-NetFirewallRule -Name $r.Name -ErrorAction SilentlyContinue}}
}}
foreach($ip in $want){{
  $name='Qizi Direct Block '+$ip.Replace(':','_').Replace('%','_')
  if(-not (Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue)){{
    New-NetFirewallRule -Name $name -DisplayName $name -Group $group -Direction Outbound -Action Block -Profile Any -LocalAddress $ip -Protocol Any | Out-Null
  }}
}}"""
    powershell(script, timeout=20)


def clear_direct_block_rules():
    if os.name != "nt": return
    try:
        powershell(f"Get-NetFirewallRule -Group '{DIRECT_BLOCK_GROUP}' -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue", timeout=12)
    except Exception:
        pass

class SimpleApp(IntegratedApp):
    # V2.5.2：基于 V2.5，仅调整定位状态判定与界面日志。
    # 规则：Tunnel 连着就持续发送；定位子进程退出码不再作为手机定位成功/失败的回执。
    # 只有 Wi-Fi Tunnel 真正断开/人工停止时，主界面才退出“定位中”。
    # 新设备坐标默认留空；已保存坐标的设备在 Wi-Fi Tunnel 成功后自动恢复定位。
    BLUE='#1565c0';GREEN='#16803a';RED='#c62828';GRAY='#777777'
    WIDTHS=(128,178,72,85,102,102,82,92)
    OP_LOG_LIMIT=200
    LOCATION_NETWORK_DELAY=10.0
    ROUTE_NETWORK_BLOCK_DELAY=60.0

    def __init__(self):
        # Route/health helpers are initialized before base constructors because inherited
        # startup callbacks dynamically dispatch to the final class methods.
        self.exit_health_scheduler=ExitHealthScheduler(normal_interval=90,offline_interval=60,batch_size=3,start_spacing=1.5)
        self._manual_health_queue=deque()
        self.route_plan_file=DATA_DIR / 'qizi_route_plans.json'
        self.route_state_file=DATA_DIR / 'qizi_route_state.json'
        self.route_plans=load_json_dict(self.route_plan_file)
        self.route_states=load_json_dict(self.route_state_file)
        self.route_runtime={}
        self.route_dialogs={}
        self.route_events=queue.Queue()
        self._location_network_started={}
        self._location_network_allowed=set()
        self._location_network_phase={}
        self._last_exit_index=None
        self.row_widgets={}
        self.selected_ip=None
        self.pending_actions={}
        self.network_identities={}
        self.trust_targets={}
        self.row_messages={}
        self.last_identity_scan=0
        self.identity_at=0
        self._edit_save_job=None
        self._device_online_snapshot={}
        self._scan_was_running=False
        self._last_operation_text=''
        self._last_operation_at=0.0
        self.operation_log_file=APP_DIR / 'qizi_operation.log'
        self.operation_logs=deque(maxlen=self.OP_LOG_LIMIT)
        try:
            if self.operation_log_file.exists():
                previous=self.operation_log_file.read_text(encoding='utf-8',errors='replace').splitlines()
                self.operation_logs.extend(previous[-self.OP_LOG_LIMIT:])
        except OSError:
            pass
        # Constructors dispatch to our simple build_ui, refresh and poll methods.
        super().__init__()
        # V2.3 auto restore: freeze only associations that already existed when the
        # program started. resolve_discovery() may create links for newly seen phones,
        # but those must not silently become startup auto-connect candidates.
        self._startup_saved_links=dict(self.links)
        self._auto_restore_uids={str(uid) for uid in self._startup_saved_links.values() if uid}
        self._auto_attempted_uids=set()
        self._last_discovered_uids=set()
        self.ios=SimpleIOSController(self.ios.interpreter)
        self.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei')
        self.geometry('1080x620');self.minsize(980,500)
        self.after(250,self._poll_scan_status)
        self.machine_code=qizi_machine_code()
        self.license_code,self.license_data=load_qizi_license()
        self._license_last_check=0.0
        self._direct_block_ips=set()
        self.after(500,self._license_periodic_check)
        self.after(250,self._poll_route_events)
        self.after(1200,self._resume_saved_routes)
        self.log_operation('程序已启动：授权有效；同Wi-Fi局域网代理控制已启用；自动寻路模块已加载。',dedupe=False)

    def build_ui(self):
        self.style=ttk.Style(self)
        if 'vista' in self.style.theme_names():self.style.theme_use('vista')
        self.style.configure('TButton',padding=(4,1),font=('Microsoft YaHei UI',9))
        self.style.configure('Exit.Treeview',rowheight=24,font=('Microsoft YaHei UI',9))
        for name,color in [('Good',self.GREEN),('Bad',self.RED),('Neutral',self.GRAY)]:
            self.style.configure(name+'.TCombobox',foreground=color)
            self.style.map(name+'.TCombobox',foreground=[('readonly',color),('focus',color)],fieldbackground=[('readonly','white'),('focus','white')],selectforeground=[('readonly',color)],selectbackground=[('readonly','white')])
        self.status=tk.StringVar(value='代理准备中')
        self.hotspot_info=tk.StringVar(value='热点扫描准备中')
        self.gateway_info=tk.StringVar(value='LAN控制：禁止网络 / 本地出口 / 指定出口（指定出口定位10秒后放行）')
        # detail 继续保留给旧代理引擎写状态，但不再直接显示，避免自动扫描覆盖操作记录。
        self.detail=tk.StringVar(value='已配置设备：启动后自动恢复 Wi-Fi 与定位；新设备坐标保持空白，录入后自动启用。')
        self.deployment_status=tk.StringVar();self.ios_summary=tk.StringVar(value='正在检查定位环境…')
        self.summary=tk.StringVar(value='设备')
        self.license_status=tk.StringVar(value='授权：检查中')
        self.scan_status=tk.StringVar(value='扫描：准备中')
        self.lon_var=tk.StringVar();self.lat_var=tk.StringVar();self.interval_var=tk.StringVar(value='1')
        self.auto_scan_var=tk.BooleanVar(value=False)
        container=ttk.Frame(self,padding=6);container.pack(fill='both',expand=True)
        top=ttk.Frame(container);top.pack(fill='x',pady=(0,5))
        ttk.Label(top,text='设备出口 / 定位',font=('Microsoft YaHei UI',11,'bold')).pack(side='left',padx=(0,15))
        ttk.Label(top,textvariable=self.license_status,foreground=self.BLUE).pack(side='left',padx=(0,10))
        ttk.Button(top,text='刷新设备',command=self.scan_all).pack(side='left',padx=2)
        ttk.Button(top,text='添加设备',command=self.add_manual_device).pack(side='left',padx=2)
        ttk.Checkbutton(top,text='自动扫描',variable=self.auto_scan_var).pack(side='left',padx=(6,2))
        ttk.Label(top,textvariable=self.scan_status,foreground=self.GRAY).pack(side='left',padx=(2,8))
        more=ttk.Menubutton(top,text='更多');more.pack(side='right')
        menu=tk.Menu(more,tearoff=False);more['menu']=menu
        for label,command in [('授权/续期',self.show_license_dialog),('详细日志',self.show_ios_log),('定位运行环境',self.environment_dialog),
            ('修复防火墙',self.repair_firewall),('网络自检',self.run_self_check),('自检报告',self.show_report),
            ('浏览器诊断',self.browser_security_diagnostic),('刷新代理服务',self.start_selected),
            ('停止代理服务',self.stop)]:menu.add_command(label=label,command=command)
        ttk.Label(top,textvariable=self.summary).pack(side='right',padx=12)

        main=ttk.Panedwindow(container,orient='horizontal');main.pack(fill='both',expand=True)
        left=ttk.Frame(main);right=ttk.Frame(main,width=215)
        main.add(left,weight=1);main.add(right,weight=0)
        self.canvas=tk.Canvas(left,background='white',highlightthickness=1,highlightbackground='#cccccc')
        ys=ttk.Scrollbar(left,orient='vertical',command=self.canvas.yview)
        xs=ttk.Scrollbar(left,orient='horizontal',command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=ys.set,xscrollcommand=xs.set)
        self.canvas.grid(row=0,column=0,sticky='nsew');ys.grid(row=0,column=1,sticky='ns');xs.grid(row=1,column=0,sticky='ew')
        left.columnconfigure(0,weight=1);left.rowconfigure(0,weight=1)
        self.rows_frame=tk.Frame(self.canvas,bg='white')
        self.canvas.create_window((0,0),window=self.rows_frame,anchor='nw')
        self.rows_frame.bind('<Configure>',lambda e:self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        header=tk.Frame(self.rows_frame,bg='#eeeeee',height=27,width=sum(self.WIDTHS));header.pack(fill='x');header.pack_propagate(False)
        for title,width in zip(('设备IP','出口','信任','Wi-Fi状态','纬度','经度','定位','自动寻路'),self.WIDTHS):
            cell=tk.Frame(header,bg='#eeeeee',width=width);cell.pack(side='left',fill='y');cell.pack_propagate(False)
            tk.Label(cell,text=title,bg='#eeeeee',font=('Microsoft YaHei UI',9,'bold')).pack(expand=True)
        def scroll(e):
            if e.delta:self.canvas.yview_scroll(-1 if e.delta>0 else 1,'units')
        self.canvas.bind('<MouseWheel>',scroll)
        self.rows_frame.bind('<MouseWheel>',scroll)

        ttk.Label(right,text='出口管理',font=('Microsoft YaHei UI',10,'bold')).pack(anchor='w',padx=4)
        toolbar=ttk.Frame(right);toolbar.pack(fill='x',padx=2,pady=4)
        for name,command in [('添加',self.add_exit),('修改',self.edit_exit),('删除',self.delete_exit),('检测',self.test_all_manual)]:
            ttk.Button(toolbar,text=name,width=4,command=command).pack(side='left',padx=1)
        wrapper=ttk.Frame(right);wrapper.pack(fill='both',expand=True,padx=2)
        self.exit_table=ttk.Treeview(wrapper,columns=('name','host','port','country','public_ip','status'),
            displaycolumns=('name',),show='headings',style='Exit.Treeview',selectmode='browse')
        self.exit_table.heading('name',text='国家 + IP')
        self.exit_table.column('name',width=188,minwidth=145)
        sy=ttk.Scrollbar(wrapper,orient='vertical',command=self.exit_table.yview)
        self.exit_table.configure(yscrollcommand=sy.set)
        sy.pack(side='right',fill='y');self.exit_table.pack(fill='both',expand=True)
        self.exit_table.tag_configure('exit_online',foreground=self.GREEN)
        self.exit_table.tag_configure('exit_offline',foreground=self.RED)
        self.exit_table.tag_configure('exit_pending',foreground=self.GRAY)
        self.exit_table.bind('<Double-1>',lambda e:self.edit_exit())
        self.exit_table.bind('<Button-3>', self._exit_context_menu)
        # V2.5.7: 单击仅短暂选择，随后自动取消蓝色高亮，让红/绿状态色始终可见。
        self.exit_table.bind('<<TreeviewSelect>>', self._schedule_exit_deselect)
        self.exit_summary=tk.StringVar(value='绿：正常  红：断线')
        ttk.Label(right,textvariable=self.exit_summary).pack(anchor='w',padx=4,pady=4)

        ttk.Label(container,textvariable=self.gateway_info).pack(fill='x',pady=(4,2))
        log_box=ttk.LabelFrame(container,text='操作记录（仅执行/状态变化｜最近200条写入 qizi_operation.log）',padding=(5,3))
        log_box.pack(fill='x',pady=(2,0))
        log_inner=ttk.Frame(log_box);log_inner.pack(fill='x')
        self.operation_text=tk.Text(log_inner,height=6,wrap='none',font=('Consolas',9),state='disabled',relief='flat')
        log_scroll=ttk.Scrollbar(log_inner,orient='vertical',command=self.operation_text.yview)
        self.operation_text.configure(yscrollcommand=log_scroll.set)
        self.operation_text.pack(side='left',fill='x',expand=True);log_scroll.pack(side='right',fill='y')
        self._refresh_operation_log_widget()

        # Engine-only compatibility widgets; these are not displayed to the user.
        self.device_table=ttk.Treeview(self,columns=('ip','mac','interface','remark','online','exit'))
        self.exit_combo=ttk.Combobox(self,state='readonly')
        self.start_btn=ttk.Button(self);self.stop_btn=ttk.Button(self);self.gateway_btn=ttk.Button(self)

    def _license_valid(self):
        try:
            if not self.license_code:
                return False
            self.license_data=verify_qizi_license(self.license_code,self.machine_code)
            return True
        except Exception:
            self.license_data=None
            return False

    def _update_license_status(self):
        if not self._license_valid():
            self.license_status.set('授权：未激活/已到期')
            return
        exp=int(self.license_data.get('expires',0) or 0)
        if not exp:
            self.license_status.set('授权：永久')
        else:
            self.license_status.set('授权至 '+time.strftime('%Y-%m-%d',time.localtime(exp)))

    def _license_periodic_check(self):
        if getattr(self,'_closing',False): return
        # V2.6.1 硬锁：运行期间授权一旦失效，立即结束主程序。
        # 这样旧配置恢复、自动扫描、Tunnel、定位和出口都不能继续在后台运行。
        if not self._license_valid():
            try: self.auto_scan_var.set(False)
            except Exception: pass
            try: self.log_operation('授权已失效，程序将停止运行。',dedupe=False)
            except Exception: pass
            messagebox.showwarning('授权已到期','当前授权已失效。\n\n程序将停止运行，请重新激活后再启动。',parent=self)
            self.after(50,self.close)
            return
        self._update_license_status()
        self.after(60000,self._license_periodic_check)

    def show_license_dialog(self):
        win=tk.Toplevel(self);win.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei');win.geometry('760x430');win.transient(self)
        box=ttk.Frame(win,padding=14);box.pack(fill='both',expand=True)
        ttk.Label(box,text='本机机器码').pack(anchor='w')
        row=ttk.Frame(box);row.pack(fill='x',pady=(4,10))
        mv=tk.StringVar(value=self.machine_code)
        ttk.Entry(row,textvariable=mv,state='readonly',font=('Consolas',10)).pack(side='left',fill='x',expand=True)
        def copy_machine():
            self.clipboard_clear();self.clipboard_append(self.machine_code)
        ttk.Button(row,text='复制机器码',command=copy_machine).pack(side='left',padx=(8,0))
        ttk.Label(box,text='激活码 / 续期码').pack(anchor='w')
        txt=tk.Text(box,height=10,wrap='word',font=('Consolas',9));txt.pack(fill='both',expand=True,pady=(4,8))
        status=tk.StringVar(value=self.license_status.get());ttk.Label(box,textvariable=status).pack(anchor='w')
        def activate():
            code=txt.get('1.0','end').strip()
            if not code:return messagebox.showinfo('授权','请输入激活码。',parent=win)
            try:
                data=verify_qizi_license(code,self.machine_code)
                save_qizi_license(code);self.license_code=code;self.license_data=data
                self._update_license_status();self.refresh_devices()
                status.set(self.license_status.get());messagebox.showinfo('授权','激活/续期成功。',parent=win)
            except Exception as exc:
                messagebox.showerror('授权失败',str(exc),parent=win)
        ttk.Button(box,text='激活 / 续期',command=activate).pack(anchor='e',pady=(8,0))

    def _sync_direct_source_guard(self):
        """在线/已记录热点设备一律禁止绕过本机代理直接走 ICS。"""
        wanted={ip for ip,d in self.devices.items() if d.get('online')}
        if wanted==getattr(self,'_direct_block_ips',set()): return
        try:
            apply_direct_block_ips(wanted)
            self._direct_block_ips=set(wanted)
            self.gateway_info.set(f'直连保护：已阻断 {len(wanted)} 台热点设备绕过代理；代理出口按定位联锁放行')
        except Exception as exc:
            self.log_operation('直连保护规则更新失败：'+str(exc))

    def _refresh_operation_log_widget(self):
        widget=getattr(self,'operation_text',None)
        if widget is None or not widget.winfo_exists():return
        widget.configure(state='normal');widget.delete('1.0','end')
        if self.operation_logs:widget.insert('end','\n'.join(self.operation_logs))
        widget.configure(state='disabled');widget.see('end')

    def _persist_operation_log(self):
        try:
            tmp=self.operation_log_file.with_suffix(self.operation_log_file.suffix+'.tmp')
            tmp.write_text(('\n'.join(self.operation_logs)+'\n') if self.operation_logs else '',encoding='utf-8')
            os.replace(tmp,self.operation_log_file)
        except OSError:
            # 日志失败不能影响主程序操作。
            pass

    def log_operation(self,text,dedupe=True):
        text=re.sub(r'\s+',' ',str(text or '')).strip()
        if not text:return
        now=time.monotonic()
        if dedupe and text==self._last_operation_text and now-self._last_operation_at<2.0:return
        self._last_operation_text=text;self._last_operation_at=now
        self.operation_logs.append(time.strftime('%Y-%m-%d %H:%M:%S')+'  '+text)
        self._persist_operation_log();self._refresh_operation_log_widget()

    def _append_ios_detail(self,text):
        self.ios_logs.append(time.strftime('%H:%M:%S')+' '+str(text))

    def log_ios(self,text):
        # 人工操作/结果进入底部操作记录；Tunnel逐行输出只进“详细日志”。
        self._append_ios_detail(text)
        self.log_operation(text)

    def _ip_for_uid(self,uid):
        for ip in list(self.devices):
            try:
                if self.uid_for_ip(ip)==uid:return ip
            except Exception:
                pass
        return uid[-8:] if uid else '未知设备'

    def _startup_pair_matches(self,ip,uid):
        """Only auto-connect identities already saved before this program launch."""
        if not uid or uid not in self._auto_restore_uids:
            return False
        d=self.devices.get(ip,{})
        mac=normalize_mac(d.get('mac',''))
        if valid_device_mac(mac):
            return self._startup_saved_links.get('mac:'+mac)==uid
        # Legacy/IP-only records are accepted only for the same current IPv4.
        return self._startup_saved_links.get('ip:'+ip)==uid

    def _maybe_auto_restore_wifi(self):
        """ios_v2-style one-shot startup restore; never spin in a reconnect loop."""
        if not self.ios_ready or not self.discovered:
            return
        for uid,data in list(self.discovered.items()):
            uid=str(uid or '')
            if not uid or uid in self._auto_attempted_uids:
                continue
            ip=self.discovery_ip(uid,data)
            try:
                if ipaddress.ip_address(ip).version!=4:
                    continue
            except (ValueError,TypeError):
                continue
            if not self._startup_pair_matches(ip,uid):
                continue
            state=self.ios_states.setdefault(uid,{})
            if state.get('developer_ready') is False:
                continue
            if state.get('wifi') in ('建立中','连接中') or uid in self.ios.workers:
                continue
            generation=self.ios.connect(uid)
            if not generation:
                continue
            # Mark only after the worker was actually accepted. If it later fails,
            # this online cycle does not retry forever; a real disappearance/rejoin
            # clears the marker below in poll_ios().
            self._auto_attempted_uids.add(uid)
            state.update(generation=generation,wifi='建立中',location='未开启',error='')
            self.row_messages.pop(ip,None)
            self.log_operation(ip+'：已识别上次连接记录，正在自动建立 Wi-Fi 连接…')

    def scan_devices(self):
        # 不再把自动扫描提示写进 detail/操作记录，避免每5秒遮盖真正的操作结果。
        if self.scanning:return
        self.scanning=True
        if hasattr(self,'scan_status'):self.scan_status.set('扫描：进行中…')
        threading.Thread(target=self.scan_worker,daemon=True).start()

    def _poll_scan_status(self):
        if self._closing:return
        running=bool(self.scanning)
        if running:
            self.scan_status.set('扫描：进行中…')
        elif self._scan_was_running:
            info=self.hotspot_info.get()
            if info.startswith('热点扫描失败'):
                self.scan_status.set('扫描：失败 '+time.strftime('%H:%M:%S'))
            else:
                online=sum(bool(d.get('online')) for d in self.devices.values())
                self.scan_status.set(f'扫描：{time.strftime("%H:%M:%S")} · 在线{online}')
        self._scan_was_running=running
        self.after(250,self._poll_scan_status)

    def selected_device_ip(self):return self.selected_ip

    def choose_row(self,ip):
        self.selected_ip=ip
        if self.device_table.exists(ip):self.device_table.selection_set(ip)
        uid=self.uid_for_ip(ip)
        state=self.ios_states.get(uid,{})
        error=self.row_messages.get(ip) or state.get('error','')
        self.detail.set((ip+'：'+error) if error else f'{ip}：直接选择出口、信任、连接，再开启定位。右键有断开和恢复定位。')

    def choice_values(self):
        ids=[''];labels=['未绑定']
        raw=[self.exit_label(p) for p in self.exits]
        for p,label in zip(self.exits,raw):
            ids.append(p['id'])
            labels.append(label if raw.count(label)==1 else label+' ['+p['id'][:5]+']')
        return ids,labels

    def new_row(self,ip):
        index=len(self.row_widgets);bg='white' if index%2==0 else '#f8f8f8'
        frame=tk.Frame(self.rows_frame,bg=bg,height=31,width=sum(self.WIDTHS),highlightthickness=0)
        frame.pack(fill='x');frame.pack_propagate(False)
        cells=[]
        for width in self.WIDTHS:
            c=tk.Frame(frame,bg=bg,width=width);c.pack(side='left',fill='y');c.pack_propagate(False);cells.append(c)
        label=tk.Label(cells[0],text=ip,bg=bg,anchor='w',font=('Consolas',9,'bold'));label.pack(fill='both',expand=True,padx=4)
        combo=ttk.Combobox(cells[1],state='readonly',width=21,font=('Microsoft YaHei UI',9));combo.pack(fill='x',padx=3,pady=4)
        def action_button(cell,text,command):
            b=tk.Button(cell,text=text,command=command,fg=self.BLUE,bg=bg,relief='groove',bd=1,
                padx=3,pady=0,font=('Microsoft YaHei UI',9),cursor='hand2',activeforeground=self.BLUE)
            b.pack(fill='x',padx=4,pady=4);return b
        trust=action_button(cells[2],'信任',lambda:self.trust_row(ip))
        wifi=action_button(cells[3],'连接',lambda:self.connect_row(ip))
        # 纬度在左、经度在右；新设备坐标默认留空。
        lat=tk.StringVar(value='');lon=tk.StringVar(value='')
        entries=[]
        for cell,var in [(cells[4],lat),(cells[5],lon)]:
            entry=ttk.Entry(cell,textvariable=var,justify='center',font=('Consolas',9));entry.pack(fill='x',padx=3,pady=4)
            entry.bind('<FocusOut>',lambda e,ip=ip:self.commit_row(ip,quiet=True,auto_start=True));entries.append(entry)
        locate=action_button(cells[6],'开启',lambda:self.locate_row(ip))
        route=action_button(cells[7],'自动寻路',lambda:self.open_route_dialog(ip))
        row=dict(frame=frame,ip=label,combo=combo,trust=trust,wifi=wifi,locate=locate,route=route,lon=lon,lat=lat,
                 entries=entries,key=None,ids=[],labels=[],menu_open=False)
        combo.configure(postcommand=lambda:self.prepare_choices(ip))
        # V2.9.29.1: readonly Combobox must never change selection from the mouse wheel.
        # Exit changes are explicit clicks only.
        combo.bind('<MouseWheel>',lambda e:'break')
        combo.bind('<Button-4>',lambda e:'break')
        combo.bind('<Button-5>',lambda e:'break')
        combo.bind('<<ComboboxSelected>>',lambda e:self.bind_row_exit(ip))
        # V2.5.7: 下拉选择完成后移走焦点，避免 readonly Combobox 长期蓝底/白字。
        combo.bind('<<ComboboxSelected>>',lambda e,w=combo:self.after(80,lambda:self._release_combo_focus(w)),add='+')
        combo.bind('<Escape>',lambda e:row.update(menu_open=False))
        combo.bind('<FocusOut>',lambda e:row.update(menu_open=False))
        self.row_widgets[ip]=row
        for widget in [frame,label,combo,trust,wifi,locate,route,*entries]:
            widget.bind('<Button-1>',lambda e,ip=ip:self.choose_row(ip),add='+')
            widget.bind('<Button-3>',lambda e,ip=ip:self.row_menu(e,ip))
        self.prepare_choices(ip);row['menu_open']=False
        return row

    def test_all(self):
        """Background health sweep: stagger only a small due batch per tick."""
        for idx in self.exit_health_scheduler.due_indices(self.exits):
            self.start_direct_test(idx)

    def test_all_manual(self):
        """Manual button eventually checks every exit, but never bursts hundreds at once."""
        self._manual_health_queue=deque(range(len(self.exits)))
        self._pump_manual_exit_tests()

    def _pump_manual_exit_tests(self):
        if getattr(self,'_closing',False):return
        active=len(getattr(self,'test_jobs',{}))
        capacity=max(0,4-active)
        while capacity and self._manual_health_queue:
            idx=self._manual_health_queue.popleft()
            if 0<=idx<len(self.exits):
                self.exit_health_scheduler.mark_now(self.exits,idx,delay=90)
                self.start_direct_test(idx);capacity-=1
        if self._manual_health_queue:
            self.after(500,self._pump_manual_exit_tests)

    def _schedule_exit_deselect(self,event=None):
        table=getattr(self,'exit_table',None)
        if table is None:return
        selected=table.selection()
        if selected:
            try:self._last_exit_index=int(selected[0])
            except (TypeError,ValueError):pass
        self.after(140,self._clear_exit_selection)

    def selected_exit_index(self):
        table=getattr(self,'exit_table',None)
        if table is not None:
            selected=table.selection()
            if selected:
                try:
                    self._last_exit_index=int(selected[0])
                    return self._last_exit_index
                except (TypeError,ValueError):
                    pass
        idx=getattr(self,'_last_exit_index',None)
        return idx if isinstance(idx,int) and 0<=idx<len(self.exits) else None

    def _clear_exit_selection(self):
        table=getattr(self,'exit_table',None)
        if table is None or not table.winfo_exists():return
        selected=table.selection()
        if selected:table.selection_remove(*selected)
        table.focus('')

    def _release_combo_focus(self,combo):
        if combo is None or not combo.winfo_exists():return
        try:combo.selection_clear()
        except tk.TclError:pass
        # 焦点回主窗口后，Combobox 立即按 Good/Bad 样式恢复绿/红字体。
        self.focus_set()
        self.after_idle(self.refresh_devices)

    def prepare_choices(self,ip):
        row=self.row_widgets.get(ip)
        if not row:return
        ids,labels=self.choice_values();row['ids']=ids;row['labels']=labels;row['menu_open']=True
        row['combo']['values']=labels
        eid=self.devices.get(ip,{}).get('exit_id','')
        row['combo'].set(labels[ids.index(eid)] if eid in ids else '未绑定')

    def bind_row_exit(self,ip):
        row=self.row_widgets[ip];index=row['combo'].current();row['menu_open']=False
        if index<0 or index>=len(row['ids']):return
        eid=row['ids'][index]
        if eid and not any(p['id']==eid for p in self.exits):return self.refresh_devices()
        with self.device_lock:self.devices[ip]['exit_id']=eid
        save_devices(self.devices);self._update_live_routes();self.refresh_devices()
        label=self.exit_name_by_id(eid) or '未绑定'
        self.detail.set(ip+'：出口已切换。');self.log_operation(f'{ip}：出口切换为 {label}')

    def _exit_context_menu(self,event):
        row=self.exit_table.identify_row(event.y) if hasattr(self,'exit_table') else ''
        if row:
            self.exit_table.selection_set(row);self.exit_table.focus(row)
        menu=tk.Menu(self,tearoff=False)
        menu.add_command(label='修改此出口',command=self.edit_exit,state='normal' if row else 'disabled')
        menu.add_command(label='重新检测此出口',command=self.test_selected_direct,state='normal' if row else 'disabled')
        menu.add_separator()
        menu.add_command(label='删除此出口',command=self.delete_exit,state='normal' if row else 'disabled')
        menu.tk_popup(event.x_root,event.y_root)

    def exit_label(self,p):
        if not p:return '未绑定'
        return f"{country_with_chinese(p.get('country'))} {p.get('public_ip') or p.get('host','')}"

    def refresh_exits(self,selected=None):
        if not hasattr(self,'exit_table'):return
        V10ProxyManager.refresh_exits(self,selected)
        good=bad=pending=0
        for i,p in enumerate(self.exits):
            status=str(p.get('status',''))
            if self._exit_is_online(p):
                tag='exit_online';shown='在线';good+=1
            elif status.startswith('断线'):
                tag='exit_offline';shown='断线';bad+=1
            else:
                tag='exit_pending';shown=status if status.startswith('待确认') else '待检测';pending+=1
            self.exit_table.item(str(i),values=(self.exit_label(p),p.get('host',''),p.get('port',''),
                p.get('country',''),p.get('public_ip',''),shown),tags=(tag,))
        suffix=f'  ·  确认中 {pending}' if pending else ''
        self.exit_summary.set(f'出口 {len(self.exits)}  ·  正常 {good}  ·  异常 {bad}'+suffix)
        self.refresh_devices()

    def refresh_devices(self,selected_ip=None):
        if not hasattr(self,'rows_frame'):return
        if selected_ip:self.selected_ip=selected_ip
        for ip in set(self.row_widgets)-set(self.devices):
            self.row_widgets.pop(ip)['frame'].destroy()
            self._device_online_snapshot.pop(ip,None)
            if self.device_table.exists(ip):self.device_table.delete(ip)
        for ip,d in self.devices.items():
            online=bool(d.get('online'))
            previous=self._device_online_snapshot.get(ip)
            if previous is not None and previous!=online:
                self.log_operation(f'{ip}：设备'+('已上线' if online else '已离线'))
            elif previous is None and online:
                self.log_operation(f'{ip}：设备已上线')
            self._device_online_snapshot[ip]=online
            row=self.row_widgets.get(ip) or self.new_row(ip)
            if not self.device_table.exists(ip):self.device_table.insert('','end',iid=ip,values=(ip,))
            uid=self.uid_for_ip(ip);key=uid or 'ip:'+ip;state=self.ios_states.get(uid,{})
            if row['key']!=key:
                coord=self.coordinates.get(key,self.coordinates.get(row['key'],{}))
                row['lon'].set(str(coord.get('lon','')) if isinstance(coord,dict) else '')
                row['lat'].set(str(coord.get('lat','')) if isinstance(coord,dict) else '')
                row['key']=key
            row['ip'].configure(fg=self.GREEN if online else self.RED)
            p=next((p for p in self.exits if p['id']==d.get('exit_id')),None)
            if p:
                pstatus=str(p.get('status',''))
                exit_style='Good' if self._exit_is_online(p) else ('Bad' if pstatus.startswith('断线') else 'Neutral')
                row['combo'].configure(style=exit_style+'.TCombobox')
            else:
                row['combo'].configure(style='Neutral.TCombobox')
            if not row['menu_open']:
                ids,labels=self.choice_values();row['ids']=ids;row['labels']=labels;row['combo']['values']=labels
                eid=d.get('exit_id','');row['combo'].set(labels[ids.index(eid)] if eid in ids else '未绑定')
            trust_busy=bool(uid and 'trust:'+uid in self.ios.busy) or ip in self.trust_targets
            trusted=bool(state.get('trusted'))
            developer_blocked=(state.get('developer_ready') is False)
            if trusted and developer_blocked:
                trust_text='开发模式'
                trust_color=self.BLUE
                trust_state='disabled' if trust_busy else 'normal'
            else:
                trust_text='已信任' if trusted else ('信任中' if trust_busy else '信任')
                trust_color=self.GREEN if trusted else self.BLUE
                trust_state='disabled' if trusted or trust_busy else 'normal'
            row['trust'].configure(text=trust_text,fg=trust_color,state=trust_state,
                disabledforeground=trust_color)
            wifi=state.get('wifi','未连接');connected=wifi=='连接中'
            row['wifi'].configure(text='连接中' if connected else ('连接中…' if wifi=='建立中' else ('已断开' if wifi in ('已断开','断开中') else '连接')),
                fg=self.GREEN if connected else (self.RED if wifi=='已断开' else self.BLUE),
                state='disabled' if wifi in ('建立中','断开中') else 'normal')
            location=state.get('location','未开启');running=location=='定位中'
            preparing=location=='准备发送';recovering=location=='正在恢复'
            waiting=location in ('正在停止','正在清除')
            if running and connected:
                locate_text='定位中'
            elif preparing and connected:
                locate_text='确认中'
            elif recovering and connected:
                locate_text='恢复中'
            elif waiting and connected:
                locate_text='处理中'
            else:
                locate_text='开启'
            route_running=self._route_is_running(uid)
            if route_running:
                row['locate'].configure(text='寻路接管',fg=self.GREEN,state='disabled')
            else:
                row['locate'].configure(text=locate_text,
                    fg=self.GREEN if running and connected else self.BLUE,
                    state='normal' if connected and not waiting else 'disabled')
            route_enabled=bool(trusted and connected)
            row['route'].configure(text='寻路中' if route_running else '自动寻路',
                fg=self.GREEN if route_running else self.BLUE,
                state='normal' if route_enabled else 'disabled')
            # While route playback owns the device, the main exit selector is locked.
            # After 60 seconds the route controller switches it to 禁止网络 itself.
            row['combo'].configure(state='disabled' if route_running else 'readonly')
            lock_entries=route_running or running or preparing or recovering or waiting
            for entry in row['entries']:entry.configure(state='disabled' if lock_entries else 'normal')
        self.summary.set(f"设备 {len(self.devices)}  ·  在线 {sum(bool(d.get('online')) for d in self.devices.values())}")
        self._update_license_status()
        # V2.6.1：主程序仅在授权预检通过后创建，不再采用局部功能解锁。
        self._sync_direct_source_guard()

    def _location_gate_uid_for_mac(self,mac):
        mac=normalize_mac(mac)
        if valid_device_mac(mac):
            return self.links.get('mac:'+mac)
        return None

    def _location_gate_is_allowed_uid(self,uid,now=None):
        if not uid:return False
        state=self.ios_states.get(uid,{})
        location_ok=(state.get('location')=='定位中' or self._route_is_running(uid))
        if state.get('wifi')!='连接中' or not location_ok:
            return False
        started=self._location_network_started.get(uid)
        if started is None:return False
        return (time.monotonic() if now is None else now)-started >= self.LOCATION_NETWORK_DELAY

    def _location_gate_is_allowed_ip(self,ip,now=None):
        d=self.devices.get(ip,{})
        uid=self._location_gate_uid_for_mac(d.get('mac','')) or self.uid_for_ip(ip)
        return self._location_gate_is_allowed_uid(uid,now)

    def _update_live_routes(self):
        """Only publish routes for phones whose location has passed the 10-second gate."""
        gateway=getattr(self,'persistent_gateway',None)
        if gateway is None:return
        now=time.monotonic()
        with self.device_lock:
            allowed_devices={ip:dict(d) for ip,d in self.devices.items() if self._location_gate_is_allowed_ip(ip,now)}
            gateway.update(self.exits,allowed_devices)

    def resolve_proxy_for_ip(self,new_ip):
        """MAC-first IP learning is retained, but it can never bypass the location gate."""
        row=self._neighbor_for_ip(new_ip)
        if not row:return None
        mac=normalize_mac(row.get('mac',''))
        uid=self._location_gate_uid_for_mac(mac)
        if not self._location_gate_is_allowed_uid(uid):
            return None
        return V10ProxyManager.resolve_proxy_for_ip(self,new_ip)

    def _sync_location_network_policy(self):
        """Start the 10s timer on location startup and revoke traffic immediately on any break."""
        now=time.monotonic()
        live_uids=set()
        policy_changed=False
        for ip,d in list(self.devices.items()):
            uid=self._location_gate_uid_for_mac(d.get('mac','')) or self.uid_for_ip(ip)
            state=self.ios_states.get(uid,{}) if uid else {}
            wifi=state.get('wifi')
            location=state.get('location')
            candidate=bool(uid and wifi=='连接中' and (location=='定位中' or self._route_is_running(uid)))
            if candidate:
                live_uids.add(uid)
                self._location_network_started.setdefault(uid,now)
            allowed=bool(candidate and location=='定位中' and self._location_gate_is_allowed_uid(uid,now))
            old_allowed=ip in self._location_network_allowed
            if allowed and not old_allowed:
                self._location_network_allowed.add(ip);policy_changed=True
                self.log_operation(f'{ip}：定位已连续开启10秒，已放行指定出口网络')
            elif not allowed and old_allowed:
                self._location_network_allowed.discard(ip);policy_changed=True
                gateway=getattr(self,'persistent_gateway',None)
                if gateway is not None:gateway.revoke_ip(ip)
                self.log_operation(f'{ip}：定位已断开/未满足条件，已立即关闭出口网络')
            if allowed:
                phase='allowed'
            elif candidate:
                remain=max(0,int(self.LOCATION_NETWORK_DELAY-(now-self._location_network_started.get(uid,now))+0.999))
                phase='waiting:'+str(remain)
            else:
                phase='blocked'
            self._location_network_phase[ip]=phase
        for uid in list(self._location_network_started):
            if uid not in live_uids:
                self._location_network_started.pop(uid,None)
        stale=set(self._location_network_allowed)-set(self.devices)
        for ip in stale:
            self._location_network_allowed.discard(ip);policy_changed=True
            gateway=getattr(self,'persistent_gateway',None)
            if gateway is not None:gateway.revoke_ip(ip)
        if policy_changed:
            self._update_live_routes()
        else:
            # Crossing the 10-second threshold is detected here even without a new iOS event.
            self._update_live_routes()

    def uid_for_ip(self,ip):
        d=self.devices.get(ip,{})
        mac=normalize_mac(d.get('mac',''))
        saved=self.links.get('mac:'+mac) if valid_device_mac(mac) else self.links.get('ip:'+ip)
        # The original discovery's identifier is passed unchanged into start-tunnel.
        if saved:return saved
        hits=[uid for uid,data in self.discovered.items() if self.discovery_ip(uid,data)==ip]
        return hits[0] if len(hits)==1 else None

    def discovery_ip(self,uid,data):
        address=data.get('address','')
        try:
            if ipaddress.ip_address(address).version==4:return address
        except ValueError:pass
        if time.monotonic()-self.identity_at<45:
            return self.network_identities.get(uid) or address
        return address

    def _configured_coordinate(self,ip,uid=None):
        """Return one validated saved coordinate, or None when this device is unconfigured."""
        uid=uid or self.uid_for_ip(ip)
        candidates=[]
        if uid:candidates.append(uid)
        candidates.append('ip:'+ip)
        for key in candidates:
            coord=self.coordinates.get(key)
            if not isinstance(coord,dict):continue
            lon_text=str(coord.get('lon','')).strip();lat_text=str(coord.get('lat','')).strip()
            if not lon_text or not lat_text:continue
            try:
                lon=float(lon_text);lat=float(lat_text);interval=float(coord.get('interval',1) or 1)
                if not(-180<=lon<=180 and -90<=lat<=90 and 1<=interval<=60):continue
            except (TypeError,ValueError):
                continue
            return dict(lon=str(lon),lat=str(lat),interval=interval)
        return None

    def _maybe_auto_start_location(self,ip,uid=None,reason='Wi-Fi 已连接'):
        """Start location automatically once Wi-Fi is ready, but only with saved non-empty coords."""
        uid=uid or self.uid_for_ip(ip)
        if not uid:return False
        if self._route_is_running(uid):
            self.after(100,self._resume_saved_routes)
            return False
        state=self.ios_states.setdefault(uid,{})
        if state.get('wifi')!='连接中':return False
        if state.get('location') in ('定位中','准备发送','正在停止','正在清除'):
            return False
        coord=self._configured_coordinate(ip,uid)
        if coord is None:
            marker='坐标为空，未自动开启定位'
            if self.row_messages.get(ip)!=marker:
                self.row_messages[ip]=marker
                self.log_operation(ip+'：Wi-Fi 已连接；坐标为空，未自动开启定位，请录入经纬度。')
            return False
        self.row_messages.pop(ip,None)
        if self.ios.send(uid,dict(op='start',**coord)):
            state.update(location='准备发送',error='')
            self.log_operation(f"{ip}：{reason}，检测到已保存坐标，自动开启定位 → {coord['lon']}, {coord['lat']}")
            self.refresh_devices()
            return True
        return False

    def commit_row(self,ip,quiet=False,auto_start=False):
        row=self.row_widgets.get(ip)
        if row is None:return False
        lon_text=row['lon'].get().strip();lat_text=row['lat'].get().strip()
        key=self.uid_for_ip(ip) or 'ip:'+ip
        # Both empty means intentionally unconfigured. Keep the cells blank and remove stale config.
        if not lon_text and not lat_text:
            removed=False
            for candidate in {key,'ip:'+ip}:
                if candidate in self.coordinates:
                    self.coordinates.pop(candidate,None);removed=True
            if removed:self.save_ios()
            return False
        # During typing/focus changes, a half-entered coordinate is normal; do not pop an error.
        if not lon_text or not lat_text:
            if not quiet:messagebox.showerror('坐标','经度和纬度必须同时填写；不配置时请两项都留空。',parent=self)
            return False
        try:
            lat,lon=float(lat_text),float(lon_text)
            if not(-90<=lat<=90 and -180<=lon<=180):raise ValueError('经纬度超出范围')
        except ValueError:
            if not quiet:messagebox.showerror('坐标','请输入有效经纬度：经度 -180～180，纬度 -90～90。',parent=self)
            return False
        self.coordinates[key]=dict(lon=str(lon),lat=str(lat),interval=1)
        # If an old IP-keyed value exists after association, keep only the current identity key.
        if not key.startswith('ip:'):self.coordinates.pop('ip:'+ip,None)
        self.save_ios()
        if auto_start:
            self._maybe_auto_start_location(ip,self.uid_for_ip(ip),reason='坐标已保存且 Wi-Fi 已连接')
        return True

    def locate_row(self,ip):
        self.choose_row(ip);uid=self.uid_for_ip(ip);state=self.ios_states.get(uid,{})
        if self._route_is_running(uid):
            self.log_operation(ip+'：自动寻路正在接管定位，请先在寻路窗口停止。');return
        if state.get('wifi')!='连接中':
            self.log_operation(ip+'：Wi-Fi 未连接，定位未执行');return
        if state.get('location') in ('定位中','准备发送','正在恢复'):
            if self.ios.send(uid,dict(op='stop')):
                state['location']='正在停止';self._sync_location_network_policy();self.log_operation(ip+'：正在停止定位…')
        else:
            if not self.commit_row(ip):return
            coord=self.coordinates[uid]
            if self.ios.send(uid,dict(op='start',**coord)):
                state.update(location='准备发送',error='')
                self._sync_location_network_policy()
                self.log_operation(f"{ip}：开始定位 → {coord['lon']}, {coord['lat']}")
        self.refresh_devices()

    def connect_row(self,ip):
        self.choose_row(ip)
        if not self.ios_ready:
            self.log_ios('正在检测原定位环境，请稍后再点击连接。');self.check_ios_environment();return
        uid=self.uid_for_ip(ip)
        if not uid:
            self.pending_actions[ip]='connect';self.log_ios(ip+'：正在自动查找对应 iPhone…')
            self.ios.scan();self.ios.identity_scan();return
        state=self.ios_states.setdefault(uid,{})
        if state.get('wifi')=='连接中':
            self.log_operation(ip+'：Wi-Fi 已经连接');return
        if state.get('remote_pair_ready') is False:
            message=('RemotePairing 配对失败，Wi-Fi Tunnel 无法建立。请保持 USB 连接，重新执行“信任/开发模式”；'
                     '再到“更多 → 详细日志”查看 RemotePairing 的具体错误。')
            self.row_messages[ip]=message
            self.log_operation(ip+'：'+message)
            self.refresh_devices(ip)
            return
        if state.get('developer_ready') is False:
            message='开发者模式未开启。请保持 USB 连接，点击“开发模式”；若手机有锁屏密码，请先在手机 设置 → 隐私与安全性 → 开发者模式 手动开启并重启。'
            self.row_messages[ip]=message
            self.log_operation(ip+'：'+message)
            self.refresh_devices();return
        self.row_messages.pop(ip,None)
        generation=self.ios.connect(uid)
        if generation:
            state.update(generation=generation,wifi='建立中',location='未开启',error='')
            self.log_operation(ip+'：正在建立 Wi-Fi 连接…')
        else:self.log_operation(ip+'：Wi-Fi 连接未启动，设备可能正在工作')
        self.refresh_devices()

    def trust_row(self,ip):
        if not self._license_valid():
            self.show_license_dialog();return
        self.choose_row(ip)
        if not self.ios_ready:
            self.log_ios('正在检测原定位环境，请稍后再点击信任。');self.check_ios_environment();return
        self.trust_targets[ip]='scan'
        self.log_ios(ip+'：开始 USB 信任，请插数据线、解锁手机并确认“信任”。')
        self.ios.usb_scan();self.refresh_devices()

    def choose_usb_for_row(self,ip):
        if ip not in self.devices:return
        uid=self.uid_for_ip(ip)
        candidates={str(d.get('UniqueDeviceID') or d.get('Identifier')):d for d in self.usb_devices
                    if d.get('UniqueDeviceID') or d.get('Identifier')}
        if uid and uid in candidates:return self.begin_trust(ip,uid)
        if not candidates:
            self.trust_targets.pop(ip,None)
            self.row_messages[ip]='没有发现 USB 手机。请插线并解锁后再点信任。'
            self.log_operation(ip+'：信任失败，没有发现 USB 手机')
            messagebox.showinfo('信任',self.row_messages[ip],parent=self);return
        win=tk.Toplevel(self);win.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei');win.geometry('610x160')
        ttk.Label(win,text=f'请选择设备 {ip} 对应的 USB 手机（首次确认一次，之后自动识别）。').pack(padx=10,pady=10)
        ids=list(candidates)
        names=[f"{candidates[u].get('DeviceName','iPhone')}  |  {u}" for u in ids]
        combo=ttk.Combobox(win,values=names,state='readonly',width=75);combo.pack(padx=10)
        if len(ids)==1:combo.current(0)
        def apply():
            if combo.current()<0:return
            chosen=ids[combo.current()]
            # User explicitly identified this USB device; no list-index guess in commands.
            self.begin_trust(ip,chosen);win.destroy()
        def cancel():self.trust_targets.pop(ip,None);self.refresh_devices();win.destroy()
        ttk.Button(win,text='信任此手机',command=apply).pack(pady=10)
        win.protocol('WM_DELETE_WINDOW',cancel)

    def begin_trust(self,ip,uid):
        old=self.uid_for_ip(ip)
        if old and old!=uid and old in self.ios.workers:
            self.trust_targets.pop(ip,None);return messagebox.showinfo('信任','请先断开原连接。',parent=self)
        for other in self.devices:
            if other!=ip and self.uid_for_ip(other)==uid:
                self.trust_targets.pop(ip,None)
                return messagebox.showerror('设备','该手机已对应 '+other+'，请核对设备IP。',parent=self)
        self.trust_targets[ip]=uid
        if not self.ios.trust(uid):
            self.trust_targets.pop(ip,None);self.log_ios(ip+'：设备正在工作，请先断开连接再做USB信任。')
        else:self.log_operation(ip+'：正在验证 USB 信任并开启 Wi-Fi…')
        self.refresh_devices()

    def associate(self,ip,uid):
        d=self.devices.get(ip)
        if d is None:return
        mac=normalize_mac(d.get('mac',''))
        for old_ip,old_device in list(self.devices.items()):
            if old_ip==ip:continue
            old_mac=normalize_mac(old_device.get('mac',''))
            old_key='mac:'+old_mac if valid_device_mac(old_mac) else 'ip:'+old_ip
            if self.links.get(old_key)!=uid:continue
            if old_device.get('exit_id') and not d.get('exit_id'):d['exit_id']=old_device['exit_id']
            if not d.get('remark'):d['remark']=old_device.get('remark','')
            self.links.pop(old_key,None);self.links.pop('ip:'+old_ip,None)
            self.devices.pop(old_ip,None)
        key='mac:'+mac if valid_device_mac(mac) else 'ip:'+ip
        old=self.links.get(key)
        if old and old!=uid:
            self.ios.disconnect(old)
            self.ios_states.setdefault(old,{}).update(wifi='已断开',location='不可定位')
        self.links[key]=uid
        if valid_device_mac(mac):self.links.pop('ip:'+ip,None)
        d['manual']=True
        coord=self.coordinates.pop('ip:'+ip,None)
        if coord and uid not in self.coordinates:self.coordinates[uid]=coord
        self.save_ios();save_devices(self.devices)
        self._update_live_routes()

    def resolve_discovery(self):
        for uid,data in self.discovered.items():
            ip=self.discovery_ip(uid,data)
            try:
                if ipaddress.ip_address(ip).version!=4:continue
            except ValueError:continue
            # An iOS-discovered IPv4 device may precede the hotspot ARP scan.
            d=self.devices.setdefault(ip,dict(ip=ip,mac='',interface='',remark='',exit_id='',manual=True,online=False))
            self.associate(ip,uid)
        for ip,action in list(self.pending_actions.items()):
            uid=self.uid_for_ip(ip)
            if uid:
                self.pending_actions.pop(ip,None);self.connect_row(ip)
        self.refresh_devices()

    def row_menu(self,event,ip):
        self.choose_row(ip);menu=tk.Menu(self,tearoff=False)
        menu.add_command(label='断开 Wi-Fi 连接',command=lambda:self.disconnect_row(ip))
        menu.add_command(label='恢复真实定位',command=lambda:self.clear_row(ip))
        menu.add_command(label='设备详情 / 错误',command=lambda:self.row_details(ip))
        menu.add_command(label='手动匹配 iPhone',command=lambda:self.match_row(ip))
        menu.add_separator();menu.add_command(label='删除设备',command=self.delete_device)
        menu.tk_popup(event.x_root,event.y_root)

    def disconnect_row(self,ip):
        uid=self.uid_for_ip(ip)
        if uid:
            self.ios.disconnect(uid);self.ios_states.setdefault(uid,{}).update(wifi='断开中',location='正在停止')
            self._sync_location_network_policy()
            self.log_operation(ip+'：正在断开 Wi-Fi 连接…')
        else:self.log_operation(ip+'：没有可断开的 Wi-Fi 连接')
        self.refresh_devices()

    def clear_row(self,ip):
        uid=self.uid_for_ip(ip);state=self.ios_states.get(uid,{})
        if state.get('wifi')!='连接中':return messagebox.showinfo('恢复定位','请先连接该手机。',parent=self)
        if self.ios.send(uid,dict(op='clear')):
            state['location']='正在清除';self._sync_location_network_policy();self.log_operation(ip+'：正在恢复真实定位…')
        self.refresh_devices()

    def row_details(self,ip):
        uid=self.uid_for_ip(ip);state=self.ios_states.get(uid,{})
        messagebox.showinfo('设备 '+ip,f"UDID：{uid or '未发现'}\nWi-Fi：{state.get('wifi','未连接')}\n定位：{state.get('location','未开启')}\n"
            f"发送次数：{state.get('count',0)}\n最近发送：{state.get('last','-')}\n{self.row_messages.get(ip) or state.get('error') or '无错误'}",parent=self)

    def match_row(self,ip):
        self.choose_row(ip)
        if not self.discovered:
            self.scan_all();return messagebox.showinfo('匹配','正在扫描 iPhone，请稍后重试。',parent=self)
        # Rare IPv6-only discovery: explicit one-time selection instead of guessing.
        win=tk.Toplevel(self);win.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei');win.geometry('660x150')
        ids=list(self.discovered)
        labels=[f'{self.discovery_ip(u,self.discovered[u])} | {u}' for u in ids]
        ttk.Label(win,text='请选择这台手机，仅在不能自动对应 IP 时需要。').pack(pady=10)
        combo=ttk.Combobox(win,values=labels,state='readonly',width=80);combo.pack()
        def apply():
            if combo.current()<0:return
            uid=ids[combo.current()];address=self.discovery_ip(uid,self.discovered[uid])
            if address and ':' not in address and address!=ip:
                return messagebox.showerror('IP不一致','发现的手机IP是 '+address+'，请核对。',parent=win)
            self.associate(ip,uid);self.refresh_devices();self.log_operation(ip+'：已手动匹配 iPhone');win.destroy()
        ttk.Button(win,text='匹配此手机',command=apply).pack(pady=10)

    def scan_all(self):
        self.scan_devices()
        if self.ios_ready:self.ios.scan();self.ios.identity_scan()

    def check_ios_environment(self):
        def work():
            try:
                # --help is available in the old working CLI; 'version' is not a startup gate.
                self.ios.run(['--help'],20)
                self.ios.events.put(('environment',True,' '.join(self.ios.interpreter)))
            except Exception as exc:self.ios.events.put(('environment',False,str(exc)))
        self.ios.job('environment',work)

    def poll_ios(self):
        if self._closing:return
        changed=False
        for _ in range(400):
            try:event=self.ios.events.get_nowait()
            except queue.Empty:break
            kind=event[0]
            if kind=='environment':
                self.ios_ready=event[1]
                self.ios_summary.set(event[2])
                if event[1]:self.ios.scan();self.ios.identity_scan()
                else:
                    error_lines=str(event[2] or '').splitlines()
                    error_tail='\n'.join(error_lines[-12:])[-1400:]
                    message='定位环境无法运行。错误详情见下方末尾：\n'+error_tail
                    self.ios_summary.set('定位环境无法运行；请查看详细日志末尾的异常信息。')
                    self.log_ios(message)
                    self.detail.set(error_tail[-500:] or message)
            elif kind=='discovered':
                current_uids=set(event[1])
                # Match ios_v2 behavior: once a phone truly disappears from Wi-Fi
                # discovery, its next appearance may receive one fresh auto attempt.
                for gone_uid in self._last_discovered_uids-current_uids:
                    self._auto_attempted_uids.discard(str(gone_uid))
                self._last_discovered_uids={str(x) for x in current_uids}
                self.discovered=event[1];self.discovery_at=time.monotonic();self.resolve_discovery();changed=True
                for ip in self.pending_actions:
                    message='未找到对应 iPhone；先插USB做信任。已信任设备可右键“手动匹配 iPhone”。'
                    if self.row_messages.get(ip)!=message:
                        self.log_operation(ip+'：连接未执行，'+message)
                    self.row_messages[ip]=message
                    self.detail.set(ip+'：'+message)
            elif kind=='network_identities':
                identities={}
                for d in event[1]:
                    uid=d.get('UniqueDeviceID') or d.get('udid')
                    ip=d.get('Identifier') or d.get('address')
                    if uid and ip:
                        try:
                            if ipaddress.ip_address(ip).version==4:identities[str(uid)]=str(ip)
                        except ValueError:pass
                self.network_identities=identities;self.identity_at=time.monotonic();self.resolve_discovery();changed=True
            elif kind=='usb':
                self.usb_devices=event[1]
                for ip,value in list(self.trust_targets.items()):
                    if value=='scan':self.trust_targets[ip]='choose';self.choose_usb_for_row(ip)
                changed=True
            elif kind=='trust_progress':
                uid,message=event[1],event[2]
                ip=self._ip_for_uid(uid)
                for candidate,value in self.trust_targets.items():
                    if value==uid:
                        ip=candidate;break
                self.log_operation(ip+'：'+message)
                changed=True
            elif kind=='trusted':
                uid=event[1];meta=event[2] if len(event)>2 and isinstance(event[2],dict) else {}
                state=self.ios_states.setdefault(uid,{})
                state['trusted']=True
                if 'developer_ready' in meta:state['developer_ready']=meta.get('developer_ready')
                if 'ddi_ready' in meta:state['ddi_ready']=meta.get('ddi_ready')
                if 'remote_pair_ready' in meta:state['remote_pair_ready']=meta.get('remote_pair_ready')
                trusted_ip=None
                for ip,value in list(self.trust_targets.items()):
                    if value==uid:
                        trusted_ip=ip;self.associate(ip,uid);self.trust_targets.pop(ip,None);self.row_messages.pop(ip,None)
                ip=trusted_ip or self._ip_for_uid(uid)
                if meta.get('developer_ready') is True:
                    suffix='开发者模式已就绪'
                    if meta.get('ddi_ready') is True:suffix+='，开发者组件已挂载'
                    elif meta.get('ddi_ready') is False:suffix+='，但开发者组件挂载失败'
                    self.log_ios(ip+'：信任成功，Wi-Fi 连接能力已开启；'+suffix+'。')
                elif meta.get('developer_ready') is False:
                    self.log_ios(ip+'：USB 信任成功，但开发者模式尚未开启，暂不建立 Wi-Fi Tunnel。')
                else:
                    self.log_ios(ip+'：信任成功，已开启 Wi-Fi 连接能力；开发者模式状态未能确认。')
                warnings=meta.get('warnings') or []
                if warnings:
                    self.row_messages[ip]=warnings[-1]
                    for warning in warnings:self.log_operation(ip+'：'+warning)
                self.ios.identity_scan();changed=True
            elif kind=='job_error':
                key,error=event[1],event[2]
                if key.startswith('trust:'):
                    for ip,value in list(self.trust_targets.items()):
                        if value==key[6:]:self.trust_targets.pop(ip,None);self.row_messages[ip]=error
                    self.log_ios(key+'：'+error)
                elif key=='usb':
                    for ip in list(self.trust_targets):self.row_messages[ip]=error
                    self.trust_targets.clear();self.log_ios(key+'：'+error)
                elif key in ('scan','network-identities'):
                    # 后台 iOS 扫描失败只进入详细日志，不占用底部操作记录。
                    self._append_ios_detail(key+'：'+error)
                else:self.log_ios(key+'：'+error)
                changed=True
            elif kind=='worker':
                _,uid,generation,data=event;state=self.ios_states.setdefault(uid,{})
                if state.get('generation')!=generation:continue
                if data['kind']=='heartbeat':continue
                if data['kind']=='tunnel_log':
                    # 原始 tunnel 输出仅进详细日志，不进入底部6行操作记录。
                    self._append_ios_detail(uid+'：'+data.get('text',''))
                    continue
                # V2.6.2: after the first successful send has already put the row in
                # “定位中”, later 1 Hz sent events only update counters/timestamps. Do
                # not repaint the complete device table eight times per second.
                if data['kind']=='sent' and hasattr(self,'_qizi_track_device_coordinate'):
                    self._qizi_track_device_coordinate(uid,data)
                if data['kind']=='sent' and state.get('location')=='定位中' and data.get('location')=='定位中':
                    for key in ('count','last','backend'):
                        if key in data:state[key]=data[key]
                    continue
                old_wifi=state.get('wifi','未连接');old_location=state.get('location','未开启')
                state.update({k:v for k,v in data.items() if k not in ('kind','udid')})
                if data.get('wifi')=='连接中':
                    for ip in self.devices:
                        if self.uid_for_ip(ip)==uid:self.row_messages.pop(ip,None)
                worker_disconnected=data['kind'] in ('error','ended','closed')
                if worker_disconnected:
                    state.update(wifi='已断开',location='不可定位')
                ip=self._ip_for_uid(uid)
                if data['kind']=='error':
                    err=str(data.get('error',''))
                    low=err.lower()
                    if 'no tunnel services discovered' in low or 'device is not connected' in low:
                        hint='未发现可用 Tunnel 服务。新设备请插 USB 点击“信任/开发模式”，程序会检查开发者模式、RemotePairing 与开发者组件。'
                        if self.row_messages.get(ip)!=hint:
                            self.row_messages[ip]=hint;self.log_operation(ip+'：'+hint)
                new_wifi=state.get('wifi','未连接');new_location=state.get('location','未开启')
                if new_wifi!=old_wifi:
                    if new_wifi=='连接中':
                        self.log_operation(ip+'：Wi-Fi 连接成功')
                        # V2.5 restore chain: trust/identity -> Wi-Fi -> saved coordinates -> location.
                        self._maybe_auto_start_location(ip,uid)
                    elif new_wifi=='已断开':
                        if old_location in ('定位中','准备发送'):
                            self.log_operation(ip+'：Wi-Fi 已断开，定位已停止')
                        else:
                            self.log_operation(ip+'：Wi-Fi 已断开')
                    elif new_wifi=='建立中':self.log_operation(ip+'：正在建立 Wi-Fi 连接…')
                if new_location!=old_location:
                    if new_location=='定位中':self.log_operation(ip+'：定位已开始')
                    elif new_location=='已停止':self.log_operation(ip+'：定位已停止')
                    elif new_location=='已清除模拟':self.log_operation(ip+'：已恢复真实定位')
                    # V2.5.2 不再把定位命令子进程退出码显示成“定位发送失败”。
                if data.get('error'):
                    # 详细异常仍保留在“更多 → 详细日志”用于排查；
                    # 底部操作记录只显示用户能直接理解的连接/断开状态。
                    self._append_ios_detail(uid+'：'+data['error'])
                    if not worker_disconnected:
                        for device_ip in self.devices:
                            if self.uid_for_ip(device_ip)==uid:self.row_messages[device_ip]=data['error']
                changed=True
        # Auto restore is evaluated every poll, not only on the browse event. This
        # matters when remote browse arrives before the Windows hotspot scan has
        # refreshed the phone's MAC address. The frozen startup link still decides
        # whether the device is eligible.
        self._maybe_auto_restore_wifi()
        now=time.monotonic()
        if self.ios_ready and self.auto_scan_var.get() and now-self.last_ios_scan>=IOS_AUTO_SCAN_SECONDS:
            self.last_ios_scan=now;self.ios.scan();self.ios.identity_scan()
        self._sync_location_network_policy()
        if changed:self.refresh_devices()
        self.after(150,self.poll_ios)

    # ---------------- V2.9.29.1 自动寻路 + 寻路网络锁 ----------------
    def _route_key(self,ip=None,uid=None):
        uid=uid or (self.uid_for_ip(ip) if ip else None)
        return str(uid or ('ip:'+str(ip) if ip else ''))

    def _route_is_running(self,uid):
        if not uid:return False
        state=self.route_states.get(str(uid),{})
        return bool(state.get('running'))

    def _save_route_state(self):
        save_route_plans(self.route_state_file,self.route_states)

    def _save_route_plans(self):
        save_route_plans(self.route_plan_file,self.route_plans)

    def _route_log(self,ip,text):
        self.route_events.put(('log',ip,str(text)))

    def open_route_dialog(self,ip):
        uid=self.uid_for_ip(ip);state=self.ios_states.get(uid,{}) if uid else {}
        if not uid or not state.get('trusted') or state.get('wifi')!='连接中':
            return messagebox.showinfo('自动寻路','设备必须已信任并且 Wi-Fi 状态为“连接中”后才能使用自动寻路。',parent=self)
        old=self.route_dialogs.get(uid)
        if old is not None and old.winfo_exists():old.lift();return
        row=self.row_widgets.get(ip);saved=self.route_plans.get(uid,{})
        win=tk.Toplevel(self);self.route_dialogs[uid]=win;win.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei');win.geometry('510x435');win.resizable(False,False);win.transient(self)
        box=ttk.Frame(win,padding=14);box.pack(fill='both',expand=True)
        exit_ids,exit_labels=self.choice_values()
        saved_target=str(saved.get('target_exit_id',LAN_BLOCK_EXIT_ID) or LAN_BLOCK_EXIT_ID)
        if saved_target not in exit_ids:saved_target=LAN_BLOCK_EXIT_ID if LAN_BLOCK_EXIT_ID in exit_ids else (exit_ids[0] if exit_ids else '')
        target_label=exit_labels[exit_ids.index(saved_target)] if saved_target in exit_ids else '禁止网络'
        values={
            'start_lat':tk.StringVar(value=(row['lat'].get().strip() if row else '') or str(saved.get('start_lat',''))),
            'start_lon':tk.StringVar(value=(row['lon'].get().strip() if row else '') or str(saved.get('start_lon',''))),
            'dest_lat':tk.StringVar(value=str(saved.get('dest_lat',''))),
            'dest_lon':tk.StringVar(value=str(saved.get('dest_lon',''))),
            'car':tk.StringVar(value=str(saved.get('car_kmh',220))),
            'plane':tk.StringVar(value=str(saved.get('plane_kmh',958))),
            'mode':tk.StringVar(value=str(saved.get('mode','WiFi模式')) if saved.get('mode') else 'WiFi模式'),
            'target_exit':tk.StringVar(value=target_label),
        }
        # 当前起点输入框的引用交给当前位置模块，实时同步而不修改已规划轨迹。
        win.qizi_route_values = values
        fields=[('起点纬度',values['start_lat']),('起点经度',values['start_lon']),('终点纬度',values['dest_lat']),('终点经度',values['dest_lon']),
                ('汽车时速 km/h',values['car']),('飞机时速 km/h',values['plane'])]
        for r,(label,var) in enumerate(fields):
            ttk.Label(box,text=label).grid(row=r,column=0,sticky='w',pady=5)
            ttk.Entry(box,textvariable=var,width=31).grid(row=r,column=1,sticky='ew',padx=(12,0),pady=5)
        ttk.Label(box,text='连接方式').grid(row=6,column=0,sticky='w',pady=5)
        mode=ttk.Combobox(box,textvariable=values['mode'],values=('WiFi模式','USB数据线模式'),state='readonly',width=28);mode.grid(row=6,column=1,sticky='ew',padx=(12,0),pady=5)
        mode.bind('<MouseWheel>',lambda e:'break');mode.bind('<Button-4>',lambda e:'break');mode.bind('<Button-5>',lambda e:'break')
        ttk.Label(box,text='网络出口').grid(row=7,column=0,sticky='w',pady=5)
        target_exit=ttk.Combobox(box,textvariable=values['target_exit'],values=exit_labels,state='readonly',width=28);target_exit.grid(row=7,column=1,sticky='ew',padx=(12,0),pady=5)
        target_exit.bind('<MouseWheel>',lambda e:'break');target_exit.bind('<Button-4>',lambda e:'break');target_exit.bind('<Button-5>',lambda e:'break')
        def qizi_route_target_changed(_event=None):
            idx=target_exit.current()
            eid=exit_ids[idx] if 0<=idx<len(exit_ids) else ''
            target=next((p for p in self.exits if p.get('id')==eid),None)
            coordinates=exit_coordinates(target)
            if coordinates:
                values['dest_lat'].set(coordinates[0])
                values['dest_lon'].set(coordinates[1])
            else:
                values['dest_lat'].set('')
                values['dest_lon'].set('')
        target_exit.bind('<<ComboboxSelected>>',qizi_route_target_changed)
        if not saved.get('dest_lat') or not saved.get('dest_lon'):
            qizi_route_target_changed()
        ttk.Label(box,text='说明：开始寻路60秒后强制禁止网络；此处选择的是寻路结束后恢复的出口。',foreground=self.GRAY,wraplength=470).grid(row=8,column=0,columnspan=2,sticky='w',pady=(5,2))
        status=tk.StringVar(value='先规划路线；规划完成后才能开始执行。')
        ttk.Label(box,textvariable=status,wraplength=470,foreground=self.GRAY).grid(row=9,column=0,columnspan=2,sticky='w',pady=(6,6))
        bar=ttk.Frame(box);bar.grid(row=10,column=0,columnspan=2,sticky='ew',pady=(8,0));box.columnconfigure(1,weight=1)
        plan_btn=ttk.Button(bar,text='规划路线');start_btn=ttk.Button(bar,text='开始执行');stop_btn=ttk.Button(bar,text='停止')
        plan_btn.pack(side='left');start_btn.pack(side='left',padx=8);stop_btn.pack(side='left')
        def plan():
            if self._route_is_running(uid):
                return messagebox.showinfo('自动寻路','请先停止当前自动寻路，再重新规划路线。',parent=win)
            try:
                slat=float(values['start_lat'].get());slon=float(values['start_lon'].get());dlat=float(values['dest_lat'].get());dlon=float(values['dest_lon'].get())
                car=float(values['car'].get());plane=float(values['plane'].get())
                if car<=0 or plane<=0:raise ValueError
            except Exception:
                return messagebox.showerror('自动寻路','请输入有效的起点/终点经纬度和正数速度。',parent=win)
            plan_mode=values['mode'].get()
            target_index=target_exit.current()
            plan_target_id=exit_ids[target_index] if 0<=target_index<len(exit_ids) else LAN_BLOCK_EXIT_ID
            plan_btn.configure(state='disabled');start_btn.configure(state='disabled');status.set('正在获取机场和真实道路并预生成每秒坐标…')
            def worker():
                try:
                    logger=lambda text:self._route_log(ip,text)
                    plan_obj=build_auto_route((slat,slon),(dlat,dlon),car,plane,DATA_DIR,logger=logger,force_flight=True)
                    timeline=precompute_auto_route_timeline(plan_obj,car,plane,1.0)
                    self.route_events.put(('planned',ip,uid,dict(start_lat=slat,start_lon=slon,dest_lat=dlat,dest_lon=dlon,
                        car_kmh=car,plane_kmh=plane,mode=plan_mode,target_exit_id=plan_target_id,timeline=timeline_to_dict(timeline)),status,plan_btn,start_btn))
                except Exception as exc:self.route_events.put(('plan_error',ip,str(exc),status,plan_btn,start_btn))
            threading.Thread(target=worker,name='route-plan-'+uid[-8:],daemon=True).start()
        def start_route():
            idx=target_exit.current()
            target_id=exit_ids[idx] if 0<=idx<len(exit_ids) else LAN_BLOCK_EXIT_ID
            self._start_route(ip,uid,values['mode'].get(),status,target_exit_id=target_id)
        def stop_route():self._stop_route(uid,manual=True,status_var=status)
        plan_btn.configure(command=plan);start_btn.configure(command=start_route);stop_btn.configure(command=stop_route)
        if saved.get('timeline'):
            from qizi_route_checkpoint_engine import eta_chinese
            route_checkpoint=self.route_states.get(uid,{})
            remaining_idx=int(route_checkpoint.get('next_index',0) or 0)
            start_btn.configure(state='normal')
            suffix='；点击开始从检查点继续' if route_checkpoint.get('paused') else '；可直接开始执行'
            status.set(f"已有规划：预计剩余{eta_chinese(len(saved['timeline']),remaining_idx)}"+suffix)
        else:start_btn.configure(state='disabled')
        if self._route_is_running(uid):status.set('自动寻路正在执行，可点击停止。')
        def close_dialog():self.route_dialogs.pop(uid,None);win.destroy()
        win.protocol('WM_DELETE_WINDOW',close_dialog)

    def _poll_route_events(self):
        if getattr(self,'_closing',False):return
        for _ in range(100):
            try:event=self.route_events.get_nowait()
            except queue.Empty:break
            kind=event[0]
            if kind=='log':
                _,ip,text=event;self.log_operation(f'{ip}：寻路 {text}')
            elif kind=='planned':
                _,ip,uid,data,status,plan_btn,start_btn=event
                self.route_plans[uid]=data;self._save_route_plans()
                if hasattr(self,'_qizi_on_new_route_plan'):
                    self._qizi_on_new_route_plan(uid)
                from qizi_route_checkpoint_engine import eta_chinese
                status.set(f"规划完成：预计需要{eta_chinese(len(data['timeline']))}；{len(data['timeline']):,} 个秒级坐标。")
                plan_btn.configure(state='normal');start_btn.configure(state='normal')
                self.log_operation(f"{ip}：自动寻路规划完成，共 {len(data['timeline']):,} 个秒级坐标")
            elif kind=='plan_error':
                _,ip,error,status,plan_btn,start_btn=event;status.set('规划失败：'+error);plan_btn.configure(state='normal');start_btn.configure(state='disabled');self.log_operation(ip+'：寻路规划失败：'+error)
            elif kind=='usb_error':
                _,uid,error=event
                state=self.route_states.get(uid)
                if state and state.get('running'):
                    state['waiting_reason']='USB定位失败：'+error;self._save_route_state()
                    runtime=self.route_runtime.get(uid,{})
                    job=runtime.pop('job',None)
                    if job:
                        try:self.after_cancel(job)
                        except Exception:pass
                    self.log_operation(self._ip_for_uid(uid)+'：自动寻路暂停，'+state['waiting_reason'])
        self.after(250,self._poll_route_events)

    def _start_route(self,ip,uid,mode,status_var=None,resume=False,target_exit_id=None):
        if not resume and self._route_is_running(uid):
            if status_var:status_var.set('自动寻路已经在执行。')
            return
        data=self.route_plans.get(uid,{})
        if not data.get('timeline'):
            if status_var:status_var.set('请先规划路线。')
            return
        state=self.ios_states.get(uid,{})
        if state.get('wifi')!='连接中' or not state.get('trusted'):
            if status_var:status_var.set('设备必须已信任并保持 Wi-Fi 连接。')
            return
        if not resume:
            self.ios.send(uid,{'op':'stop'})
            self.ios_states.setdefault(uid,{})['location']='正在停止'
            next_index=0
        else:
            next_index=int(self.route_states.get(uid,{}).get('next_index',0) or 0)
        runtime=self.route_runtime.setdefault(uid,{})
        if status_var is not None:runtime['status_var']=status_var
        runtime['waiting_stop']=not resume
        runtime['stop_deadline']=time.monotonic()+3.5
        old_sender=runtime.pop('usb_sender',None)
        if old_sender:
            try:old_sender.stop()
            except Exception:pass
        if mode=='USB数据线模式':
            runtime['usb_sender']=UsbRouteLocationSender(self.ios.interpreter,uid,
                on_result=lambda ok,msg,u=uid:self.route_events.put(('usb_ok' if ok else 'usb_error',u,msg)))
        route_state=self.route_states.setdefault(uid,{})
        valid_exit_ids={LAN_BLOCK_EXIT_ID,LAN_LOCAL_EXIT_ID}|{p.get('id') for p in self.exits}
        if resume:
            target_exit_id=str(route_state.get('target_exit_id',data.get('target_exit_id',LAN_BLOCK_EXIT_ID)) or LAN_BLOCK_EXIT_ID)
            started_at=float(route_state.get('started_at',time.time()) or time.time())
            network_blocked=bool(route_state.get('network_blocked',False))
        else:
            target_exit_id=str(target_exit_id or data.get('target_exit_id',LAN_BLOCK_EXIT_ID) or LAN_BLOCK_EXIT_ID)
            started_at=time.time();network_blocked=False
        if target_exit_id not in valid_exit_ids:target_exit_id=LAN_BLOCK_EXIT_ID
        data['mode']=mode;data['target_exit_id']=target_exit_id;self.route_plans[uid]=data;self._save_route_plans()
        route_state.update(running=True,ip=ip,mode=mode,target_exit_id=target_exit_id,next_index=next_index,
            started_at=started_at,network_block_at=started_at+self.ROUTE_NETWORK_BLOCK_DELAY,network_blocked=network_blocked,
            updated=time.time(),waiting_reason='')
        self._save_route_state();self._location_network_started.setdefault(uid,time.monotonic())
        self._schedule_route_tick(uid,150)
        target_name=self.exit_name_by_id(target_exit_id) or '禁止网络'
        if status_var:status_var.set(f'自动寻路正在执行：每秒发送1个坐标；60秒后强制断网；结束后切换为 {target_name}。')
        self.log_operation(f'{ip}：自动寻路开始执行｜{mode}｜从第 {next_index} 个坐标开始｜60秒后强制禁止网络｜结束目标出口 {target_name}')
        self.refresh_devices()

    def _route_force_network_block(self,uid,state=None):
        """Force this route device to BLOCK and tear down any existing proxy tunnel."""
        state=state if state is not None else self.route_states.get(uid,{})
        ip=state.get('ip') or self._ip_for_uid(uid)
        if not ip or ip not in self.devices:return False
        if state.get('network_blocked') and self.devices.get(ip,{}).get('exit_id')==LAN_BLOCK_EXIT_ID:
            return True
        gateway=getattr(self,'persistent_gateway',None)
        if gateway is not None:gateway.revoke_ip(ip)
        self._location_network_allowed.discard(ip)
        self._location_network_started.pop(uid,None)
        with self.device_lock:self.devices[ip]['exit_id']=LAN_BLOCK_EXIT_ID
        save_devices(self.devices)
        state['network_blocked']=True;state['network_blocked_at']=time.time();state['updated']=time.time()
        self._save_route_state();self._update_live_routes();self.refresh_devices()
        self.log_operation(f'{ip}：自动寻路已运行60秒，已强制切换为 禁止网络，并断开现有代理连接')
        return True

    def _route_maybe_force_network_block(self,uid,state):
        if not state.get('running'):return
        # A saved running route must remain blocked after a crash/restart even if the
        # device profile was externally changed while the program was down.
        if state.get('network_blocked'):
            self._route_force_network_block(uid,state);return
        block_at=float(state.get('network_block_at',0) or 0)
        if not block_at:
            started=float(state.get('started_at',time.time()) or time.time())
            block_at=started+self.ROUTE_NETWORK_BLOCK_DELAY;state['network_block_at']=block_at
        if time.time()>=block_at:self._route_force_network_block(uid,state)

    def _route_restore_target_exit(self,uid,state,ip):
        """After route stop/completion restore the selected post-route exit before restarting random location."""
        valid_ids={p.get('id') for p in self.exits}
        target=str(state.get('target_exit_id',LAN_BLOCK_EXIT_ID) or LAN_BLOCK_EXIT_ID)
        if target not in (LAN_BLOCK_EXIT_ID,LAN_LOCAL_EXIT_ID) and target not in valid_ids:
            target=LAN_BLOCK_EXIT_ID
        gateway=getattr(self,'persistent_gateway',None)
        if gateway is not None:gateway.revoke_ip(ip)
        self._location_network_allowed.discard(ip);self._location_network_started.pop(uid,None)
        with self.device_lock:
            if ip in self.devices:self.devices[ip]['exit_id']=target
        save_devices(self.devices);self._update_live_routes();self.refresh_devices()
        return target

    def _schedule_route_tick(self,uid,delay=1000):
        runtime=self.route_runtime.setdefault(uid,{})
        old=runtime.get('job')
        if old:
            try:self.after_cancel(old)
            except Exception:pass
        runtime['job']=self.after(max(1,int(delay)),lambda u=uid:self._route_tick(u))

    def _route_tick(self,uid):
        runtime=self.route_runtime.setdefault(uid,{});runtime['job']=None
        state=self.route_states.get(uid,{})
        if not state.get('running'):return
        ip=state.get('ip') or self._ip_for_uid(uid)
        self._route_maybe_force_network_block(uid,state)
        ios_state=self.ios_states.get(uid,{})
        if ios_state.get('wifi')!='连接中':
            state['waiting_reason']='等待 Wi-Fi 重新连接';self._save_route_state();return
        if runtime.get('waiting_stop'):
            stopped=ios_state.get('location') in ('已停止','未开启','不可定位')
            if not stopped and time.monotonic()<float(runtime.get('stop_deadline',0) or 0):
                self._schedule_route_tick(uid,180);return
            runtime['waiting_stop']=False
        data=self.route_plans.get(uid,{})
        try:timeline=timeline_from_dict(data.get('timeline') or [])
        except Exception as exc:
            state['running']=False;state['waiting_reason']='路线配置损坏：'+str(exc);self._save_route_state();return
        index=int(state.get('next_index',0) or 0)
        if index>=len(timeline):
            return self._finish_route(uid)
        point=timeline[index];mode=state.get('mode','WiFi模式')
        sent=False
        if mode=='USB数据线模式':
            sender=runtime.get('usb_sender')
            if sender is None:
                sender=UsbRouteLocationSender(self.ios.interpreter,uid,
                    on_result=lambda ok,msg,u=uid:self.route_events.put(('usb_ok' if ok else 'usb_error',u,msg)))
                runtime['usb_sender']=sender
            sent=sender.send(point.lat,point.lon)
        else:
            sent=self.ios.send(uid,{'op':'route','lat':point.lat,'lon':point.lon,'interval':1})
        if not sent:
            state['waiting_reason']='定位发送队列不可用';self._save_route_state();return
        # Successful enqueue == this second's authoritative coordinate. Keep UI + main location config in lockstep.
        self._apply_route_coordinate(ip,uid,point.lat,point.lon)
        state.update(next_index=index+1,last_lat=point.lat,last_lon=point.lon,stage=point.stage,updated=time.time(),waiting_reason='')
        self._save_route_state();self.ios_states.setdefault(uid,{})['location']='定位中';self._sync_location_network_policy()
        self._schedule_route_tick(uid,1000)

    def _apply_route_coordinate(self,ip,uid,lat,lon):
        coord=dict(lat=str(float(lat)),lon=str(float(lon)),interval=1)
        row=self.row_widgets.get(ip)
        if row:
            row['locate'].configure(text='寻路接管',fg=self.GREEN,state='disabled')
            row['route'].configure(text='寻路中',fg=self.GREEN,state='normal')

    def _finish_route(self,uid):
        state=self.route_states.get(uid,{})
        ip=state.get('ip') or self._ip_for_uid(uid)
        data=self.route_plans.get(uid,{})
        timeline=timeline_from_dict(data.get('timeline') or []) if data.get('timeline') else []
        if timeline:
            final=timeline[-1];self._apply_route_coordinate(ip,uid,final.lat,final.lon)
        status_var=self.route_runtime.get(uid,{}).get('status_var')
        self._stop_route(uid,manual=False,status_var=status_var)
        self.log_operation(ip+'：自动寻路已到达最终目的地，主定位已冻结在最终坐标')

    def _stop_route(self,uid,manual=True,status_var=None):
        state=dict(self.route_states.get(uid,{}) or {})
        ip=state.get('ip') or self._ip_for_uid(uid)
        runtime=self.route_runtime.get(uid,{})
        job=runtime.pop('job',None)
        if job:
            try:self.after_cancel(job)
            except Exception:pass
        sender=runtime.pop('usb_sender',None)
        if sender:
            try:sender.stop()
            except Exception:pass

        # Route no longer counts as an active location source. Restore the exit chosen
        # in the route dialog FIRST, then keep the last route position on devices
        # supporting the hold hook; older clients fall back to ordinary location.
        # Remote exits retain their network gate; local/block retain LAN semantics.
        if uid in self.route_states:self.route_states[uid]['running']=False
        target_exit=self._route_restore_target_exit(uid,state,ip) if ip else LAN_BLOCK_EXIT_ID
        self.route_states.pop(uid,None);self._save_route_state()
        coord=(self._qizi_live_coordinate(uid)
               if hasattr(self,'_qizi_live_coordinate') else None)
        if coord is None:coord=self.coordinates.get(uid)
        if coord and self.ios_states.get(uid,{}).get('wifi')=='连接中':
            # V3.10.18: a paused flight holds the exact stop point, it must not
            # accidentally start the random-walk engine from an old IP origin.
            hook=getattr(self,'_qizi_hold_route_after_stop',None)
            held=bool(hook(uid,coord,manual)) if callable(hook) else False
            if not held:
                self.ios.send(uid,dict(op='start',**coord))
                self.ios_states.setdefault(uid,{})['location']='准备发送'
        target_name=self.exit_name_by_id(target_exit) or '禁止网络'
        if status_var:
            status_var.set(('自动寻路已停止；' if manual else '已到达目的地；')+f'主定位保持当前坐标，网络目标：{target_name}。')
        if manual:self.log_operation(ip+f'：自动寻路已停止；已切换目标出口 {target_name}，主定位保持当前坐标')
        else:self.log_operation(ip+f'：自动寻路结束；已切换目标出口 {target_name}，主定位保持最终坐标')
        self._sync_location_network_policy();self.refresh_devices()

    def _resume_saved_routes(self):
        if getattr(self,'_closing',False):return
        for uid,state in list(self.route_states.items()):
            if not state.get('running') or uid not in self.route_plans:continue
            runtime=self.route_runtime.setdefault(uid,{})
            if runtime.get('job'):continue
            ip=state.get('ip') or self._ip_for_uid(uid)
            ios_state=self.ios_states.get(uid,{})
            if ip and ios_state.get('trusted') and ios_state.get('wifi')=='连接中':
                self._start_route(ip,uid,state.get('mode','WiFi模式'),resume=True,target_exit_id=state.get('target_exit_id'))
        self.after(2000,self._resume_saved_routes)

    def _shutdown_route_runtime(self):
        # Called only while the whole app is closing. Do NOT clear route_states: a running
        # route is intentionally resumed from its saved next_index after next startup.
        for uid,runtime in list(self.route_runtime.items()):
            job=runtime.pop('job',None)
            if job:
                try:self.after_cancel(job)
                except Exception:pass
            sender=runtime.pop('usb_sender',None)
            if sender:
                try:sender.stop()
                except Exception:pass

    def delete_device(self):
        ip=self.selected_ip
        if not ip or ip not in self.devices:return
        uid=self.uid_for_ip(ip)
        if uid in self.ios.workers:
            return messagebox.showinfo('删除','请先右键断开这台手机，再删除。',parent=self)
        d=self.devices.pop(ip)
        self.links.pop('ip:'+ip,None)
        self.links.pop('mac:'+normalize_mac(d.get('mac','')),None)
        self.pending_actions.pop(ip,None)
        self.trust_targets.pop(ip,None)
        if uid:
            self.route_states.pop(uid,None);self.route_plans.pop(uid,None);self._save_route_state();self._save_route_plans()
        self.save_ios();save_devices(self.devices);self._update_live_routes();self.refresh_devices()
        self.log_operation(ip+'：设备已从列表删除')

    def close(self):
        self.log_operation('程序正在关闭并清理连接；运行中的自动寻路进度已保存，下次启动可续接。',dedupe=False)
        self._shutdown_route_runtime()
        clear_direct_block_rules()
        for ip in list(self.row_widgets):self.commit_row(ip,quiet=True)
        super().close()


# ================= V2.6.4 同 Wi-Fi LAN 网络控制 =================
# 不再要求 iPhone 连接 Windows 移动热点。电脑和手机只要位于同一当前上网 LAN，
# 手机把 Wi-Fi HTTP 代理指向本机 LAN_IP:端口即可由程序执行三态策略：
#   1) 禁止网络：拒绝该来源的代理请求（默认）
#   2) 本地出口：由电脑直接访问目标，不经过配置的远端代理，不要求定位
#   3) 指定出口：沿用原 HTTP 出口，并继续要求真实定位 ACK 连续 10 秒后放行
# 注意：这是 LAN 代理接管，不会也不能在“手机未使用本机代理”时凭空成为其默认网关。
LAN_BLOCK_EXIT_ID = '__QIZI_BLOCK__'
LAN_LOCAL_EXIT_ID = '__QIZI_LOCAL__'


def current_lan_control_interfaces():
    """Return active physical/current-Internet LAN interfaces with an IPv4 gateway.

    This intentionally ignores Windows Wi-Fi Direct/mobile-hotspot adapters and common
    VPN/virtual adapters. USB/RNDIS cellular routers are retained because they normally
    expose a real IPv4 default gateway just like Ethernet/Wi-Fi.
    """
    if os.name != 'nt':
        return []
    script = r'''
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$rows=@()
$configs=@(Get-NetIPConfiguration | Where-Object {
    $_.NetAdapter -and $_.NetAdapter.Status -eq 'Up' -and $_.IPv4Address -and $_.IPv4DefaultGateway
})
foreach($cfg in $configs){
    $adapter=$cfg.NetAdapter
    $name=[string]$adapter.Name
    $desc=[string]$adapter.InterfaceDescription
    $joined=($name+' '+$desc)
    if($joined -match '(?i)Wi-Fi Direct|Tailscale|WireGuard|Wintun|TAP-Windows|Hyper-V|vEthernet|VMware|VirtualBox|Loopback|WSL|Npcap'){
        continue
    }
    $gateway=''
    if($cfg.IPv4DefaultGateway){ $gateway=[string]$cfg.IPv4DefaultGateway.NextHop }
    foreach($addr in @($cfg.IPv4Address)){
        if(-not $addr){ continue }
        $rows += [pscustomobject]@{
            InterfaceIndex=[int]$cfg.InterfaceIndex
            InterfaceAlias=$name
            InterfaceDescription=$desc
            IPAddress=[string]$addr.IPAddress
            PrefixLength=[int]$addr.PrefixLength
            Gateway=$gateway
        }
    }
}
ConvertTo-Json -InputObject @($rows) -Compress
'''
    try:
        raw=_json_list(powershell(script,timeout=20))
    except Exception:
        raw=[]
    result=[]
    seen=set()
    for entry in raw:
        try:
            address=ipaddress.IPv4Address(str(entry.get('IPAddress','')).strip())
            prefix=int(entry.get('PrefixLength',24))
            index=int(entry.get('InterfaceIndex',0))
            if not address.is_private or address.is_loopback or address.is_link_local:
                continue
            network=ipaddress.ip_network(f'{address}/{prefix}',strict=False)
            key=(index,str(address),prefix)
            if key in seen:
                continue
            seen.add(key)
            gateway=str(entry.get('Gateway','') or '').strip()
            result.append({
                'index':index,
                'alias':str(entry.get('InterfaceAlias','') or entry.get('InterfaceDescription','') or f'接口{index}'),
                'description':str(entry.get('InterfaceDescription','') or ''),
                'ip':str(address),'prefix':prefix,'network':network,'gateway':gateway,
            })
        except Exception:
            continue
    if result:
        return result

    # Conservative fallback: use only the interface that currently owns Windows' default route.
    try:
        default_index=get_default_interface_index()
        for entry in get_ipv4_interfaces():
            if default_index is not None and int(entry.get('InterfaceIndex',0)) != int(default_index):
                continue
            address=ipaddress.IPv4Address(entry['IPAddress'])
            if not address.is_private or address.is_loopback or address.is_link_local:
                continue
            prefix=int(entry.get('PrefixLength',24))
            result.append({'index':int(entry.get('InterfaceIndex',0)),
                           'alias':str(entry.get('InterfaceAlias','')),
                           'description':'','ip':str(address),'prefix':prefix,
                           'network':ipaddress.ip_network(f'{address}/{prefix}',strict=False),
                           'gateway':''})
    except Exception:
        pass
    return result


class LanPersistentGateway(PersistentGateway):
    """Persistent HTTP/HTTPS proxy supporting both a direct local route and named HTTP exits."""
    def update(self, exits, devices):
        by_id={p.get('id'):dict(p) for p in exits}
        routes={}
        for ip,device in devices.items():
            eid=str(device.get('exit_id','') or '')
            if eid==LAN_LOCAL_EXIT_ID:
                routes[ip]={'id':LAN_LOCAL_EXIT_ID,'name':'本地出口','__direct__':True}
            elif eid in by_id:
                route=dict(by_id[eid]);route['__direct__']=False
                routes[ip]=route
        with self.lock:
            self.routes=routes

    async def _accept(self, reader, writer):
        task=asyncio.current_task()
        if len(self.tasks)>=MAX_GATEWAY_CONNECTIONS:
            writer.close();return
        self.tasks.add(task)
        upstream=None
        replied=False
        peer=writer.get_extra_info('peername')
        ip=peer[0] if peer else ''
        self.task_ips[task]=ip
        try:
            raw=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),20)
            # V2.9.29.8: this concrete TCP listener, not Bonjour/ARP/saved
            # pairing, provides the only positive evidence for device visibility.
            self.events.put(('traffic', ip, '', '本机代理端口收到请求'))
            diagnostic_token=''
            if ip=='127.0.0.1':
                for header in raw.split(b'\r\n')[1:]:
                    key,_,value=header.partition(b':')
                    if key.lower()==b'proxy-authorization':
                        scheme,_,encoded=value.strip().partition(b' ')
                        if scheme.lower()==b'basic':
                            try:
                                user,_,secret=base64.b64decode(encoded,validate=True).decode().partition(':')
                                if user=='qizi-diagnostic':diagnostic_token=secret
                            except (ValueError,UnicodeError):pass
            with self.lock:
                route=(self.diagnostics.get(diagnostic_token) if diagnostic_token else None)
                if route is None:route=self.routes.get(ip)
            if route is None and self.resolver is not None:
                try:
                    route=await asyncio.wait_for(asyncio.to_thread(self.resolver,ip),timeout=2.5)
                except Exception:
                    route=None
                if route is not None:
                    route=dict(route)
                    with self.lock:self.routes[ip]=route
                    self.events.put(('traffic',ip,route.get('id',''),'新IP已自动学习'))
            if route is None:
                await self._error(writer,403,'Network blocked by controller')
                self.events.put(('traffic',ip,'','拒绝：设备默认为禁止网络或指定出口尚未通过定位联锁'))
                return

            lines=raw[:-4].split(b'\r\n')
            method,target,version=lines[0].decode('ascii').split()
            if version not in ('HTTP/1.0','HTTP/1.1'):
                raise ValueError('unsupported HTTP version')
            headers=[]
            for line in lines[1:]:
                key,sep,value=line.partition(b':')
                if not sep or key.strip()!=key:raise ValueError('invalid header')
                if key.lower() not in (b'proxy-authorization',b'proxy-connection',b'connection'):
                    headers.append(line)

            from urllib.parse import urlsplit
            direct=bool(route.get('__direct__'))
            if method=='CONNECT':
                parsed=urlsplit('//'+target)
                if not parsed.hostname or not parsed.port or parsed.username:
                    raise ValueError('invalid CONNECT target')
                if direct:
                    up_reader,upstream=await asyncio.wait_for(
                        asyncio.open_connection(parsed.hostname,int(parsed.port),limit=65536),10)
                    replied=True
                    writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                    await writer.drain()
                else:
                    outgoing=f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n'.encode('ascii')
                    if route.get('username'):
                        credentials=(route['username']+':'+route.get('password','')).encode('utf-8')
                        outgoing+=b'Proxy-Authorization: Basic '+base64.b64encode(credentials)+b'\r\n'
                    outgoing+=b'\r\n'
                    up_reader,upstream=await asyncio.wait_for(
                        asyncio.open_connection(route['host'],int(route['port']),limit=65536),10)
                    upstream.write(outgoing);await upstream.drain()
                    response=await asyncio.wait_for(up_reader.readuntil(b'\r\n\r\n'),15)
                    status=response.split(b'\r\n',1)[0].split()
                    if (len(status)<2 or status[0] not in (b'HTTP/1.0',b'HTTP/1.1')
                            or len(status[1])!=3 or not status[1].isdigit()
                            or not 200<=int(status[1])<300):
                        code=status[1].decode('ascii','replace') if len(status)>1 else '?'
                        await self._error(writer,502,'Upstream CONNECT failed');replied=True
                        self.events.put(('traffic',ip,route.get('id',''),'上游拒绝 CONNECT，状态 '+code))
                        return
                    replied=True
                    writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                    await writer.drain()
            else:
                parsed=urlsplit(target)
                if parsed.scheme!='http' or not parsed.hostname or parsed.username:
                    raise ValueError('use CONNECT for HTTPS')
                if direct:
                    port=int(parsed.port or 80)
                    path=parsed.path or '/'
                    if parsed.query:path+='?'+parsed.query
                    first=f'{method} {path} {version}'.encode('ascii')
                    outgoing=first+b'\r\n'+b'\r\n'.join(headers)+b'\r\nConnection: close\r\n\r\n'
                    up_reader,upstream=await asyncio.wait_for(
                        asyncio.open_connection(parsed.hostname,port,limit=65536),10)
                    upstream.write(outgoing);await upstream.drain()
                else:
                    outgoing=lines[0]+b'\r\n'+b'\r\n'.join(headers)+b'\r\nConnection: close\r\n'
                    if route.get('username'):
                        credentials=(route['username']+':'+route.get('password','')).encode('utf-8')
                        outgoing+=b'Proxy-Authorization: Basic '+base64.b64encode(credentials)+b'\r\n'
                    outgoing+=b'\r\n'
                    up_reader,upstream=await asyncio.wait_for(
                        asyncio.open_connection(route['host'],int(route['port']),limit=65536),10)
                    upstream.write(outgoing);await upstream.drain()

            for stream in (writer,upstream):
                transport_socket=stream.get_extra_info('socket') if stream else None
                if transport_socket is not None:
                    try:transport_socket.setsockopt(socket.SOL_SOCKET,socket.SO_KEEPALIVE,1)
                    except OSError:pass
            replied=True
            self.events.put(('traffic',ip,route.get('id',''),'已转发'))
            last_activity=[time.monotonic()]
            async def pump(source,destination):
                while True:
                    try:data=await asyncio.wait_for(source.read(65536),60)
                    except asyncio.TimeoutError:
                        if time.monotonic()-last_activity[0]>=300:raise
                        continue
                    if data:last_activity[0]=time.monotonic()
                    if not data:
                        if destination.can_write_eof():
                            destination.write_eof();await destination.drain()
                        return
                    destination.write(data);await destination.drain()
            sending=asyncio.create_task(pump(reader,upstream))
            receiving=asyncio.create_task(pump(up_reader,writer))
            try:
                done,_=await asyncio.wait((sending,receiving),return_when=asyncio.FIRST_COMPLETED)
                for finished in done:finished.result()
                if receiving not in done:await receiving
            finally:
                sending.cancel();receiving.cancel()
                await asyncio.gather(sending,receiving,return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not replied:
                try:await self._error(writer,502,'Proxy request failed')
                except Exception:pass
            self.events.put(('traffic',ip,'','转发失败：'+type(exc).__name__+
                             (f' (系统码 {exc.errno})' if isinstance(exc,OSError) and exc.errno else '')))
        finally:
            for stream in (upstream,writer):
                if stream:
                    stream.close()
                    try:await asyncio.wait_for(stream.wait_closed(),2)
                    except Exception:pass
            self.tasks.discard(task);self.task_ips.pop(task,None)


def select_this_pc_lan_interface():
    """Select only this PC's active/default LAN interface for the private gateway."""
    interfaces=current_lan_control_interfaces()
    if not interfaces:
        return None
    try:
        default_index=get_default_interface_index()
    except Exception:
        default_index=None
    if default_index is not None:
        for item in interfaces:
            try:
                if int(item.get('index',0))==int(default_index):
                    return item
            except Exception:
                pass
    return interfaces[0]


class LanSimpleApp(SimpleApp):
    """V2.6.4: same iOS/location stack, LAN proxy control replaces Windows-hotspot control."""
    def __init__(self):
        super().__init__()
        old_gateway=getattr(self,'persistent_gateway',None)
        if old_gateway is not None and old_gateway.thread and old_gateway.thread.is_alive():
            try:old_gateway.stop()
            except Exception:pass
        self._lan_interface=select_this_pc_lan_interface()
        lan_host=(self._lan_interface or {}).get('ip') or '127.0.0.1'
        gateway=LanPersistentGateway(self.events,host=lan_host,port=GATEWAY_PORT,
                                     resolver=self.resolve_proxy_for_ip)
        gateway.auto_port=True
        self.persistent_gateway=gateway
        self._hotspot_interfaces_cache=[self._lan_interface] if self._lan_interface else []
        self._lan_gateway_seen_ips=set()
        self._lan_gateway_seen_macs=set()
        self._lan_guard_cleaned=False
        # MACs deleted during this run. Used only to reject already-queued stale
        # learning events; a genuinely new request (old_ip=None + default block)
        # clears the tombstone and creates a fresh blocked device.
        self._lan_deleted_macs=set()
        self._normalize_lan_device_modes(save=True)
        self.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei')
        self.gateway_info.set(f'本机独立网关准备中：{lan_host}:{GATEWAY_PORT}')
        self.log_operation('设备列表仅显示已配置设备及访问本机代理的设备；删除后再次访问将按新设备默认禁止网络。',dedupe=False)
        self.refresh_devices()

    def on_listener_ready(self,gateway):
        super().on_listener_ready(gateway)
        self.gateway_info.set(f'本机独立网关：http://{gateway.host}:{gateway.port}｜仅显示本机代理访问设备及已配置设备')

    def _normalize_lan_device_modes(self,save=False):
        valid_ids={p.get('id') for p in self.exits}
        changed=[]
        for ip,d in self.devices.items():
            eid=str(d.get('exit_id','') or '')
            if not eid or (eid not in (LAN_BLOCK_EXIT_ID,LAN_LOCAL_EXIT_ID) and eid not in valid_ids):
                if eid!=LAN_BLOCK_EXIT_ID:
                    d['exit_id']=LAN_BLOCK_EXIT_ID;changed.append(ip)
        if changed and save:
            save_devices(self.devices)
        return changed

    def scan_worker(self):
        try:
            primary=select_this_pc_lan_interface()
            interfaces=[primary] if primary else []
            self._lan_interface=primary
            self._hotspot_interfaces_cache=list(interfaces)
            if not interfaces:
                raise RuntimeError('没有识别到当前上网的局域网接口/默认网关')
            found,info=scan_hotspot_devices(interfaces)
            gateways={str(x.get('gateway','')).strip() for x in interfaces if x.get('gateway')}
            own={x.get('ip') for x in interfaces}
            found=[x for x in found if x.get('ip') not in gateways and x.get('ip') not in own]
            # V2.6.7 startup recovery:
            # 1) A device that reaches this PC gateway in the current run is visible.
            # 2) A device already bound/saved by THIS program is also restored as soon
            #    as its IP/MAC is present on the LAN. This fixes the old behavior where
            #    an iPhone already connected before program startup stayed invisible
            #    until Wi-Fi was disconnected/reconnected.
            # 3) Unknown same-router devices are still hidden until they actually use
            #    this PC gateway, preserving multi-PC isolation.
            seen=set(getattr(self,'_lan_gateway_seen_ips',set()) or set())
            with self.device_lock:
                saved_ips=set(self.devices.keys())
                saved_macs={normalize_mac(d.get('mac','')) for d in self.devices.values()}
                saved_macs={m for m in saved_macs if valid_device_mac(m)}
            visible=[]
            for row in found:
                rip=str(row.get('ip','') or '')
                rmac=normalize_mac(row.get('mac',''))
                if rip in seen or rip in saved_ips or (valid_device_mac(rmac) and rmac in saved_macs):
                    visible.append(row)
                    # Once a saved binding is confirmed alive on this LAN, treat its
                    # current IP as belonging to this gateway session too.
                    if rip:
                        self._lan_gateway_seen_ips.add(rip)
            found=visible
            self.events.put(('scan_ready',found,info,interfaces))
        except Exception as exc:
            self.events.put(('devices_scan_error',f'{type(exc).__name__}: {exc}'))

    def _hotspot_snapshot(self):
        cached=list(getattr(self,'_hotspot_interfaces_cache',[]) or [])
        if cached:return cached
        try:cached=current_lan_control_interfaces()
        except Exception:cached=[]
        if cached:self._hotspot_interfaces_cache=list(cached)
        return cached

    def migrate_device_bindings_by_mac(self,found):
        changed=super().migrate_device_bindings_by_mac(found)
        normalized=self._normalize_lan_device_modes(save=True)
        return bool(changed or normalized)

    def exit_name_by_id(self,exit_id):
        if exit_id==LAN_BLOCK_EXIT_ID:return '禁止网络'
        if exit_id==LAN_LOCAL_EXIT_ID:return '本地出口'
        return super().exit_name_by_id(exit_id)

    def choice_values(self):
        ids=[LAN_BLOCK_EXIT_ID,LAN_LOCAL_EXIT_ID]
        labels=['禁止网络','本地出口']
        raw=[self.exit_label(p) for p in self.exits]
        for p,label in zip(self.exits,raw):
            ids.append(p['id'])
            labels.append(label if raw.count(label)==1 else label+' ['+p['id'][:5]+']')
        return ids,labels

    def gateway_bindings(self):
        by_id={p.get('id'):p for p in self.exits}
        result=[]
        for ip,d in self.devices.items():
            eid=d.get('exit_id','')
            if eid==LAN_LOCAL_EXIT_ID:
                result.append((ip,eid,{'id':LAN_LOCAL_EXIT_ID,'name':'本地出口','__direct__':True}))
            elif eid in by_id:
                result.append((ip,eid,by_id[eid]))
        return result

    def bind_row_exit(self,ip):
        uid=self.uid_for_ip(ip)
        if uid and self._route_is_running(uid):
            self.log_operation(f'{ip}：自动寻路执行期间出口由寻路网络策略接管，不能手动修改')
            self.refresh_devices(ip);return
        row=self.row_widgets[ip];index=row['combo'].current();row['menu_open']=False
        if index<0 or index>=len(row['ids']):return
        eid=row['ids'][index]
        if eid not in (LAN_BLOCK_EXIT_ID,LAN_LOCAL_EXIT_ID) and not any(p.get('id')==eid for p in self.exits):
            return self.refresh_devices()
        uid=self.uid_for_ip(ip)
        if uid:self._location_network_started.pop(uid,None)
        self._location_network_allowed.discard(ip)
        gateway=getattr(self,'persistent_gateway',None)
        if gateway is not None:gateway.revoke_ip(ip)
        with self.device_lock:self.devices[ip]['exit_id']=eid
        save_devices(self.devices)
        self._sync_location_network_policy()
        self._update_live_routes();self.refresh_devices()
        label=self.exit_name_by_id(eid)
        if eid==LAN_BLOCK_EXIT_ID:
            self.log_operation(f'{ip}：网络模式切换为 禁止网络，已有代理连接已立即断开')
        elif eid==LAN_LOCAL_EXIT_ID:
            self.log_operation(f'{ip}：网络模式切换为 本地出口，不需要开启定位')
        else:
            self.log_operation(f'{ip}：指定出口切换为 {label}；重新计时，定位连续10秒后放行')
        self.detail.set(ip+'：网络控制已更新。')

    def refresh_devices(self,selected_ip=None):
        changed=self._normalize_lan_device_modes(save=False)
        if changed:
            gateway=getattr(self,'persistent_gateway',None)
            if gateway is not None:
                for ip in changed:gateway.revoke_ip(ip)
            try:save_devices(self.devices)
            except Exception:pass
        super().refresh_devices(selected_ip)
        for ip,row in list(getattr(self,'row_widgets',{}).items()):
            d=self.devices.get(ip,{})
            eid=d.get('exit_id','')
            if eid==LAN_BLOCK_EXIT_ID:
                row['combo'].configure(style='Bad.TCombobox')
            elif eid==LAN_LOCAL_EXIT_ID:
                row['combo'].configure(style='Good.TCombobox')

    def _sync_direct_source_guard(self):
        if not getattr(self,'_lan_guard_cleaned',False):
            clear_direct_block_rules()
            self._lan_guard_cleaned=True
            self._direct_block_ips=set()
        interfaces=list(getattr(self,'_hotspot_interfaces_cache',[]) or [])
        addresses=' / '.join(x.get('ip','') for x in interfaces if x.get('ip')) or '等待识别'
        names='；'.join(f"{x.get('alias','')} {x.get('ip','')}/{x.get('prefix','')}" for x in interfaces)
        if hasattr(self,'hotspot_info'):
            self.hotspot_info.set('局域网接口：'+(names or '等待扫描'))
        gateway=getattr(self,'persistent_gateway',None)
        port=getattr(gateway,'port',GATEWAY_PORT) if gateway is not None else GATEWAY_PORT
        if hasattr(self,'gateway_info'):
            self.gateway_info.set(
                f'同Wi-Fi代理：{addresses}:{port} ｜ 默认禁止网络 ｜ 本地出口直接走电脑网络 ｜ 指定出口需定位连续10秒')

    def _maybe_auto_start_location(self,ip,uid=None,reason='Wi-Fi 已连接'):
        """Only a named/specified exit may auto-start location. Local/blocked never do."""
        eid=str(self.devices.get(ip,{}).get('exit_id','') or '')
        valid_ids={p.get('id') for p in self.exits}
        if eid not in valid_ids:
            return False
        return super()._maybe_auto_start_location(ip,uid,reason)

    def _update_live_routes(self):
        gateway=getattr(self,'persistent_gateway',None)
        if gateway is None:return
        now=time.monotonic()
        valid_ids={p.get('id') for p in self.exits}
        with self.device_lock:
            allowed={}
            for ip,d in self.devices.items():
                eid=d.get('exit_id','')
                if eid==LAN_LOCAL_EXIT_ID:
                    allowed[ip]=dict(d)
                elif eid in valid_ids and self._location_gate_is_allowed_ip(ip,now):
                    allowed[ip]=dict(d)
            gateway.update(self.exits,allowed)

    def _apply_learned_ip(self, old_ip, new_ip, mac, interface, eid, remark, manual):
        """Prevent a queued pre-delete learning event from resurrecting old policy."""
        mac=normalize_mac(mac)
        deleted=getattr(self,'_lan_deleted_macs',set())
        if valid_device_mac(mac) and mac in deleted:
            # Only a resolver event with no previous profile and the default BLOCK
            # policy is allowed to turn this physical phone into a fresh device.
            if old_ip is None and eid==LAN_BLOCK_EXIT_ID:
                deleted.discard(mac)
            else:
                return False
        return super()._apply_learned_ip(old_ip,new_ip,mac,interface,eid,remark,manual)

    def delete_device(self):
        """Hard-delete one device profile and revoke every live/stored policy for it."""
        ip=self.selected_ip
        if not ip or ip not in self.devices:return
        d=dict(self.devices.get(ip,{}) or {})
        mac=normalize_mac(d.get('mac',''))
        related_ips={ip}
        if valid_device_mac(mac):
            related_ips.update(candidate_ip for candidate_ip,device in self.devices.items()
                               if normalize_mac(device.get('mac',''))==mac)
        related_uids={str(uid) for uid in [self.uid_for_ip(ip)] if uid}
        related_ip_keys={'ip:'+candidate_ip for candidate_ip in related_ips}
        for link_key,linked_uid in self.links.items():
            if (link_key in related_ip_keys
                    or (valid_device_mac(mac) and link_key=='mac:'+mac)) and linked_uid:
                related_uids.add(str(linked_uid))
        if any(uid in self.ios.workers for uid in related_uids):
            return messagebox.showinfo('删除','请先右键断开这台手机，再删除。',parent=self)

        # Critical: update(routes=...) alone does not terminate an already-open
        # CONNECT/direct tunnel. Revoke first so a previous LOCAL route cannot
        # keep carrying traffic after the profile is deleted/recreated as BLOCK.
        gateway=getattr(self,'persistent_gateway',None)
        if gateway is not None:
            for candidate_ip in related_ips:
                gateway.revoke_ip(candidate_ip)

        if valid_device_mac(mac):
            self._lan_deleted_macs.add(mac)
            self._lan_gateway_seen_macs.discard(mac)
        for candidate_ip in related_ips:
            self._lan_gateway_seen_ips.discard(candidate_ip)
            self._location_network_allowed.discard(candidate_ip)
            self._location_network_phase.pop(candidate_ip,None)

        with self.device_lock:
            for candidate_ip in related_ips:
                self.devices.pop(candidate_ip,None)
                self.pending_actions.pop(candidate_ip,None)
                self.trust_targets.pop(candidate_ip,None)
                self.coordinates.pop('ip:'+candidate_ip,None)

        for key,value in list(self.links.items()):
            if (value in related_uids or key in related_ip_keys
                    or (valid_device_mac(mac) and key=='mac:'+mac)):
                self.links.pop(key,None)
        self._startup_saved_links={key:value for key,value in self._startup_saved_links.items()
                                   if value not in related_uids
                                   and key not in related_ip_keys
                                   and not (valid_device_mac(mac) and key=='mac:'+mac)}
        for uid in related_uids:
            self.coordinates.pop(uid,None)
            self.route_states.pop(uid,None)
            self.route_plans.pop(uid,None)
            self.ios_states.pop(uid,None)
            self.discovered.pop(uid,None)
            self._last_discovered_uids.discard(uid)
            self._location_network_started.pop(uid,None)
            self._auto_restore_uids.discard(uid)
            self._auto_attempted_uids.discard(uid)
            self._qizi_known_trust.discard(uid)
            self._qizi_route_positions.pop(uid,None)
            self._qizi_live_positions.pop(uid,None)
            self._qizi_checkpoint_last_write.pop(uid,None)
            self._qizi_live_last_write.pop(uid,None)
            runtime=self.route_runtime.pop(uid,{})
            job=runtime.get('job')
            if job is not None:
                try:self.after_cancel(job)
                except Exception:pass
            sender=runtime.get('usb_sender')
            if sender is not None:
                try:sender.stop()
                except Exception:pass
            dialog=self.route_dialogs.pop(uid,None)
            if dialog is not None:
                try:
                    if dialog.winfo_exists():dialog.destroy()
                except Exception:pass

        self._save_route_state()
        self._save_route_plans()
        cache_files=(
            (self._qizi_trust_cache_path,{uid:True for uid in sorted(self._qizi_known_trust)}),
            (self._qizi_route_checkpoint_path,self._qizi_route_positions),
            (self._qizi_live_positions_path,self._qizi_live_positions),
        )
        for path,data in cache_files:
            try:save_json(path,data)
            except OSError as exc:self.log_operation('删除设备后清理缓存失败：'+str(exc)[:100])

        self.save_ios();save_devices(self.devices)
        self._update_live_routes();self.refresh_devices()
        self.log_operation(ip+'：设备全部本地关联、信任、路线及定位配置已清理；再次访问本机代理时按新设备默认禁止网络处理')

    def resolve_proxy_for_ip(self,new_ip):
        """Resolve LAN source IP->MAC->saved policy without allowing a location-gate bypass."""
        if new_ip not in ('127.0.0.1','::1'):
            self._lan_gateway_seen_ips.add(new_ip)
        row=self._neighbor_for_ip(new_ip)
        if not row:return None
        mac=normalize_mac(row.get('mac',''))
        if not valid_device_mac(mac):return None
        self._lan_gateway_seen_macs.add(mac)
        with self.device_lock:
            profile_ip=None;profile=None
            exact=self.devices.get(new_ip)
            if exact is not None:
                profile_ip=new_ip;profile=dict(exact)
            else:
                for old_ip,device in self.devices.items():
                    if normalize_mac(device.get('mac',''))==mac:
                        profile_ip=old_ip;profile=dict(device);break
        if profile is None:
            self.events.put(('device_ip_learned_v9',None,new_ip,mac,row.get('interface',''),
                             LAN_BLOCK_EXIT_ID,'',False))
            return None
        eid=str(profile.get('exit_id','') or LAN_BLOCK_EXIT_ID)
        if (profile_ip!=new_ip or normalize_mac(profile.get('mac',''))!=mac
                or profile.get('interface','')!=row.get('interface','')):
            self.events.put(('device_ip_learned_v9',profile_ip,new_ip,mac,row.get('interface',''),
                             eid,profile.get('remark',''),bool(profile.get('manual',False))))
        if eid==LAN_BLOCK_EXIT_ID:
            return None
        if eid==LAN_LOCAL_EXIT_ID:
            return {'id':LAN_LOCAL_EXIT_ID,'name':'本地出口','__direct__':True,'_device_mac':mac}
        proxy=next((dict(p) for p in self.exits if p.get('id')==eid),None)
        if proxy is None:return None
        uid=self._location_gate_uid_for_mac(mac) or self.uid_for_ip(new_ip)
        if not self._location_gate_is_allowed_uid(uid):
            return None
        proxy['__direct__']=False;proxy['_device_mac']=mac
        return proxy

    def _sync_location_network_policy(self):
        """LAN policy: local is immediate; remote exits retain the original 10-second location interlock."""
        now=time.monotonic();live_uids=set()
        valid_ids={p.get('id') for p in self.exits}
        for ip,d in list(self.devices.items()):
            eid=d.get('exit_id','')
            uid=self._location_gate_uid_for_mac(d.get('mac','')) or self.uid_for_ip(ip)
            state=self.ios_states.get(uid,{}) if uid else {}
            old_allowed=ip in self._location_network_allowed

            if eid==LAN_LOCAL_EXIT_ID:
                if old_allowed:self._location_network_allowed.discard(ip)
                self._location_network_phase[ip]='local'
                if uid:self._location_network_started.pop(uid,None)
                continue
            if eid==LAN_BLOCK_EXIT_ID or eid not in valid_ids:
                if old_allowed:
                    self._location_network_allowed.discard(ip)
                    gateway=getattr(self,'persistent_gateway',None)
                    if gateway is not None:gateway.revoke_ip(ip)
                self._location_network_phase[ip]='blocked'
                if uid:self._location_network_started.pop(uid,None)
                continue

            candidate=bool(uid and state.get('wifi')=='连接中' and (state.get('location')=='定位中' or self._route_is_running(uid)))
            if candidate:
                live_uids.add(uid);self._location_network_started.setdefault(uid,now)
            allowed=bool(candidate and self._location_gate_is_allowed_uid(uid,now))
            if allowed and not old_allowed:
                self._location_network_allowed.add(ip)
                self.log_operation(f'{ip}：定位已连续开启10秒，已放行指定出口网络')
            elif not allowed and old_allowed:
                self._location_network_allowed.discard(ip)
                gateway=getattr(self,'persistent_gateway',None)
                if gateway is not None:gateway.revoke_ip(ip)
                self.log_operation(f'{ip}：定位已断开/未满足条件，已立即关闭指定出口网络')
            if allowed:self._location_network_phase[ip]='allowed'
            elif candidate:
                remain=max(0,int(self.LOCATION_NETWORK_DELAY-(now-self._location_network_started.get(uid,now))+0.999))
                self._location_network_phase[ip]='waiting:'+str(remain)
            else:self._location_network_phase[ip]='blocked'

        for uid in list(self._location_network_started):
            if uid not in live_uids:self._location_network_started.pop(uid,None)
        stale=set(self._location_network_allowed)-set(self.devices)
        for ip in stale:
            self._location_network_allowed.discard(ip)
            gateway=getattr(self,'persistent_gateway',None)
            if gateway is not None:gateway.revoke_ip(ip)
        self._update_live_routes()


def qizi_activation_gate():
    """V2.6.1 启动硬锁：授权无效时只显示小型激活窗，主程序完全不初始化。"""
    machine=qizi_machine_code()
    code,data=load_qizi_license()
    if code and data:
        return True

    gate=tk.Tk();gate.title('多设备管理器-v3.10.18 有问题联系TG:Laosermei');gate.geometry('430x245');gate.resizable(False,False)
    result={'ok':False}
    try:
        gate.update_idletasks()
        x=max(0,(gate.winfo_screenwidth()-430)//2);y=max(0,(gate.winfo_screenheight()-245)//2)
        gate.geometry(f'430x245+{x}+{y}')
    except Exception:pass
    box=ttk.Frame(gate,padding=16);box.pack(fill='both',expand=True)
    ttk.Label(box,text='软件授权',font=('Microsoft YaHei UI',12,'bold')).pack(anchor='w')
    ttk.Label(box,text='机器码').pack(anchor='w',pady=(12,3))
    mrow=ttk.Frame(box);mrow.pack(fill='x')
    shown=machine[:18]+'…' if len(machine)>18 else machine
    mv=tk.StringVar(value=shown)
    ttk.Entry(mrow,textvariable=mv,state='readonly').pack(side='left',fill='x',expand=True)
    status=tk.StringVar(value='未激活 / 授权已到期')
    def copy_machine():
        gate.clipboard_clear();gate.clipboard_append(machine);status.set('机器码已复制')
    ttk.Button(mrow,text='复制',width=7,command=copy_machine).pack(side='left',padx=(6,0))
    ttk.Label(box,text='激活码').pack(anchor='w',pady=(10,3))
    av=tk.StringVar();entry=ttk.Entry(box,textvariable=av,show='•');entry.pack(fill='x')
    ttk.Label(box,textvariable=status).pack(anchor='w',pady=(8,0))
    def activate():
        value=av.get().strip()
        if not value:status.set('请输入激活码');return
        try:
            verified=verify_qizi_license(value,machine);save_qizi_license(value);result['ok']=True
            exp=int(verified.get('expires',0) or 0)
            status.set('激活成功' if not exp else '激活成功，到期：'+time.strftime('%Y-%m-%d',time.localtime(exp)))
            gate.after(300,gate.destroy)
        except Exception as exc:status.set('激活失败：'+str(exc))
    buttons=ttk.Frame(box);buttons.pack(fill='x',pady=(12,0))
    ttk.Button(buttons,text='激活并进入',command=activate).pack(side='right')
    ttk.Button(buttons,text='退出',command=gate.destroy).pack(side='right',padx=(0,8))
    entry.bind('<Return>',lambda e:activate());entry.focus_set();gate.mainloop()
    return bool(result['ok'])



if __name__=='__main__':
    # V2.6.1：先授权、后初始化。未授权时不会读取旧配置、不会自动扫描、
    # 不会启动出口、Tunnel、定位、自动恢复或其他主程序后台任务。
    if qizi_activation_gate():
        from qizi_exit_ui_engine import make_app_class
        IntegratedApp=make_app_class(LanSimpleApp, {
            'DATA_DIR': APP_DIR, 'EXITS_FILE': APP_DIR / 'proxy_exits.json',
            'ExitDialog': ExitDialog, 'save_json': save_json, 'save_devices': save_devices,
            'test_http_exit': test_http_exit,
            'scan_hotspot_devices': scan_hotspot_devices,
            'select_this_pc_lan_interface': select_this_pc_lan_interface,
            'normalize_mac': normalize_mac, 'valid_device_mac': valid_device_mac,
        })
        integrated_main()
