; 多设备管理器 v3.10.10.1 - NSIS 安装脚本
;
; PyInstaller 发布文件夹：dist\多设备管理
; MMDB 与 LICENSE_LITE.TXT 位于主 EXE 同级目录
; 最终安装包：多设备管理器v3.10.10.1客户端.exe

Unicode True
RequestExecutionLevel admin

SetCompressor /SOLID lzma
SetCompressorDictSize 64

!define APPNAME "多设备管理器"
!define VERSION "3.10.10.1"
!define PUBLISHER "七子"
!define EXE "多设备管理器.exe"
!define MMDB "GeoLite2-City.mmdb"

!define DISTDIR "多设备管理"
!define INSTALLDIR "$PROGRAMFILES64\多设备管理"

; 沿用原来的产品注册表路径，安装新版时仍识别为同一款软件。
!define PRODUCT_REG_KEY "Software\七子\多设备管理"
!define UNINSTALL_REG_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\多设备管理器"

Name "${APPNAME} ${VERSION}"
OutFile "多设备管理器v3.10.10.1客户端.exe"
InstallDir "${INSTALLDIR}"

; 故意不使用 InstallDirRegKey，避免历史 WiFi/WIFI 版路径覆盖固定安装目录。

ShowInstDetails show
ShowUninstDetails show
BrandingText "七子"

; 图标优先使用 NSI 同级的 installer.ico；否则尝试复用旧目录图标。
!if /FileExists "installer.ico"
  Icon "installer.ico"
  UninstallIcon "installer.ico"
!else
  !if /FileExists "D:\出口\installer.ico"
    Icon "D:\出口\installer.ico"
    UninstallIcon "D:\出口\installer.ico"
  !endif
!endif

Page directory
Page instfiles

UninstPage uninstConfirm
UninstPage instfiles

Section "主程序" SEC_MAIN
  SetShellVarContext all
  SetOutPath "$INSTDIR"

  ; 编译安装包前检查完整 onedir 产物。
  !if /FileExists "dist\${DISTDIR}\${EXE}"
  !else
    !error "缺少 dist\${DISTDIR}\${EXE}，请先使用 v3.10.10.1 SPEC 完成 PyInstaller 打包。"
  !endif

  !if /FileExists "dist\${DISTDIR}\ios_runtime\python.exe"
  !else
    !error "缺少 dist\${DISTDIR}\ios_runtime\python.exe。"
  !endif

  !if /FileExists "dist\${DISTDIR}\ios_runtime\python313.dll"
  !else
    !error "缺少 dist\${DISTDIR}\ios_runtime\python313.dll。"
  !endif

  !if /FileExists "dist\${DISTDIR}\ios_runtime\Lib\site-packages\sitecustomize.py"
  !else
    !error "缺少 Runtime IPv6 scope 兼容补丁 sitecustomize.py，请用最新 v3.10.10.1 SPEC 重新打包。"
  !endif

  !if /FileExists "dist\${DISTDIR}\${MMDB}"
  !else
    !error "缺少 dist\${DISTDIR}\${MMDB}，请确认 SPEC 已将 MMDB 打入发布目录。"
  !endif

  ; 安装完整 PyInstaller onedir；不打入开发目录中的配置 JSON 和日志。
  File /r "dist\${DISTDIR}\*.*"

  ; 安装后检查关键文件。
  IfFileExists "$INSTDIR\${EXE}" +2 0
    Abort "主程序安装不完整：缺少 ${EXE}。"

  IfFileExists "$INSTDIR\ios_runtime\python.exe" +2 0
    Abort "内置 iOS Runtime 安装不完整。"

  IfFileExists "$INSTDIR\ios_runtime\python313.dll" +2 0
    Abort "内置 Python 3.13 DLL 安装不完整。"

  IfFileExists "$INSTDIR\ios_runtime\Lib\site-packages\sitecustomize.py" +2 0
    Abort "内置 Runtime IPv6 scope 兼容补丁安装不完整。"

  IfFileExists "$INSTDIR\${MMDB}" +2 0
    Abort "离线城市数据库安装不完整：缺少 ${MMDB}。"

  ; 使用与应用启动检测相同的 CLI 命令验证完整 Runtime 及其依赖。
  nsExec::ExecToStack '"$INSTDIR\ios_runtime\python.exe" -u -m pymobiledevice3 --help'
  Pop $0
  Pop $1
  StrCmp $0 "0" runtime_ok
    DetailPrint "Runtime check output: $1"
    MessageBox MB_ICONSTOP "内置 iOS Runtime 无法启动 pymobiledevice3 CLI。错误码：$0$\r$\n$1$\r$\n安装将终止。"
    Abort

runtime_ok:

  WriteRegStr HKLM "${PRODUCT_REG_KEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKLM "${UNINSTALL_REG_KEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKLM "${UNINSTALL_REG_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKLM "${UNINSTALL_REG_KEY}" "Publisher" "${PUBLISHER}"
  WriteRegStr HKLM "${UNINSTALL_REG_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "${UNINSTALL_REG_KEY}" "DisplayIcon" "$INSTDIR\${EXE}"
  WriteRegStr HKLM "${UNINSTALL_REG_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKLM "${UNINSTALL_REG_KEY}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINSTALL_REG_KEY}" "NoRepair" 1

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateDirectory "$SMPROGRAMS\多设备管理"
  CreateShortcut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\${EXE}" "" "$INSTDIR\${EXE}" 0
  CreateShortcut "$SMPROGRAMS\多设备管理\${APPNAME}.lnk" "$INSTDIR\${EXE}" "" "$INSTDIR\${EXE}" 0
  CreateShortcut "$SMPROGRAMS\多设备管理\卸载.lnk" "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Uninstall"
  SetShellVarContext all
  nsExec::ExecToLog 'taskkill /F /IM "${EXE}"'
  Sleep 500

  Delete "$DESKTOP\${APPNAME}.lnk"
  Delete "$SMPROGRAMS\多设备管理\${APPNAME}.lnk"
  Delete "$SMPROGRAMS\多设备管理\卸载.lnk"
  RMDir "$SMPROGRAMS\多设备管理"

  ; 配置在安装目录；完整卸载删除目录，覆盖安装不执行此节。
  RMDir /r "$INSTDIR"

  DeleteRegKey HKLM "${PRODUCT_REG_KEY}"
  DeleteRegKey HKLM "${UNINSTALL_REG_KEY}"
SectionEnd
