#!/usr/bin/env bash
# Build the Linux onefile binary against an OLD glibc (2.17) so the result runs
# on old servers (CentOS 7+, Alibaba Cloud Linux 2/3, etc.).
#
# Why: PyInstaller binaries are only FORWARD compatible across glibc — you must
# build on the oldest glibc you want to support. Building on the host (glibc
# 2.39) produced a binary that died with "GLIBC_2.38 not found" on older boxes.
#
# manylinux2014 (CentOS 7, glibc 2.17) is the base, but its bundled Pythons are
# static; PyInstaller needs a SHARED libpython, so we compile CPython 3.11 with
# --enable-shared inside the container.
#
# Usage (from project root):  bash build/linux/build_in_manylinux.sh
# Output: build/linux/dist-manylinux/laintas-cli
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE="quay.io/pypa/manylinux2014_x86_64"
OUT_DIR="$PROJECT_DIR/build/linux/dist-manylinux"
PYVER="3.11.9"

mkdir -p "$OUT_DIR"

docker run --rm \
  -v "$PROJECT_DIR":/src \
  -v "$OUT_DIR":/out \
  -w /src \
  -e PYVER="$PYVER" \
  "$IMAGE" \
  bash -euo pipefail -c '
    echo "== building CPython $PYVER --enable-shared (glibc 2.17) =="
    cd /tmp
    curl -fsSLO "https://www.python.org/ftp/python/$PYVER/Python-$PYVER.tgz"
    tar xf "Python-$PYVER.tgz"
    cd "Python-$PYVER"
    ./configure --enable-shared --prefix=/opt/py --with-ensurepip=install \
      LDFLAGS="-Wl,-rpath,/opt/py/lib" >/tmp/cfg.log 2>&1
    make -j"$(nproc)" >/tmp/make.log 2>&1
    make install >/tmp/install.log 2>&1
    export LD_LIBRARY_PATH=/opt/py/lib
    PY=/opt/py/bin/python3
    "$PY" -m pip install --upgrade pip >/dev/null
    "$PY" -m pip install --prefer-binary pyinstaller requests certifi rich prompt_toolkit >/dev/null
    echo "== running PyInstaller =="
    rm -rf /tmp/b /tmp/d
    "$PY" -m PyInstaller \
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
