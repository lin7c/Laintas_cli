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
if [ -z "$PREBUILT_LAUNCHER" ]; then
  for tool in x86_64-w64-mingw32-g++ x86_64-w64-mingw32-windres; do
    command -v "$tool" >/dev/null 2>&1 || {
      echo "$tool is required (Debian/Ubuntu: apt install g++-mingw-w64-x86-64)" >&2
      exit 2
    }
  done
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
  # The icon is a linked resource rather than something the installer sets
  # afterwards: the launcher is what the user pins, alt-tabs to and double
  # clicks, and an icon that lives only in the shortcut is lost the moment
  # they make their own.
  x86_64-w64-mingw32-windres \
    --include-dir "$PROJECT_DIR/build/windows" \
    "$PROJECT_DIR/build/windows/launcher.rc" \
    -O coff -o "$WORK_DIR/launcher.res.o"
  x86_64-w64-mingw32-g++ \
    -std=c++17 -Os -s -municode -static-libgcc -static-libstdc++ \
    "$PROJECT_DIR/build/windows/launcher.cpp" \
    "$WORK_DIR/launcher.res.o" \
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
# The icon as a standalone file (a Windows Terminal profile references it by
# path, and the copy linked into the launcher is not reachable that way) and
# the profile itself.
cp "$PROJECT_DIR/build/windows/icon.ico" "$WORK_DIR/package/icon.ico"
cp "$PROJECT_DIR/build/windows/terminal-fragment.json" "$WORK_DIR/package/terminal-fragment.json"
cp "$LINUX_BINARY" "$WORK_DIR/package/laintas-cli-linux"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
cp -a "$WORK_DIR/package/." "$OUT_DIR/"

echo "Built Windows installer payload: $OUT_DIR"
