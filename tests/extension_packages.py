"""Import an official extension's modules the way the host does.

An extension is loaded from its own directory as a package, not from
`sys.path` — `extension_runtime.load` passes `submodule_search_locations`, so
`main.py` and its siblings import each other relatively and never collide with
a module of the same name in the CLI. A test that reaches into one has to do
the same thing, or it is testing a copy that only exists in the test.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]

_packages: dict[str, ModuleType] = {}


def extension_package(name: str) -> ModuleType:
    """Load `extensions/<name>/main.py` as a package, once per process."""
    package = _packages.get(name)
    if package is not None:
        return package
    directory = ROOT / "extensions" / name
    module_name = f"laintas_test_extension_{name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(
        module_name, directory / "main.py",
        submodule_search_locations=[str(directory)])
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = package
    spec.loader.exec_module(package)
    _packages[name] = package
    return package


def extension_module(name: str, module: str) -> ModuleType:
    """One module inside an extension, e.g. `extension_module("canvas", "canvas")`."""
    package = extension_package(name)
    return importlib.import_module(f"{package.__name__}.{module}")


class RecordingConsole:
    """A console an extension can print to, and a test can read back."""

    def __init__(self):
        self.lines: list[str] = []

    def print(self, *args, **_kwargs):
        self.lines.append(" ".join(str(a) for a in args))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def bind_context(package: ModuleType, cwd: str = ".", name: str = ""):
    """Give a loaded extension the host context it would get at setup().

    Without it `_ctx` is None and every console line raises — the extension is
    written to take its console from the host rather than import the CLI's
    global, which is exactly what makes this substitutable in a test.
    """
    from types import SimpleNamespace

    console = RecordingConsole()
    package._ctx = SimpleNamespace(
        name=name or getattr(package, "__name__", "extension"),
        console=console, cwd=cwd, directory=None)
    return package._ctx
