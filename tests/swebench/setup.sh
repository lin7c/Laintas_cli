#!/bin/bash
# SWE-bench evaluation framework setup for laintas_cli
# Creates isolated venv and installs SWE-bench harness
set -e

cd "$(dirname "$0")"

echo "=== SWE-bench Setup for laintas_cli ==="
echo ""

# Check Docker
echo "[1/4] Checking Docker..."
if ! command -v docker &> /dev/null; then
    echo "ERROR: Docker is required for SWE-bench evaluation"
    echo "Install: https://docs.docker.com/get-docker/"
    exit 1
fi
if ! docker info > /dev/null 2>&1; then
    echo "ERROR: Docker daemon not running or no permission"
    echo "Try: sudo systemctl start docker"
    exit 1
fi
echo "  ✓ Docker OK"

# Check Python
echo "[2/4] Checking Python..."
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 is required"
    exit 1
fi
echo "  ✓ Python $(python3 --version)"

# Create venv
echo "[3/4] Creating virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "  ✓ venv created"
else
    echo "  ✓ venv already exists"
fi

# Activate and install SWE-bench
echo "[4/4] Installing SWE-bench..."
source venv/bin/activate
pip install --quiet --upgrade pip

if [ ! -d "SWE-bench" ]; then
    echo "  Cloning SWE-bench repository..."
    git clone --quiet https://github.com/princeton-nlp/SWE-bench.git
    cd SWE-bench
    pip install --quiet -e .
    cd ..
    echo "  ✓ SWE-bench installed"
else
    echo "  ✓ SWE-bench already cloned"
    cd SWE-bench
    pip install --quiet -e .
    cd ..
fi

# Create directories
mkdir -p repos predictions results

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. source tests/swebench/venv/bin/activate"
echo "  2. python tests/swebench/preflight.py"
echo "  3. ./tests/swebench/quick_test.sh"
echo ""
