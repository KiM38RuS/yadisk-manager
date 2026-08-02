; Инсталлер YaDisk Manager — Inno Setup 6
; Сборка: ISCC.exe installer.iss
; (пути к файлам — относительно папки скрипта)

#ifndef MyAppVersion
  #define MyAppVersion "0.11.6"
#endif

#define MyAppName "YaDisk Manager"
#define MyAppExeName "YaDiskManager.exe"
#define MyAppPublisher "YaDisk Manager"
#define MyAppURL "https://github.com/"

[Setup]
AppId={{8F1E2C3D-4A5B-6C7D-8E9F-0A1B2C3D4E5F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\YaDiskManager
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=Setup_YaDiskManager_v{#MyAppVersion}
SetupIconFile=icon-main.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Без прав администратора, если возможно (в AppData)
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; Русский язык интерфейса (встроен)
; Языки добавляются автоматически из установленного Inno Setup

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительно:"; Flags: unchecked

[Files]
Source: "YaDiskManager.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "icon-main.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Удалить {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\apply_update.bat"
Type: files; Name: "{app}\YaDiskManager.exe.old"
Type: files; Name: "{app}\YaDiskManager.new.exe"
