# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the standalone Linux build (GUI), one-file binary.
# Build: pyinstaller dualrip_linux.spec  ->  dist/DualRip
#
# Runtime deps this build does NOT bundle (install via your distro's package
# manager before running the result): libportaudio2 (sounddevice) and the Qt
# xcb platform plugin's system libraries (libegl1, libxkbcommon0,
# libxcb-cursor0 and friends -- see .github/workflows for the exact list used
# in CI). ELF binaries carry no embedded icon; icon.ico is bundled as data
# purely for the in-app window icon (see dualrip/gui/app.py:_icon_path).

a = Analysis(
    ['DualRip.py'],
    pathex=[],
    binaries=[],
    datas=[('icon.ico', '.')],
    hiddenimports=['sounddevice'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # heavy Qt modules the app does not use
        'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
        'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuick3D',
        'PySide6.QtPdf', 'PySide6.QtCharts',
        'PySide6.QtDesigner', 'PySide6.QtTest',
        'tkinter', 'matplotlib', 'scipy', 'PIL',
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='DualRip',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
