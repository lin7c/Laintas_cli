#!/usr/bin/env bash
# build_deb.sh — Build laintas-cli .deb package using fpm
#
# Prerequisites:
#   sudo apt install ruby ruby-dev
#   sudo gem install fpm
#
#   Or on macOS:
#   brew install fpm
#
# Usage:
#   ./build/linux/build_deb.sh
#   ./build/linux/build_deb.sh 0.1.1   # specify version

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
VERSION="${1:-0.1.1}"
BUILD_DIR="$PROJECT_DIR/build/linux/tmp"
PKG_DIR="$BUILD_DIR/pkg"
OUTPUT_DIR="$PROJECT_DIR/build/linux"

# ── Clean ────────────────────────────────────────────────────────────────
rm -rf "$BUILD_DIR"
mkdir -p "$PKG_DIR/usr/lib/laintas_cli" \
         "$PKG_DIR/usr/bin" \
         "$PKG_DIR/usr/share/doc/laintas-cli"

# ── Copy source files ────────────────────────────────────────────────────
for f in laintas_cli.py agent_loop.py tools.py skills.py mcp_client.py \
         policy.py memory_system.py hooks.py plan_mode.py task_manager.py \
         agent_persistence.py agent_roles.py workflow_engine.py \
         paths.py migrate.py cloud_provider.py requirements.txt; do
    cp "$PROJECT_DIR/$f" "$PKG_DIR/usr/lib/laintas_cli/"
done

# ── Launcher script ──────────────────────────────────────────────────────
cat > "$PKG_DIR/usr/bin/laintas-cli" << 'LAUNCHER'
#!/usr/bin/env bash
# laintas-cli launcher — ensures deps, then runs the agent

INSTALL_DIR="/usr/lib/laintas_cli"
WORKSPACE="${LAINTAS_WORKSPACE:-$HOME/laintas_workspace}"

# Create workspace on first run
if [ ! -d "$WORKSPACE" ]; then
    mkdir -p "$WORKSPACE"
    echo "Created workspace: $WORKSPACE"
fi

# Check for dependencies; install if pip is available and deps are missing
check_deps() {
    python3 -c "import requests, certifi, rich, prompt_toolkit" 2>/dev/null
}

if ! check_deps; then
    echo "[laintas-cli] Installing Python dependencies..."
    if command -v pip3 &>/dev/null; then
        pip3 install -r "$INSTALL_DIR/requirements.txt" --quiet
    elif command -v pip &>/dev/null; then
        pip install -r "$INSTALL_DIR/requirements.txt" --quiet
    else
        echo "ERROR: pip not found. Install pip and re-run:"
        echo "  sudo apt install python3-pip"
        exit 1
    fi
fi

cd "$WORKSPACE"
exec python3 "$INSTALL_DIR/laintas_cli.py" "$@"
LAUNCHER
chmod 755 "$PKG_DIR/usr/bin/laintas-cli"

# ── Build .deb with fpm ──────────────────────────────────────────────────
echo "Building laintas-cli v${VERSION}..."

fpm \
    -s dir \
    -t deb \
    -n laintas-cli \
    -v "$VERSION" \
    --description "Laintas CLI - Autonomous AI agent for your terminal" \
    --url "https://github.com/lin7c/laintas_cli_pre" \
    --maintainer "Laintas" \
    --license "MIT" \
    --architecture amd64 \
    --depends "python3 >= 3.10" \
    --depends "python3-pip" \
    --after-install "$SCRIPT_DIR/postinst.sh" \
    --before-remove "$SCRIPT_DIR/prerm.sh" \
    -C "$PKG_DIR" \
    -p "$OUTPUT_DIR/laintas-cli_${VERSION}_amd64.deb" \
    usr/

echo ""
echo "Done: $OUTPUT_DIR/laintas-cli_${VERSION}_amd64.deb"
echo "Install: sudo dpkg -i $OUTPUT_DIR/laintas-cli_${VERSION}_amd64.deb"
