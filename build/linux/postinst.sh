#!/usr/bin/env bash
# postinst.sh — run after laintas-cli .deb install
# The binary is self-contained (PyInstaller onefile); no pip step needed.

set -e

echo ""
echo "── Laintas CLI ────────────────────────────────────────────────"
echo "  Binary:    /usr/bin/laintas-cli"
echo "  Workspace: ~/laintas_workspace  (created on first run)"
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

echo ""
echo "  Run 'laintas-cli' to start."
echo "── ────────────────────────────────────────────────────────────"
