#!/usr/bin/env bash
# build_deb.sh — Build self-contained laintas-cli .deb package
#
# Pipeline:
#   1. PyInstaller produces a single-file binary (bundles Python + deps).
#   2. fpm wraps the binary + launcher into a .deb.
#
# Prerequisites:
#   sudo apt install ruby ruby-dev python3-venv binutils
#   sudo gem install fpm
#   (PyInstaller is installed inside the project's venv on demand.)
#
# Usage:
#   ./build/linux/build_deb.sh [version]
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
VERSION="${1:-$(date +%Y.%m.%d)}"
OUTPUT_DIR="$PROJECT_DIR/dist"
PKG_DIR="$(mktemp -d)"
trap 'rm -rf "$PKG_DIR"' EXIT

echo "Project:  $PROJECT_DIR"
echo "Version:  $VERSION"
echo "Staging:  $PKG_DIR"

mkdir -p "$OUTPUT_DIR"

# ── 1. Ensure a venv with PyInstaller ─────────────────────────────────
VENV_DIR="$PROJECT_DIR/venv"
if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "Creating venv at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip >/dev/null
if [ -f "$PROJECT_DIR/requirements.txt" ]; then
    pip install -r "$PROJECT_DIR/requirements.txt" >/dev/null
fi
pip install --upgrade pyinstaller >/dev/null

# ── 2. Build single-file binary with PyInstaller ──────────────────────
cd "$PROJECT_DIR"
rm -rf build/linux/build build/linux/dist
pyinstaller \
    --clean \
    --noconfirm \
    --distpath build/linux/dist \
    --workpath build/linux/build \
    build/linux/laintas_cli.spec

BINARY="$PROJECT_DIR/build/linux/dist/laintas-cli"
if [ ! -f "$BINARY" ]; then
    echo "PyInstaller did not produce $BINARY" >&2
    exit 1
fi
chmod 755 "$BINARY"

# ── 3. Stage the .deb tree ────────────────────────────────────────────
mkdir -p "$PKG_DIR/usr/bin"
install -m 755 "$BINARY" "$PKG_DIR/usr/bin/laintas-cli"

# ── 4. Build .deb with fpm ────────────────────────────────────────────
echo "Building laintas-cli v${VERSION} .deb..."

fpm \
    -s dir \
    -t deb \
    -n laintas-cli \
    -v "$VERSION" \
    --description "Laintas CLI - Autonomous AI agent for your terminal (self-contained build)" \
    --url "https://laintas.com" \
    --maintainer "Laintas <support@laintas.com>" \
    --license "Proprietary" \
    --architecture amd64 \
    --after-install "$SCRIPT_DIR/postinst.sh" \
    --before-remove "$SCRIPT_DIR/prerm.sh" \
    -C "$PKG_DIR" \
    -p "$OUTPUT_DIR/laintas-cli_${VERSION}_amd64.deb" \
    usr/

echo ""
echo "Done: $OUTPUT_DIR/laintas-cli_${VERSION}_amd64.deb"
echo "Install: sudo dpkg -i $OUTPUT_DIR/laintas-cli_${VERSION}_amd64.deb"
