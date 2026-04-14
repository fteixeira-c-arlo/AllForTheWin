; Inno Setup 6 — single-file installer for testers.
; Output (commit / zip for testers): ..\release\ArloHub-Windows\Install-ArloHub.exe
; Build: from repo root run  powershell -ExecutionPolicy Bypass -File .\build_installer.ps1

#define MyAppName "ArloHub"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Arlo"
#define MyAppExeName "ArloHub.exe"
; Relative to this .iss file (installer\)
#define SourceDir "..\dist\ArloHub"
#define SetupIcon "..\assets\ArloShell_icon.ico"

[Setup]
AppId={{7E5A9C12-3F8B-4D21-A6E4-9B1C0D7F52A8}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=..\release\ArloHub-Windows
OutputBaseFilename=Install-ArloHub
SetupIconFile={#SetupIcon}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
DisableProgramGroupPage=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupLogging=yes
InfoBeforeFile=InstallWizardIntro.txt

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
