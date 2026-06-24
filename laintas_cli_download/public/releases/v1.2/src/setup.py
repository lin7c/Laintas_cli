import os

from setuptools import setup

# Single source of truth — read version.py directly so the packaged version
# always matches what `/v` reports at runtime.
_version = "0.0.0"
try:
    _vns = {}
    with open(os.path.join(os.path.dirname(__file__), "version.py")) as _vf:
        exec(_vf.read(), _vns)
    _version = _vns.get("__version__", _version)
except Exception:
    pass

setup(
    name="laintas-cli",
    version=_version,
    description="Laintas CLI - Autonomous AI agent for your terminal",
    author="Laintas",
    url="https://github.com/lin7c/laintas_cli_pre",
    py_modules=[
        "laintas_cli",
        "version",
        "updater",
        "agent_loop",
        "tools",
        "skills",
        "mcp_client",
        "policy",
        "memory_system",
        "hooks",
        "plan_mode",
        "task_manager",
        "agent_persistence",
        "agent_roles",
        "workflow_engine",
        "paths",
        "migrate",
        "cloud_provider",
        "hwo_runner",
        "hwo_ui",
    ],
    install_requires=[
        "requests>=2.28.0",
        "certifi>=2024.0.0",
        "rich>=13.0.0",
        "prompt_toolkit>=3.0.0",
    ],
    entry_points={
        "console_scripts": [
            "laintas-cli=laintas_cli:main",
        ],
    },
    python_requires=">=3.10",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: POSIX :: Linux",
        "Operating System :: Microsoft :: Windows",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Terminals",
    ],
)
