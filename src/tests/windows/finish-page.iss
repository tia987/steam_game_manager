; A tiny installer that ends like a repack's: a Finished page with ticked
; boxes that open a "website" and "launch the game", and code that opens the
; website again whenever the installer exits, ticked or not, the way repacks
; open bit.ly/fitgirl-repacks-site. CI builds it and checks that the store
; closes it on that page, so none of the three ever runs.
[Setup]
AppName=NSGM Finish Page Test
AppVersion=1.0
DefaultDirName={autopf}\NSGMFinishTest
PrivilegesRequired=lowest
DisableWelcomePage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableFinishedPage=no
OutputDir=out
OutputBaseFilename=finish-page-test
Uninstallable=no

[Files]
Source: "finish-page.iss"; DestDir: "{app}"

[Run]
Filename: "{cmd}"; Parameters: "/c echo opened> ""{app}\website-opened.txt"""; Description: "Visit the website"; Flags: postinstall nowait runhidden
Filename: "{cmd}"; Parameters: "/c echo launched> ""{app}\game-launched.txt"""; Description: "Launch the game"; Flags: postinstall nowait runhidden

[Code]
procedure DeinitializeSetup();
begin
  SaveStringToFile(ExpandConstant('{app}\exit-code-ran.txt'), 'opened on exit', False);
end;
