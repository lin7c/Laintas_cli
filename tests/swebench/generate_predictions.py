"""
Generate predictions for SWE-bench instances using laintas_cli.

Processes each instance sequentially:
1. Clone/checkout the target repo at the specific base commit
2. Run laintas_cli --execute with the issue description
3. Capture the git diff as the "model patch"
4. Write result as JSONL (one JSON object per line)

Features:
- Resume support: skips already-completed instances
- JSONL output format (required by SWE-bench harness)
- Proper repo reset sequence to ensure clean state
"""
import json
import os
import sys
import subprocess
from pathlib import Path

# Add current dir to path for adapter import
sys.path.insert(0, str(Path(__file__).parent))
from adapter import run_agent

# Default dataset
DEFAULT_DATASET = "princeton-nlp/SWE-bench_Lite"
DEFAULT_OUTPUT = "predictions/laintas_cli_predictions.jsonl"

THIS_DIR = Path(__file__).parent
REPOS_DIR = THIS_DIR / "repos"


def setup_repo(instance: dict) -> str:
    """Clone repo and checkout specific commit for this instance.

    Reset sequence (critical for correctness):
    1. git reset --hard HEAD   — discard all staged/unstaged changes
    2. git clean -fdx          — remove all untracked files + ignored files
    3. git checkout <commit>   — switch to base commit
    4. git clean -fdx          — clean again after checkout

    Returns absolute path to the repo.
    """
    repo_name = instance["repo"]  # e.g., "django/django"
    base_commit = instance["base_commit"]
    repo_path = REPOS_DIR / repo_name.replace("/", "__")

    if not repo_path.exists():
        print(f"  Cloning {repo_name}...")
        subprocess.run(
            ["git", "clone", "--quiet", f"https://github.com/{repo_name}.git", str(repo_path)],
            check=True,
        )
        print(f"  ✓ Cloned to {repo_path}")

    # Reset sequence — order matters!
    subprocess.run(
        ["git", "reset", "--hard", "HEAD"],
        cwd=repo_path, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "clean", "-fdx"],
        cwd=repo_path, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "checkout", base_commit],
        cwd=repo_path, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "clean", "-fdx"],
        cwd=repo_path, capture_output=True, check=True,
    )

    # Prevent .laintas/ (per-cwd project files) from being tracked by git.
    # The CLI creates .laintas/ in whatever directory it runs in, but we don't
    # want it polluting the patch diff.
    gitignore_path = repo_path / ".gitignore"
    gitignore_content = gitignore_path.read_text() if gitignore_path.exists() else ""
    if ".laintas" not in gitignore_content:
        with open(gitignore_path, "a") as f:
            f.write("\n# SWE-bench: exclude laintas CLI project files\n.laintas/\n")
        # Commit this change so it doesn't appear in the agent's patch
        subprocess.run(
            ["git", "add", ".gitignore"],
            cwd=repo_path, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "SWE-bench: add .laintas/ to gitignore"],
            cwd=repo_path, capture_output=True, check=True,
        )

    return str(repo_path)


def load_existing_predictions(output_path: str) -> set:
    """Load already-completed instance IDs for resume support."""
    existing = set()
    if os.path.exists(output_path):
        with open(output_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        obj = json.loads(line)
                        existing.add(obj["instance_id"])
                    except (json.JSONDecodeError, KeyError):
                        pass
    return existing


def load_dataset(dataset_name: str) -> list:
    """Load SWE-bench dataset.

    Tries swebench library first; falls back to HuggingFace datasets.
    """
    try:
        from swebench.harness.utils import load_swebench_dataset
        return list(load_swebench_dataset(dataset_name))
    except ImportError:
        pass

    # Fallback: use datasets library directly
    try:
        from datasets import load_dataset as hf_load
        split = "test"
        ds = hf_load(dataset_name, split=split)
        return [dict(row) for row in ds]
    except ImportError:
        print("ERROR: Neither swebench nor datasets library installed.")
        print("Run: pip install datasets")
        sys.exit(1)


def check_auth() -> bool:
    """Check that laintas_cli authentication is available."""
    session_file = Path.home() / ".laintas" / "session.json"
    laintas_home = os.environ.get("LAINTAS_HOME", "")
    if laintas_home:
        session_file = Path(laintas_home) / "session.json"

    if not session_file.exists():
        print(f"WARNING: No session file at {session_file}")
        print("  Run laintas_cli and /login first, or set LAINTAS_HOME")
        return False

    try:
        with open(session_file) as f:
            session = json.load(f)
        if not session.get("userId"):
            print(f"WARNING: Session file missing userId: {session_file}")
            return False
        return True
    except (json.JSONDecodeError, IOError):
        print(f"WARNING: Cannot read session file: {session_file}")
        return False


def generate_predictions(
    dataset_name: str = DEFAULT_DATASET,
    output_path: str = DEFAULT_OUTPUT,
    max_instances: int = None,
):
    """Generate predictions for SWE-bench instances.

    Args:
        dataset_name: HuggingFace dataset name
        output_path: Where to save predictions (JSONL format)
        max_instances: Limit number of instances (for testing)
    """
    print(f"Dataset: {dataset_name}")
    print(f"Output:  {output_path}")
    print()

    # Check auth
    if not check_auth():
        print("Proceeding anyway (agent will fail on first instance if auth missing)")
        print()

    # Load dataset
    print("Loading dataset...")
    dataset = load_dataset(dataset_name)
    print(f"  ✓ {len(dataset)} instances loaded")

    if max_instances:
        dataset = dataset[:max_instances]
        print(f"  Limited to {len(dataset)} instances")

    # Resume support
    existing_ids = load_existing_predictions(output_path)
    if existing_ids:
        print(f"  Resuming: {len(existing_ids)} instances already completed")
    print()

    # Ensure output directory exists
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # Process instances
    completed = 0
    errors = 0

    with open(output_path, "a") as f:
        for i, instance in enumerate(dataset):
            iid = instance["instance_id"]

            if iid in existing_ids:
                print(f"[{i+1}/{len(dataset)}] SKIP {iid} (already done)")
                continue

            print(f"[{i+1}/{len(dataset)}] {iid}")
            print(f"  Repo: {instance['repo']} @ {instance['base_commit'][:8]}")

            try:
                repo_path = setup_repo(instance)
            except subprocess.CalledProcessError as e:
                print(f"  ✗ Repo setup failed: {e}")
                errors += 1
                continue

            result = run_agent(
                instance_id=iid,
                repo_path=repo_path,
                issue_description=instance["problem_statement"],
                repo_name=instance["repo"],
                base_commit=instance["base_commit"],
            )

            # Write JSONL (one JSON object per line)
            f.write(json.dumps(result) + "\n")
            f.flush()

            exit_code = result["exit_code"]
            patch_size = len(result["model_patch"])

            if exit_code == 0 and patch_size > 0:
                print(f"  ✓ exit={exit_code} patch={patch_size} chars")
                completed += 1
            elif exit_code == -1:
                print(f"  ✗ TIMEOUT")
                errors += 1
            elif exit_code == -2:
                print(f"  ✗ ADAPTER ERROR")
                errors += 1
            else:
                print(f"  ~ exit={exit_code} patch={patch_size} chars")
                completed += 1

    print()
    print(f"=== Complete ===")
    print(f"  Processed: {completed + errors}")
    print(f"  Successful: {completed}")
    print(f"  Errors: {errors}")
    print(f"  Output: {output_path}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate SWE-bench predictions using laintas_cli"
    )
    parser.add_argument(
        "--max-instances", type=int, default=None,
        help="Limit number of instances (for testing)"
    )
    parser.add_argument(
        "--output", type=str, default=DEFAULT_OUTPUT,
        help=f"Output path (default: {DEFAULT_OUTPUT})"
    )
    parser.add_argument(
        "--dataset", type=str, default=DEFAULT_DATASET,
        help=f"Dataset name (default: {DEFAULT_DATASET})"
    )
    args = parser.parse_args()

    generate_predictions(
        dataset_name=args.dataset,
        output_path=args.output,
        max_instances=args.max_instances,
    )
