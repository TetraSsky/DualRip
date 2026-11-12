# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the standalone DualRip CLI (no Qt, no audio device).

a = Analysis(
    ['DualRipcli.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # the CLI never imports the GUI: keep Qt and PortAudio out of the bundle
        'PySide6', 'sounddevice', '_sounddevice_data', 'shiboken6',
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
    name='DualRip-cli',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
