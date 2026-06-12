# SWE-bench Evaluation for laintas_cli

SWE-bench evaluation framework for benchmarking laintas_cli against mainstream coding agents.

## What is SWE-bench?

[SWE-bench](https://www.swebench.com) is the industry-standard benchmark for evaluating coding agents on real-world software engineering tasks. It uses real GitHub issues from popular open-source projects (Django, scikit-learn, SymPy, etc.) and measures whether the agent can fix them correctly (verified by running the project's test suite).

**Why it matters:** SWE-bench scores are widely recognized and comparable across different agents. If you publish your results, they can be directly compared with Claude Code, OpenAI Codex, Devin, and other mainstream agents.

### Current Benchmarks (SWE-bench Verified)

| Agent | Pass Rate |
|---|---|
| Claude Code | ~60-65% |
| Devin | ~50% |
| OpenHands | ~40-50% |
| OpenAI Codex | ~30-40% |
| SWE-agent | ~15-20% |

## Quick Start

### 1. Setup

```bash
cd tests/swebench
./setup.sh
```

This will:
- Check Docker is installed and running
- Create an isolated Python virtual environment
- Clone and install the official SWE-bench harness
- Create required directories

### 2. Validate Environment

```bash
source venv/bin/activate
python preflight.py
```

Checks:
- Docker available and running
- laintas_cli authentication valid (`~/.laintas/session.json`)
- Policy mode is not "enforce" (would block git commands)
- SWE-bench library installed
- Network connectivity to GitHub
- Git available

### 3. Quick Test (3 instances)

```bash
./quick_test.sh
```

Runs 3 SWE-bench Lite instances to verify the framework works end-to-end. Takes ~30-60 minutes.

### 4. Full Evaluation

```bash
# Generate predictions for all 300 SWE-bench Lite instances
python generate_predictions.py

# Run evaluation (requires Docker)
python run_evaluation.py
```

## Files

| File | Purpose |
|---|---|
| `setup.sh` | Environment setup (venv, SWE-bench install) |
| `preflight.py` | Pre-flight validation checks |
| `adapter.py` | Wraps `laintas_cli --execute` for SWE-bench |
| `generate_predictions.py` | Generates predictions from SWE-bench issues |
| `run_evaluation.py` | Runs official SWE-bench evaluation |
| `quick_test.sh` | Quick test with 3 instances |
| `venv/` | Isolated Python virtual environment |
| `SWE-bench/` | Official SWE-bench repository |
| `repos/` | Cloned target repositories |
| `predictions/` | Agent predictions (JSONL format) |
| `results/` | Evaluation results |

## How It Works

### Prediction Generation (`generate_predictions.py`)

For each SWE-bench instance:

1. **Setup repo**: Clone the target repository (if not already cloned) and checkout the specific base commit
2. **Clean state**: `git reset --hard` + `git clean -fdx` to ensure clean working tree
3. **Run agent**: Call `laintas_cli --execute "<issue description>"` with isolated `LAINTAS_HOME`
4. **Capture patch**: `git add -A && git diff --cached HEAD` to get all changes (including new files)
5. **Write result**: Append as JSONL (one JSON object per line)

### Evaluation (`run_evaluation.py`)

Uses the official SWE-bench harness to:

1. Apply each prediction's patch to the repo
2. Run the project's test suite inside a Docker container
3. Check if the fix resolves the issue (tests pass)
4. Generate results with pass/fail for each instance

### Key Design Decisions

- **`LAINTAS_HOME` isolation**: Each instance runs with `LAINTAS_HOME` set to a temp directory to prevent `.laintas/` from being created inside the target repo
- **`git add -A`**: Captures ALL changes including new files (not just modified tracked files)
- **Proper repo reset**: `reset --hard` → `clean -fdx` → `checkout` → `clean -fdx` ensures clean state
- **JSONL format**: Required by SWE-bench harness (one JSON object per line)
- **Resume support**: Skips already-completed instances if the script is interrupted

## Configuration

### Runtime Config

Before running, consider increasing these values for better results:

| Key | Default | Recommended | Reason |
|---|---|---|---|
| `max_loops` | 50 | 100 | Complex issues need many iterations |
| `staleness_limit` | 5 | 8 | Allow more thinking steps |
| `warning_force_limit` | 5 | 8 | Don't exit too early on warnings |

Set via REPL: `/config max_loops 100`

### Policy Mode

Ensure `~/.laintas/policy.json` mode is NOT `"enforce"`. In enforce mode, git commands (checkout, reset, etc.) will be blocked, and the agent cannot function.

Use `"audit"` or `"disabled"` mode for SWE-bench evaluation.

## Cost & Time Estimates

| Metric | Per Instance | SWE-bench Lite (300) |
|---|---|---|
| **API cost** | ~$0.50–$3.00 | ~$150–$900 |
| **Time** | 15–30 min | 75–150 hours |

Costs depend on the model used by laintas_cli's backend and the complexity of each issue.

## Output Format

### Predictions (`predictions/*.jsonl`)

```json
{
  "instance_id": "django__django-12345",
  "model_patch": "diff --git a/...\n...",
  "model_name_or_path": "laintas_cli",
  "exit_code": 0,
  "stdout": "...",
  "stderr": "..."
}
```

### Results (`results/summary_*.json`)

```json
{
  "model": "laintas_cli",
  "dataset": "SWE-bench Lite",
  "run_id": "laintas_cli_20260601_120000",
  "total_evaluated": 300,
  "resolved": 45,
  "failed": 240,
  "errored": 15,
  "pass_rate": 15.0,
  "timestamp": "2026-06-01T12:00:00"
}
```

## Troubleshooting

### Docker not found
SWE-bench evaluation requires Docker. Install from https://docs.docker.com/get-docker/

### Authentication failed
Run `laintas_cli` and use `/login` to authenticate. The session is cached in `~/.laintas/session.json`.

### Policy blocking git commands
Edit `~/.laintas/policy.json` and set `"mode": "audit"` or `"mode": "disabled"`.

### Agent timeout
Default timeout is 30 minutes per instance. Increase via `--timeout` argument in adapter if needed.

### Resume after crash
`generate_predictions.py` supports resume — it skips instances already in the output file. Just re-run the same command.

## Submitting Results

To submit your results to the official SWE-bench leaderboard:

1. Ensure predictions are in JSONL format
2. Include model name and configuration details
3. Follow submission guidelines at https://www.swebench.com
