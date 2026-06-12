"""
Run SWE-bench evaluation on laintas_cli predictions.

Uses the official SWE-bench harness to evaluate predictions by running
each instance's test suite inside a Docker container.

Requirements:
- Docker installed and running
- SWE-bench library installed (via setup.sh)
- Predictions in JSONL format (from generate_predictions.py)
"""
import json
import os
import sys
import subprocess
import glob
from pathlib import Path
from datetime import datetime

THIS_DIR = Path(__file__).parent

DEFAULT_PREDICTIONS = "predictions/laintas_cli_predictions.jsonl"
DEFAULT_RESULTS_DIR = "results"
DEFAULT_DATASET = "princeton-nlp/SWE-bench_Lite"


def check_docker() -> bool:
    """Verify Docker is available."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def run_evaluation(
    predictions_path: str = DEFAULT_PREDICTIONS,
    dataset_name: str = DEFAULT_DATASET,
    results_dir: str = DEFAULT_RESULTS_DIR,
    run_id: str = None,
    max_workers: int = 1,
):
    """Run SWE-bench evaluation using official harness.

    Args:
        predictions_path: Path to predictions JSONL file
        dataset_name: SWE-bench dataset name
        results_dir: Where to save evaluation results
        run_id: Unique run identifier (auto-generated if None)
        max_workers: Number of parallel evaluation workers
    """
    # Validate inputs
    predictions_file = THIS_DIR / predictions_path
    if not predictions_file.exists():
        print(f"ERROR: Predictions file not found: {predictions_file}")
        sys.exit(1)

    if not check_docker():
        print("ERROR: Docker is not available")
        print("SWE-bench evaluation requires Docker to run test suites")
        sys.exit(1)

    # Generate run_id
    if run_id is None:
        run_id = f"laintas_cli_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    results_path = THIS_DIR / results_dir
    results_path.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("SWE-bench Evaluation")
    print("=" * 60)
    print(f"Predictions: {predictions_file}")
    print(f"Dataset:     {dataset_name}")
    print(f"Run ID:      {run_id}")
    print(f"Results:     {results_path}")
    print(f"Workers:     {max_workers}")
    print()

    # Count predictions
    pred_count = 0
    with open(predictions_file) as f:
        for line in f:
            if line.strip():
                pred_count += 1
    print(f"Predictions to evaluate: {pred_count}")
    print()

    # Build evaluation command
    cmd = [
        "python", "-m", "swebench.harness.run_evaluation",
        "--predictions_path", str(predictions_file),
        "--dataset_name", dataset_name,
        "--split", "test",
        "--max_workers", str(max_workers),
        "--run_id", run_id,
    ]

    print("Running evaluation (this may take a while)...")
    print(f"Command: {' '.join(cmd)}")
    print()

    try:
        result = subprocess.run(cmd, cwd=str(THIS_DIR))
    except FileNotFoundError:
        print("ERROR: swebench.harness module not found")
        print("Make sure you activated the venv: source tests/swebench/venv/bin/activate")
        sys.exit(1)

    if result.returncode != 0:
        print(f"\nWARNING: Evaluation exited with code {result.returncode}")

    # Try to find and parse results
    print()
    print("Looking for results...")
    _parse_and_display_results(run_id, results_path, pred_count)


def _parse_and_display_results(run_id: str, results_dir: Path, total_predictions: int):
    """Find evaluation results and display summary."""

    # SWE-bench harness outputs to logs/ or a run-specific directory
    # Try multiple possible locations
    search_patterns = [
        results_dir / "**" / "results.json",
        results_dir / "**" / "*.json",
        Path("logs") / run_id / "**" / "*.json",
        Path("logs") / "**" / "results.json",
    ]

    results_files = []
    for pattern in search_patterns:
        results_files.extend(glob.glob(str(pattern), recursive=True))

    if not results_files:
        print("No results files found. Check SWE-bench harness output above.")
        print(f"Look in: {results_dir}/ or logs/")
        return

    # Try to parse each results file
    total = 0
    resolved = 0
    failed = 0
    errored = 0

    for rf in results_files:
        try:
            with open(rf) as f:
                data = json.load(f)

            if isinstance(data, dict):
                # SWE-bench results format varies by version
                # Could be {instance_id: {resolved: bool, ...}}
                for iid, info in data.items():
                    if isinstance(info, dict):
                        total += 1
                        if info.get("resolved", False):
                            resolved += 1
                        elif info.get("error", False):
                            errored += 1
                        else:
                            failed += 1
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        total += 1
                        if item.get("resolved", False):
                            resolved += 1
                        elif item.get("error", False):
                            errored += 1
                        else:
                            failed += 1
        except (json.JSONDecodeError, IOError):
            continue

    if total == 0:
        print("Could not parse any results from found files.")
        return

    # Display results
    pass_rate = (resolved / total * 100) if total > 0 else 0

    print()
    print("=" * 60)
    print("EVALUATION RESULTS")
    print("=" * 60)
    print(f"Total evaluated: {total}")
    print(f"Resolved:        {resolved}")
    print(f"Failed:          {failed}")
    print(f"Errors:          {errored}")
    print(f"Pass rate:       {pass_rate:.1f}%")
    print()
    print("Comparison with mainstream agents (SWE-bench Verified):")
    print("  Claude Code:    ~60-65%")
    print("  Devin:          ~50%")
    print("  OpenHands:      ~40-50%")
    print("  OpenAI Codex:   ~30-40%")
    print("  SWE-agent:      ~15-20%")
    print()

    # Save summary
    summary = {
        "model": "laintas_cli",
        "dataset": "SWE-bench Lite",
        "run_id": run_id,
        "total_evaluated": total,
        "resolved": resolved,
        "failed": failed,
        "errored": errored,
        "pass_rate": round(pass_rate, 2),
        "timestamp": datetime.now().isoformat(),
    }

    summary_file = results_dir / f"summary_{run_id}.json"
    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"Summary saved to: {summary_file}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Run SWE-bench evaluation on laintas_cli predictions"
    )
    parser.add_argument(
        "--predictions", type=str, default=DEFAULT_PREDICTIONS,
        help=f"Path to predictions JSONL (default: {DEFAULT_PREDICTIONS})"
    )
    parser.add_argument(
        "--dataset", type=str, default=DEFAULT_DATASET,
        help=f"Dataset name (default: {DEFAULT_DATASET})"
    )
    parser.add_argument(
        "--results-dir", type=str, default=DEFAULT_RESULTS_DIR,
        help=f"Results directory (default: {DEFAULT_RESULTS_DIR})"
    )
    parser.add_argument(
        "--run-id", type=str, default=None,
        help="Run ID (auto-generated if not specified)"
    )
    parser.add_argument(
        "--max-workers", type=int, default=1,
        help="Parallel workers (default: 1)"
    )
    args = parser.parse_args()

    run_evaluation(
        predictions_path=args.predictions,
        dataset_name=args.dataset,
        results_dir=args.results_dir,
        run_id=args.run_id,
        max_workers=args.max_workers,
    )
