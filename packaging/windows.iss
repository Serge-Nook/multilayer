#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
[Setup]
AppId={{6B5B468A-261E-4775-BAD4-A8D3F4716710}
AppName=Мультислой
AppVersion={#AppVersion}
AppPublisher=Горшков Сергей Владимирович
AppPublisherURL=https://nookbat.ru
AppSupportURL=https://github.com/Serge-Nook/multilayer
DefaultDirName={localappdata}\Programs\Multilayer
DefaultGroupName=Мультислой
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=Multilayer-{#AppVersion}-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=Мультислой
CloseApplications=yes
SetupLogging=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Files]
Source: "..\dist\Multilayer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\multilayer-cli\*"; DestDir: "{app}\cli"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Мультислой"; Filename: "{app}\Multilayer.exe"; Parameters: "--gui"
Name: "{group}\Удалить Мультислой"; Filename: "{uninstallexe}"
Name: "{userdesktop}\Мультислой"; Filename: "{app}\Multilayer.exe"; Parameters: "--gui"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; Flags: unchecked

[Run]
Filename: "{app}\Multilayer.exe"; Parameters: "--gui"; Description: "Запустить Мультислой"; Flags: nowait postinstall skipifsilent

[Code]
var
  ModePage: TInputOptionWizardPage;

procedure InitializeWizard;
begin
  ModePage := CreateInputOptionPage(wpWelcome, 'Мультислой',
    'Выберите действие', 'Диски и настройки виртуальных машин сохраняются при обновлении и удалении.', True, False);
  ModePage.Add('Установить / обновить');
  ModePage.Add('Удалить');
  ModePage.Add('Выйти');
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
        MsgBox('Не удалось запустить удаление.', mbError, MB_OK);
    end else
      MsgBox('Установленное приложение не найдено.', mbInformation, MB_OK);
    WizardForm.Close;
    Result := False;
  end;
end;
