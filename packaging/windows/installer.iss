; ACL per-user Windows installer of the reviewed source commit.
#define MyAppName "Adopy Campaign Launcher"
#define MyAppPublisher "Adopy"

#ifndef MyAppVersion
  #define MyAppVersion "0.1.0-dev"
#endif
#ifndef BundleDir
  #error BundleDir must point to the PyInstaller ACL directory
#endif
#ifndef CurrentManifest
  #error CurrentManifest must point to generated current.json
#endif
#ifndef ProjectRoot
  #error ProjectRoot must point to the source checkout
#endif
#ifndef InstallerOutputDir
  #define InstallerOutputDir "."
#endif

[Setup]
AppId={{D5369274-38CC-4D9E-98FD-80AE2B946A1C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\ACL\versions\{#MyAppVersion}
DefaultGroupName=Adopy Campaign Launcher
UsePreviousAppDir=no
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#InstallerOutputDir}
OutputBaseFilename=Adopy-Campaign-Launcher-Setup-{#MyAppVersion}
SetupIconFile={#ProjectRoot}\assets\dashboard.ico
UninstallDisplayIcon={app}\ACL.exe
UninstallFilesDir={localappdata}\Programs\ACL
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
AppMutex=Local\ACL-Campaign-Launcher-Server-v1
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Dirs]
Name: "{localappdata}\ACL"; Flags: uninsneveruninstall
Name: "{localappdata}\ACL\logs"; Flags: uninsneveruninstall
Name: "{localappdata}\ACL\diagnostics"; Flags: uninsneveruninstall
Name: "{localappdata}\ACL\workspace"; Flags: uninsneveruninstall

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#CurrentManifest}"; DestDir: "{localappdata}\Programs\ACL"; DestName: "current.json"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Adopy Campaign Launcher\Open Campaign Launcher"; Filename: "{app}\ACL.exe"; WorkingDir: "{localappdata}\ACL\workspace"
Name: "{autoprograms}\Adopy Campaign Launcher\Setup Campaign Launcher"; Filename: "{app}\ACL.exe"; Parameters: "--setup"
Name: "{autoprograms}\Adopy Campaign Launcher\Setup guide"; Filename: "{app}\_internal\setup-guide.html"
Name: "{autoprograms}\Adopy Campaign Launcher\Stop Campaign Launcher"; Filename: "{app}\ACL.exe"; Parameters: "--stop-server"
Name: "{autoprograms}\Adopy Campaign Launcher\Settings"; Filename: "{app}\ACL.exe"; Parameters: "--settings"
Name: "{autoprograms}\Adopy Campaign Launcher\Diagnostics"; Filename: "{app}\ACL.exe"; Parameters: "--diagnostics"
Name: "{autoprograms}\Adopy Campaign Launcher\Create diagnostic bundle"; Filename: "{app}\ACL.exe"; Parameters: "--export-diagnostics"
Name: "{autoprograms}\Adopy Campaign Launcher\Uninstall"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Adopy Campaign Launcher"; Filename: "{app}\ACL.exe"; WorkingDir: "{localappdata}\ACL\workspace"; Tasks: desktopicon

[Run]
Filename: "{app}\_internal\setup-guide.html"; Description: "Open the setup guide"; Flags: shellexec nowait postinstall skipifsilent
Filename: "{app}\ACL.exe"; Description: "Open Adopy Campaign Launcher"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\ACL.exe"; Parameters: "--stop-server"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "StopACLServer"
