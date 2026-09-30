#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != Darwin ]]; then
    echo "macOS packaging must run on a Mac" >&2
    exit 1
fi

case "$(uname -m)" in
    arm64) arch=arm64 ;;
    x86_64) arch=amd64 ;;
    *) echo "Unsupported Mac architecture: $(uname -m)" >&2; exit 1 ;;
esac

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo"
python3 -m PyInstaller --noconfirm --clean build/mac/laintas_cli.spec
binary="$repo/dist/laintas-cli"
test -x "$binary"
"$binary" --version
"$binary" --help >/dev/null

staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
cp "$binary" "$staging/laintas-cli"
cp LICENSE "$staging/LICENSE"
cp build/mac/install.sh "$staging/install.sh"
chmod 755 "$staging/install.sh"
tar -C "$staging" -czf "$repo/laintas-cli_darwin_${arch}.tar.gz" .
echo "Built laintas-cli_darwin_${arch}.tar.gz"
