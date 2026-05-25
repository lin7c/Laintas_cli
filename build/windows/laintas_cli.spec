# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for laintas_cli.exe
# Build: pyinstaller build/windows/laintas_cli.spec

a = Analysis(
    ['../../laintas_cli.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('../../agent_loop.py', '.'),
    ],
    hiddenimports=[
        'requests',
        'rich',
        'rich.console',
        'rich.panel',
        'rich.markdown',
        'rich.table',
        'rich.live',
        'rich.spinner',
        'rich.text',
        'rich.padding',
        'prompt_toolkit',
        'prompt_toolkit.application',
        'prompt_toolkit.history',
        'prompt_toolkit.completion',
        'prompt_toolkit.key_binding',
        'prompt_toolkit.layout',
        'prompt_toolkit.styles',
        'prompt_toolkit.auto_suggest',
        'json',
        'shlex',
        'subprocess',
        'platform',
        'socket',
        'urllib',
        'pathlib',
        'datetime',
        'uuid',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
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
    name='laintas_cli',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,           # CLI tool — needs console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
