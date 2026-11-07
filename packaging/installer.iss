; Inno Setup 6 script for the Windows installer. Built by packaging\build.ps1:
;   ISCC /DAppVersion=1.7.0 /DAppSourceDir=<dist\AlbumArtOverlay> /O<dist> installer.iss
;
; Per-user, no admin prompt: the app goes to %LOCALAPPDATA%\Programs\Album Art
; Overlay. The user's settings, library and log are NOT in here — they live in
; %LOCALAPPDATA%\AlbumArtOverlay (app/config.py DATA_DIR), which this installer
; never touches, so upgrading, reinstalling and uninstalling all keep them.
;
; What gets installed is the app's source plus python.org's embeddable Python
; (see build.ps1 for why): the shortcuts run python\pythonw.exe -m app.desktop.

#ifndef AppVersion
  #error Pass /DAppVersion=x.y.z (build.ps1 reads it from app\__init__.py)
#endif
#ifndef AppSourceDir
  #error Pass /DAppSourceDir=<dist\AlbumArtOverlay, as build.ps1 assembles it>
#endif

#define AppName "Album Art Overlay"
; The launcher. pythonw has no console window; the tray icon is the app.
#define AppRunner "{app}\python\pythonw.exe"
#define AppArgs "-m app.desktop"
#define RepoUrl "https://github.com/deadhead1971/streamersonglist-album-art-overlay"

[Setup]
; The installer's identity. Generated once; NEVER change it, or upgrades stop
; recognising earlier installs and Windows lists the app twice.
AppId={{60989E7B-C013-4985-90D2-C125D19ED152}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Alan Thompson
AppPublisherURL={#RepoUrl}
AppSupportURL={#RepoUrl}/issues
AppUpdatesURL={#RepoUrl}/releases
VersionInfoVersion={#AppVersion}

PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
UsePreviousAppDir=yes

; A running copy is closed by [Code] below, not by AppMutex: AppMutex is
; checked before anything can close the app, so it could only tell the user
; to go and find a tray icon (and made a silent upgrade abort). The Restart
; Manager still handles any other process holding a file open.
CloseApplications=yes
RestartApplications=no

ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=AlbumArtOverlay-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\icon.ico
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[InstallDelete]
; A new version is not a superset of the old one; clear the old code out
; rather than leave stale modules beside the new ones.
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\obs"

[Files]
Source: "{#AppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; "for StreamerSonglist" so a Start-menu search for either word finds it.
Name: "{autoprograms}\{#AppName} for StreamerSonglist"; Filename: "{#AppRunner}"; Parameters: "{#AppArgs}"; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"; Comment: "Album art and queue overlays for OBS"
Name: "{autodesktop}\{#AppName}"; Filename: "{#AppRunner}"; Parameters: "{#AppArgs}"; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Run]
Filename: "{#AppRunner}"; Parameters: "{#AppArgs}"; WorkingDir: "{app}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Anything Python compiled at run time is not in the install log. The build
; precompiles everything, so this is a backstop; the data folder is untouched.
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\app"

[Code]
const
  { app/instance.py MUTEX_NAME and the app's fixed port. }
  AppMutexName = 'AlbumArtOverlay.SingleInstance';
  QuitUrl = 'http://127.0.0.1:5050/api/quit';
  QuitWaitMs = 15000;

function AppRunning(): Boolean;
begin
  Result := CheckForMutexes(AppMutexName);
end;

{ Ask a running copy to quit the way its own Quit does: the runtime stops and
  the server shuts down cleanly, so nothing is left half-written. JSON only,
  as /api/quit requires. A source run (python -m app.dashboard) has no quit
  endpoint and answers 400; that falls through to asking the user. }
procedure RequestQuit();
var
  Http: Variant;
begin
  try
    Http := CreateOleObject('WinHttp.WinHttpRequest.5.1');
    Http.Open('POST', QuitUrl, False);
    Http.SetTimeouts(2000, 2000, 2000, 2000);
    Http.SetRequestHeader('Content-Type', 'application/json');
    Http.Send('{}');
  except
    Log('Quit request failed: ' + GetExceptionMessage);
  end;
end;

{ True once no copy is running: asked to quit first, then the user as a last
  resort. False means the user gave up (or a silent install can't ask). }
function CloseRunningApp(): Boolean;
var
  Waited: Integer;
begin
  Result := True;
  if not AppRunning() then Exit;
  Log('Album Art Overlay is running; asking it to quit');
  RequestQuit();
  Waited := 0;
  while AppRunning() and (Waited < QuitWaitMs) do
  begin
    Sleep(250);
    Waited := Waited + 250;
  end;
  while AppRunning() do
  begin
    if SuppressibleMsgBox('Album Art Overlay is still running.' + #13#10#13#10 +
        'Quit it from its icon in the system tray (or close its window), ' +
        'then click Retry.', mbError, MB_RETRYCANCEL, IDCANCEL) = IDCANCEL then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

{ After the user clicks Install, not at launch: opening the installer must not
  take the overlays down before the user has decided anything. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not CloseRunningApp() then
    Result := 'Album Art Overlay is still running. Quit it, then run Setup again.';
end;

function InitializeUninstall(): Boolean;
begin
  Result := CloseRunningApp();
end;
