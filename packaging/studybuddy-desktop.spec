# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the studybuddy desktop build.

    pyinstaller packaging/studybuddy-desktop.spec --noconfirm

Produces ``dist/StudyBuddy/StudyBuddy.exe`` on Windows and
``dist/StudyBuddy.app`` on macOS.

Onedir rather than onefile on purpose: onefile unpacks itself into ``%TEMP%`` on
every start, which is both slower and a common source of "Could not create
temporary directory" failures on locked-down machines. Onedir ships the runtime
next to the exe and starts immediately.

The window backend is the browser app mode, so no pywebview/pythonnet bundle is
needed; users who want a native window can still
``pip install "studybuddy[desktop]"`` and run the console script.
"""

import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH is injected by PyInstaller
SRC = ROOT / "src"
WEBUI = SRC / "studybuddy" / "webui"

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "desktop_entry.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[(str(WEBUI), "studybuddy/webui"),
           (str(ROOT / "tests" / "fixtures" / "zh_sample.pdf"), "studybuddy")],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc_data"],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="StudyBuddy",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="StudyBuddy",
)

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name="StudyBuddy.app",
        icon=None,
        bundle_identifier="io.github.ray144165154.studybuddy",
        info_plist={
            "CFBundleShortVersionString": "0.8.5",
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
        },
    )
