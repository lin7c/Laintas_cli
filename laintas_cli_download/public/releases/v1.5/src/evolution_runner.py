"""Reliability-oriented baseline/candidate runner for Evolution Lab."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional


def _safe_relative(value: str) -> bool:
    path = Path(value or "")
    return bool(value and not path.is_absolute() and ".." not in path.parts)


def static_check(candidate: dict) -> tuple[bool, list[str]]:
    errors: list[str] = []
    files = candidate.get("files")
    if not isinstance(files, list) or not files:
        return False, ["candidate has no files"]
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or not _safe_relative(str(item.get("path") or "")):
            errors.append("candidate contains an invalid relative path")
            continue
        relative = str(item["path"])
        if relative in seen:
            errors.append(f"duplicate candidate path: {relative}")
        seen.add(relative)
        content = item.get("content")
        if not isinstance(content, str):
            errors.append(f"{relative}: content must be text")
            continue
        if relative.endswith(".py"):
            try:
                ast.parse(content, filename=relative)
            except SyntaxError as exc:
                errors.append(f"{relative}:{exc.lineno}: {exc.msg}")
        if relative == "extension.json":
            try:
                manifest = json.loads(content)
                if (not isinstance(manifest, dict)
                        or manifest.get("schemaVersion") != 1
                        or manifest.get("name") != candidate.get("name")
                        or manifest.get("entrypoint", "main.py") != "main.py"):
                    errors.append("extension.json fields do not match candidate")
            except ValueError as exc:
                errors.append(f"extension.json: {exc}")
    target_type = candidate.get("target_type")
    if target_type == "extension":
        for required in ("extension.json", "main.py"):
            if required not in seen:
                errors.append(f"extension candidate is missing {required}")
    elif target_type in ("commands", "loop"):
        expected = f"{target_type}.py"
        if seen != {expected}:
            errors.append(f"{target_type} candidate must contain only {expected}")
    else:
        errors.append(f"unknown target type: {target_type}")
    return not errors, errors


def _materialize(candidate: dict, root: Path) -> Path:
    directory = root / str(candidate.get("name") or candidate.get("target_type"))
    directory.mkdir(parents=True, exist_ok=True)
    for item in candidate.get("files") or []:
        destination = directory / item["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(item["content"], encoding="utf-8")
    return directory


_HARNESS = r'''
import importlib.util, json, pathlib, sys
kind, root = sys.argv[1], pathlib.Path(sys.argv[2])
class Reg:
    def __init__(self, fn): self.fn=fn
    def register(self, *args, **kwargs): return self.fn(*args, **kwargs)
class Fake:
    def __init__(self):
        self.registered_commands=[]; self.registered_tools=[]; self.registered_loops=[]
        self.commands=Reg(self.register_command); self.tools=Reg(self.register_tool); self.loop=Reg(self.register_loop)
    def register_command(self, name, fn): self.registered_commands.append(name)
    def register_tool(self, tool): self.registered_tools.append(getattr(tool, "name", "?"))
    def register_loop(self, fn): self.registered_loops.append(getattr(fn, "__name__", "handler"))
if kind == "extension":
    target=root/"main.py"; required="setup"
else:
    target=root/(kind+".py"); required=("handle_extra_command" if kind=="commands" else "handle_loop_command")
spec=importlib.util.spec_from_file_location("evolution_candidate", target, submodule_search_locations=[str(root)])
sys.modules["evolution_candidate"]=importlib.util.module_from_spec(spec)
module=sys.modules["evolution_candidate"]; spec.loader.exec_module(module)
fn=getattr(module, required, None)
if not callable(fn): raise RuntimeError("missing callable "+required)
result={"callable": required}
if kind == "extension":
    fake=Fake(); fake.console=None; fake.backend=None; fake.cwd=str(root)
    fn(fake); result.update(commands=fake.registered_commands, tools=fake.registered_tools, loops=fake.registered_loops)
print(json.dumps(result))
'''


def _child_env() -> dict:
    env = {
        key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL")
        if key in os.environ
    }
    # Product modules such as tools.Tool remain importable to candidate code.
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent)
    env["LAINTAS_EVOLUTION_TEST"] = "1"
    return env


def _run_directory(kind: str, directory: Path, timeout: float = 15.0) -> dict:
    env = _child_env()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _HARNESS, kind, str(directory)],
            capture_output=True, text=True, timeout=timeout, env=env,
            cwd=str(directory),
        )
        payload: Any = None
        if proc.returncode == 0:
            try:
                payload = json.loads(proc.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                payload = {"output": proc.stdout[-2000:]}
        return {
            "passed": proc.returncode == 0,
            "returncode": proc.returncode,
            "result": payload,
            "output": (proc.stdout + proc.stderr)[-4000:],
        }
    except subprocess.TimeoutExpired:
        return {"passed": False, "returncode": -1, "output": "test timed out"}


def _run_test_file(directory: Path, timeout: float = 30.0) -> Optional[dict]:
    test_file = directory / "tests.py"
    if not test_file.is_file():
        return None
    try:
        proc = subprocess.run(
            [sys.executable, str(test_file)], capture_output=True, text=True,
            timeout=timeout, env=_child_env(), cwd=str(directory),
        )
        return {
            "passed": proc.returncode == 0, "returncode": proc.returncode,
            "output": (proc.stdout + proc.stderr)[-8000:],
        }
    except subprocess.TimeoutExpired:
        return {"passed": False, "returncode": -1, "output": "tests.py timed out"}


def run_candidate(candidate: dict, baseline_path: Path | None = None) -> dict:
    static_ok, errors = static_check(candidate)
    report = {"static_passed": static_ok, "static_errors": errors}
    if not static_ok:
        report.update(passed=False, baseline=None, candidate=None)
        return report
    with tempfile.TemporaryDirectory(prefix="laintas-evolve-") as tmp:
        temp_root = Path(tmp)
        candidate_dir = _materialize(candidate, temp_root / "candidate")
        candidate_run = _run_directory(str(candidate["target_type"]), candidate_dir)
        candidate_tests = _run_test_file(candidate_dir)
        candidate_run["tests"] = candidate_tests
        if candidate_tests is not None:
            candidate_run["passed"] = bool(
                candidate_run.get("passed") and candidate_tests.get("passed"))
        baseline_run = None
        if baseline_path and baseline_path.exists():
            baseline_dir = temp_root / "baseline" / candidate_dir.name
            if baseline_path.is_dir():
                __import__("shutil").copytree(baseline_path, baseline_dir)
            else:
                baseline_dir.mkdir(parents=True)
                __import__("shutil").copy2(baseline_path, baseline_dir / baseline_path.name)
            candidate_test_file = candidate_dir / "tests.py"
            if candidate_test_file.is_file():
                __import__("shutil").copy2(candidate_test_file, baseline_dir / "tests.py")
            baseline_run = _run_directory(str(candidate["target_type"]), baseline_dir)
            baseline_tests = _run_test_file(baseline_dir)
            baseline_run["tests"] = baseline_tests
            if baseline_tests is not None:
                baseline_run["passed"] = bool(
                    baseline_run.get("passed") and baseline_tests.get("passed"))
        report.update(
            baseline=baseline_run,
            candidate=candidate_run,
            passed=bool(candidate_run.get("passed")),
        )
        return report
