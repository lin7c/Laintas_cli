import json
import os

from setuptools import setup

_HERE = os.path.dirname(os.path.abspath(__file__))

# Single source of truth — package_manifest.json drives setup.py, both
# PyInstaller specs, and build_download_assets.sh. Edit the JSON, not here.
with open(os.path.join(_HERE, "package_manifest.json"), encoding="utf-8") as _f:
    _pm = json.load(_f)

# version.py is the runtime version source (read by updater.py / `/v` too).
_version = "0.0.0"
try:
    _vns = {}
    with open(os.path.join(_HERE, "version.py")) as _vf:
        exec(_vf.read(), _vns)
    _version = _vns.get("__version__", _version)
except Exception:
    pass

setup(
    name="laintas-cli",
    version=_version,
    description="Laintas CLI - Autonomous AI agent for your terminal",
    author="湖北林塔斯科技有限公司",
    license="FSL-1.1-MIT",
    license_files=("LICENSE",),
    url="https://github.com/lin7c/Laintas_cli",
    packages=_pm["packages"],
    py_modules=_pm["modules"],
    package_data=_pm["package_data"],
    include_package_data=True,
    install_requires=_pm["core_requires"],
    extras_require=_pm["extras_require"],
    entry_points={
        "console_scripts": [
            "laintas-cli=laintas_cli:main",
        ],
    },
    python_requires=">=3.10",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "License :: Other/Proprietary License",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Terminals",
    ],
)
