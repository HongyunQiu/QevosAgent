!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "preserve-paths.nsh"

Var /GLOBAL PreserveBackupRoot
!ifdef BUILD_UNINSTALLER
Var /GLOBAL PreservePathExists
Var /GLOBAL PreserveWorkContents
!endif

!ifndef BUILD_UNINSTALLER
!macro BackupPreservePathMacro REL_PATH
  Push "${REL_PATH}"
  Call BackupPreservePath
!macroend

!macro RestorePreservePathMacro REL_PATH
  Push "${REL_PATH}"
  Call RestorePreservePath
!macroend

!endif

!ifdef BUILD_UNINSTALLER
!macro UnBackupPreservePathMacro REL_PATH
  Push "${REL_PATH}"
  Call un.BackupPreservePath
!macroend

!macro UnRestorePreservePathMacro REL_PATH
  Push "${REL_PATH}"
  Call un.RestorePreservePath
!macroend

!macro UnMarkPreservePathExistsMacro REL_PATH
  Push "${REL_PATH}"
  Call un.MarkPreservePathExists
!macroend
!endif

!ifndef BUILD_UNINSTALLER
Function BackupPreservePath
  Exch $0
  Push $1
  Push $2

  StrCpy $1 "$INSTDIR\$0"

  IfFileExists "$1\*.*" 0 checkFile
    ${GetParent} "$PreserveBackupRoot\$0" $2
    CreateDirectory "$2"
    CopyFiles /SILENT "$1" "$2"
    RMDir /r "$1"
    Goto done

  checkFile:
    IfFileExists "$1" 0 done
      ${GetParent} "$PreserveBackupRoot\$0" $2
      CreateDirectory "$2"
      CopyFiles /SILENT "$1" "$2"
      Delete "$1"

  done:
    Pop $2
    Pop $1
    Pop $0
FunctionEnd

Function RestorePreservePath
  Exch $0
  Push $1
  Push $2

  StrCpy $1 "$PreserveBackupRoot\$0"

  IfFileExists "$1\*.*" 0 checkFile
    ${GetParent} "$INSTDIR\$0" $2
    CreateDirectory "$2"
    RMDir /r "$INSTDIR\$0"
    CopyFiles /SILENT "$1" "$2"
    Goto done

  checkFile:
    IfFileExists "$1" 0 done
      ${GetParent} "$INSTDIR\$0" $2
      CreateDirectory "$2"
      Delete "$INSTDIR\$0"
      CopyFiles /SILENT "$1" "$2"

  done:
    Pop $2
    Pop $1
    Pop $0
FunctionEnd

!endif

!ifdef BUILD_UNINSTALLER
Function un.BackupPreservePath
  Exch $0
  Push $1
  Push $2

  StrCpy $1 "$INSTDIR\$0"

  IfFileExists "$1\*.*" 0 unCheckFileBackup
    ${GetParent} "$PreserveBackupRoot\$0" $2
    CreateDirectory "$2"
    CopyFiles /SILENT "$1" "$2"
    RMDir /r "$1"
    Goto unBackupDone

  unCheckFileBackup:
    IfFileExists "$1" 0 unBackupDone
      ${GetParent} "$PreserveBackupRoot\$0" $2
      CreateDirectory "$2"
      CopyFiles /SILENT "$1" "$2"
      Delete "$1"

  unBackupDone:
    Pop $2
    Pop $1
    Pop $0
FunctionEnd

Function un.RestorePreservePath
  Exch $0
  Push $1
  Push $2

  StrCpy $1 "$PreserveBackupRoot\$0"

  IfFileExists "$1\*.*" 0 unCheckFileRestore
    ${GetParent} "$INSTDIR\$0" $2
    CreateDirectory "$2"
    RMDir /r "$INSTDIR\$0"
    CopyFiles /SILENT "$1" "$2"
    Goto unRestoreDone

  unCheckFileRestore:
    IfFileExists "$1" 0 unRestoreDone
      ${GetParent} "$INSTDIR\$0" $2
      CreateDirectory "$2"
      Delete "$INSTDIR\$0"
      CopyFiles /SILENT "$1" "$2"

  unRestoreDone:
    Pop $2
    Pop $1
    Pop $0
FunctionEnd

Function un.MarkPreservePathExists
  Exch $0
  Push $1

  StrCpy $1 "$INSTDIR\$0"

  IfFileExists "$1\*.*" 0 unCheckFileExists
    StrCpy $PreservePathExists "1"
    Goto unMarkDone

  unCheckFileExists:
    IfFileExists "$1" 0 unMarkDone
      StrCpy $PreservePathExists "1"

  unMarkDone:
    Pop $1
    Pop $0
FunctionEnd
!endif

!ifndef BUILD_UNINSTALLER
Function PreserveExistingWorkPageCreate
  StrCpy $PreserveBackupRoot "$PLUGINSDIR\preserve-install"
  RMDir /r "$PreserveBackupRoot"
  CreateDirectory "$PreserveBackupRoot"
  !insertmacro DefineInstallOverwriteProtectedPaths BackupPreservePathMacro

  Abort
FunctionEnd

Function PreserveExistingWorkPageLeave
FunctionEnd

!macro customPageAfterChangeDir
  Page custom PreserveExistingWorkPageCreate PreserveExistingWorkPageLeave
!macroend

!macro customInstall
  StrCpy $PreserveBackupRoot "$PLUGINSDIR\preserve-install"
  !insertmacro DefineInstallOverwriteProtectedPaths RestorePreservePathMacro

  StrCpy $0 "$INSTDIR\resources\icon.ico"
  ${If} ${FileExists} "$0"
    Delete "$newDesktopLink"
    CreateShortCut "$newDesktopLink" "$appExe" "" "$0" 0 "" "" "${APP_DESCRIPTION}"
    ClearErrors
    WinShell::SetLnkAUMI "$newDesktopLink" "${APP_ID}"
  ${EndIf}
!macroend
!endif

!ifdef BUILD_UNINSTALLER
!macro customUnInit
  StrCpy $PreserveWorkContents "1"

  ${If} ${isUpdated}
    Goto done
  ${EndIf}

  ${If} ${Silent}
    Goto done
  ${EndIf}

  StrCpy $PreservePathExists "0"
  !insertmacro DefinePreservePaths UnMarkPreservePathExistsMacro

  ${If} $PreservePathExists == "0"
    Goto done
  ${EndIf}

  MessageBox MB_ICONQUESTION|MB_YESNO|MB_DEFBUTTON1 \
    "是否保留现有工作内容？$\r$\n$\r$\n选择“是”将保留 AGENTS.md、ADVISOR.md、SKILLS、runs、记忆文件和自定义工具文件。" \
    IDYES done

  MessageBox MB_ICONEXCLAMATION|MB_YESNO|MB_DEFBUTTON2 \
    "如果继续删除，现有工作内容将被永久移除且不可恢复。$\r$\n$\r$\n确定要删除这些工作内容吗？" \
    IDYES confirmDelete IDNO done

  confirmDelete:
    StrCpy $PreserveWorkContents "0"

  done:
!macroend

!macro customRemoveFiles
  ${If} ${isUpdated}
    ; Keep the install directory in place during updates so the new installer
    ; can directly overwrite application files without touching large user data.
  ${ElseIf} $PreserveWorkContents == "1"
    StrCpy $PreservePathExists "0"
    !insertmacro DefinePreservePaths UnMarkPreservePathExistsMacro

    ${If} $PreservePathExists == "1"
      ${GetParent} "$INSTDIR" $R0
      StrCpy $PreserveBackupRoot "$R0\${APP_FILENAME}-preserve"
      RMDir /r "$PreserveBackupRoot"
      CreateDirectory "$PreserveBackupRoot"
      !insertmacro DefinePreservePaths UnBackupPreservePathMacro
      RMDir /r $INSTDIR
      CreateDirectory "$INSTDIR"
      !insertmacro DefinePreservePaths UnRestorePreservePathMacro
      RMDir /r "$PreserveBackupRoot"
    ${Else}
      RMDir /r $INSTDIR
    ${EndIf}
  ${Else}
    RMDir /r $INSTDIR
  ${EndIf}
!macroend
!endif
