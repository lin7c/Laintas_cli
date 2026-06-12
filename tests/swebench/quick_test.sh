#!/bin/bash
# Quick test: run 3 SWE-bench instances to verify the framework works end-to-end
set -e

cd "$(dirname "$0")"

echo "=== SWE-bench Quick Test (3 instances) ==="
echo ""

# Activate venv
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
else
    echo "ERROR: venv not found. Run ./setup.sh first."
    exit 1
fi

# Pre-flight
echo "[1/3] Pre-flight checks..."
python preflight.py
echo ""

# Generate predictions for 3 instances
echo "[2/3] Generating predictions (3 instances)..."
python generate_predictions.py \
    --max-instances 3 \
    --output predictions/quick_test.jsonl
echo ""

# Summary
echo "[3/3] Quick test complete!"
echo ""
echo "Results: predictions/quick_test.jsonl"
echo ""
echo "To evaluate these 3 predictions:"
echo "  python run_evaluation.py --predictions predictions/quick_test.jsonl"
echo ""
echo "To run full SWE-bench Lite (300 instances):"
echo "  python generate_predictions.py"
echo ""
