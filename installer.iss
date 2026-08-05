; Inno Setup script for the Windows installer.
;
; Installs per-user by default, which means no administrator prompt and no
; UAC dialog. Everything lands under the user's own AppData, and Add/Remove
; Programs gets a proper entry.

#define AppName "Non-Steam Game Manager"
#define AppExe "nonsteam-game-manager.exe"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
; Where PyInstaller left the binary, relative to this file. The project
; lives in src/, so its dist folder does too - pointing at "dist" from the
; repository root found nothing and the installer step failed.
#ifndef DistDir
  #define DistDir "src\dist"
#endif

[Setup]
AppId={{8E2A5F31-4C7D-4B29-9E13-3A7C4D5E6F70}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=tia987
AppUpdatesURL=https://github.com/tia987/steam_game_manager/releases
DefaultDirName={autopf}\NonSteamGameManager
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputBaseFilename=nonsteam-game-manager-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Per-user: no administrator rights, no UAC prompt.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExe}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#DistDir}\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent
