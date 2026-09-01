# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for laintas-cli (Linux)
# Build from project root:
#   pyinstaller build/linux/laintas_cli.spec
#
# datas/hiddenimports are derived from package_manifest.json (single source of
# truth). See build/HEADLESS_BROWSER_PACKAGING.md for the per-platform strategy.

import json
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

_PROJECT_DIR = os.path.abspath(os.path.join(SPECPATH, '..', '..'))
with open(os.path.join(_PROJECT_DIR, 'package_manifest.json'), encoding='utf-8') as _f:
    _PM = json.load(_f)

# Top-level .py modules → datas (shipped as data so PyInstaller includes them
# both as importable modules and as on-disk files for the self-updater).
_datas = [(os.path.join(_PROJECT_DIR, m + '.py'), '.') for m in _PM['modules']]
_datas.append((os.path.join(_PROJECT_DIR, 'LICENSE'), '.'))

# Sub-packages → datas (whole directory, preserving structure).
for _pkg in _PM['packages'] + _PM['data_dirs']:
    _datas.append((os.path.join(_PROJECT_DIR, _pkg), _pkg))

_datas += collect_data_files('certifi')

# hiddenimports: all top-level modules + sub-packages + frozen_deps (linux).
_hidden = list(_PM['modules']) + list(_PM['packages']) + list(_PM['frozen_deps']['linux'])
# Collect every submodule of the heavy deps so PyInstaller doesn't miss any.
for _dep in ('websockets', 'aiortc', 'av', 'cffi'):
    try:
        _hidden += collect_submodules(_dep)
    except Exception:
        pass
# Standard lib modules that are imported lazily.
_hidden += ['json', 'shlex', 'subprocess', 'platform', 'socket', 'urllib',
            'pathlib', 'datetime', 'uuid']

a = Analysis(
    ['../../laintas_cli.py'],
    pathex=['../..'],
    binaries=[],
    datas=_datas,
    hiddenimports=_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(_PROJECT_DIR, 'build', 'linux', 'hook_ssl.py')],
    excludes=[
        'tkinter',
        'matplotlib',
        'numpy',
        'pandas',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='laintas-cli',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
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
