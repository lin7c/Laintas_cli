#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
ARCH="${1:-amd64}"
LINUX_BINARY="${2:-$PROJECT_DIR/build/linux/dist-compat/$ARCH/laintas-cli}"
OUT_DIR="${WINDOWS_OUT_DIR:-$PROJECT_DIR/build/windows/payload}"
PREBUILT_LAUNCHER="${WINDOWS_LAUNCHER:-}"

if [ "$ARCH" != "amd64" ]; then
  echo "Unsupported Windows package architecture: $ARCH (currently amd64 only)" >&2
  exit 2
fi
if [ ! -x "$LINUX_BINARY" ]; then
  echo "Linux runtime binary not found or not executable: $LINUX_BINARY" >&2
  exit 2
fi
if [ -z "$PREBUILT_LAUNCHER" ] && ! command -v x86_64-w64-mingw32-g++ >/dev/null 2>&1; then
  echo "x86_64-w64-mingw32-g++ is required (Debian/Ubuntu: apt install g++-mingw-w64-x86-64)" >&2
  exit 2
fi
if [ -n "$PREBUILT_LAUNCHER" ] && [ ! -f "$PREBUILT_LAUNCHER" ]; then
  echo "Prebuilt Windows launcher not found: $PREBUILT_LAUNCHER" >&2
  exit 2
fi
for command in docker gzip; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "$command is required to build the Windows package" >&2
    exit 2
  }
done

WORK_DIR="$(mktemp -d -t laintas-windows-build.XXXXXX)"
CONTAINER_ID=""
IMAGE_TAG="laintas-wsl-rootfs-$$"
cleanup() {
  if [ -n "$CONTAINER_ID" ]; then
    docker rm -f "$CONTAINER_ID" >/dev/null 2>&1 || true
  fi
  docker image rm "$IMAGE_TAG" >/dev/null 2>&1 || true
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

mkdir -p "$WORK_DIR/rootfs" "$WORK_DIR/package"
cp "$PROJECT_DIR/build/windows/Dockerfile.rootfs" "$WORK_DIR/rootfs/Dockerfile"
cp "$PROJECT_DIR/build/windows/wsl.conf" "$WORK_DIR/rootfs/wsl.conf"
cp "$PROJECT_DIR/build/windows/wsl-distribution.conf" "$WORK_DIR/rootfs/wsl-distribution.conf"

echo "==> Building native Windows launcher"
if [ -n "$PREBUILT_LAUNCHER" ]; then
  cp "$PREBUILT_LAUNCHER" "$WORK_DIR/package/laintas-cli.exe"
else
  x86_64-w64-mingw32-g++ \
    -std=c++17 -Os -s -municode -static-libgcc -static-libstdc++ \
    "$PROJECT_DIR/build/windows/launcher.cpp" \
    -o "$WORK_DIR/package/laintas-cli.exe" \
    -lwslapi
fi

echo "==> Building private WSL root filesystem"
docker build --platform linux/amd64 --tag "$IMAGE_TAG" "$WORK_DIR/rootfs"
CONTAINER_ID="$(docker create --platform linux/amd64 "$IMAGE_TAG")"
docker export "$CONTAINER_ID" | gzip -9 > "$WORK_DIR/package/laintas-rootfs.tar.gz"
docker rm "$CONTAINER_ID" >/dev/null
CONTAINER_ID=""

cp "$PROJECT_DIR/build/windows/install.ps1" "$WORK_DIR/package/install.ps1"
cp "$PROJECT_DIR/build/windows/uninstall.ps1" "$WORK_DIR/package/uninstall.ps1"
cp "$LINUX_BINARY" "$WORK_DIR/package/laintas-cli-linux"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
cp -a "$WORK_DIR/package/." "$OUT_DIR/"

echo "Built Windows installer payload: $OUT_DIR"
