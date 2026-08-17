"""Runtime for loaded extensions — Evolution Lab's, and the Enterprise
organisation layer installed by ``/v enterprise``.

Extensions are intentionally expressive local Python. They register commands,
tools and shell interceptors through a small stable context.

**The context is a convenience, not a boundary.** It hands over a console and a
narrowed inference gateway rather than the raw Laintas session, which keeps the
*common* path honest — but an extension executes in this process, with this
user's permissions, and can read `~/.laintas/session.json` for itself. The
Enterprise extension does exactly that, because it must authenticate to its own
organisation. Nothing here prevents any other extension from doing the same.

So the trust in a loaded extension comes from where it came from — code the user
wrote, or a package whose Ed25519 signature `enterprise_installer` verified —
and never from the shape of this API. Read `load()` with that in mind before
widening what may be loaded.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import paths
from tools import Tool, get_registry


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
#: An extension may claim its own tool namespace instead of the default
#: `extension.<owner>.` — kept short and lower-case because these names end up
#: in front of the model on every request.
_SAFE_TOOL_PREFIX = re.compile(r"^[a-z][a-z0-9_]{0,15}\.$")


def _positional_arity(func: Callable) -> Optional[int]:
    """Return the number of positional parameters, or None if it can't be determined."""
    try:
        sig = inspect.signature(func)
        count = 0
        for param in sig.parameters.values():
            if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
                count += 1
            elif param.kind == param.VAR_POSITIONAL:
                return None
        return count
    except (ValueError, TypeError):
        return None


def _extension_roots() -> list[Path]:
    """Where `load` looks for an extension, most specific first.

    A project-local extension shadows a machine-wide one of the same name, the
    same precedence `skills.py` gives user skills over bundled ones.
    """
    return [paths.extensions_dir(), paths.global_extensions_dir()]


def _lab_owned(manifest_path: Path) -> bool:
    """True when the Evolution Lab stamped this install as its own.

    Lab-owned extensions load through their profile (`load_active_extensions`)
    so a version pinned there is not silently replaced by whatever is on disk,
    and `/evolve disable` keeps meaning "off" across sessions.
    """
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    install = manifest.get("install") if isinstance(manifest, dict) else None
    return (isinstance(install, dict)
            and install.get("trustedBy") == "evolution-lab")


class _Registrar:
    def __init__(self, callback: Callable):
        self._callback = callback

    def register(self, *args, **kwargs):
        return self._callback(*args, **kwargs)


class BackendGateway:
    """Narrow inference facade; raw authentication never crosses this API."""

    def __init__(self, callback: Optional[Callable[..., dict]] = None):
        self._callback = callback

    def chat(self, message: str, system_prompt: str = "",
             **options: Any) -> dict:
        if self._callback is None:
            return {"error": True, "reply": "Backend gateway is not configured."}
        allowed = {
            key: value for key, value in options.items()
            if key in {"history", "messages", "lang", "tools_enabled"}
        }
        return self._callback(
            message=str(message), system_prompt=str(system_prompt), **allowed)


@dataclass
class ExtensionContext:
    name: str
    console: Any
    backend: BackendGateway
    cwd: str
    _runtime: "ExtensionRuntime"

    def register_command(self, name: str, handler: Callable, *,
                         description: str = "",
                         subcommands: Optional[list[tuple[str, str]]] = None) -> None:
        self._runtime.register_command(self.name, name, handler,
                                       description=description,
                                       subcommands=subcommands)

    def register_tool(self, tool: Tool) -> None:
        self._runtime.register_tool(self.name, tool)

    def register_loop(self, handler: Callable) -> None:
        self._runtime.register_loop(self.name, handler)

    @property
    def commands(self) -> _Registrar:
        return _Registrar(self.register_command)

    @property
    def tools(self) -> _Registrar:
        return _Registrar(self.register_tool)

    @property
    def loop(self) -> _Registrar:
        return _Registrar(self.register_loop)


@dataclass
class LoadedExtension:
    name: str
    path: Path
    module_name: str
    module: Any
    version: str


class ExtensionRuntime:
    def __init__(self):
        self._lock = threading.RLock()
        self._loaded: dict[str, LoadedExtension] = {}
        self._commands: dict[str, tuple[str, Callable]] = {}
        self._command_meta: dict[str, dict] = {}  # {"/org": {"description": str, "subcommands": [(name, desc), ...]}}
        self._loops: dict[str, list[Callable]] = {}
        self._tool_prefixes: dict[str, str] = {}
        self._console: Any = None
        self._backend = BackendGateway()
        self._reserved_commands: set[str] = set()

    def configure(self, console: Any = None,
                  backend_callback: Optional[Callable[..., dict]] = None,
                  reserved_commands: Optional[list[str]] = None) -> None:
        self._console = console
        self._backend = BackendGateway(backend_callback)
        self._reserved_commands = {
            str(name).lower() for name in (reserved_commands or [])
        }

    def register_command(self, owner: str, name: str, handler: Callable,
                         *, description: str = "",
                         subcommands: Optional[list[tuple[str, str]]] = None) -> None:
        normalized = name if str(name).startswith("/") else f"/{name}"
        if not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9_-]{0,63}", normalized):
            raise ValueError(f"invalid extension command: {name}")
        if not callable(handler):
            raise TypeError("command handler must be callable")
        if normalized.lower() in self._reserved_commands:
            raise ValueError(f"command is reserved by laintas-cli: {normalized}")
        existing = self._commands.get(normalized.lower())
        if existing and existing[0] != owner:
            raise ValueError(f"extension command already registered: {normalized}")
        self._commands[normalized.lower()] = (owner, handler, _positional_arity(handler))
        self._command_meta[normalized.lower()] = {
            "description": description,
            "subcommands": list(subcommands) if subcommands else [],
        }

    def register_tool(self, owner: str, tool: Tool) -> None:
        if not isinstance(tool, Tool):
            raise TypeError("extension tool must be a Tool")
        prefix = self._tool_prefixes.get(owner) or f"extension.{owner}."
        if not tool.name.startswith(prefix):
            tool.name = prefix + tool.name.lstrip(".")
        tool.source = f"extension:{owner}"
        tool.trust_level = "trusted-extension"
        if not get_registry().register(tool, overwrite=False):
            raise ValueError(f"tool name already registered: {tool.name}")

    def register_loop(self, owner: str, handler: Callable) -> None:
        if not callable(handler):
            raise TypeError("loop handler must be callable")
        self._loops.setdefault(owner, []).append(handler)

    def _manifest(self, directory: Path) -> dict:
        path = directory / "extension.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"invalid extension manifest: {exc}") from exc
        name = value.get("name") if isinstance(value, dict) else None
        if (not isinstance(value, dict)
                or value.get("schemaVersion") not in (1, 2)
                or not isinstance(name, str) or not _SAFE_NAME.fullmatch(name)
                or value.get("entrypoint", "main.py") != "main.py"):
            raise ValueError("extension manifest fields are invalid")
        if name != directory.name:
            raise ValueError("manifest name must match extension directory")
        prefix = value.get("toolPrefix")
        if prefix is not None and (not isinstance(prefix, str)
                                   or not _SAFE_TOOL_PREFIX.fullmatch(prefix)):
            raise ValueError(
                "toolPrefix must look like 'org.' — lower-case, dot-terminated")
        return value

    def load(self, name: str) -> tuple[bool, str]:
        if not _SAFE_NAME.fullmatch(name or ""):
            return False, "invalid extension name"
        with self._lock:
            if name in self._loaded:
                self.unload(name)
            candidates = [root / name for root in _extension_roots()]
            directory = next(
                (item for item in candidates if (item / "extension.json").is_file()),
                candidates[0])
            try:
                manifest = self._manifest(directory)
                self._tool_prefixes[name] = str(manifest.get("toolPrefix") or "")
                entrypoint = directory / "main.py"
                if not entrypoint.is_file() or entrypoint.is_symlink():
                    raise ValueError("missing main.py entrypoint")

                # ── Trust gate ────────────────────────────────────────
                # Extensions with a signed or lab-verified provenance skip
                # the hash check.  Legacy extensions (no "install" block)
                # are allowed through for backward compatibility.
                install_meta = manifest.get("install") or {}
                trusted_by = str(install_meta.get("trustedBy") or "")
                if install_meta and trusted_by not in ("ed25519", "evolution-lab"):
                    import trust_store as _ts
                    related = tuple(
                        p for p in sorted(directory.rglob("*.py"))
                        if p.is_file() and not p.is_symlink()
                        and p.name != "main.py"
                    )
                    status = _ts.extension_status(
                        "runtime", name, entrypoint,
                        related_paths=related)
                    if not status.get("trusted"):
                        raise ValueError(
                            f"extension {name} is not trusted "
                            f"({status.get('reason', 'unknown')}). "
                            f"Run /extensions trust {name} to approve it.")

                safe_module_name = name.replace("-", "_")
                module_name = f"laintas_extension_{safe_module_name}_{id(self)}"
                spec = importlib.util.spec_from_file_location(
                    module_name, entrypoint,
                    submodule_search_locations=[str(directory)])
                if spec is None or spec.loader is None:
                    raise ValueError("could not create module loader")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                setup = getattr(module, "setup", None)
                if not callable(setup):
                    raise ValueError("extension must define setup(ctx)")
                ctx = ExtensionContext(
                    name, self._console, self._backend, str(Path.cwd()), self)
                setup(ctx)
                self._loaded[name] = LoadedExtension(
                    name, directory, module_name, module,
                    str(manifest.get("version") or "0.0.0"))
                return True, f"{name} {self._loaded[name].version} loaded"
            except Exception as exc:
                self._remove_registrations(name)
                module_prefix = f"laintas_extension_{name.replace('-', '_')}_"
                for module_name in list(sys.modules):
                    if module_name.startswith(module_prefix):
                        sys.modules.pop(module_name, None)
                return False, f"{name}: {type(exc).__name__}: {exc}"

    def _remove_registrations(self, name: str) -> None:
        self._commands = {
            command: item for command, item in self._commands.items()
            if item[0] != name
        }
        self._command_meta = {
            command: meta for command, meta in self._command_meta.items()
            if self._commands.get(command)  # keep only still-registered
        }
        self._loops.pop(name, None)
        self._tool_prefixes.pop(name, None)
        get_registry().unregister_source(f"extension:{name}")

    def unload(self, name: str) -> tuple[bool, str]:
        with self._lock:
            loaded = self._loaded.pop(name, None)
            self._remove_registrations(name)
            if loaded:
                teardown = getattr(loaded.module, "teardown", None)
                if callable(teardown):
                    try:
                        teardown()
                    except Exception:
                        pass
                for module_name in list(sys.modules):
                    if (module_name == loaded.module_name
                            or module_name.startswith(loaded.module_name + ".")):
                        sys.modules.pop(module_name, None)
            return True, f"{name} unloaded"

    def reload(self, name: str) -> tuple[bool, str]:
        self.unload(name)
        return self.load(name)

    def load_installed(self) -> list[tuple[str, bool, str]]:
        """Load every installed-but-not-loaded extension in both roots.

        "Installed means enabled": `/extensions install` is the opt-in, so a
        later session must not leave the extension dormant - the rule the
        organisation layer already follows.  Lab-owned extensions stay with
        their profile, and names the Lab or the organisation layer already
        loaded keep their registration and priority.  Every name still passes
        the full trust gate in `load`.
        """
        names: list[str] = []
        with self._lock:
            for root in _extension_roots():
                if not root.is_dir():
                    continue
                for directory in sorted(root.iterdir()):
                    name = directory.name
                    if (name in names or name in self._loaded
                            or not _SAFE_NAME.fullmatch(name)
                            or not (directory / "extension.json").is_file()):
                        continue
                    if _lab_owned(directory / "extension.json"):
                        continue
                    names.append(name)
        return [(name, *self.load(name)) for name in names]

    def invoke_command(self, action: str, parts: list[str], raw_line: str = "") -> tuple[bool, Any]:
        item = self._commands.get(str(action).lower())
        if item is None:
            return False, None
        handler = item[1]
        arity = item[2] if len(item) > 2 else None
        try:
            if arity == 1:
                return True, handler(parts)
            return True, handler(parts, raw_line)
        except TypeError:
            if arity is None:
                return True, handler(parts)
            raise

    def intercept_loop(self, command: str, ctx: dict) -> Optional[str]:
        for owner in list(self._loaded):
            for handler in self._loops.get(owner, []):
                result = handler(command, ctx)
                if result is not None:
                    if not isinstance(result, str):
                        raise TypeError(f"{owner} loop handler returned non-string")
                    return result
        return None

    def list(self) -> list[dict]:
        return [
            {"name": item.name, "version": item.version, "path": str(item.path)}
            for item in sorted(self._loaded.values(), key=lambda value: value.name)
        ]

    def command_names(self) -> list[str]:
        return sorted(self._commands)

    def command_description(self, name: str) -> str:
        meta = self._command_meta.get(str(name).lower())
        return meta.get("description", "") if meta else ""

    def command_subcommands(self, name: str) -> list[tuple[str, str]]:
        meta = self._command_meta.get(str(name).lower())
        if not meta:
            return []
        # Strip any third element (nested children) so callers always get
        # (name, description) pairs regardless of whether the entry has
        # sub-subcommands.
        return [(entry[0], entry[1]) for entry in meta.get("subcommands", [])]

    def command_subcommands_at(self, name: str, *path: str) -> list[tuple[str, str]]:
        """Resolve a subcommand *path* and return the subcommands at that level.

        With no *path*, returns the top-level subcommands (same as
        :meth:`command_subcommands`).  Each subcommand entry may optionally
        carry a third element -- a list of nested subcommands -- which is what
        this method descends into so that ``/org policy <TAB>`` can complete
        ``publish``.
        """
        meta = self._command_meta.get(str(name).lower())
        if not meta:
            return []
        subs = meta.get("subcommands", [])
        for segment in path:
            found = None
            for entry in subs:
                if entry[0].casefold() == segment.casefold():
                    found = entry
                    break
            if found is None:
                return []
            subs = found[2] if len(found) > 2 else []
        return [(entry[0], entry[1]) for entry in subs]


_runtime = ExtensionRuntime()


def get_runtime() -> ExtensionRuntime:
    return _runtime
