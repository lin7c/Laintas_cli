#!/usr/bin/env bash
set -e

BASE_URL="https://cli.laintas.com"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

echo ""
echo "── Laintas CLI Installer ─────────────────────────────────────"

# Detect OS
UNAME_S=$(uname -s)
if [ "$UNAME_S" = "Linux" ]; then
    INSTALL_MODE="linux"
elif echo "$UNAME_S" | grep -qi "MINGW\|MSYS\|CYGWIN"; then
    INSTALL_MODE="windows"
else
    echo "Unsupported OS: $UNAME_S"
    exit 1
fi

if [ "$INSTALL_MODE" = "linux" ]; then
    echo "  Detected: Linux"

    # Download tarball
    echo "  Downloading…"
    curl -fsSL "$BASE_URL/laintas-cli.tar.gz" -o "$TMP_DIR/laintas-cli.tar.gz"

    # Extract
    echo "  Installing to /usr/local/"
    cd /
    if [ "$(id -u)" -eq 0 ]; then
        tar xzf "$TMP_DIR/laintas-cli.tar.gz" -C /
    else
        sudo tar xzf "$TMP_DIR/laintas-cli.tar.gz" -C /
    fi

    # Create workspace
    WORKSPACE="$HOME/laintas_workspace"
    if [ ! -d "$WORKSPACE" ]; then
        mkdir -p "$WORKSPACE"
        echo "  Workspace: $WORKSPACE"
    fi

    # Install Python deps
    echo "  Installing Python dependencies…"
    if command -v pip3 &>/dev/null; then
        pip3 install -r /usr/lib/laintas_cli/requirements.txt --quiet 2>/dev/null || echo "  (pip install skipped, deps will install on first run)"
    elif command -v pip &>/dev/null; then
        pip install -r /usr/lib/laintas_cli/requirements.txt --quiet 2>/dev/null || echo "  (pip install skipped, deps will install on first run)"
    else
        echo "  WARNING: pip not found. Install python3-pip first:"
        echo "    sudo apt install python3-pip"
    fi

    printf '\n  Done! Run `laintas-cli` to start.\n\n'

elif [ "$INSTALL_MODE" = "windows" ]; then
    echo "  Detected: Windows (Git Bash / MSYS2)"
    echo "  Downloading Windows installer…"
    curl -fsSL "$BASE_URL/releases/latest/laintas_cli_setup.exe" -o "$TMP_DIR/laintas_cli_setup.exe"
    echo "  Launching installer…"
    "$TMP_DIR/laintas_cli_setup.exe"
fi

echo "── ────────────────────────────────────────────────────────────"
