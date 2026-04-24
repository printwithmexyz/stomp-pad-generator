# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Stomp Pad Generator bulk processor GUI.
# Single spec produces a windowed binary on Windows/Linux and a .app bundle on macOS.

import sys

block_cipher = None

a = Analysis(
    ['bulk_processor_gui.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'shapely.geometry',
        'shapely.ops',
        'shapely.affinity',
        'skimage.morphology',
        'scipy.spatial',
        'PIL.Image',
        'PIL.ImageTk',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='StompPadGenerator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if sys.platform == 'darwin':
    app = BUNDLE(
        exe,
        name='StompPadGenerator.app',
        icon=None,
        bundle_identifier='com.benkahan.stomppadgenerator',
    )
