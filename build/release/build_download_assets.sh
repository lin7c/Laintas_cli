#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
RELEASE_DIR="$PROJECT_DIR/laintas_cli_download/public/releases/latest"
TMP_DIR="$(mktemp -d)"
PYI_DIST_DIR="$TMP_DIR/pyinstaller-dist"
PYI_BUILD_DIR="$TMP_DIR/pyinstaller-build"
VENV_PYINSTALLER="$PROJECT_DIR/venv/bin/pyinstaller"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

if [ ! -x "$VENV_PYINSTALLER" ]; then
  echo "Missing venv tooling. Expected: $VENV_PYINSTALLER"
  exit 1
fi

SOURCE_FILES=(
  agent_loop.py
  agent_persistence.py
  agent_roles.py
  cloud_provider.py
  hooks.py
  hwo_runner.py
  hwo_ui.py
  laintas_cli.py
  mcp_client.py
  memory_system.py
  migrate.py
  paths.py
  plan_mode.py
  policy.py
  PROJECT.md
  requirements.txt
  setup.py
  skills.py
  task_manager.py
  tools.py
  updater.py
  version.py
  workflow_engine.py
)

mkdir -p "$RELEASE_DIR"
rm -f \
  "$RELEASE_DIR/laintas-cli_linux.tar.gz" \
  "$RELEASE_DIR/laintas-cli_macos.tar.gz" \
  "$RELEASE_DIR/laintas-cli_source.zip" \
  "$RELEASE_DIR/laintas_cli.exe"

echo "Building Linux standalone binary in old-glibc container..."
# IMPORTANT: do NOT build the Linux binary with the host's PyInstaller — the
# build host's glibc (2.39) is newer than most servers, producing a binary that
# dies with "GLIBC_2.x not found" on older boxes. Build in a glibc-2.28 container
# instead (see build/linux/build_linux_compat.sh).
bash "$PROJECT_DIR/build/linux/build_linux_compat.sh"
COMPAT_BINARY="$PROJECT_DIR/build/linux/dist-compat/laintas-cli"
if [ ! -x "$COMPAT_BINARY" ]; then
  echo "Container build did not produce $COMPAT_BINARY"
  exit 1
fi

LINUX_PACKAGE_DIR="$TMP_DIR/laintas-cli"
mkdir -p "$LINUX_PACKAGE_DIR"
cp "$COMPAT_BINARY" "$LINUX_PACKAGE_DIR/laintas-cli"
chmod 755 "$LINUX_PACKAGE_DIR/laintas-cli"
cat > "$LINUX_PACKAGE_DIR/install.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="/usr/local/bin/laintas-cli"

if [ ! -x "$SCRIPT_DIR/laintas-cli" ]; then
  echo "Missing binary: $SCRIPT_DIR/laintas-cli"
  exit 1
fi

if [ "$(id -u)" -eq 0 ]; then
  install -m 755 "$SCRIPT_DIR/laintas-cli" "$TARGET"
else
  sudo install -m 755 "$SCRIPT_DIR/laintas-cli" "$TARGET"
fi

echo "Installed to $TARGET"
echo "Run: laintas-cli"
EOF
chmod 755 "$LINUX_PACKAGE_DIR/install.sh"
tar -C "$TMP_DIR" -czf "$RELEASE_DIR/laintas-cli_linux.tar.gz" laintas-cli

echo "Building macOS source bundle..."
MAC_PACKAGE_DIR="$TMP_DIR/laintas-cli-macos/laintas-cli"
mkdir -p "$MAC_PACKAGE_DIR"
for file in "${SOURCE_FILES[@]}"; do
  cp "$PROJECT_DIR/$file" "$MAC_PACKAGE_DIR/"
done
cat > "$MAC_PACKAGE_DIR/install.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/usr/local/lib/laintas_cli"
BIN_PATH="/usr/local/bin/laintas-cli"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required"
  exit 1
fi

if [ "$(id -u)" -eq 0 ]; then
  mkdir -p "$INSTALL_DIR"
  cp "$SCRIPT_DIR"/*.py "$INSTALL_DIR/"
  cp "$SCRIPT_DIR"/requirements.txt "$INSTALL_DIR/"
  cat > "$BIN_PATH" <<'LAUNCHER'
#!/usr/bin/env bash
exec python3 /usr/local/lib/laintas_cli/laintas_cli.py "$@"
LAUNCHER
  chmod 755 "$BIN_PATH"
else
  sudo mkdir -p "$INSTALL_DIR"
  sudo cp "$SCRIPT_DIR"/*.py "$INSTALL_DIR/"
  sudo cp "$SCRIPT_DIR"/requirements.txt "$INSTALL_DIR/"
  printf '%s\n' '#!/usr/bin/env bash' 'exec python3 /usr/local/lib/laintas_cli/laintas_cli.py "$@"' | sudo tee "$BIN_PATH" >/dev/null
  sudo chmod 755 "$BIN_PATH"
fi

python3 -m pip install -r "$SCRIPT_DIR/requirements.txt"

echo "Installed to $BIN_PATH"
echo "Run: laintas-cli"
EOF
chmod 755 "$MAC_PACKAGE_DIR/install.sh"
tar -C "$TMP_DIR/laintas-cli-macos" -czf "$RELEASE_DIR/laintas-cli_macos.tar.gz" laintas-cli

echo "Building source zip..."
SOURCE_PACKAGE_DIR="$TMP_DIR/laintas-cli-source"
mkdir -p "$SOURCE_PACKAGE_DIR"
for file in "${SOURCE_FILES[@]}"; do
  cp "$PROJECT_DIR/$file" "$SOURCE_PACKAGE_DIR/"
done
(cd "$TMP_DIR" && zip -qr "$RELEASE_DIR/laintas-cli_source.zip" laintas-cli-source)

echo "Refreshing Windows executable from checked-in artifact..."
cp "$PROJECT_DIR/build/windows/laintas_cli.exe" "$RELEASE_DIR/laintas_cli.exe"

echo "Publishing loose source files + manifest for partial self-update (/v update)..."
# The self-updater downloads only the .py files whose sha256 changed. To make
# that possible we publish each module individually under src/ plus a manifest
# that lists version + per-file sha256.
SRC_DIR="$RELEASE_DIR/src"
rm -rf "$SRC_DIR"
mkdir -p "$SRC_DIR"
for file in "${SOURCE_FILES[@]}"; do
  case "$file" in *.py) cp "$PROJECT_DIR/$file" "$SRC_DIR/";; esac
done

VERSION="$(python3 -c "import sys; sys.path.insert(0, '$PROJECT_DIR'); from version import __version__; print(__version__)")"
python3 - "$PROJECT_DIR" "$SRC_DIR/manifest.json" "$VERSION" "${SOURCE_FILES[@]}" <<'PY'
import hashlib, json, os, sys, datetime
project_dir, out_path, version, *files = sys.argv[1:]
manifest = {
    "version": version,
    "released": datetime.date.today().isoformat(),
    "files": {},
}
for name in files:
    if not name.endswith(".py"):
        continue
    p = os.path.join(project_dir, name)
    data = open(p, "rb").read()
    manifest["files"][name] = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }
with open(out_path, "w") as fh:
    json.dump(manifest, fh, indent=2)
print(f"  manifest: v{version}, {len(manifest['files'])} files")
PY
# The manifest is also expected at the release-dir root (the updater reads
# releases/<channel>/manifest.json).
cp "$SRC_DIR/manifest.json" "$RELEASE_DIR/manifest.json"

echo "Release assets updated in $RELEASE_DIR"
