#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != Darwin ]]; then
    echo "This package is for macOS" >&2
    exit 1
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="$HOME/.local/bin/laintas-cli"
mkdir -p "$(dirname "$target")"
install -m 755 "$here/laintas-cli" "$target"
echo "Installed $target"
echo 'Add ~/.local/bin to PATH if needed, then run: laintas-cli'
