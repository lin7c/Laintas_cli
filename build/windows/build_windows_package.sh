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
for command in docker gzip curl unzip sha256sum; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "$command is required to build the Windows package" >&2
    exit 2
  }
done

# Windows Terminal, bundled for machines that have none of their own. conhost
# delivers mouse input only as INPUT_RECORDs and never as VT sequences, which
# a WSL process cannot receive at all, so on Windows 10 there is no terminal
# on the machine that can run this CLI properly. Pinned by version and hash:
# an unpinned download makes the installer's contents depend on the day it
# was built, and the hash is what makes the download safe to trust.
WT_VERSION="${LAINTAS_WT_VERSION:-1.24.11911.0}"
WT_SHA256="${LAINTAS_WT_SHA256:-7691efeb71c8dd0b95536c84e366fa4cf809a42c534912f9cefa1056534383bd}"
WT_ZIP_NAME="Microsoft.WindowsTerminal_${WT_VERSION}_x64.zip"
WT_URL="https://github.com/microsoft/terminal/releases/download/v${WT_VERSION}/${WT_ZIP_NAME}"
WT_CACHE="${LAINTAS_WT_CACHE:-$PROJECT_DIR/build/windows/.cache}"

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
cp "$PROJECT_DIR/build/windows/terminal-settings.json" "$WORK_DIR/package/terminal-settings.json"
cp "$LINUX_BINARY" "$WORK_DIR/package/laintas-cli-linux"

echo "==> Fetching Windows Terminal $WT_VERSION"
mkdir -p "$WT_CACHE"
WT_ZIP="$WT_CACHE/$WT_ZIP_NAME"
if [ ! -f "$WT_ZIP" ] || ! echo "$WT_SHA256  $WT_ZIP" | sha256sum -c - >/dev/null 2>&1; then
  curl -fsSL "$WT_URL" -o "$WT_ZIP.part"
  mv "$WT_ZIP.part" "$WT_ZIP"
fi
echo "$WT_SHA256  $WT_ZIP" | sha256sum -c - >/dev/null || {
  echo "Windows Terminal download does not match the pinned hash" >&2
  exit 2
}
# Flatten the archive's single versioned top directory so the installer does
# not have to know the version, and drop the shell extension: registering a
# context-menu handler is not something a bundled copy should be doing.
unzip -q "$WT_ZIP" -d "$WORK_DIR/wt"
WT_ROOT="$(find "$WORK_DIR/wt" -maxdepth 1 -mindepth 1 -type d | head -n 1)"
[ -x "$WT_ROOT/WindowsTerminal.exe" ] || [ -f "$WT_ROOT/WindowsTerminal.exe" ] || {
  echo "Windows Terminal archive did not contain WindowsTerminal.exe" >&2
  exit 2
}
rm -f "$WT_ROOT/WindowsTerminalShellExt.dll"
mkdir -p "$WORK_DIR/package/terminal"
cp -a "$WT_ROOT/." "$WORK_DIR/package/terminal/"
# The portable-mode marker: settings live beside the executable, so this copy
# never reads or writes the user's own Windows Terminal configuration.
: > "$WORK_DIR/package/terminal/.portable"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
cp -a "$WORK_DIR/package/." "$OUT_DIR/"

echo "Built Windows installer payload: $OUT_DIR"
