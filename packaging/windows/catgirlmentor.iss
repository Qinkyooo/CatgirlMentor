#ifndef AppRoot
  #error AppRoot must point at the staged distribution
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif
#ifndef AppVersion
  #define AppVersion "0.3.0"
#endif

[Setup]
AppId={{BDF4C949-C95E-4B6A-9E80-859A29628C41}
AppName=CatgirlMentor
AppVersion={#AppVersion}
AppPublisher=CatgirlMentor contributors
DefaultDirName={localappdata}\Programs\catgirlmentor
DefaultGroupName=catgirlmentor
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=catgirlmentor-{#AppVersion}-windows-x64-setup
SetupIconFile={#AppRoot}\desktop.ico
UninstallDisplayIcon={app}\catgirlmentor.exe
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
CloseApplications=no
RestartApplications=no
LicenseFile={#AppRoot}\LICENSE

[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"

[Files]
Source: "{#AppRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; Flags: checkedonce

[Icons]
Name: "{autoprograms}\CatgirlMentor"; Filename: "{app}\catgirlmentor.exe"
Name: "{autodesktop}\CatgirlMentor"; Filename: "{app}\catgirlmentor.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\catgirlmentor.exe"; Description: "打开 CatgirlMentor"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Python can create bytecode caches after installation; runtime contains no user data.
Type: filesandordirs; Name: "{app}\runtime"

[Code]
var
  RuntimeBackedUp: Boolean;
  InstallFinished: Boolean;

function StopApplication(): Boolean;
var Code: Integer;
begin
  Result := True;
  if FileExists(ExpandConstant('{app}\catgirlmentor.exe')) then
    Result := Exec(ExpandConstant('{app}\catgirlmentor.exe'), '--shutdown --silent', '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not StopApplication() then begin
    Result := '请先退出 CatgirlMentor，再继续安装。用户数据将会保留。';
    Exit;
  end;
  if RuntimeBackedUp then Exit;
  if DirExists(ExpandConstant('{app}\runtime')) then begin
    if not FileExists(ExpandConstant('{app}\build-manifest.json')) then begin
      Result := '所选目录包含其他运行环境，请选择一个空目录安装。';
      Exit;
    end;
    if DirExists(ExpandConstant('{app}\runtime.previous')) then begin
      Result := '检测到上次升级的备份。请先恢复或移走 runtime.previous 文件夹，再继续升级。';
      Exit;
    end;
    RuntimeBackedUp := RenameFile(ExpandConstant('{app}\runtime'), ExpandConstant('{app}\runtime.previous'));
    if not RuntimeBackedUp then
      Result := '无法备份旧版本，请完全退出 CatgirlMentor 后重试。';
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssDone then begin
    InstallFinished := True;
    if RuntimeBackedUp then
      DelTree(ExpandConstant('{app}\runtime.previous'), True, True, True);
  end;
end;

procedure DeinitializeSetup();
begin
  if RuntimeBackedUp and not InstallFinished then begin
    // Only the application-owned runtime is restored; user data is outside the install directory.
    if DirExists(ExpandConstant('{app}\runtime')) then
      DelTree(ExpandConstant('{app}\runtime'), True, True, True);
    RenameFile(ExpandConstant('{app}\runtime.previous'), ExpandConstant('{app}\runtime'));
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := StopApplication();
  if not Result then
    MsgBox('请先退出 CatgirlMentor，再继续卸载。', mbError, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var Command: String;
begin
  if CurUninstallStep = usUninstall then begin
    if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'catgirlmentor', Command) then
      if Pos(ExpandConstant('{app}\catgirlmentor.exe'), Command) > 0 then
        RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'catgirlmentor');
  end;
end;
