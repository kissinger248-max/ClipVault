; Windows installer for ClipVault (Inno Setup 6). Built by packaging/build.py, which
; passes AppVersion, SourceDir (the PyInstaller folder), OutputDir and IconFile.
;
; Installs for the current user only (no administrator prompt) into
; %LOCALAPPDATA%\Programs\ClipVault, adds Start menu (and optional desktop)
; shortcuts, and starts ClipVault at the end so the control panel opens.
; Uninstalling removes the program and its start-at-login entry; the clip library
; and settings (%LOCALAPPDATA%\ClipVault) are kept.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F1E2B8C-6B0B-4C43-9E2B-1C2D9A4F7C31}
AppName=ClipVault
AppVersion={#AppVersion}
AppVerName=ClipVault {#AppVersion}
AppPublisher=ClipVault
AppPublisherURL=https://github.com/kissinger248-max/ClipVault
DefaultDirName={localappdata}\Programs\ClipVault
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=ClipVault-Setup-Windows
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\ClipVault.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Put a ClipVault shortcut on the desktop"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\ClipVault"; Filename: "{app}\ClipVault.exe"
Name: "{userdesktop}\ClipVault"; Filename: "{app}\ClipVault.exe"; Tasks: desktopicon

[Registry]
; ClipVault adds itself here on first run ("Open when I log in"); remove it on uninstall.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "ClipVault"; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{app}\ClipVault.exe"; Description: "Start ClipVault now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im ClipVault.exe"; Flags: runhidden; RunOnceId: "StopClipVault"

[Code]
// Stop a running ClipVault before files are replaced (an update over an older copy).
// Its jobs are safe to interrupt: each one resumes where it stopped.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im ClipVault.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;
