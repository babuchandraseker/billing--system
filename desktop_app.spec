# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs
import os

a = Analysis(
    ['desktop_app.py'],
    pathex=[os.path.abspath('backend')],
    binaries=[
        # uharfbuzz / freetype ship their shared libs (.pyd/.dll/.so) as package
        # data, not as normal Python binaries -- PyInstaller's static import
        # scan does not pull these in automatically. Without this, the EXE
        # either fails to import uharfbuzz/freetype or silently lacks the
        # native shaping engine, and Tamil text falls back to scrambled
        # PIL basic-layout rendering. See backend/services/tamil_text_render.py.
        *collect_dynamic_libs('uharfbuzz'),
        *collect_dynamic_libs('freetype'),
    ],
    datas=[
        ('frontend', 'frontend'),
        ('backend/templates', 'backend/templates'),
        ('backend/static', 'backend/static'),
        # Bundled Tamil fonts (Noto Sans Tamil) used by tamil_text_render.py /
        # escpos_generator.py for shaped raster printing. Must ship in the EXE
        # or Tamil printing raises "Tamil thermal printing requires a Tamil
        # font" at runtime on machines without Nirmala/Latha installed.
        ('backend/fonts', 'backend/fonts'),
    ] + collect_data_files('escpos') + collect_data_files('uharfbuzz') + collect_data_files('freetype'),
    hiddenimports=[
        'app',
        'webview', 'escpos', 'escpos.printer', 'escpos.exceptions', 'escpos.constants', 'escpos.capabilities',
        'usb', 'usb.core', 'usb.util', 'win32print', 'win32api', 'printer_routes', 'services',
        'services.printer_manager', 'services.receipt_formatter', 'services.escpos_generator',
        'services.thermal_printer', 'nutrition_engine',
        # Lazily imported inside functions (escpos_generator._generate_raster_receipt_bytes,
        # printer_routes.debug_tamil_raster) -- PyInstaller's static analysis misses these
        # without an explicit hint, leaving the old scrambled-Tamil code path as the only
        # one that actually works in the built EXE.
        'services.tamil_text_render', 'uharfbuzz', 'freetype', 'wcwidth',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DhanaDhanyaKadai',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='printer_print_22133.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DhanaDhanyaKadai',
)