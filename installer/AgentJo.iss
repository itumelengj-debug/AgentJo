; Agent Jo - Windows installer (Inno Setup 6.1 or newer)
;
; Build it with installer\build_installer.bat, which finds or installs Inno
; Setup and stamps the version from BUILD_ID in agent\config.py. The result is
; dist\AgentJo-Setup-<version>.exe.
;
; What the installer does, in order:
;   1. copies the app into your own folder - no administrator prompt
;   2. finds Python 3.10+ in the registry; if there's none, downloads the
;      official installer from python.org and installs it for you alone
;   3. runs install_agent_jo.py with that exact Python - the same setup
;      install.bat runs - so a PATH that hasn't caught up can't break it
;   4. makes Start menu and (if you tick it) desktop shortcuts
; Uninstalling removes the app folder and its shortcuts. Your data, in
; %USERPROFILE%\.local_agent, is never touched.

#ifndef AppVersion
  #define AppVersion "0.0.0.0"
#endif
#define AppName "Agent Jo"
#define AppPublisher "Symbolic Synapse"
#define Starter "start_agent_jo.bat"
#define PyVer "3.12.7"

[Setup]
AppId={{a9bcd648-6bfc-440e-8dcd-6b94b749617f}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=AgentJo-Setup-{#AppVersion}
SetupIconFile=..\agent-jo.ico
UninstallDisplayIcon={app}\agent-jo.ico
UninstallDisplayName={#AppName}
LicenseFile=..\LICENSE
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
SetupLogging=yes
CloseApplications=no

[Messages]
WelcomeLabel2=This will install [name/ver] on your computer.%n%nIf Agent Jo is already running, close its window first.%n%nSetup needs an internet connection: it installs the app's Python packages, and Python itself if this PC doesn't have it.

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; everything the app needs, nothing a build or a previous run left behind
Source: "..\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion; \
  Excludes: "\.update,\signing-key.private,\.venv,\venv,\env,\dist,\build,\.git,\.github,\installer,__pycache__,*.pyc,\install.log,\install-report.json,\*.zip,\Build Windows Installer.bat"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#Starter}"; WorkingDir: "{app}"; \
  IconFilename: "{app}\agent-jo.ico"; Comment: "Agent Jo - by Symbolic Synapse"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#Starter}"; WorkingDir: "{app}"; \
  IconFilename: "{app}\agent-jo.ico"; Comment: "Agent Jo - by Symbolic Synapse"; Tasks: desktopicon

[Run]
Filename: "{app}\{#Starter}"; Description: "Start Agent Jo now"; WorkingDir: "{app}"; \
  Flags: postinstall nowait skipifsilent shellexec

[InstallDelete]
; installing over an older version: a file a release removed mustn't linger
; from the last one. The Python environment (.venv) stays and is updated.
Type: filesandordirs; Name: "{app}\agent"
Type: filesandordirs; Name: "{app}\web"
Type: filesandordirs; Name: "{app}\web_phone"
Type: filesandordirs; Name: "{app}\tests"
Type: filesandordirs; Name: "{app}\tools"

[UninstallDelete]
; the Python environment and logs were made after install, so name them
Type: filesandordirs; Name: "{app}"

[Code]
var
  PythonExe: String;

{ Python 3.10 or newer, from the registry: per-user installs first, then
  machine-wide (both registry views). Not from PATH - after a fresh Python
  install this process still has the old PATH, which is exactly why
  install.bat had to ask you to open a new window and run it again. }
function FindPython(var Exe: String): Boolean;
var
  Minor: Integer;
  Key, P: String;
begin
  Result := False;
  for Minor := 14 downto 10 do
  begin
    Key := 'Software\Python\PythonCore\3.' + IntToStr(Minor) + '\InstallPath';
    if RegQueryStringValue(HKCU, Key, 'ExecutablePath', P) and FileExists(P) then
    begin
      Exe := P; Result := True; Exit;
    end;
    if RegQueryStringValue(HKCU, Key, '', P) and FileExists(AddBackslash(P) + 'python.exe') then
    begin
      Exe := AddBackslash(P) + 'python.exe'; Result := True; Exit;
    end;
    if IsWin64 then
      if RegQueryStringValue(HKLM64, Key, 'ExecutablePath', P) and FileExists(P) then
      begin
        Exe := P; Result := True; Exit;
      end;
    if RegQueryStringValue(HKLM32, Key, 'ExecutablePath', P) and FileExists(P) then
    begin
      Exe := P; Result := True; Exit;
    end;
  end;
end;

function OnDownloadProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
begin
  if ProgressMax > 0 then
    WizardForm.StatusLabel.Caption := 'Downloading Python ' + '{#PyVer}' + ' from python.org: ' +
      IntToStr(Progress div 1048576) + ' of ' + IntToStr(ProgressMax div 1048576) + ' MB';
  Result := True;
end;

{ Downloads the official installer and installs Python for this user only:
  no administrator prompt, the same choices get-python.ps1 makes. }
function InstallPython(): Boolean;
var
  Arch, FileName: String;
  RC: Integer;
begin
  Result := False;
  Arch := GetEnv('PROCESSOR_ARCHITEW6432');
  if Arch = '' then Arch := GetEnv('PROCESSOR_ARCHITECTURE');
  if CompareText(Arch, 'ARM64') = 0 then
    FileName := 'python-{#PyVer}-arm64.exe'
  else
    FileName := 'python-{#PyVer}-amd64.exe';
  try
    DownloadTemporaryFile('https://www.python.org/ftp/python/{#PyVer}/' + FileName,
      FileName, '', @OnDownloadProgress);
  except
    Log('Python download failed: ' + GetExceptionMessage);
    Exit;
  end;
  WizardForm.StatusLabel.Caption := 'Installing Python {#PyVer} for this user...';
  if not Exec(ExpandConstant('{tmp}\') + FileName,
      '/quiet InstallAllUsers=0 InstallLauncherAllUsers=0 PrependPath=1 Include_pip=1 Include_launcher=1 Include_test=0',
      '', SW_SHOWNORMAL, ewWaitUntilTerminated, RC) then
    Exit;
  Log('Python installer exit code: ' + IntToStr(RC));
  Result := FindPython(PythonExe);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  RC: Integer;
begin
  if CurStep <> ssPostInstall then Exit;
  if not FindPython(PythonExe) then
    if not InstallPython() then
    begin
      SuppressibleMsgBox('Agent Jo was copied, but Python 3.10 or newer couldn''t be installed.' + #13#10 + #13#10 +
        'Install Python from python.org (tick "Add python.exe to PATH"), then run install.bat in:' + #13#10 +
        ExpandConstant('{app}'), mbError, MB_OK, IDOK);
      Exit;
    end;
  Log('Using Python: ' + PythonExe);
  WizardForm.StatusLabel.Caption := 'Setting up Agent Jo: its Python packages (a few minutes the first time)...';
  if not Exec(PythonExe, AddQuotes(ExpandConstant('{app}\install_agent_jo.py')) + ' --quiet --no-shortcut',
      ExpandConstant('{app}'), SW_SHOWNORMAL, ewWaitUntilTerminated, RC) or (RC <> 0) then
    SuppressibleMsgBox('Agent Jo was copied, but setting up its Python packages didn''t finish.' + #13#10 + #13#10 +
      'What happened is in install.log in:' + #13#10 + ExpandConstant('{app}') + #13#10 + #13#10 +
      'Run install.bat there to try again.', mbError, MB_OK, IDOK);
end;
