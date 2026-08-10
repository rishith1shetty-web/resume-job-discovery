; Rising Path Recruiters Job Finding Automation - Windows installer
;
; Compiled by installer\build.ps1, which stages every input under
; installer\staging first. Compiling this file directly will fail on missing
; files, by design: a half-staged build must not produce an installer that
; looks complete.
;
; The order of operations is the important part:
;
;   1. BEFORE anything is written to the computer, confirm PostgreSQL 16+ is
;      installed, its service is running and it is answering on 5432. If any
;      of that is untrue the wizard explains what to do and exits, leaving
;      nothing to undo.
;   2. Collect the postgres administrator password and validate it on the page
;      where it was typed, so a typo is corrected in place rather than after a
;      file copy.
;   3. Copy files.
;   4. Provision the database. On failure, roll back and abort the install.
;
; The administrator password is never written to the registry, never passed on
; a command line, and never stored. It goes into a file in the elevated user's
; own temporary directory that the provisioning tool deletes as it reads.

#define AppName        "Rising Path Recruiters Job Finding Automation"
#define AppShortName   "RisingPathRecruitersJobFindingAutomation"
#define AppVersion      "1.0.0"
#define AppPublisher   "Rising Path Recruiters"
#define AppExeName     "RisingPathRecruitersJobFindingAutomation.exe"
#define ProvisionExe   "provision-database.exe"
#define DataFolder     "RisingPathRecruitersJobFindingAutomation"

[Setup]
AppId={{8E4C1E27-6B4D-4E58-9C2E-2F1A7B9D5C31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=RisingPathRecruitersJobFindingAutomationSetup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The application writes to Program Files never, and provisioning needs to
; query services, so an administrative install is required.
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Data lives under the user profile and survives uninstall; see [UninstallDelete].
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
SetupLogging=yes
MinVersion=10.0.17763

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; The frozen application: launcher, provisioning tool, and everything they
; need. No Python, Node or Git is required on the target computer.
Source: "staging\app\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "staging\docs\*"; DestDir: "{app}\docs"; Flags: ignoreversion recursesubdirs
; A second, temporary copy of the provisioning tool. The password page has to
; validate the password BEFORE any file is copied, which means running this
; while {app} does not yet exist. Built as a single file precisely so it can be
; extracted and run on its own.
Source: "staging\app\{#ProvisionExe}"; Flags: dontcopy noencryption

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop a running instance before removing its files, so uninstall does not
; fail on a locked executable.
Filename: "{app}\{#AppExeName}"; Parameters: "--shutdown"; Flags: runhidden; RunOnceId: "StopApp"

[UninstallDelete]
; Program files only. The user's database, resumes, history and logs live
; under %LOCALAPPDATA% and are deliberately kept, so reinstalling finds them.
Type: filesandordirs; Name: "{app}\_internal"

[Messages]
BeveledLabel={#AppPublisher}

[Code]
var
  PasswordPage: TInputQueryWizardPage;
  ProvisionResultPage: TOutputMsgWizardPage;
  PostgresVersion: Integer;
  PasswordAttempts: Integer;
  ProvisionSummary: String;

const
  MaxPasswordAttempts = 5;
  RequiredPostgresMajor = 16;

  { Exit codes from installer\provision.py. Kept in sync by hand; a code that
    drifts shows as "unexpected" rather than as the wrong explanation. }
  EXIT_OK = 0;
  EXIT_USAGE = 1;
  EXIT_CANNOT_CONNECT = 2;
  EXIT_BAD_PASSWORD = 3;
  EXIT_SERVER_TOO_OLD = 4;
  EXIT_ROLE_FAILED = 5;
  EXIT_DATABASE_FAILED = 6;
  EXIT_MIGRATION_FAILED = 7;
  EXIT_SCHEMA_INCOMPLETE = 8;
  EXIT_SEED_FAILED = 9;
  EXIT_CREDENTIAL_FAILED = 10;
  EXIT_VERIFY_FAILED = 11;

{ ---------------------------------------------------------------------------
  PostgreSQL detection

  Reads the registry rather than probing the filesystem: an installation that
  did not register itself (a Docker container, WSL, an unzipped copy) is not
  something this installer can configure a Windows service for, and pretending
  otherwise produces a much more confusing failure later.
  --------------------------------------------------------------------------- }

function GetPostgresMajorVersion(): Integer;
var
  Names: TArrayOfString;
  Index: Integer;
  Version: String;
  Major: Integer;
  Best: Integer;
begin
  Best := 0;
  if RegGetSubkeyNames(HKLM, 'SOFTWARE\PostgreSQL\Installations', Names) then
  begin
    for Index := 0 to GetArrayLength(Names) - 1 do
    begin
      if RegQueryStringValue(HKLM, 'SOFTWARE\PostgreSQL\Installations\' + Names[Index],
                             'Version', Version) then
      begin
        Major := StrToIntDef(Copy(Version, 1, Pos('.', Version + '.') - 1), 0);
        if Major > Best then
          Best := Major;
      end;
    end;
  end;
  Result := Best;
end;

{ Runs a command through cmd so its exit code is available. Hidden: the user
  is looking at a wizard, not a console. }
function RunHidden(const Command: String; var ResultCode: Integer): Boolean;
begin
  Result := Exec(ExpandConstant('{cmd}'), '/C ' + Command, '', SW_HIDE,
                 ewWaitUntilTerminated, ResultCode);
end;

function PostgresServiceRunning(): Boolean;
var
  ResultCode: Integer;
begin
  { `sc query` returns 0 only when the named service exists and is queryable;
    findstr then confirms it is actually RUNNING rather than merely present. }
  Result := RunHidden('sc query postgresql-x64-' + IntToStr(RequiredPostgresMajor) +
                      ' | findstr /I "RUNNING" >nul 2>&1', ResultCode) and (ResultCode = 0);
  if not Result then
    { Some installations register the service without the major version. }
    Result := RunHidden('sc query postgresql | findstr /I "RUNNING" >nul 2>&1',
                        ResultCode) and (ResultCode = 0);
end;

function PostgresPortAnswering(): Boolean;
var
  ResultCode: Integer;
begin
  { A running service is not the same as a server accepting connections on the
    port this application uses. PowerShell is used because Inno Setup's
    scripting has no socket support. }
  Result := RunHidden('powershell -NoProfile -ExecutionPolicy Bypass -Command ' +
                      '"exit [int](-not (Test-NetConnection -ComputerName 127.0.0.1 ' +
                      '-Port 5432 -InformationLevel Quiet))"', ResultCode)
            and (ResultCode = 0);
end;

function InitializeSetup(): Boolean;
begin
  Result := False;
  PasswordAttempts := 0;

  PostgresVersion := GetPostgresMajorVersion();

  if PostgresVersion = 0 then
  begin
    MsgBox('PostgreSQL is not installed on this computer.' + #13#10#13#10 +
           'Rising Path Recruiters Job Finding Automation keeps all of your data in a local ' +
           'PostgreSQL database, so PostgreSQL 16 must be installed first.' +
           '' + #13#10#13#10 +
           'Install PostgreSQL 16, remember the password you set for the ' +
           '"postgres" account, then run this setup again.' + #13#10#13#10 +
           'Nothing has been installed on your computer.',
           mbCriticalError, MB_OK);
    Exit;
  end;

  if PostgresVersion < RequiredPostgresMajor then
  begin
    MsgBox('This computer has PostgreSQL ' + IntToStr(PostgresVersion) + ', but ' +
           'Rising Path Recruiters Job Finding Automation requires PostgreSQL ' +
           IntToStr(RequiredPostgresMajor) + ' or newer.' + #13#10#13#10 +
           'Install PostgreSQL ' + IntToStr(RequiredPostgresMajor) +
           ' and run this setup again.' + #13#10#13#10 +
           'Nothing has been installed on your computer.',
           mbCriticalError, MB_OK);
    Exit;
  end;

  if not PostgresServiceRunning() then
  begin
    MsgBox('PostgreSQL ' + IntToStr(PostgresVersion) + ' is installed, but its ' +
           'Windows service is not running.' + #13#10#13#10 +
           'Open PowerShell as an administrator and run:' + #13#10#13#10 +
           '    Start-Service -Name postgresql-x64-' +
           IntToStr(RequiredPostgresMajor) + #13#10#13#10 +
           'Then run this setup again.' + #13#10#13#10 +
           'Nothing has been installed on your computer.',
           mbCriticalError, MB_OK);
    Exit;
  end;

  if not PostgresPortAnswering() then
  begin
    MsgBox('PostgreSQL is running, but nothing is answering on 127.0.0.1 port 5432.' +
           '' + #13#10#13#10 +
           'Either PostgreSQL is listening on a different port, or another ' +
           'program is holding 5432. This application only supports the ' +
           'standard port on this computer.' + #13#10#13#10 +
           'Nothing has been installed on your computer.',
           mbCriticalError, MB_OK);
    Exit;
  end;

  Result := True;
end;

{ ---------------------------------------------------------------------------
  Password page
  --------------------------------------------------------------------------- }

procedure InitializeWizard();
begin
  PasswordPage := CreateInputQueryPage(wpSelectTasks,
    'PostgreSQL Administrator Password',
    'Setup needs this once, to create the application''s own database.',
    'Enter the password for the PostgreSQL "postgres" account - the password ' +
    'you chose when you installed PostgreSQL.' + #13#10#13#10 +
    'It is used only during this installation and is never stored. The ' +
    'application creates its own separate database account with its own ' +
    'password, protected for your Windows account.');
  PasswordPage.Add('&PostgreSQL administrator password:', True);

  { Anchored to wpInstalling so it appears between the copy and the finish
    page — which is exactly when provisioning has just run. }
  ProvisionResultPage := CreateOutputMsgPage(wpInstalling,
    'Database setup', 'Your local database is ready.', '');
end;

{ Writes the password to a file only this elevated session can reach, and
  returns the path. The provisioning tool deletes it as it reads it. This
  exists because Inno Setup cannot write to a child process's stdin, and a
  command-line argument would be visible in the process table and echoed into
  Inno's own log. }
function WritePasswordFile(const Password: String): String;
var
  Path: String;
begin
  Path := ExpandConstant('{tmp}\pgpw.tmp');
  if not SaveStringToFile(Path, Password, False) then
    Path := '';
  Result := Path;
end;

function ValidateAdminPassword(const Password: String; var ExitCode: Integer): Boolean;
var
  PasswordFile: String;
  ProvisionPath: String;
begin
  Result := False;
  ExitCode := -1;

  PasswordFile := WritePasswordFile(Password);
  if PasswordFile = '' then
  begin
    MsgBox('The password could not be passed securely to the database step.' +
           '' + #13#10#13#10 +
           'This usually means setup was approved by a different administrator ' +
           'account from the one signed in. Sign in as the account that will ' +
           'use the application and run setup again.',
           mbCriticalError, MB_OK);
    Exit;
  end;

  { Before the files are copied the tool is not in the application folder
    yet, so the temporary copy is extracted and run from there. }
  ProvisionPath := ExpandConstant('{tmp}\{#ProvisionExe}');
  if not FileExists(ProvisionPath) then
  begin
    ExtractTemporaryFile('{#ProvisionExe}');
  end;
  if not FileExists(ProvisionPath) then
  begin
    MsgBox('Setup could not prepare its database tool. The download may be ' +
           'incomplete - verify the file''s checksum and try again.',
           mbCriticalError, MB_OK);
    Exit;
  end;

  Exec(ProvisionPath, '--test-password --password-from "' + PasswordFile + '"',
       '', SW_HIDE, ewWaitUntilTerminated, ExitCode);

  { Belt and braces: the tool deletes this itself, but a crash before that
    point must not leave a password on disk. }
  DeleteFile(PasswordFile);

  Result := (ExitCode = EXIT_OK);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  ExitCode: Integer;
begin
  Result := True;
  if CurPageID <> PasswordPage.ID then
    Exit;

  if Trim(PasswordPage.Values[0]) = '' then
  begin
    MsgBox('Enter the PostgreSQL administrator password to continue.',
           mbError, MB_OK);
    Result := False;
    Exit;
  end;

  WizardForm.StatusLabel.Caption := 'Checking the PostgreSQL password...';
  if ValidateAdminPassword(PasswordPage.Values[0], ExitCode) then
    Exit;

  PasswordAttempts := PasswordAttempts + 1;
  Result := False;

  if ExitCode = EXIT_SERVER_TOO_OLD then
  begin
    MsgBox('This computer''s PostgreSQL is older than version ' +
           IntToStr(RequiredPostgresMajor) + '. Install PostgreSQL ' +
           IntToStr(RequiredPostgresMajor) + ' and run setup again.',
           mbCriticalError, MB_OK);
    Exit;
  end;

  if ExitCode = EXIT_CANNOT_CONNECT then
  begin
    MsgBox('PostgreSQL stopped responding on 127.0.0.1:5432.' + #13#10#13#10 +
           'Check that the PostgreSQL service is still running, then try again.',
           mbError, MB_OK);
    Exit;
  end;

  if PasswordAttempts >= MaxPasswordAttempts then
  begin
    MsgBox('The PostgreSQL administrator password was not accepted after ' +
           IntToStr(MaxPasswordAttempts) + ' attempts.' + #13#10#13#10 +
           'Setup will close. Nothing has been installed. Find the password ' +
           'you set when installing PostgreSQL and run setup again.',
           mbCriticalError, MB_OK);
    WizardForm.Close;
    Exit;
  end;

  MsgBox('PostgreSQL did not accept that password.' + #13#10#13#10 +
         'This is the password for the "postgres" database account, chosen ' +
         'when PostgreSQL was installed. It is not your Windows password.' +
         '' + #13#10#13#10 + 'Attempt ' + IntToStr(PasswordAttempts) + ' of ' +
         IntToStr(MaxPasswordAttempts) + '.',
         mbError, MB_OK);
end;

{ ---------------------------------------------------------------------------
  Provisioning, after the files are in place
  --------------------------------------------------------------------------- }

function DescribeProvisionFailure(ExitCode: Integer): String;
begin
  case ExitCode of
    EXIT_CANNOT_CONNECT:
      Result := 'PostgreSQL stopped responding during setup.' + #13#10 +
                'Check that its Windows service is still running.';
    EXIT_BAD_PASSWORD:
      Result := 'PostgreSQL rejected the administrator password.';
    EXIT_SERVER_TOO_OLD:
      Result := 'This computer''s PostgreSQL is older than version ' +
                IntToStr(RequiredPostgresMajor) + '.';
    EXIT_ROLE_FAILED:
      Result := 'The application''s database account could not be created.' + #13#10 +
                'The postgres account may not have permission to create roles.';
    EXIT_DATABASE_FAILED:
      Result := 'The application database could not be created.';
    EXIT_MIGRATION_FAILED:
      Result := 'The database structure could not be created.';
    EXIT_SCHEMA_INCOMPLETE:
      Result := 'The database was created but is missing some tables.';
    EXIT_SEED_FAILED:
      Result := 'The database was created but its starting data could not be written.';
    EXIT_CREDENTIAL_FAILED:
      Result := 'Windows could not protect the database password for your account.' +
                '' + #13#10 + 'Try a different administrator account on this computer.';
    EXIT_VERIFY_FAILED:
      Result := 'The database was created, but the application could not ' +
                'connect to it afterwards.';
  else
    Result := 'The database step failed unexpectedly (code ' +
              IntToStr(ExitCode) + ').';
  end;
end;

function ProvisionDatabase(): Boolean;
var
  PasswordFile: String;
  ExitCode: Integer;
  LogPath: String;
begin
  PasswordFile := WritePasswordFile(PasswordPage.Values[0]);
  if PasswordFile = '' then
  begin
    MsgBox('The password could not be passed securely to the database step.',
           mbCriticalError, MB_OK);
    Result := False;
    Exit;
  end;

  WizardForm.StatusLabel.Caption := 'Creating the application database...';

  Exec(ExpandConstant('{app}\{#ProvisionExe}'),
       '--password-from "' + PasswordFile + '" --data-dir "' +
       ExpandConstant('{localappdata}\{#DataFolder}') + '"',
       ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ExitCode);

  DeleteFile(PasswordFile);

  Result := (ExitCode = EXIT_OK);

  if Result then
  begin
    ProvisionSummary := 'The application database is ready.' + #13#10#13#10 +
      'Database    resume_job_discovery' + #13#10 +
      'Account     resume_job_discovery_app (created for this computer)' + #13#10 +
      'Your data   ' + ExpandConstant('{localappdata}\{#DataFolder}') + #13#10#13#10 +
      'The PostgreSQL administrator password was not stored. The application ' +
      'uses its own account, protected for your Windows account.';
    Exit;
  end;

  LogPath := ExpandConstant('{localappdata}\{#DataFolder}\logs\install.log');
  MsgBox('Setup could not finish preparing the database.' + #13#10#13#10 +
         DescribeProvisionFailure(ExitCode) + #13#10#13#10 +
         'Any database objects this setup created have been removed, so ' +
         'nothing is left half-configured. Existing data was not touched.' +
         '' + #13#10#13#10 + 'Details:' + #13#10 + LogPath,
         mbCriticalError, MB_OK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then
    Exit;

  if ProvisionDatabase() then
  begin
    ProvisionResultPage.MsgLabel.Caption := ProvisionSummary;
    Exit;
  end;

  { Provisioning failed and has already rolled back its own database changes.
    Removing the copied files too leaves the computer as it was found. }
  DelTree(ExpandConstant('{app}'), True, True, True);
  Abort;
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  MsgBox('Uninstalling removes the program only.' + #13#10#13#10 +
         'Your resumes, searches and application history are KEPT, in:' + #13#10 +
         ExpandConstant('{localappdata}\{#DataFolder}') + #13#10#13#10 +
         'The PostgreSQL database "resume_job_discovery" is also kept. ' +
         'Reinstalling later picks everything up again.' + #13#10#13#10 +
         'To remove your data as well, see docs\WINDOWS_INSTALLER.md.',
         mbInformation, MB_OK);
end;
