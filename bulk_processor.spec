# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Stomp Pad Generator bulk processor GUI.
# Single spec produces a windowed binary on Windows/Linux and a .app bundle on macOS.

import sys

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# The geometry pipeline lives in the ``stomppad`` package (Phase 0 of v2);
# the top-level ``pyramid_position_calculator`` is a thin re-export shim.
# ``collect_submodules`` walks the package so Phase 1's split modules
# (geometry, packing, patterns, project) are picked up without re-touching
# this spec.
stomppad_hiddenimports = collect_submodules('stomppad')

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
        # Phase 2.3 Edit tab. The import is lazy + try-except guarded in
        # bulk_processor_gui.setup_edit_tab, so PyInstaller's static
        # analyzer doesn't pick it up — declare it explicitly.
        'desktop_editor',
        # Back-compat shim for legacy callers (user scripts, frozen pre-v2
        # binaries). collect_submodules('stomppad') doesn't pick it up
        # since it lives at the repo root, not inside the package.
        'pyramid_position_calculator',
        *stomppad_hiddenimports,
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
