# -*- mode: python ; coding: utf-8 -*-
"""
多设备管理器 v3.10.10.1 - PyInstaller onedir SPEC

适用源码（与本 SPEC 同级）：
  qizi_ios_exit_simple_v2_9_29_9_route_checkpoint_trust_modular.py
  qizi_*.py 运行模块
  dbip-city-lite-*.mmdb
  LICENSE_LITE.TXT

输出：dist\\多设备管理\\多设备管理器.exe
"""

from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve()

APP_NAME = "多设备管理器"
DIST_FOLDER_NAME = "多设备管理"

SOURCE_CANDIDATES = [
    ROOT / "qizi_ios_exit_simple_v2_9_29_9_route_checkpoint_trust_modular.py",
]
SOURCE = next((p for p in SOURCE_CANDIDATES if p.is_file()), None)

MODULE_FILES = [
    ROOT / "qizi_exit_ui_engine.py",
    ROOT / "qizi_dynamic_ui_engine.py",
    ROOT / "qizi_dynamic_ip_engine.py",
    ROOT / "qizi_exit_import_engine.py",
    ROOT / "qizi_exit_usage_engine.py",
    ROOT / "qizi_geo_engine.py",
    ROOT / "qizi_geo_source_engine.py",
    ROOT / "qizi_gateway_ui_engine.py",
    ROOT / "qizi_route_ui_engine.py",
    ROOT / "qizi_route_engine.py",
    ROOT / "qizi_route_checkpoint_engine.py",
    ROOT / "qizi_location_engine.py",
    ROOT / "qizi_exit_engine.py",
]

MMDB_FILES = sorted(ROOT.glob("*.mmdb"))
MMDB_FILE = next(
    (path for path in MMDB_FILES if "city" in path.name.lower()),
    MMDB_FILES[0] if MMDB_FILES else None,
)
RUNTIME_COMPAT = ROOT / "packaging_runtime" / "sitecustomize.py"

IOS_RUNTIME_CANDIDATES = [
    ROOT / "ios_runtime",
    Path(r"D:\出口\ios_runtime"),
]
IOS_RUNTIME = next(
    (path for path in IOS_RUNTIME_CANDIDATES if (path / "python.exe").is_file()),
    None,
)

ICON_CANDIDATES = [
    ROOT / "installer.ico",
    Path(r"D:\出口\installer.ico"),
]
ICON_FILE = next((path for path in ICON_CANDIDATES if path.is_file()), None)

if SOURCE is None:
    raise SystemExit(
        "找不到 V3.10.10.1 主程序。\n"
        "请把 SPEC 与 qizi_ios_exit_simple_v2_9_29_9_route_checkpoint_trust_modular.py 放在同一目录。"
    )

missing_modules = [path.name for path in MODULE_FILES if not path.is_file()]
if missing_modules:
    raise SystemExit(
        "缺少运行模块：" + ", ".join(missing_modules) + "\n"
        "请把主程序、全部 qizi_*.py 运行模块和 SPEC 放在同一目录。"
    )

if MMDB_FILE is None:
    raise SystemExit(
        "找不到城市 MMDB 数据库。\n"
        "请把 dbip-city-lite-*.mmdb 放在 SPEC 同级目录。"
    )

if IOS_RUNTIME is None:
    raise SystemExit(
        "找不到 ios_runtime\\python.exe。\n\n"
        "请任选一种方式：\n"
        "1. 把完整 ios_runtime 复制到本目录的 ios_runtime；或\n"
        "2. 保留旧版 D:\\出口\\ios_runtime，SPEC 会自动复用。"
    )

if not (IOS_RUNTIME / "python313.dll").is_file():
    raise SystemExit(f"iOS Runtime 不完整：{IOS_RUNTIME} 中缺少 python313.dll。")

if not RUNTIME_COMPAT.is_file():
    raise SystemExit("缺少 Windows iOS Runtime 兼容补丁：packaging_runtime\\sitecustomize.py")

# pymobiledevice3 由外置 ios_runtime 运行，不冻结进主 EXE。
datas = []
binaries = []
hiddenimports = [
    # 主模块在启动/工厂函数中动态加载这些 UI 和功能模块。
    *[path.stem for path in MODULE_FILES],
    "tkinter",
    "tkinter.ttk",
    "tkinter.messagebox",
    "tkinter.simpledialog",
    "tkinter.filedialog",
    "tkinter.scrolledtext",
]

for package in (
    "requests",
    "urllib3",
    "certifi",
    "charset_normalizer",
    "idna",
    "cryptography",
    "cffi",
    "maxminddb",
):
    try:
        package_datas, package_binaries, package_imports = collect_all(package)
        datas += package_datas
        binaries += package_binaries
        hiddenimports += package_imports
    except Exception:
        try:
            hiddenimports += collect_submodules(package)
        except Exception:
            pass

# 程序以 exe 所在目录作为 APP_DIR，因此 MMDB 和许可必须与 exe 同级。
datas.append((str(IOS_RUNTIME), "ios_runtime"))
datas.append((str(MMDB_FILE), "."))
datas.append((str(RUNTIME_COMPAT), "ios_runtime/Lib/site-packages"))

icon = str(ICON_FILE) if ICON_FILE is not None else None
hiddenimports = list(dict.fromkeys(hiddenimports))

a = Analysis(
    [str(SOURCE)],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
    uac_admin=True,
    contents_directory=".",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=DIST_FOLDER_NAME,
)
