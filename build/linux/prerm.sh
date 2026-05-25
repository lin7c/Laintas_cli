#!/usr/bin/env bash
# prerm.sh — run before laintas-cli .deb removal
set -e
echo "Removing laintas-cli..."
# Workspace files in user directories are left intact.
