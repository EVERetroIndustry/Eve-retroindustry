; Inno Setup script for EVE Retroindustry (Windows).
;
; Deliberately a PER-USER install into %LOCALAPPDATA%\Programs: no admin rights,
; no UAC prompt, and — importantly — the install directory stays writable by the
; app, so the existing in-app updater (which copies a new build over itself and
; relaunches) keeps working exactly as it does for the portable ZIP. A
; Program Files install would need elevation on every update.
;
; User data lives in %LOCALAPPDATA%\EVE Retroindustry (separate from this
; directory), so installing, updating and uninstalling never touch characters,
; prices or projects.
;
; Build:  iscc /DAppVersion=0.9.4 installer\eve_retroindustry.iss
; Expects the PyInstaller output in dist\EVE_Retroindustry\.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "EVE Retroindustry"
#define AppExeName "EVE_Retroindustry.exe"
#define AppPublisher "ScoopEMPRetro"
#define AppURL "https://github.com/ScoopEMPRetro/Eve-retroindustry"

[Setup]
; A stable AppId is what makes the next installer recognise this one and upgrade
; in place instead of leaving a second copy behind. Never change it.
AppId={{7F3A9C42-5D18-4B6E-9E2A-1C8B0F4D7A63}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases

; Per-user install — no admin, no UAC, writable install dir (see header).
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={localappdata}\Programs\EVE Retroindustry
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto

; Qt 6 (PyQt6/QtWebEngine) requires Windows 10 1809 or newer, so refuse older
; systems with a clear message instead of letting the app crash on launch.
MinVersion=10.0.17763
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

OutputDir=.
OutputBaseFilename=EVE_Retroindustry-v{#AppVersion}-setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; The app is unsigned, so don't pretend otherwise — SmartScreen may warn.
AllowNoIcons=yes
CloseApplications=yes
CloseApplicationsFilter=*.exe
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The whole PyInstaller one-dir output (exe + _internal + bundled sde_base.db).
Source: "..\dist\EVE_Retroindustry\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Files the app writes into its own directory at runtime (in-app updater
; leftovers). User data is elsewhere and is intentionally left alone.
Type: files; Name: "{app}\update.bat"
Type: files; Name: "{app}\update.zip.tmp"
Type: filesandordirs; Name: "{app}\_update_tmp"
