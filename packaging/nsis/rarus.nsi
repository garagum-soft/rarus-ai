; Installer for Rarus AI (NSIS 3.x), written from the NSIS documentation (spec D-20: no code of the
; Alembox installer is used).
;
;   makensis /DVERSION=0.1.0 /DSOURCE=build\dist\RarusAI /DOUTFILE=build\RarusSetup-0.1.0.exe packaging\nsis\rarus.nsi
;
; Installs for the current user under %LOCALAPPDATA%\Programs\Rarus AI and asks for no
; administrator rights. Modes: interactive; /S (silent); /UPDATE (over an existing install: the
; program is closed first, the choices of the earlier install are kept, data is never touched).
; The setup log is %TEMP%\rarus-setup.log.
;
; Compiled with NSIS 3.13 (unpacked at build\tools\nsis\nsis-3.13, not part of the repository).
; The file must keep its UTF-8 byte order mark: without it NSIS reads the Russian and Chinese
; strings as ANSI. Silent install, silent uninstall and /UPDATE were run on a development machine;
; see docs/reports/milestone-M6.md for what was and was not checked.

Unicode true

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "Sections.nsh"

!insertmacro GetParameters
!insertmacro GetOptions
!insertmacro GetFileName

!ifndef VERSION
  !define VERSION "0.0.0"
!endif
!ifndef SOURCE
  !error "pass /DSOURCE=<the folder that holds RarusAI.exe>"
!endif
!ifndef OUTFILE
  !define OUTFILE "RarusSetup-${VERSION}.exe"
!endif

!define APP_NAME "Rarus AI"
!define APP_EXE "RarusAI.exe"
!define APP_KEY "Software\Rarus AI"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\RarusAI"
!define RUN_KEY "Software\Microsoft\Windows\CurrentVersion\Run"
!define WEBVIEW2_GUID "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

Name "${APP_NAME}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\Rarus AI"
InstallDirRegKey HKCU "${APP_KEY}" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
BrandingText "Rarus AI ${VERSION}"

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "FileDescription" "Rarus AI setup"
VIAddVersionKey "LegalCopyright" "Rarus AI. Built on Raven, Apache License 2.0"

Var IsUpdate

!define MUI_ABORTWARNING
; The same globe as the program (tools/dev/make_icons.py), for the setup and the uninstaller.
!define MUI_ICON "${__FILEDIR__}\..\assets\rarus.ico"
!define MUI_UNICON "${__FILEDIR__}\..\assets\rarus.ico"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "Russian"
!insertmacro MUI_LANGUAGE "English"
!insertmacro MUI_LANGUAGE "SimpChinese"

LangString NAME_MAIN ${LANG_RUSSIAN} "Rarus AI"
LangString NAME_MAIN ${LANG_ENGLISH} "Rarus AI"
LangString NAME_MAIN ${LANG_SIMPCHINESE} "Rarus AI"
LangString NAME_DESKTOP ${LANG_RUSSIAN} "Ярлык на рабочем столе"
LangString NAME_DESKTOP ${LANG_ENGLISH} "Desktop shortcut"
LangString NAME_DESKTOP ${LANG_SIMPCHINESE} "桌面快捷方式"
LangString NAME_AUTOSTART ${LANG_RUSSIAN} "Запускать вместе с Windows"
LangString NAME_AUTOSTART ${LANG_ENGLISH} "Start with Windows"
LangString NAME_AUTOSTART ${LANG_SIMPCHINESE} "随 Windows 启动"
LangString ASK_DATA ${LANG_RUSSIAN} "Удалить данные Rarus AI на этом компьютере (чаты, память, настройки)?"
LangString ASK_DATA ${LANG_ENGLISH} "Delete Rarus AI data on this computer (chats, memory, settings)?"
LangString ASK_DATA ${LANG_SIMPCHINESE} "删除此电脑上的 Rarus AI 数据（聊天、记忆、设置）？"
LangString NO_WEBVIEW2 ${LANG_RUSSIAN} "Не найден Microsoft Edge WebView2. Установите его с сайта Microsoft и запустите Rarus AI снова."
LangString NO_WEBVIEW2 ${LANG_ENGLISH} "Microsoft Edge WebView2 was not found. Install it from the Microsoft site and start Rarus AI again."
LangString NO_WEBVIEW2 ${LANG_SIMPCHINESE} "未找到 Microsoft Edge WebView2。请从 Microsoft 网站安装后重新启动 Rarus AI。"

; ---------------------------------------------------------------- log

Function Log
  Exch $0
  Push $1
  FileOpen $1 "$TEMP\rarus-setup.log" a
  FileSeek $1 0 END
  FileWrite $1 "$0$\r$\n"
  FileClose $1
  Pop $1
  Pop $0
FunctionEnd

!macro Log text
  Push "${text}"
  Call Log
!macroend

; ---------------------------------------------------------------- install

Section "!$(NAME_MAIN)" SecMain
  SectionIn RO
  ; The uninstaller removes the whole install folder, so the program always lives in a folder of
  ; its own: a reader who picks "Documents" gets "Documents\Rarus AI".
  ${GetFileName} "$INSTDIR" $0
  ${If} $0 != "Rarus AI"
    StrCpy $INSTDIR "$INSTDIR\Rarus AI"
  ${EndIf}
  ; The program may be running: close it so its files can be replaced.
  nsExec::Exec 'taskkill /IM "${APP_EXE}" /F'
  Pop $0
  Sleep 800

  SetOutPath "$INSTDIR"
  File /r "${SOURCE}\*.*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  !insertmacro Log "files copied to $INSTDIR"

  CreateDirectory "$SMPROGRAMS\Rarus AI"
  CreateShortcut "$SMPROGRAMS\Rarus AI\Rarus AI.lnk" "$INSTDIR\${APP_EXE}"
  CreateShortcut "$SMPROGRAMS\Rarus AI\Uninstall Rarus AI.lnk" "$INSTDIR\Uninstall.exe"

  WriteRegStr HKCU "${APP_KEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${APP_KEY}" "Version" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "Rarus"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${APP_EXE}"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1

  Call CheckWebView2
SectionEnd

Section /o "$(NAME_DESKTOP)" SecDesktop
  CreateShortcut "$DESKTOP\Rarus AI.lnk" "$INSTDIR\${APP_EXE}"
  !insertmacro Log "desktop shortcut created"
SectionEnd

Section /o "$(NAME_AUTOSTART)" SecAutostart
  ; The same value the program's own switch writes (rarus_app/autostart.py).
  WriteRegStr HKCU "${RUN_KEY}" "RarusAI" '"$INSTDIR\${APP_EXE}" --background'
  !insertmacro Log "autostart set"
SectionEnd

; ---------------------------------------------------------------- start

Function .onInit
  StrCpy $IsUpdate "0"
  ${GetParameters} $0
  ${GetOptions} $0 "/UPDATE" $1
  ${IfNot} ${Errors}
    StrCpy $IsUpdate "1"
  ${EndIf}
  !insertmacro Log "setup ${VERSION} started (update=$IsUpdate, dir=$INSTDIR)"
  ; An update keeps what the earlier install had chosen.
  ${If} $IsUpdate == "1"
    ReadRegStr $2 HKCU "${RUN_KEY}" "RarusAI"
    ${If} $2 != ""
      !insertmacro SelectSection ${SecAutostart}
    ${EndIf}
    ${If} ${FileExists} "$DESKTOP\Rarus AI.lnk"
      !insertmacro SelectSection ${SecDesktop}
    ${EndIf}
  ${EndIf}
FunctionEnd

; WebView2 (the window's engine): present on current Windows 10/11; when it is not, the
; Evergreen bootstrapper from the package installs it. The bootstrapper is a file Microsoft
; publishes and the build copies to redist\; without it the reader is told what to install.
Function CheckWebView2
  ReadRegStr $0 HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\${WEBVIEW2_GUID}" "pv"
  ${If} $0 == ""
    ReadRegStr $0 HKCU "Software\Microsoft\EdgeUpdate\Clients\${WEBVIEW2_GUID}" "pv"
  ${EndIf}
  ${If} $0 != ""
    !insertmacro Log "WebView2 $0 present"
    Return
  ${EndIf}
  ${If} ${FileExists} "$INSTDIR\redist\MicrosoftEdgeWebview2Setup.exe"
    !insertmacro Log "WebView2 missing: running the bootstrapper"
    ExecWait '"$INSTDIR\redist\MicrosoftEdgeWebview2Setup.exe" /silent /install' $1
    !insertmacro Log "bootstrapper exit code $1"
  ${Else}
    !insertmacro Log "WebView2 missing and no bootstrapper in the package"
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(NO_WEBVIEW2)" /SD IDOK
  ${EndIf}
FunctionEnd

Function .onInstSuccess
  !insertmacro Log "setup finished"
  ; An update or a silent install starts the program itself: the finish page is not shown there.
  ${If} $IsUpdate == "1"
    Exec '"$INSTDIR\${APP_EXE}"'
  ${EndIf}
FunctionEnd

; ---------------------------------------------------------------- remove

Section "Uninstall"
  nsExec::Exec 'taskkill /IM "${APP_EXE}" /F'
  Pop $0
  Sleep 800

  Delete "$SMPROGRAMS\Rarus AI\Rarus AI.lnk"
  Delete "$SMPROGRAMS\Rarus AI\Uninstall Rarus AI.lnk"
  RMDir "$SMPROGRAMS\Rarus AI"
  Delete "$DESKTOP\Rarus AI.lnk"
  DeleteRegValue HKCU "${RUN_KEY}" "RarusAI"
  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "${APP_KEY}"

  ; Only the install folder, and only when it holds the program: a registry value that points
  ; somewhere else must never turn into a recursive delete of that place.
  ${If} ${FileExists} "$INSTDIR\${APP_EXE}"
    RMDir /r "$INSTDIR"
  ${EndIf}

  ; The reader's data (chats, memory, settings, keys) stays unless they say otherwise; a silent
  ; uninstall never removes it.
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "$(ASK_DATA)" /SD IDNO IDNO keep_data
  RMDir /r "$PROFILE\.rarus"
  keep_data:
SectionEnd
