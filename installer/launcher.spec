# PyInstaller spec for the application the recruiter runs.
#
# One directory, not one file. A one-file build re-extracts the whole bundle to
# a temporary folder on every launch, which for an application this size means
# several seconds of nothing happening after a double-click — the exact moment
# a user decides the program is broken and clicks again.
#
# Windowed, so there is no console. Every failure the launcher can anticipate
# becomes a message box; everything else goes to the rolling log.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

REPO = Path(SPECPATH).resolve().parent

# Bundled read-only resources. `app/core/paths.py` reads these from the bundle
# root and never writes to them.
datas = [
    # The compiled interface, served by the API itself so no Node is needed.
    (str(REPO / "frontend" / "dist"), "frontend_dist"),
    # Alembic needs its scripts at run time: provisioning applies migrations.
    (str(REPO / "migrations"), "migrations"),
    (str(REPO / "alembic.ini"), "."),
]

hiddenimports = [
    # Imported by name through the registry rather than statically, so the
    # analysis cannot see them.
    *collect_submodules("app.portals.adapters"),
    # Alembic loads migration modules dynamically.
    *collect_submodules("alembic"),
    # `migrations/env.py` is loaded by Alembic at run time, so the analysis
    # never sees its imports. Every one of them has to be named here or the
    # frozen build fails at "alembic upgrade" with a bare ModuleNotFoundError.
    "logging.config",
    "logging.handlers",
    "app.models",
    "app.core.config",
    # Chosen by SQLAlchemy from the URL string.
    "asyncpg",
    "sqlalchemy.dialects.postgresql.asyncpg",
    # uvicorn resolves these from configuration strings.
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

analysis = Analysis(
    [str(REPO / "desktop" / "launcher.py")],
    pathex=[str(REPO)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Test-only and build-only machinery. Excluded so the shipped artifact
    # contains nothing a user could run against their own data by accident.
    excludes=["pytest", "_pytest", "tkinter", "PyInstaller", "playwright"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="RisingPathRecruitersJobFindingAutomation",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="RisingPathRecruitersJobFindingAutomation",
)
