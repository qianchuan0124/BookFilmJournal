# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

PROJECT_ROOT = Path(SPECPATH).resolve().parent
PACKAGING_DIR = PROJECT_ROOT / 'packaging'

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from app.config import APP_VERSION  # noqa: E402

qf_datas, qf_binaries, qf_hiddenimports = collect_all('qfluentwidgets')

hiddenimports = qf_hiddenimports + [
    'bs4',
    'openpyxl',
    'PIL',
    'requests',
    'PySide6.QtNetwork',
    'PySide6.QtSvg',
    'PySide6.QtWebEngineCore',
    'PySide6.QtWebEngineWidgets',
]

excludes = ['tkinter', 'pytest', 'unittest']

a = Analysis(
    [str(PROJECT_ROOT / 'main.py')],
    pathex=[str(PROJECT_ROOT)],
    binaries=qf_binaries,
    datas=qf_datas + [(str(PROJECT_ROOT / 'config'), 'config')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe_icon = str(PACKAGING_DIR / 'icon.ico') if sys.platform == 'win32' else None
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ShuyingJilu',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=exe_icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ShuyingJilu',
)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='书影记录.app',
        icon=str(PACKAGING_DIR / 'icon.icns') if (PACKAGING_DIR / 'icon.icns').exists() else None,
        bundle_identifier='com.shuyingjilu.app',
        version=APP_VERSION,
        info_plist={
            'CFBundleDisplayName': '书影记录',
            'CFBundleName': '书影记录',
            'CFBundleIdentifier': 'com.shuyingjilu.app',
            'NSHighResolutionCapable': True,
        },
    )
