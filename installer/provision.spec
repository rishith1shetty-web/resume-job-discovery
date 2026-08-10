# PyInstaller spec for the database provisioning tool.
#
# One FILE, unlike the launcher. The installer has to validate the postgres
# password on the page where it was typed — before a single file has been
# copied to the computer — which means running this from Inno Setup's
# temporary directory with `ExtractTemporaryFile`. That only works for a
# self-contained executable.
#
# Console build: Inno Setup captures its exit code, and the output is the
# installation log. It is never seen by the user, who sees the wizard.

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

REPO = Path(SPECPATH).resolve().parent

datas = [
    # The whole point of this tool: applying migrations needs the scripts.
    (str(REPO / "migrations"), "migrations"),
    (str(REPO / "alembic.ini"), "."),
]

hiddenimports = [
    # Seeding registers every adapter to derive portal capabilities.
    *collect_submodules("app.portals.adapters"),
    *collect_submodules("alembic"),
    # `migrations/env.py` is loaded by Alembic at run time, so the analysis
    # never sees its imports. Every one of them has to be named here or the
    # frozen build fails at "alembic upgrade" with a bare ModuleNotFoundError.
    "logging.config",
    "logging.handlers",
    "app.models",
    "app.core.config",
    "asyncpg",
    "sqlalchemy.dialects.postgresql.asyncpg",
]

analysis = Analysis(
    [str(REPO / "installer" / "provision.py")],
    pathex=[str(REPO)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "_pytest", "tkinter", "PyInstaller", "playwright", "uvicorn"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="provision-database",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
