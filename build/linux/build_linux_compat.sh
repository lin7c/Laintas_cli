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
# Usage (from project root):  bash build/linux/build_linux_compat.sh
# Output: build/linux/dist-compat/laintas-cli
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
OUT_DIR="$PROJECT_DIR/build/linux/dist-compat"
IMAGE="${1:-python:3.11-slim-buster}"

mkdir -p "$OUT_DIR"

echo "== building in $IMAGE =="
docker run --rm \
  -v "$PROJECT_DIR":/src \
  -v "$OUT_DIR":/out \
  -w /src \
  "$IMAGE" \
  bash -euo pipefail -c '
    python -c "import ssl; print(\"ssl OK:\", ssl.OPENSSL_VERSION)"
    python -m pip install --upgrade pip >/dev/null
    python -m pip install --prefer-binary pyinstaller requests certifi rich prompt_toolkit >/dev/null
    rm -rf /tmp/b /tmp/d
    python -m PyInstaller \
      --noconfirm --onefile --name laintas-cli \
      --distpath /tmp/d --workpath /tmp/b --specpath /tmp \
      --collect-data certifi \
      --runtime-hook /src/build/windows/hook_ssl.py \
      --hidden-import requests \
      --hidden-import certifi \
      --hidden-import rich.console \
      --hidden-import rich.panel \
      --hidden-import rich.markdown \
      --hidden-import rich.table \
      --hidden-import rich.live \
      --hidden-import rich.spinner \
      --hidden-import rich.text \
      --hidden-import rich.padding \
      --hidden-import prompt_toolkit.application \
      --hidden-import prompt_toolkit.history \
      --hidden-import prompt_toolkit.completion \
      --hidden-import prompt_toolkit.key_binding \
      --hidden-import prompt_toolkit.layout \
      --hidden-import prompt_toolkit.styles \
      --hidden-import prompt_toolkit.auto_suggest \
      /src/laintas_cli.py
    cp /tmp/d/laintas-cli /out/laintas-cli
    chmod 755 /out/laintas-cli
    echo "== done =="
  '

echo "Built: $OUT_DIR/laintas-cli"
