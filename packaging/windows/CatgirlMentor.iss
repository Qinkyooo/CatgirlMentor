#ifndef AppRoot
  #error AppRoot must point at the staged distribution
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif
#ifndef AppVersion
  #define AppVersion "0.3.5"
#endif

[Setup]
AppId={{BDF4C949-C95E-4B6A-9E80-859A29628C41}
AppName=CatgirlMentor
AppVersion={#AppVersion}
AppPublisher=CatgirlMentor contributors
DefaultDirName={localappdata}\Programs\CatgirlMentor
DefaultGroupName=CatgirlMentor
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=CatgirlMentor-{#AppVersion}-Windows-x64-Setup
SetupIconFile={#AppRoot}\desktop.ico
UninstallDisplayIcon={app}\CatgirlMentor.exe
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
Name: "{autoprograms}\CatgirlMentor"; Filename: "{app}\CatgirlMentor.exe"
Name: "{autodesktop}\CatgirlMentor"; Filename: "{app}\CatgirlMentor.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CatgirlMentor.exe"; Description: "打开 CatgirlMentor"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Python can create bytecode caches after installation; runtime contains no user data.
Type: filesandordirs; Name: "{app}\runtime"

[Code]
var
  RuntimeBackedUp: Boolean;
  InstallFinished: Boolean;
  StartupEnabled: Boolean;

function StartupMatchesApplication(Command: String): Boolean;
var Launcher: String;
begin
  Launcher := ExpandConstant('{app}\CatgirlMentor.exe');
  Result := (CompareText(Command, '"' + Launcher + '" --silent') = 0) or
    (CompareText(Command, Launcher + ' --silent') = 0);
end;

function NormalizePathCase(Target: String): Boolean;
var Found: TFindRec; Previous, Temporary: String;
begin
  Result := True;
  if not FindFirst(Target, Found) then Exit;
  try
    // Do not relocate junctions/symlinks, which may point at custom user data.
    if (Found.Attributes and $400) <> 0 then Exit;
    if Found.Name = ExtractFileName(Target) then Exit;
    Previous := AddBackslash(ExtractFileDir(Target)) + Found.Name;
  finally
    FindClose(Found);
  end;
  Temporary := Target + '.case-rename';
  Result := False;
  if FileExists(Temporary) or DirExists(Temporary) then Exit;
  if not RenameFile(Previous, Temporary) then Exit;
  Result := RenameFile(Temporary, Target);
  if not Result then RenameFile(Temporary, Previous);
end;

function StopApplication(): Boolean;
var Code: Integer;
begin
  Result := True;
  if FileExists(ExpandConstant('{app}\CatgirlMentor.exe')) then
    Result := Exec(ExpandConstant('{app}\CatgirlMentor.exe'), '--shutdown --silent', '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var Command, DefaultInstall: String;
begin
  Result := '';
  if not StopApplication() then begin
    Result := '请先退出 CatgirlMentor，再继续安装。用户数据将会保留。';
    Exit;
  end;
  if RuntimeBackedUp then Exit;
  StartupEnabled := RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'CatgirlMentor', Command) and
    StartupMatchesApplication(Command);
  if DirExists(ExpandConstant('{app}\runtime')) and
    not FileExists(ExpandConstant('{app}\build-manifest.json')) then begin
    Result := '所选目录包含其他运行环境，请选择一个空目录安装。';
    Exit;
  end;
  // Normalize only the product's default folders; honor arbitrary custom paths.
  DefaultInstall := ExpandConstant('{localappdata}\Programs\CatgirlMentor');
  if CompareText(ExpandConstant('{app}'), DefaultInstall) = 0 then begin
    if not NormalizePathCase(DefaultInstall) then begin
      Result := '无法更新安装目录名称，请完全退出 CatgirlMentor 后重试。';
      Exit;
    end;
    WizardForm.DirEdit.Text := DefaultInstall;
  end;
  if not NormalizePathCase(ExpandConstant('{localappdata}\CatgirlMentor')) or
    not NormalizePathCase(ExpandConstant('{app}\CatgirlMentor.exe')) then begin
    Result := '无法更新应用或数据目录名称，请完全退出 CatgirlMentor 后重试。用户数据将会保留。';
    Exit;
  end;
  if DirExists(ExpandConstant('{app}\runtime')) then begin
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
  if (CurStep = ssPostInstall) and StartupEnabled then begin
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'CatgirlMentor');
    RegWriteStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'CatgirlMentor',
      '"' + ExpandConstant('{app}\CatgirlMentor.exe') + '" --silent');
  end;
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
    if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'CatgirlMentor', Command) then
      if StartupMatchesApplication(Command) then
        RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'CatgirlMentor');
  end;
end;
