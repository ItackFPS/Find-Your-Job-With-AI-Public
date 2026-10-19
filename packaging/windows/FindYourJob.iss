; Find Your Job With AI -- Windows installer (Inno Setup 6).
;
; Built by packaging/windows/build.ps1, which stages the application (code, prebuilt
; dashboard, private Python runtime) and passes the defines below. Do not compile this
; file by hand from a checkout: the stage directory is what gets installed.
;
; Per-user install, no administrator rights. Application files and user data share the
; install folder (the app reads data/, config/ and logs/ next to its code); the rules
; below keep them apart:
;   * code, runtime and dashboard are replaced on every upgrade;
;   * config/*.yaml defaults are installed only when missing and never uninstalled, so
;     an upgrade never rewrites a setting (personal values live in config/*.local.yaml,
;     which no installer ever writes);
;   * data/, logs/, .env and *.local.yaml are never installed, so never touched;
;   * uninstalling removes the application and asks, default No, before deleting data.

#ifndef AppVersion
  #error AppVersion is not defined: build with packaging/windows/build.ps1
#endif
#ifndef StageDir
  #error StageDir is not defined: build with packaging/windows/build.ps1
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif
#ifndef VersionInfo
  #define VersionInfo "0.0.0.0"
#endif

#define AppName "Find Your Job With AI"
#define AppExeName "FindYourJob.cmd"

[Setup]
; Never change AppId: upgrades find the existing installation through it.
AppId={{6F0D6C2A-5B7E-4C1B-9E55-2D3C4F9A7B10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Find Your Job With AI contributors
AppPublisherURL=https://github.com/ItackFPS/Find-Your-Job-With-AI-Public
VersionInfoVersion={#VersionInfo}
VersionInfoProductVersion={#VersionInfo}
VersionInfoDescription={#AppName} setup
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename=FindYourJobWithAI-Setup-{#AppVersion}
SetupIconFile={#StageDir}\FindYourJob.ico
UninstallDisplayIcon={app}\FindYourJob.ico
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
WizardStyle=modern
; The running app is stopped through its own launcher (see PrepareToInstall), never killed.
CloseApplications=no
RestartApplications=no
LicenseFile={#StageDir}\LICENSE

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[InstallDelete]
; Upgrades replace the application wholesale so no module of an older version lingers.
Type: filesandordirs; Name: "{app}\src"
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\frontend\dist"
Type: filesandordirs; Name: "{app}\scripts"
Type: filesandordirs; Name: "{app}\docs"

[Dirs]
Name: "{app}\data"; Flags: uninsneveruninstall
Name: "{app}\logs"; Flags: uninsneveruninstall

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Excludes: "\config\*"; Flags: recursesubdirs createallsubdirs ignoreversion
; Default settings: only when missing, never removed (an upgrade keeps the user's files).
Source: "{#StageDir}\config\*"; DestDir: "{app}\config"; Flags: recursesubdirs createallsubdirs onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\FindYourJob.ico"; Comment: "Start {#AppName} and open its dashboard"
Name: "{group}\Stop {#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "stop"; WorkingDir: "{app}"; IconFilename: "{app}\FindYourJob.ico"; Comment: "Stop the running {#AppName}"
Name: "{group}\{#AppName} logs"; Filename: "{app}\logs"; Comment: "Open the folder with the application's logs"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\FindYourJob.ico"; Comment: "Start {#AppName} and open its dashboard"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Description: "Start {#AppName} now"; Flags: postinstall nowait skipifsilent shellexec

[UninstallRun]
; Stop a running copy through its launcher (graceful: scans are told to stop, SQLite closes).
Filename: "{app}\runtime\python.exe"; Parameters: "-m src.launcher stop"; WorkingDir: "{app}"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "StopApp"

[Code]
function StopRunningApp(): String;
var
  Python: String;
  Code: Integer;
begin
  Result := '';
  Python := ExpandConstant('{app}\runtime\python.exe');
  if not FileExists(Python) then
    Exit;
  { The launcher stops only what it started and waits for it to exit. }
  if not Exec(Python, '-m src.launcher stop', ExpandConstant('{app}'), SW_HIDE,
              ewWaitUntilTerminated, Code) then
    Exit;
  if Code <> 0 then
    Result := 'Find Your Job With AI is still running and did not stop in time. ' +
              'Close its window (or use "Stop Find Your Job With AI"), then run the installer again.';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := StopRunningApp();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Root: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  Root := ExpandConstant('{app}');
  if not DirExists(Root) then
    Exit;
  { Silent uninstalls always keep user data. }
  if UninstallSilent() then
    Exit;
  if MsgBox('The application has been removed.' + #13#10 + #13#10 +
            'Your job data, settings, feedback and logs are still in:' + #13#10 + Root + #13#10 + #13#10 +
            'Delete them too? This cannot be undone. Choose No to keep them for a later reinstall.',
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    DelTree(Root, True, True, True);
end;
