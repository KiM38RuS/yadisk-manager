; Инсталлер YaDisk Manager — Inno Setup 6
; Сборка: ISCC.exe installer.iss
; (пути к файлам — относительно папки скрипта)

#ifndef MyAppVersion
  #define MyAppVersion "0.13.0"
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
OutputBaseFilename=YaDiskManager_v{#MyAppVersion}_setup
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
; Закрытием работающего приложения управляем сами (см. [Code]).
; Штатный CloseApplications = Restart Manager: он закрывает только GUI-процесс,
; а процесс-обёртка PyInstaller onefile (без окон) остаётся жить и держит
; YaDiskManager.exe заблокированным. В результате установка «поверх» вечно
; висит на шаге «Закрытие приложений…» с диалогом «Попробовать снова».
CloseApplications=no
RestartApplications=no
; Русский язык интерфейса (встроен)
; Языки добавляются автоматически из установленного Inno Setup

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительно:"; Flags: unchecked
Name: "autorun"; Description: "Добавить в автозагрузку"; GroupDescription: "Дополнительно:"; Check: NeedAutorunTask

[Files]
Source: "YaDiskManager.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "icon-main.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Удалить {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "YaDiskManager"; ValueData: """{app}\{#MyAppExeName}"" --silent"; Tasks: autorun; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить ЯДиск Менеджер"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\apply_update.bat"
Type: files; Name: "{app}\YaDiskManager.exe.old"
Type: files; Name: "{app}\YaDiskManager.new.exe"

[Code]

{ ── Принудительное закрытие запущенного приложения ────────────────────────
  Почему не штатный CloseApplications (Restart Manager):
  YaDiskManager.exe собран PyInstaller'ом в режиме onefile, поэтому при
  запуске живут ДВА процесса с одним именем: GUI-процесс (Python/Qt) и
  процесс-обёртка (bootloader) без окон, который держит exe-файл открытым.
  Restart Manager закрывает только GUI-процесс, обёртка остаётся — файл
  остаётся заблокированным, установщик не может его заменить и показывает
  «Попробовать снова», которое обречено на провал.
  Поэтому закрываем принудительно: taskkill /F /T /IM завершает и обёртку,
  и GUI-процесс. }

const
  AppImageName = '{#MyAppExeName}';

{ Запущен ли хотя бы один процесс приложения (по имени образа). }
function IsAppRunning(): Boolean;
var
  TmpFile: String;
  Lines: TArrayOfString;
  ResultCode, I: Integer;
begin
  Result := False;
  TmpFile := ExpandConstant('{tmp}\ydm-tasklist.txt');
  if Exec(ExpandConstant('{cmd}'),
          '/C tasklist /FI "IMAGENAME eq ' + AppImageName + '" /NH > "' +
          TmpFile + '" 2>&1',
          '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    if LoadStringsFromFile(TmpFile, Lines) then
      for I := 0 to GetArrayLength(Lines) - 1 do
        if Pos(Lowercase(AppImageName), Lowercase(Lines[I])) > 0 then
          Result := True;
    DeleteFile(TmpFile);
  end;
end;

{ Закрывает все процессы приложения. True — если ни одного не осталось. }
function CloseRunningApp(): Boolean;
var
  ResultCode, I: Integer;
begin
  Result := not IsAppRunning();
  if Result then
    Exit;
  for I := 1 to 10 do
  begin
    Exec(ExpandConstant('{sys}\taskkill.exe'),
         '/F /T /IM ' + AppImageName,
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(600);
    if not IsAppRunning() then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not CloseRunningApp() then
    Result := 'Не удалось закрыть запущенный YaDisk Manager.' + #13#10 +
              'Закройте все процессы YaDiskManager.exe через Диспетчер задач ' +
              'и повторите установку.';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  { Страховка для тихой установки (/SILENT, /VERYSILENT), где
    PrepareToInstall может не вызываться. Операция идемпотентна. }
  if CurStep = ssInstall then
  begin
    if not CloseRunningApp() then
      SuppressibleMsgBox('Не удалось закрыть запущенный YaDisk Manager.' + #13#10 +
        'Закройте все процессы YaDiskManager.exe через Диспетчер задач ' +
        'и запустите установку заново.',
        mbError, MB_OK, IDOK);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { Та же проблема при удалении: обёртка держит exe. }
  if CurUninstallStep = usUninstall then
    CloseRunningApp();
end;

{ Чекбокс автозагрузки скрыт, если запись уже есть в HKCU Run
  (те же координаты, что в ui_shared.py: AUTORUN_KEY/AUTORUN_NAME). }
function NeedAutorunTask: Boolean;
var
  Value: String;
begin
  Result := not RegQueryStringValue(HKEY_CURRENT_USER,
    'Software\Microsoft\Windows\CurrentVersion\Run',
    'YaDiskManager', Value);
end;
