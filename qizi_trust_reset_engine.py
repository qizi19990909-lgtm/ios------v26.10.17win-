# -*- coding: utf-8 -*-
"""七子多设备管理器：仅对用户明确选择的 USB iPhone 重置 Windows 配对记录。

不扫描/删除其他设备的 Lockdown 记录。不在 UI 线程执行命令。
对应用户的三个步骤：备份并移除目标 plist -> 重启 Apple Mobile Device Service -> 检查服务。
Windows 下运行前要求管理员权限（必要时触发 UAC）；PowerShell 进程执行完自动退出。
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

UDID_PATTERN = re.compile(r'(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{16}|[0-9a-fA-F]{40})\Z')
SERVICE_NAME = 'Apple Mobile Device Service'


def validate_udid(value):
    value = str(value or '').strip()
    if not UDID_PATTERN.fullmatch(value):
        raise ValueError('UDID 格式不符合 iPhone USB 设备编号，已拒绝处理')
    return value.upper()


def usb_candidates(raw):
    """只接受本次 pymobiledevice3 usbmux list 确认的 USB 设备。"""
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, list):
        raise ValueError('USB 检测没有返回设备列表')
    result = {}
    for device in raw:
        if not isinstance(device, dict) or str(device.get('ConnectionType', '')).upper() != 'USB':
            continue
        value = device.get('UniqueDeviceID') or device.get('UDID') or device.get('Identifier')
        try:
            udid = validate_udid(value)
        except ValueError:
            continue
        result[udid] = dict(udid=udid, name=str(device.get('DeviceName') or 'iPhone')[:60])
    return list(result.values())


def _ps_single_quoted(value):
    """Literal PowerShell single-quoted string (never interpolate user content)."""
    return "'" + str(value).replace("'", "''") + "'"


def build_reset_script(udid, status_path):
    """Only exact UDID.plist and UDID.plist.tmp are touched; copy SHA256 verified first."""
    udid = validate_udid(udid)
    status_path = str(Path(status_path).resolve())
    return r'''$ErrorActionPreference = 'Stop'
$Udid = __UDID__
$ResultPath = __STATUS__
$Folder = Join-Path $env:ProgramData 'Apple\Lockdown'
$ServiceName = 'Apple Mobile Device Service'
$Result = [ordered]@{
    ok = $false; udid = $Udid; removed = @(); backup_dir = ''
    service_status = ''; error = ''; remaining = @()
}
$ExitCode = 0
try {
    if (-not (Test-Path -LiteralPath $Folder -PathType Container)) {
        throw 'Apple Lockdown 文件夹不存在；本次未删除任何文件。'
    }
    # Preflight before touching pair records: the service must be installed.
    $null = Get-Service -Name $ServiceName -ErrorAction Stop
    $Desktop = [Environment]::GetFolderPath('Desktop')
    if ([string]::IsNullOrWhiteSpace($Desktop)) {
        $Desktop = Join-Path $env:USERPROFILE 'Desktop'
    }
    $BackupRoot = Join-Path $Desktop 'pair_backup'
    $BackupFolder = Join-Path $BackupRoot ($Udid + '_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '_' + $PID)
    $null = New-Item -ItemType Directory -Path $BackupFolder -Force -ErrorAction Stop
    $Result.backup_dir = $BackupFolder
    # Only these two literal filenames, never wildcard delete or the whole folder.
    $Files = @( ($Udid + '.plist'), ($Udid + '.plist.tmp') )
    foreach ($Name in $Files) {
        $Source = Join-Path $Folder $Name
        if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) { continue }
        $BackupFile = Join-Path $BackupFolder $Name
        Copy-Item -LiteralPath $Source -Destination $BackupFile -ErrorAction Stop
        $Hash1 = (Get-FileHash -LiteralPath $Source -Algorithm SHA256 -ErrorAction Stop).Hash
        $Hash2 = (Get-FileHash -LiteralPath $BackupFile -Algorithm SHA256 -ErrorAction Stop).Hash
        if ($Hash1 -ne $Hash2) { throw ('备份校验失败，未删除 ' + $Name) }
        Remove-Item -LiteralPath $Source -Force -ErrorAction Stop
        if (Test-Path -LiteralPath $Source) { throw ('配对记录未能移除: ' + $Name) }
        $Result.removed += $Name
    }
    foreach ($Name in $Files) {
        if (Test-Path -LiteralPath (Join-Path $Folder $Name)) { $Result.remaining += $Name }
    }
    if ($Result.remaining.Count -gt 0) { throw '仍有目标 UDID 的配对记录，停止执行。' }
    Restart-Service -Name $ServiceName -Force -ErrorAction Stop
    $Service = Get-Service -Name $ServiceName -ErrorAction Stop
    $Service.WaitForStatus([System.ServiceProcess.ServiceControllerStatus]::Running, [TimeSpan]::FromSeconds(25))
    $Service.Refresh()
    $Result.service_status = $Service.Status.ToString()
    if ($Service.Status -ne 'Running') { throw 'Apple Mobile Device Service 没有恢复运行。' }
    $Result.ok = $true
} catch {
    $Result.error = $_.Exception.Message
    $ExitCode = 1
} finally {
    try {
        $Result | ConvertTo-Json -Compress -Depth 5 | Set-Content -LiteralPath $ResultPath -Encoding UTF8 -ErrorAction Stop
    } catch {
        $ExitCode = 2
    }
}
exit $ExitCode
'''.replace('__UDID__', _ps_single_quoted(udid)).replace('__STATUS__', _ps_single_quoted(status_path))


def _run_elevated_windows(executable, args):
    """ShellExecuteEx(runAs) prompts for UAC, waits, retrieves exit code, closes handle."""
    if os.name != 'nt':
        raise OSError('本功能只支持 Windows')

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ('cbSize', wintypes.DWORD), ('fMask', wintypes.ULONG),
            ('hwnd', wintypes.HWND), ('lpVerb', wintypes.LPCWSTR),
            ('lpFile', wintypes.LPCWSTR), ('lpParameters', wintypes.LPCWSTR),
            ('lpDirectory', wintypes.LPCWSTR), ('nShow', ctypes.c_int),
            ('hInstApp', wintypes.HINSTANCE), ('lpIDList', ctypes.c_void_p),
            ('lpClass', wintypes.LPCWSTR), ('hkeyClass', wintypes.HKEY),
            ('dwHotKey', wintypes.DWORD), ('hIconOrMonitor', wintypes.HANDLE),
            ('hProcess', wintypes.HANDLE),
        ]

    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    ole32 = ctypes.WinDLL('ole32', use_last_error=True)
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    # The click starts a background Python thread; initialise COM for the shell UAC verb.
    com_result = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    com_owned = com_result in (0, 1)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell.ShellExecuteExW.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    sei.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = 'runas'
    sei.lpFile = executable
    sei.lpParameters = subprocess.list2cmdline(args)
    sei.nShow = 0  # SW_HIDE: no persistent console window, UAC prompt still appears
    try:
        if not shell.ShellExecuteExW(ctypes.byref(sei)):
            error = ctypes.get_last_error()
            if error == 1223:
                raise RuntimeError('已取消 Windows 管理员授权；没有修改配对记录')
            raise ctypes.WinError(error)
        if not sei.hProcess:
            raise RuntimeError('无法取得 PowerShell 进程句柄')
        try:
            wait = kernel.WaitForSingleObject(sei.hProcess, 180000)
            if wait != 0:
                if wait == 0x102:
                    raise RuntimeError('PowerShell 执行超过 180 秒，请检查服务状态；未强制结束系统服务操作')
                raise RuntimeError('PowerShell 进程等待异常')
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(sei.hProcess, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return int(code.value)
        finally:
            kernel.CloseHandle(sei.hProcess)
    finally:
        if com_owned:
            ole32.CoUninitialize()


def reset_windows_pairing(udid):
    """Run in background worker. Returns a safe dict; raises clear error on failure."""
    udid = validate_udid(udid)
    if os.name != 'nt':
        raise OSError('此操作需要 Windows 及 Apple Mobile Device Service')
    # Unique private status file name. Elevated script is passed inline via encoded command.
    status_path = Path(tempfile.gettempdir()) / ('qizi_trust_reset_' + uuid.uuid4().hex + '.json')
    script = build_reset_script(udid, status_path)
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    exe = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe')
    args = ['-NoLogo', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
            '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded]
    try:
        elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
        if elevated:
            run = subprocess.run([exe] + args, capture_output=True, text=True,
                                 encoding='utf-8', errors='replace', timeout=180,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
            exit_code = run.returncode
        else:
            exit_code = _run_elevated_windows(exe, args)
        if not status_path.is_file():
            raise RuntimeError(f'PowerShell 未写入步骤结果（退出码 {exit_code}）。请检查 UAC 或系统服务权限')
        report = json.loads(status_path.read_text(encoding='utf-8-sig'))
        if not isinstance(report, dict) or str(report.get('udid', '')).upper() != udid:
            raise RuntimeError('PowerShell 结果不匹配目标 UDID')
        if exit_code != 0 or not report.get('ok'):
            raise RuntimeError('设备信任重置失败：' + str(report.get('error') or f'PowerShell 退出码 {exit_code}')[:250])
        return report
    finally:
        try:status_path.unlink(missing_ok=True)
        except OSError:pass
