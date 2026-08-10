# Windows installer and deployment

How Rising Path Recruiters Job Finding Automation is packaged, installed, configured and removed.
Written for whoever maintains the build, not for the recruiter installing it —
their instructions are `Installation-Guide.txt`, shipped beside the installer.

---

## 1. Deployment model

The application is a **local Windows desktop program**. There is no server, no
cloud component, no shared database and no telemetry.

Each computer holds its own complete, independent installation:

| Belongs to the machine | Where |
|---|---|
| Program files | `C:\Program Files\Rising Path Recruiters Job Finding Automation` |
| Database | local PostgreSQL, database `resume_job_discovery` |
| Uploaded resumes | `%LOCALAPPDATA%\RisingPathRecruitersJobFindingAutomation\storage` |
| Configuration | `%LOCALAPPDATA%\RisingPathRecruitersJobFindingAutomation\config\app.env` |
| Credential (encrypted) | `…\config\database_credential.dat` |
| Logs | `…\logs\launcher.log`, `…\logs\install.log` |

Nothing is synchronised between machines. Two installations never see each
other's resumes, searches or application history.

**PostgreSQL is the one prerequisite.** The user installs PostgreSQL 16 and
remembers its `postgres` password. Everything after that is automated.

---

## 2. What the user does

1. Install PostgreSQL 16.
2. Run `RisingPathRecruitersJobFindingAutomationSetup.exe` as administrator.
3. Enter the PostgreSQL administrator password once.
4. Launch from the Start menu or desktop shortcut.

They never see Python, Node.js, npm, a terminal, a migration or a config file.

---

## 3. Installer architecture

`installer/ResumeJobDiscovery.iss` (Inno Setup 6). The order matters more than
any individual step:

```
InitializeSetup          ← BEFORE anything is written
  ├─ registry: PostgreSQL installed? which major version?
  ├─ service: postgresql-x64-16 running?
  └─ socket:  127.0.0.1:5432 answering?
        any failure → explain, abort, write nothing

Password page
  └─ provision-database.exe --test-password
        validated where it was typed; up to 5 attempts
        creates nothing

Copy files

ssPostInstall
  └─ provision-database.exe --password-from <file> --data-dir <localappdata>
        success → summary page
        failure → provisioning rolls back its own database changes,
                  installer deletes {app}, Abort
```

Two properties are deliberate:

**Nothing is written before the environment is known good.** A user without
PostgreSQL gets an explanation and an untouched computer, so they can fix the
problem and re-run with nothing to undo.

**The password is validated on its own page.** Discovering a typo after a
114 MB file copy, and having to restart the wizard, is the difference between a
correctable mistake and a support call.

### PostgreSQL detection

Detection reads `HKLM\SOFTWARE\PostgreSQL\Installations\*`. This is a
deliberate limitation: a server in Docker, WSL or an unzipped copy does not
register there and is not detected. Those installations cannot be configured
through a Windows service either, so detecting them would only move the failure
somewhere harder to explain.

The port check runs `Test-NetConnection` through PowerShell because Inno
Setup's scripting language has no socket support. A running service is not the
same as a server accepting connections — a stopped listener, a wrong port or
another program holding 5432 all present as "service running".

---

## 4. Database provisioning

`installer/provision.py`, frozen to `provision-database.exe`.

```
connect as postgres (password from file, deleted as it is read)
  ├─ check server version ≥ 16
  ├─ role resume_job_discovery_app
  │     absent  → CREATE with a random 48-char password
  │     present → keep existing credential, or reset if we hold none
  ├─ database resume_job_discovery
  │     absent  → CREATE, owned by the ADMINISTRATOR
  │     present → KEEP, with all its data and its existing ownership
  ├─ GRANT CONNECT + USAGE ON SCHEMA          (enough to reach it)
  ├─ alembic upgrade head  AS THE ADMINISTRATOR   (worker thread — see below)
  ├─ GRANT SELECT/INSERT/UPDATE/DELETE on every table   ← only now possible
  ├─ verify all 15 tables exist AND are readable by the app role
  ├─ seed portals and the local user, AS THE APP ROLE (proves the grants)
  ├─ save DPAPI credential
  ├─ write app.env
  └─ reconnect as the app role and read a real table   ← final proof
```

### Why migrations run as the administrator

This was originally the other way round, and it failed on the first install
over a pre-existing database with `permission denied for table
alembic_version`.

Migrations are DDL, and only an object's *owner* may alter it. Owning the
schema — which the earlier version did — grants the right to create **new**
objects in it and confers nothing at all over objects already inside it. In a
database that already existed, every table belongs to whoever created it, so
the application role could not read `alembic_version`, let alone migrate it.

A fresh database hid the bug completely: there the application role created
every table itself and owned them implicitly.

Running migrations as the administrator fixes it and lands somewhere better
than the original design was aiming for. The runtime account now needs no DDL
rights at all: it can read and write rows and nothing else, so it cannot drop a
table or alter the schema even if the application were compromised. Schema
changes arrive only through the installer, which is the only thing that ever
holds the administrator password.

The ordering is load-bearing. Table grants must follow migrations, because a
privilege cannot be granted on a table that does not exist yet.

**Verification is explicit.** "Alembic exited 0" is not proof the schema is
right; the table list is checked by name, and then the tool reconnects exactly
as the application will, using only what was stored. Every earlier step can
succeed and still leave a configuration the app cannot load.

**Migrations run in a worker thread.** `migrations/env.py` drives an async
engine with `asyncio.run()`, which raises if a loop is already running.
Provisioning is itself async, so Alembic gets a thread with a clean slate.

### Rollback

On failure, only what *this run* created is removed. A database that already
existed is never dropped to clean up a failed upgrade — that would trade a
failed install for lost application history. The tool tracks
`created_role` / `created_database` and undoes exactly those.

Dropping the role needs `DROP OWNED BY <role>` inside the application database
first. PostgreSQL refuses to drop a role anything still depends on, and those
dependencies — the grants above — live inside each database rather than
globally, so `DROP ROLE` on its own fails with *"cannot be dropped because some
objects depend on it"*. This is safe against user data: the role owns nothing,
and revoking a grant does not touch the table it was granted on.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | No password supplied |
| 2 | Cannot reach PostgreSQL |
| 3 | Password rejected |
| 4 | Server older than 16 |
| 5 | Role creation failed |
| 6 | Database creation failed |
| 7 | Migration failed |
| 8 | Schema incomplete after migration |
| 9 | Seeding failed |
| 10 | DPAPI could not protect the credential |
| 11 | Application could not connect with the stored credential |
| 20 | Unexpected |

The `.iss` maps each to a sentence the user can act on. They are kept in sync
by hand; a drifted code shows as "unexpected" rather than as the wrong
explanation.

---

## 5. Credential storage

`app/core/credentials.py`.

The PostgreSQL administrator password is **never stored**. It is used once,
during installation, to create a least-privilege role:

```sql
CREATE ROLE resume_job_discovery_app WITH LOGIN PASSWORD '<48 random chars>'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION
```

It owns nothing — not the database, not the schema, not a single table. It
holds `CONNECT`, `USAGE` on the schema, and row-level `SELECT/INSERT/UPDATE/
DELETE`. That is everything the application does at run time and nothing more.

That role's password is generated locally, stored with `CryptProtectData`
(DPAPI) at **user scope**, and never written in plain text — not to `app.env`,
not to the registry, not to a log.

Why user scope and not machine scope: `CRYPTPROTECT_LOCAL_MACHINE` would let
any account on the computer recover the password, including a service account
an attacker reached. Per-user means the ciphertext is worthless to everyone
else — and worthless off the machine entirely, which an ACL cannot achieve for
a copied file.

The consequence is real and is surfaced rather than hidden: a credential
written by one Windows account cannot be read by another. `load_credential`
raises `CredentialNotOurs` specifically, and the launcher turns that into
"sign in as the account the application was installed for" rather than
"decryption failed".

The generated password is alphanumeric by design. It passes through a
PowerShell command line, an Inno Setup exec, a libpq connection string and a
SQLAlchemy URL, each with its own quoting rules; 48 alphanumeric characters
carry far more entropy than punctuation would buy back in complexity.

---

## 6. Startup architecture

`desktop/launcher.py`, frozen to `RisingPathRecruitersJobFindingAutomation.exe` (windowed, no
console).

```
--version   print and exit
--shutdown  POST /api/v1/system/shutdown to a running instance
--check     verify configuration, credential, database, schema; exit 0 if sound

default:
  named mutex → already running? open the browser at the existing instance
  preflight   → credential present, readable, configuration loadable
  serve       → uvicorn in-process on 127.0.0.1:8100
  probe       → poll /health, then open the browser
```

**In-process, not a subprocess.** A child uvicorn is the classic way to leave
an orphaned server holding a port after a crash. On a thread of this process,
the OS reclaims it however the program ends.

**One process, one port.** The React interface is compiled to static files and
served by the same FastAPI app (`app/web.py`), so there is no Vite, no Node at
run time and no second origin — which also means no CORS.

**127.0.0.1, never 0.0.0.0.** The application has no authentication because it
is not reachable from the network. That assumption holds only while the bind
address does.

**Port 8100, never 8000.** Port 8000 belongs to the separate LinkedIn/Dice
application, which may be installed on the same computer. The launcher refuses
to bind 8000 rather than break it.

---

## 7. Packaging

`installer/build.ps1`:

| Stage | Output |
|---|---|
| `npm run build` | `frontend/dist` — static interface |
| PyInstaller `launcher.spec` | `dist/RisingPathRecruitersJobFindingAutomation/` (one **directory**) |
| PyInstaller `provision.spec` | `dist/provision-database.exe` (one **file**) |
| Stage | `installer/staging/{app,docs}` |
| Inno Setup | `dist/RisingPathRecruitersJobFindingAutomationSetup.exe` + `SHA256.txt` |

The launcher is a *directory* build: a one-file build re-extracts ~115 MB to
temp on every launch, and several seconds of nothing after a double-click is
when a user decides the program is broken and clicks again.

The provisioning tool is a *single file* because the installer must run it
through `ExtractTemporaryFile` before `{app}` exists.

Bundled: the Python runtime, every dependency, Alembic migrations,
`alembic.ini`, and the built interface. Excluded: `pytest`, `PyInstaller`,
`playwright`, `tkinter` — the shipped artifact contains nothing a user could
accidentally run against their own data.

The build refuses to produce an installer from an incomplete staging tree; it
asserts the executable, `frontend_dist/index.html` and `migrations/` are all
present first.

---

## 8. Logging

| Log | Path | Rotation |
|---|---|---|
| Installer (Inno) | `%TEMP%\Setup Log*.txt` | per run |
| Provisioning | `…\logs\install.log` | appended, sectioned per run |
| Launcher | `…\logs\launcher.log` | 5 × 2 MB rolling |
| Backend | `…\logs\application.log` | 5 × 2 MB rolling |
| Last-resort stream | `…\logs\stdout.log` | appended |

`launcher.log` records startup, health and shutdown; `application.log` is the
backend's own structured log, including one line per HTTP request, and is the
file to read when the interface is reachable but a page misbehaves. Both are
written only by an installed copy — development has a console and a source
tree, and a log file there would be litter.

`stdout.log` exists because a windowed build has no standard handles. The
launcher binds `sys.stdout`/`sys.stderr` to it before importing anything, so a
library that writes to them during import cannot take the process down. It is
normally empty; content in it means something bypassed the logging setup.

No stack trace reaches the user. The launcher has no console, so anticipated
failures become a message box saying what happened and what to do; the detail
goes to the log, and the dialog names the file.

---

## 9. Error handling

| Situation | What the user sees |
|---|---|
| PostgreSQL absent | Install PostgreSQL 16 first. Nothing was installed. |
| Version < 16 | Names the version found and what is required. |
| Service stopped | Gives the exact `Start-Service` command. |
| Port 5432 silent | Explains that a running service is not the same as a listening one. |
| Wrong password | Says it is the postgres account's password, not Windows'. Retries up to 5. |
| Database exists | Kept, with its data. Reported as an upgrade. |
| Migration failure | Names the stage, rolls back, points at the log. |
| DPAPI unavailable | Suggests another administrator account. |
| Port 8100 busy | Names the port and asks the user to close what holds it. |
| Credential from another account | Explains which account to sign in as. |
| Not provisioned | "Run the installer again to create its database." |

---

## 10. Upgrades and uninstall

**Upgrade:** install over the existing installation. Do not uninstall first.
The database, resumes, history, settings and stored credential are all kept;
new migrations are applied during provisioning. The password page still appears
but the password is not used to recreate anything.

**Uninstall** removes program files, shortcuts and the Add/Remove entry only.
It keeps `%LOCALAPPDATA%\RisingPathRecruitersJobFindingAutomation`, the `resume_job_discovery`
database and the role, so reinstalling picks everything up again.

**Complete removal** (irreversible):

```powershell
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\RisingPathRecruitersJobFindingAutomation"
psql -U postgres -c "DROP DATABASE resume_job_discovery;"
psql -U postgres -c "DROP ROLE resume_job_discovery_app;"
```

---

## 11. Known limitations

- PostgreSQL 16 must be installed manually first. This is the design.
- Detection is registry-based: Docker, WSL and unzipped servers are not found.
- Only `127.0.0.1:5432` is supported. No remote server, no alternate port.
- The installer is not code-signed, so SmartScreen warns on first run. Ship
  and check `SHA256.txt`.
- Windows x64 only. `MinVersion=10.0.17763`.
- No automatic updater. Updates are manual, by running a newer installer.
- The credential is per Windows account. Installing under one account and
  running under another does not work, and says so.
- The interface calls the API at a **relative** base (`/api/v1`), because the
  backend serves both from one origin. Anything that reintroduces an absolute
  base bakes a host and port into the bundle, and port 8000 on these machines
  belongs to a different application — it would reach the wrong server rather
  than fail. `test_the_packaged_interface_calls_the_api_on_its_own_origin`
  guards this.

---

## 12. Release verification

### Verified build

| | |
|---|---|
| Version | 1.0.0 |
| Installer | `RisingPathRecruitersJobFindingAutomationSetup.exe` (66.3 MB) |
| Installer SHA-256 | `117C30EB0D8E9212147ECEC3AEC16B0074E6F2A33C4486C741A1C17954FAF90B` |
| Launcher SHA-256 | `7075E272EA4318E88290F416FBD68B77CB8ED99533E44BADD9DC8100C98E8575` |
| Interface bundle | `assets/index-DnP9hlWI.js` |

The value above is the one in `SHA256.txt` shipped beside the installer. It is
also what `Get-FileHash` returns for the file that was actually installed, so a
recipient can confirm they have the tested artifact and not a rebuild.

### Accepted end to end

The rebuilt installer was run over an existing installation on a real machine
and confirmed by the project owner:

- Installation completed successfully.
- The existing database and all data survived the upgrade.
- The application launched from the installed shortcut.
- The browser opened at `127.0.0.1:8100`.
- Resume Management loaded.
- The earlier "Cannot reach the service" failure is gone.
- The installed copy uses the corrected interface/API configuration.

This is the acceptance criterion for the phase: not that the frozen launcher
starts from a build directory, but that the **installed** program works on a
machine that was upgraded rather than freshly provisioned.

### Automated results at that build

| Suite | Result |
|---|---|
| `tests/test_desktop_packaging.py` | 37 passed |
| Full backend suite | 2070 passed, 45 failed |

The 45 failures pre-date this phase and are unrelated to it: they are
data-dependent tests asserting whole-table counts are zero (`count(SearchRun)
== 0` and similar), run here against a database holding real searches. The
count is unchanged from the baseline measured at `HEAD` in a clean worktree, so
this phase introduced no regressions. They will pass against an empty database
and should be given their own fixtures rather than being left to depend on
whatever the developer's database happens to contain.

### Verified by hand on the frozen build

Launched detached, with no inherited standard handles — the condition a
shortcut creates, and the one that a shell with piped output hides:

| Check | Result |
|---|---|
| `--version` | Names the application, exit 0 |
| `--check` | Config, credential, database (18 tables), data dir, interface all OK, exit 0 |
| Startup | `backend healthy on 8100`, no errors in `launcher.log` |
| API | `GET /api/v1/health` → 200 |
| Interface | Shell, deep links and assets served; all five routes render |
| API from the browser | 27 calls across all pages, every one 200, no console or page errors |
| Second launch | "another instance is already running" |
| `--shutdown` | Exits cleanly, port 8100 released |

### Two failures this phase found the hard way

Both were invisible to the tests that existed, and both were caused by the way
the packaged build differs from development. They are recorded because the same
blind spot will produce the same class of bug again.

**No console.** A windowed frozen build inherits no standard handles, so
`sys.stdout` is `None`. Code that logged during import reached structlog before
it was configured, and its default `PrintLogger` took a weak reference to that
`None`. Verifying the executable *from a shell with piped output* hid this
completely, because a GUI-subsystem process inherits those pipes — the harness
supplied the one thing the real launch path lacks.

**No absolute API base.** `new URL()` given a single relative string throws, so
every API call failed before a request was sent — and because the URL was built
inside the `try` guarding `fetch`, the interface reported a healthy backend as
unreachable. Development never saw it: `.env.local` uses an absolute base.

The lesson in both is the same. Verify the packaged artifact the way a user
starts it, and be suspicious of a harness that makes a test pass by being
gentler than reality.
