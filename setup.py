from setuptools import setup, find_packages

setup(
    name="laintas-cli",
    version="0.1.0",
    description="Laintas CLI - Autonomous AI agent for your terminal",
    author="Laintas",
    py_modules=["laintas_cli"],
    install_requires=[
        "requests>=2.28.0",
        "rich>=13.0.0",
    ],
    entry_points={
        "console_scripts": [
            "laintas-cli=laintas_cli:main",
        ],
    },
    python_requires=">=3.10",
)
