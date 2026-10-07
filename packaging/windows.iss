#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
[Setup]
AppId={{6B5B468A-261E-4775-BAD4-A8D3F4716710}
AppName={cm:AppTitle}
AppVersion={#AppVersion}
AppPublisher=Горшков Сергей Владимирович
AppPublisherURL=https://nookbat.ru
AppSupportURL=https://github.com/Serge-Nook/multilayer
DefaultDirName={localappdata}\Programs\Multilayer
DefaultGroupName={cm:AppTitle}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=Multilayer-{#AppVersion}-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={cm:AppTitle}
SetupIconFile=..\build\multilayer.ico
CloseApplications=yes
SetupLogging=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
russian.AppTitle=Мультислой
english.AppTitle=Multilayer
russian.Uninstall=Удалить Мультислой
english.Uninstall=Uninstall Multilayer
russian.DesktopIcon=Создать ярлык на рабочем столе
english.DesktopIcon=Create a desktop shortcut
russian.Launch=Запустить Мультислой
english.Launch=Launch Multilayer
russian.Action=Выберите действие
english.Action=Choose an action
russian.KeepData=Диски и настройки виртуальных машин сохраняются при обновлении и удалении.
english.KeepData=Virtual machine disks and settings are preserved during updates and uninstall.
russian.Install=Установить / обновить
english.Install=Install / update
russian.Remove=Удалить
english.Remove=Uninstall
russian.Exit=Выйти
english.Exit=Exit
russian.UninstallFailed=Не удалось запустить удаление.
english.UninstallFailed=Could not start the uninstaller.
russian.NotInstalled=Установленное приложение не найдено.
english.NotInstalled=No installed application was found.

[Files]
Source: "..\dist\Multilayer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\multilayer-cli\*"; DestDir: "{app}\cli"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{cm:AppTitle}"; Filename: "{app}\Multilayer.exe"; Parameters: "--gui"
Name: "{group}\{cm:Uninstall}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{cm:AppTitle}"; Filename: "{app}\Multilayer.exe"; Parameters: "--gui"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"; Flags: unchecked

[Run]
Filename: "{app}\Multilayer.exe"; Parameters: "--gui"; Description: "{cm:Launch}"; Flags: nowait postinstall skipifsilent

[Code]
var
  ModePage: TInputOptionWizardPage;

procedure InitializeWizard;
begin
  ModePage := CreateInputOptionPage(wpWelcome, ExpandConstant('{cm:AppTitle}'),
    ExpandConstant('{cm:Action}'), ExpandConstant('{cm:KeepData}'), True, False);
  ModePage.Add(ExpandConstant('{cm:Install}'));
  ModePage.Add(ExpandConstant('{cm:Remove}'));
  ModePage.Add(ExpandConstant('{cm:Exit}'));
  ModePage.SelectedValueIndex := 0;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Uninstaller: String;
  ResultCode: Integer;
begin
  Result := True;
  if CurPageID <> ModePage.ID then Exit;
  if ModePage.SelectedValueIndex = 2 then begin
    WizardForm.Close;
    Result := False;
  end;
  if ModePage.SelectedValueIndex = 1 then begin
    Uninstaller := ExpandConstant('{localappdata}\Programs\Multilayer\unins000.exe');
    if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6B5B468A-261E-4775-BAD4-A8D3F4716710}_is1', 'InstallLocation', Uninstaller) then
      Uninstaller := AddBackslash(Uninstaller) + 'unins000.exe';
    if FileExists(Uninstaller) then begin
      if not Exec(Uninstaller, '', '', SW_SHOW, ewWaitUntilTerminated, ResultCode) then
        MsgBox(ExpandConstant('{cm:UninstallFailed}'), mbError, MB_OK);
    end else
      MsgBox(ExpandConstant('{cm:NotInstalled}'), mbInformation, MB_OK);
    WizardForm.Close;
    Result := False;
  end;
end;
