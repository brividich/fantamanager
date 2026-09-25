; Inno Setup script for FantaManager Live Auction.
; Wraps the PyInstaller one-folder bundle (dist\FantaManager) into a single,
; per-user Setup.exe that installs AND cleanly uninstalls the app.
;
; Build (from project root, after PyInstaller):
;   & "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss

#define MyAppName "FantaManager"
; Keep this in sync with __version__ in liveauction/__init__.py — that's the
; value shown inside the app (startup banner, log, console footer); this one
; is what Windows shows in Add/Remove Programs.
#define MyAppVersion "1.0.0"
#define MyAppPublisher "FantaManager"
#define MyAppExeName "FantaManager.exe"

[Setup]
AppId={{B3F1B2A0-6C2E-4E2A-9E1D-FANTAMANAGER01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Per-user install: no UAC prompt, lands in %LOCALAPPDATA%\Programs.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Let the user pick whether to install or remove a previous copy.
AllowNoIcons=yes
OutputDir=..\dist
OutputBaseFilename=FantaManager-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Shown in Windows "Installed apps" / Add-Remove Programs.
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
; Lo scudetto "Asta Live" sul programma di installazione. L'exe la porta già
; con sé (vedi fantamanager.spec), quindi collegamenti e Installazioni app la
; ereditano da lì: qui serve solo per la finestra del Setup.
SetupIconFile=icon.ico

[Languages]
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"

[Tasks]
Name: "desktopicon"; Description: "Crea un'icona sul desktop"; GroupDescription: "Collegamenti:"

[Files]
; The whole PyInstaller bundle. Source is relative to this .iss file.
Source: "..\dist\FantaManager\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Disinstalla {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Avvia {#MyAppName} ora"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Make sure no running instance keeps files locked during removal.
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#MyAppExeName} /F"; Flags: runhidden; RunOnceId: "KillApp"

[Code]
{ On uninstall, offer to also remove the user's saved data
  (database + uploads) that lives in %APPDATA%\FantaManager. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{userappdata}\{#MyAppName}');
    if DirExists(DataDir) then
    begin
      if MsgBox('Vuoi eliminare anche i dati salvati (aste, leghe, immagini)?'
                + #13#10 + DataDir + #13#10#13#10
                + 'Scegli "No" per conservarli per una futura reinstallazione.',
                mbConfirmation, MB_YESNO) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
