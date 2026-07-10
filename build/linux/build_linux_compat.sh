#!/usr/bin/env bash
# Build the Linux onefile binary inside an OLD-glibc official python image so the
# result runs on older servers. PyInstaller binaries are only FORWARD compatible
# across glibc: building on the host (glibc 2.39) produced a binary that died
# with "GLIBC_2.38 not found" on older boxes.
#
# Official python images ship a SHARED libpython + working ssl, so no compiling.
# buster = Debian 10 = glibc 2.28 (covers CentOS 8 / Anolis 8 / Aliyun Linux 3 /
# Ubuntu 20.04+). Falls back to bullseye (glibc 2.31) if buster is unavailable.
#
# Usage (from project root):  bash build/linux/build_linux_compat.sh [amd64|arm64]
# Output: build/linux/dist-compat/<arch>/laintas-cli
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
ARCH="${1:-amd64}"
case "$ARCH" in
  amd64) PLATFORM="linux/amd64" ;;
  arm64) PLATFORM="linux/arm64" ;;
  *) echo "Unsupported architecture: $ARCH (expected amd64 or arm64)" >&2; exit 2 ;;
esac
OUT_DIR="$PROJECT_DIR/build/linux/dist-compat/$ARCH"
IMAGE="${BUILD_IMAGE:-python:3.11-slim-buster}"

mkdir -p "$OUT_DIR"

echo "== building in $IMAGE =="
docker run --rm \
  --platform "$PLATFORM" \
  -v "$PROJECT_DIR":/src \
  -v "$OUT_DIR":/out \
  -w /src \
  "$IMAGE" \
  bash -euo pipefail -c '
    python -c "import ssl; print(\"ssl OK:\", ssl.OPENSSL_VERSION)"
    # PyInstaller needs objdump (binutils); slim images omit it. buster is EOL so
    # apt sources moved to archive.debian.org.
    sed -i "s|deb.debian.org|archive.debian.org|g; s|security.debian.org|archive.debian.org|g; /buster-updates/d" /etc/apt/sources.list 2>/dev/null || true
    apt-get -o Acquire::Check-Valid-Until=false update >/dev/null 2>&1 || apt-get update >/dev/null 2>&1 || true
    apt-get install -y --no-install-recommends binutils >/dev/null 2>&1
    command -v objdump >/dev/null || { echo "objdump still missing"; exit 1; }
    python -m pip install --upgrade pip >/dev/null
    python -m pip install --prefer-binary pyinstaller requests certifi rich prompt_toolkit \
        websockets aiortc >/dev/null
    rm -rf /tmp/b /tmp/d
    # Use the spec file (reads package_manifest.json for datas/hiddenimports).
    python -m PyInstaller \
      --noconfirm \
      --distpath /tmp/d --workpath /tmp/b \
      /src/build/linux/laintas_cli.spec
    cp /tmp/d/laintas-cli /out/laintas-cli
    chmod 755 /out/laintas-cli
    echo "== done =="
  '

echo "Built: $OUT_DIR/laintas-cli"
