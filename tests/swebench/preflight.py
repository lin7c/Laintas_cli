"""
Pre-flight validation for SWE-bench evaluation.

Checks that all prerequisites are met before running the full evaluation:
- Docker available and running
- laintas_cli authentication valid
- Policy mode is not "enforce" (would block git commands)
- SWE-bench library installed
- Can clone at least one repo
"""
import json
import os
import sys
import subprocess
from pathlib import Path

THIS_DIR = Path(__file__).parent
PROJECT_ROOT = THIS_DIR.parent.parent

CHECKS = []
errors = []
warnings = []


def check(name):
    """Decorator to register a check function."""
    def decorator(func):
        CHECKS.append((name, func))
        return func
    return decorator


# ── Checks ────────────────────────────────────────────────────────────


@check("Docker available")
def check_docker():
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return True, f"Docker {result.stdout.strip()}"
        return False, "Docker command failed"
    except FileNotFoundError:
        return False, "Docker not installed"
    except subprocess.TimeoutExpired:
        return False, "Docker daemon not responding"


@check("Docker daemon running")
def check_docker_daemon():
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0, ""
    except Exception:
        return False, ""


@check("Python 3")
def check_python():
    return True, f"Python {sys.version.split()[0]}"


@check("Virtual environment")
def check_venv():
    venv_dir = THIS_DIR / "venv"
    if not venv_dir.exists():
        return False, f"venv not found at {venv_dir} — run setup.sh first"
    # Check if we're running inside it
    if sys.prefix == sys.base_prefix:
        return False, "Not running inside venv — activate with: source tests/swebench/venv/bin/activate"
    return True, f"Active: {sys.prefix}"


@check("SWE-bench library")
def check_swebench():
    try:
        import swebench
        return True, f"swebench {getattr(swebench, '__version__', 'installed')}"
    except ImportError:
        return False, "Not installed — run setup.sh"


@check("datasets library")
def check_datasets():
    try:
        import datasets
        return True, f"datasets {datasets.__version__}"
    except ImportError:
        return False, "Not installed — pip install datasets"


@check("laintas_cli.py exists")
def check_laintas_cli():
    cli_path = PROJECT_ROOT / "laintas_cli.py"
    if not cli_path.exists():
        return False, f"Not found at {cli_path}"
    return True, str(cli_path)


@check("Authentication (session.json)")
def check_auth():
    home = os.environ.get("LAINTAS_HOME", "")
    if home:
        session_file = Path(home) / "session.json"
    else:
        session_file = Path.home() / ".laintas" / "session.json"

    if not session_file.exists():
        return False, f"No session file at {session_file} — run /login in laintas_cli"

    try:
        with open(session_file) as f:
            session = json.load(f)
        user_id = session.get("userId", "")
        if not user_id:
            return False, "Session file missing userId"
        user_name = session.get("userName", user_id)
        return True, f"Authenticated as {user_name}"
    except (json.JSONDecodeError, IOError) as e:
        return False, f"Cannot read session: {e}"


@check("Policy mode")
def check_policy():
    home = os.environ.get("LAINTAS_HOME", "")
    if home:
        policy_file = Path(home) / "policy.json"
    else:
        policy_file = Path.home() / ".laintas" / "policy.json"

    if not policy_file.exists():
        return True, "No policy.json (defaults to audit mode — safe)"

    try:
        with open(policy_file) as f:
            policy = json.load(f)
        mode = policy.get("mode", "audit")
        if mode == "enforce":
            return False, "Policy mode is 'enforce' — git commands will be BLOCKED. Change to 'audit' or 'disabled'"
        return True, f"Mode: {mode}"
    except (json.JSONDecodeError, IOError):
        return True, "Cannot read policy (defaults to audit — safe)"


@check("Git available")
def check_git():
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, "git command failed"
    except FileNotFoundError:
        return False, "git not installed"


@check("Network connectivity")
def check_network():
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--exit-code", "https://github.com/django/django.git", "HEAD"],
            capture_output=True, timeout=15,
        )
        return result.returncode == 0, ""
    except Exception:
        return False, "Cannot reach github.com"


@check("Runtime config")
def check_runtime_config():
    """Check that max_loops is sufficient for SWE-bench."""
    # Try to import and check
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from agent_loop import get_runtime_config
        max_loops = get_runtime_config("max_loops")
        if max_loops < 80:
            return True, f"max_loops={max_loops} (consider increasing to 100 for complex issues)"
        return True, f"max_loops={max_loops}"
    except Exception:
        return True, "Could not check (will use defaults)"


# ── Main ──────────────────────────────────────────────────────────────


def main():
    print("=" * 60)
    print("SWE-bench Pre-flight Checks")
    print("=" * 60)
    print()

    passed = 0
    failed = 0

    for name, check_func in CHECKS:
        try:
            ok, detail = check_func()
        except Exception as e:
            ok, detail = False, str(e)

        if ok:
            print(f"  ✓ {name}" + (f" — {detail}" if detail else ""))
            passed += 1
        else:
            print(f"  ✗ {name}" + (f" — {detail}" if detail else ""))
            errors.append(name)
            failed += 1

    print()
    print(f"Results: {passed} passed, {failed} failed")

    if errors:
        print()
        print("Failed checks:")
        for e in errors:
            print(f"  - {e}")
        print()
        print("Fix the above issues before running the evaluation.")
        return 1

    print()
    print("✓ All checks passed. Ready to run SWE-bench evaluation.")
    print()
    print("Next step:  ./tests/swebench/quick_test.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
