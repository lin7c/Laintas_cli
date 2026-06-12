"""
Adapter to call laintas_cli in SWE-bench compatible way.

Wraps `laintas_cli --execute` to produce patches that can be evaluated
by the official SWE-bench harness.

Key design decisions:
- LAINTAS_HOME is set to a temp dir to prevent .laintas/ contamination in repos
- Uses `git add -A && git diff --cached HEAD` to capture ALL changes (including new files)
- Uses subprocess.run(cwd=repo_path) instead of os.chdir() for thread safety
"""
import subprocess
import os
import json
import tempfile
import shutil
from pathlib import Path

# Resolve paths relative to this file's location
THIS_DIR = Path(__file__).parent
PROJECT_ROOT = THIS_DIR.parent.parent
LINTAS_CLI_PATH = PROJECT_ROOT / "laintas_cli.py"


def build_task_prompt(
    instance_id: str,
    issue_description: str,
    repo_name: str = "",
    base_commit: str = "",
) -> str:
    """Build the task prompt for the agent.

    Includes repo context and structured instructions to guide the agent
    through: reproduce -> analyze -> fix -> verify.
    """
    commit_ref = base_commit[:8] if base_commit else "HEAD"
    return f"""You are working in the {repo_name} repository at commit {commit_ref}.

Fix the following issue:

{issue_description}

Instructions:
1. First, find and run the failing test(s) to reproduce the issue
2. Analyze the root cause by reading relevant source files
3. Make minimal, targeted changes to fix the issue
4. Run the tests again to verify the fix
5. Do NOT modify test files unless the issue explicitly asks for test changes

Issue ID: {instance_id}
"""


def run_agent(
    instance_id: str,
    repo_path: str,
    issue_description: str,
    repo_name: str = "",
    base_commit: str = "",
    timeout: int = 1800,
) -> dict:
    """Run laintas_cli on a single SWE-bench instance.

    Args:
        instance_id: SWE-bench instance ID (e.g., "django__django-12345")
        repo_path: Absolute path to the cloned repo (at specific commit)
        issue_description: The issue/problem statement
        repo_name: GitHub repo name (e.g., "django/django")
        base_commit: Base commit SHA for context
        timeout: Max seconds to run (default 30 min)

    Returns:
        dict with keys:
            instance_id: str
            model_patch: str (git diff)
            model_name_or_path: str
            exit_code: int (0=success, 1=failure, -1=timeout)
    """
    # Create isolated LAINTAS_HOME to prevent .laintas/ creation in repo
    laintas_home = tempfile.mkdtemp(prefix="swebench_laintas_")

    # Copy session.json into isolated home so agent can authenticate
    real_session = Path.home() / ".laintas" / "session.json"
    if real_session.exists():
        shutil.copy2(str(real_session), os.path.join(laintas_home, "session.json"))
        os.chmod(os.path.join(laintas_home, "session.json"), 0o600)

    task = build_task_prompt(instance_id, issue_description, repo_name, base_commit)

    try:
        # Build environment with isolated LAINTAS_HOME
        env = os.environ.copy()
        env["LAINTAS_HOME"] = laintas_home

        # Use the project's main venv Python (has CLI deps), not the SWE-bench venv
        cli_python = PROJECT_ROOT / "venv" / "bin" / "python"
        if not cli_python.exists():
            cli_python = Path("python")  # fallback
        else:
            # Ensure main venv bin + system paths are available for CLI subprocesses
            cli_bin = str(PROJECT_ROOT / "venv" / "bin")
            system_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            env["PATH"] = f"{cli_bin}:{system_path}"

        cmd = [
            str(cli_python),
            str(LINTAS_CLI_PATH),
            "--execute", task,
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=repo_path,
            env=env,
        )

        # Capture ALL changes including new/untracked files
        # Step 1: Stage everything
        subprocess.run(
            ["git", "add", "-A"],
            cwd=repo_path,
            capture_output=True,
        )
        # Step 1b: Unstage .laintas/ if it got staged (CLI per-cwd project files)
        subprocess.run(
            ["git", "reset", "HEAD", "--", ".laintas/"],
            cwd=repo_path,
            capture_output=True,
        )
        # Step 2: Get diff of staged changes vs HEAD (excluding .laintas/)
        diff_result = subprocess.run(
            ["git", "diff", "--cached", "HEAD", "--", ":!.laintas"],
            capture_output=True,
            text=True,
            cwd=repo_path,
        )

        return {
            "instance_id": instance_id,
            "model_patch": diff_result.stdout,
            "model_name_or_path": "laintas_cli",
            "exit_code": result.returncode,
            "stdout": result.stdout[:5000] if result.stdout else "",
            "stderr": result.stderr[:2000] if result.stderr else "",
        }

    except subprocess.TimeoutExpired:
        return {
            "instance_id": instance_id,
            "model_patch": "",
            "model_name_or_path": "laintas_cli",
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Timeout after {timeout}s",
        }

    except Exception as e:
        return {
            "instance_id": instance_id,
            "model_patch": "",
            "model_name_or_path": "laintas_cli",
            "exit_code": -2,
            "stdout": "",
            "stderr": f"Adapter error: {str(e)}",
        }

    finally:
        # Clean up temp LAINTAS_HOME
        shutil.rmtree(laintas_home, ignore_errors=True)


if __name__ == "__main__":
    # Quick self-test
    print(f"laintas_cli path: {LINTAS_CLI_PATH}")
    print(f"Exists: {LINTAS_CLI_PATH.exists()}")
    print(f"Project root: {PROJECT_ROOT}")
