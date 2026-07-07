# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the standalone macOS build (GUI), one-file binary.
# Build: pyinstaller dualrip_macos.spec  ->  dist/DualRip
#
# This produces a plain Mach-O executable (not a .app bundle) to match the
# other platform builds; run it from Terminal or double-click it in Finder.
# No .icns is bundled (we only ship icon.ico, which BUNDLE()/macOS icons
# can't use directly) -- icon.ico is still bundled as data purely for the
# in-app window icon (see dualrip/gui/app.py:_icon_path). Unsigned: first
# launch needs "right-click > Open" to clear Gatekeeper.

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
        'PySide6.QtPdf', 'PySide6.QtCharts', 'PySide6.QtMultimedia',
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
