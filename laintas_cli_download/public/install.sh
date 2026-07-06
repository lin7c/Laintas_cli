#!/usr/bin/env bash
set -euo pipefail

BASE_URL="https://cli.laintas.com"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

echo ""
echo "── Laintas CLI Installer ─────────────────────────────────────"

# Detect OS
UNAME_S=$(uname -s)
if [ "$UNAME_S" = "Linux" ]; then
    INSTALL_MODE="linux"
else
    echo "Unsupported OS: $UNAME_S (Linux only)"
    exit 1
fi

if [ "$INSTALL_MODE" = "linux" ]; then
    echo "  Detected: Linux"

    # Download tarball
    echo "  Downloading…"
    curl -fsSL "$BASE_URL/releases/v1.6/laintas-cli_linux.tar.gz" -o "$TMP_DIR/laintas-cli_linux.tar.gz"

    # Extract
    echo "  Extracting package…"
    tar xzf "$TMP_DIR/laintas-cli_linux.tar.gz" -C "$TMP_DIR"

    echo "  Installing to /usr/local/bin…"
    "$TMP_DIR/laintas-cli/install.sh"

    printf '\n  Done! Run `laintas-cli` to start.\n\n'
fi

echo "── ────────────────────────────────────────────────────────────"
