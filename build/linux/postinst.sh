#!/usr/bin/env bash
# postinst.sh — run after laintas-cli .deb install

set -e

echo ""
echo "── Laintas CLI ────────────────────────────────────────────────"
echo "  Installed to: /usr/lib/laintas_cli/"
echo "  Launcher:     /usr/bin/laintas-cli"
echo "  Workspace:    ~/laintas_workspace  (created on first run)"
echo ""

# Create default workspace for the installing user (if running as root via sudo)
REAL_USER="${SUDO_USER:-}"
if [ -n "$REAL_USER" ] && [ "$REAL_USER" != "root" ]; then
    WORKSPACE="/home/$REAL_USER/laintas_workspace"
    if [ ! -d "$WORKSPACE" ]; then
        mkdir -p "$WORKSPACE"
        chown "$REAL_USER:$REAL_USER" "$WORKSPACE"
        echo "  Workspace created: $WORKSPACE"
    fi
fi

# Pre-install pip deps so it's ready out of the box
echo "  Installing Python dependencies..."
if command -v pip3 &>/dev/null; then
    pip3 install -r /usr/lib/laintas_cli/requirements.txt --quiet 2>/dev/null || true
fi

echo ""
echo "  Run 'laintas-cli' to start."
echo "── ────────────────────────────────────────────────────────────"
