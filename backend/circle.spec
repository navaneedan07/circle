# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Circle desktop app.

Produces one executable that carries the API, the built interface and the
local-model plumbing. There is no install step, no server to start and no
database to provision: the app creates its own data folder on first run.

    .venv/Scripts/python -m PyInstaller circle.spec --noconfirm
"""
from pathlib import Path

BACKEND = Path(SPECPATH).resolve()
FRONTEND_DIST = BACKEND.parent / "frontend" / "dist"

# Collected at build time rather than discovered: import hooks miss these, and
# a build that silently omits them produces an app that fails on first use
# with no clue why.
hidden_imports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "sqlite3",
    "numpy",
    "pydantic",
    "email_validator",
    "multipart",
    "httpx",
    "jinja2",
]

# Local model libraries are large but are what makes answers work offline.
excludes = [
    "tkinter", "matplotlib", "pytest", "IPython", "notebook",
    "torch", "tensorflow", "playwright", "selenium",
]

a = Analysis(
    ["desktop_entry.py"],
    pathex=[str(BACKEND)],
    binaries=[],
    datas=[(str(FRONTEND_DIST), "frontend")] if FRONTEND_DIST.is_dir() else [],
    hiddenimports=hidden_imports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Circle",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,          # a console is how the user sees a startup failure
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)