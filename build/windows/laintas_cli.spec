# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for laintas_cli.exe
# Build: pyinstaller build/windows/laintas_cli.spec

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

# PyInstaller executes spec files without a __file__; it injects SPECPATH
# (the directory containing this spec) into the namespace instead.
PROJECT_DIR = Path(SPECPATH).resolve().parents[1]

a = Analysis(
    [str(PROJECT_DIR / 'laintas_cli.py')],
    pathex=[],
    binaries=[],
    datas=[
        (str(PROJECT_DIR / 'agent_loop.py'), '.'),
        (str(PROJECT_DIR / 'tools.py'), '.'),
        (str(PROJECT_DIR / 'skills.py'), '.'),
        (str(PROJECT_DIR / 'mcp_client.py'), '.'),
        (str(PROJECT_DIR / 'policy.py'), '.'),
        (str(PROJECT_DIR / 'memory_system.py'), '.'),
        (str(PROJECT_DIR / 'hooks.py'), '.'),
        (str(PROJECT_DIR / 'plan_mode.py'), '.'),
        (str(PROJECT_DIR / 'task_manager.py'), '.'),
        (str(PROJECT_DIR / 'agent_persistence.py'), '.'),
        (str(PROJECT_DIR / 'agent_roles.py'), '.'),
        (str(PROJECT_DIR / 'workflow_engine.py'), '.'),
        (str(PROJECT_DIR / 'paths.py'), '.'),
        (str(PROJECT_DIR / 'migrate.py'), '.'),
        (str(PROJECT_DIR / 'cloud_provider.py'), '.'),
        (str(PROJECT_DIR / 'hwo_runner.py'), '.'),
        (str(PROJECT_DIR / 'hwo_ui.py'), '.'),
    ] + collect_data_files('certifi'),
    hiddenimports=[
        'requests',
        'certifi',
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
    runtime_hooks=[str(PROJECT_DIR / 'build' / 'windows' / 'hook_ssl.py')],
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
    icon=str(PROJECT_DIR / 'build' / 'windows' / 'icon.ico'),
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
