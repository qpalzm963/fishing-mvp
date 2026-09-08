# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir spec for the existing fishing-mvp CLI.

The Windows build script supplies the Python environment and the output
directory.  Keep this file focused on Python application contents; the
official scrcpy/ADB distribution is added by build_windows.ps1.
"""

from pathlib import Path
import os

from PyInstaller.building.build_main import Analysis, COLLECT, EXE, PYZ
from PyInstaller.utils.hooks import collect_all


PROJECT_ROOT = Path(SPECPATH).resolve().parent
SOURCE_ROOT = PROJECT_ROOT / "src"
CONFIG_FILE = PROJECT_ROOT / "config" / "default.yaml"
PACKAGE_DEFAULT_FILE = SOURCE_ROOT / "fishing_mvp" / "defaults" / "default.yaml"

for required_file in (CONFIG_FILE, PACKAGE_DEFAULT_FILE):
    if not required_file.is_file():
        raise FileNotFoundError(f"Default config was not found: {required_file}")

# build_windows.ps1 supplies this hook from its isolated temporary workspace.
# Keeping the hook generated avoids adding another source file while making a
# direct frozen executable resolve the tools shipped beside it.
runtime_hook_path = os.environ.get("FISHING_MVP_RUNTIME_HOOK")
runtime_hooks = [runtime_hook_path] if runtime_hook_path else []

# PyAV is imported lazily by the scrcpy frame source, so PyInstaller cannot
# discover every decoder module from the normal import graph on its own.
# The build script installs the optional [scrcpy] extra before this spec runs.
AV_DATA_FILES, AV_BINARIES, AV_HIDDEN_IMPORTS = collect_all("av")


analysis = Analysis(
    [str(SOURCE_ROOT / "fishing_mvp" / "__main__.py")],
    pathex=[str(SOURCE_ROOT)],
    binaries=AV_BINARIES,
    datas=[
        (str(CONFIG_FILE), "config"),
        (str(PACKAGE_DEFAULT_FILE), "fishing_mvp/defaults"),
        *AV_DATA_FILES,
    ],
    hiddenimports=["av", *AV_HIDDEN_IMPORTS],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=runtime_hooks,
    excludes=[],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

# exclude_binaries keeps this as a normal onedir build.  COLLECT places the
# executable, Python support files, and bundled config under one runtime dir.
executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="FishingMVP",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="FishingMVP",
)
