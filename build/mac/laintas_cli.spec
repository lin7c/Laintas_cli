# -*- mode: python ; coding: utf-8 -*-
"""Native macOS terminal binary. Run from the repository root on a Mac."""

import json
import os
from PyInstaller.utils.hooks import collect_data_files

_ROOT = os.path.abspath(os.path.join(SPECPATH, '..', '..'))
with open(os.path.join(_ROOT, 'package_manifest.json'), encoding='utf-8') as _file:
    _MANIFEST = json.load(_file)

_datas = [(os.path.join(_ROOT, name + '.py'), '.')
          for name in _MANIFEST['modules']]
_datas.append((os.path.join(_ROOT, 'LICENSE'), '.'))
for _name in _MANIFEST['packages'] + _MANIFEST['data_dirs']:
    _datas.append((os.path.join(_ROOT, _name), _name))
_datas += collect_data_files('certifi')

_hidden = list(_MANIFEST['modules']) + list(_MANIFEST['packages'])
_hidden += ['json', 'shlex', 'subprocess', 'platform', 'socket', 'urllib',
            'pathlib', 'datetime', 'uuid']

analysis = Analysis(
    [os.path.join(_ROOT, 'laintas_cli.py')],
    pathex=[_ROOT],
    binaries=[],
    datas=_datas,
    hiddenimports=_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(_ROOT, 'build', 'linux', 'hook_ssl.py')],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas'],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, analysis.binaries, analysis.datas, [],
    name='laintas-cli',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    target_arch=None,
    codesign_identity=None,
)
