; HSYNC Client — Windows installer (Inno Setup 6)
;
; Per-user install (no admin): the frozen CLI is placed under
; %LOCALAPPDATA%\Programs\HSYNC and its directory is appended to the user PATH.
;
; Built by .github/workflows/release.yml (windows-amd64 matrix entry), which
; compiles the .exe first and passes these preprocessor defines:
;   /DAppVersion=<semver>     /DSourceExe=<path to hsync.exe>
;   /DOutDir=<output dir>     /DIconPath=<path to icon.ico>   (optional)
;
; The version mapping follows RELEASES.md (SemVer -> numeric where required).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
; Numeric file version (up to four dot-separated numbers). The workflow passes
; the SemVer stripped of its prerelease/build suffix; the fallback keeps the
; script compilable standalone.
#ifndef VersionInfoVersion
  #define VersionInfoVersion "0.0.0.0"
#endif
#ifndef SourceExe
  #define SourceExe "hsync.exe"
#endif
#ifndef OutDir
  #define OutDir "."
#endif

[Setup]
AppId={{5C0E9A11-2B1E-4E2A-9F31-7A0C1D2E3F40}
AppName=HSYNC Client
AppVersion={#AppVersion}
AppVerName=HSYNC Client {#AppVersion}
VersionInfoVersion={#VersionInfoVersion}
AppPublisher=HSYNC
AppPublisherURL=https://github.com/albertsdk/hsync-client
DefaultDirName={localappdata}\Programs\HSYNC
DefaultGroupName=HSYNC
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutDir}
OutputBaseFilename=hsync-client-{#AppVersion}-windows-amd64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes
#ifdef IconPath
SetupIconFile={#IconPath}
#endif
UninstallDisplayName=HSYNC Client

[Files]
Source: "{#SourceExe}"; DestDir: "{app}"; Flags: ignoreversion

[Registry]
; Append {app} to the per-user PATH; removed again on uninstall (see [Code]).
Root: HKCU; Subkey: "Environment"; \
    ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; \
    Check: NeedsAddPath(ExpandConstant('{app}'))

[Code]
function NeedsAddPath(Param: string): Boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', OrigPath) then
  begin
    Result := True;
    Exit;
  end;
  { Look for the path, surrounded by semicolons, case-insensitively. }
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;

procedure RemoveFromUserPath(const Dir: string);
var
  OrigPath, NewPath, Entry: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', OrigPath) then
    Exit;
  NewPath := '';
  OrigPath := OrigPath + ';';
  while OrigPath <> '' do
  begin
    P := Pos(';', OrigPath);
    Entry := Copy(OrigPath, 1, P - 1);
    OrigPath := Copy(OrigPath, P + 1, Length(OrigPath));
    if (Entry <> '') and (Uppercase(Entry) <> Uppercase(Dir)) then
    begin
      if NewPath <> '' then
        NewPath := NewPath + ';';
      NewPath := NewPath + Entry;
    end;
  end;
  RegWriteExpandStringValue(HKCU, 'Environment', 'Path', NewPath);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromUserPath(ExpandConstant('{app}'));
end;

[Run]
; No post-install action: this is a command-line tool. Open a console to run it.
Filename: "{cmd}"; Parameters: "/K ""{app}\hsync.exe --help"""; \
    Description: "Open a console with HSYNC Client"; Flags: postinstall nowait skipifsilent unchecked
