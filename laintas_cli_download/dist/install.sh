#!/usr/bin/env bash
set -euo pipefail

BASE_URL="https://cli.laintas.com"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

echo ""
echo "── Laintas CLI Installer ─────────────────────────────────────"

# Detect OS and CPU architecture before downloading a native binary.
UNAME_S=$(uname -s)
if [ "$UNAME_S" = "Linux" ]; then
    INSTALL_MODE="linux"
else
    echo "Unsupported OS: $UNAME_S (Linux only)"
    exit 1
fi

if [ "$INSTALL_MODE" = "linux" ]; then
    case "$(uname -m)" in
        x86_64|amd64) ARCH="amd64" ;;
        aarch64|arm64) ARCH="arm64" ;;
        *)
            echo "Unsupported Linux architecture: $(uname -m)"
            echo "Supported architectures: x86_64/amd64 and aarch64/arm64"
            exit 1
            ;;
    esac
    echo "  Detected: Linux $ARCH"

    # Download tarball
    echo "  Downloading…"
    ASSET="laintas-cli_linux_${ARCH}.tar.gz"
    curl -fsSL "$BASE_URL/releases/v1.7.1/$ASSET" -o "$TMP_DIR/$ASSET"

    # Extract
    echo "  Extracting package…"
    tar xzf "$TMP_DIR/$ASSET" -C "$TMP_DIR"

    echo "  Installing to /usr/local/bin…"
    # v1.7+ archives place install.sh beside the binary; older archives
    # nested both files under a laintas-cli directory.
    if [ -x "$TMP_DIR/install.sh" ]; then
        "$TMP_DIR/install.sh"
    elif [ -x "$TMP_DIR/laintas-cli/install.sh" ]; then
        "$TMP_DIR/laintas-cli/install.sh"
    else
        echo "Invalid package: install.sh was not found after extraction"
        exit 1
    fi

    printf '\n  Done! Run `laintas-cli` to start.\n\n'
fi

echo "── ────────────────────────────────────────────────────────────"
