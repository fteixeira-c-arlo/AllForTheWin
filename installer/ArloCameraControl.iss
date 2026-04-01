; Inno Setup 6 — single-file installer for testers.
; Output (commit / zip for testers): ..\release\ArloCameraControl-Windows\Install-ArloCameraControl.exe
; Build: from repo root run  powershell -ExecutionPolicy Bypass -File .\build_installer.ps1

#define MyAppName "Arlo Camera Control"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Arlo"
#define MyAppExeName "ArloCameraControl.exe"
; Relative to this .iss file (installer\)
#define SourceDir "..\dist\ArloCameraControl"

[Setup]
AppId={{E8B4A1C2-9F3D-4E5B-8A7C-1D2E3F4A5B6C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=..\release\ArloCameraControl-Windows
OutputBaseFilename=Install-ArloCameraControl
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
