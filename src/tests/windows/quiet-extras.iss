; A tiny installer that behaves like a repack installed silently, the way the
; store installs: after copying it runs a "Visual C++ redistributable", and a
; post-install entry that opens a bit.ly address. Inno Setup runs both even
; with /VERYSILENT. Each writes a file a few seconds after starting, so a file
; that exists means it was not stopped in time. CI checks that the store's
; watcher stops both, and (control) that without it both run.
[Setup]
AppName=NSGM Quiet Extras Test
AppVersion=1.0
DefaultDirName={autopf}\NSGMQuietExtrasTest
PrivilegesRequired=lowest
OutputDir=out
OutputBaseFilename=quiet-extras-test
Uninstallable=no

[Files]
Source: "quiet-extras.iss"; DestDir: "{app}"
Source: "{sys}\cmd.exe"; DestDir: "{tmp}"; DestName: "vc_redist.x64.exe"; Flags: external

[Run]
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/c ping -n 4 127.0.0.1 >nul & echo ran> ""{app}\redist-ran.txt"""; Flags: runhidden waituntilterminated
Filename: "{cmd}"; Parameters: "/c ping -n 4 127.0.0.1 >nul & echo opened> ""{app}\website-opened.txt"" & rem https://bit.ly/fitgirl-repacks-site"; Flags: postinstall runhidden nowait
