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
  workflow_engine.py
)

mkdir -p "$RELEASE_DIR"
rm -f \
  "$RELEASE_DIR/laintas-cli_linux.tar.gz" \
  "$RELEASE_DIR/laintas-cli_macos.tar.gz" \
  "$RELEASE_DIR/laintas-cli_source.zip" \
  "$RELEASE_DIR/laintas_cli.exe"

echo "Building Linux standalone binary with PyInstaller..."
"$VENV_PYINSTALLER" \
  --noconfirm \
  --onefile \
  --name laintas-cli \
  --distpath "$PYI_DIST_DIR" \
  --workpath "$PYI_BUILD_DIR" \
  --specpath "$TMP_DIR" \
  --collect-data certifi \
  --runtime-hook "$PROJECT_DIR/build/windows/hook_ssl.py" \
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
  "$PROJECT_DIR/laintas_cli.py"

LINUX_PACKAGE_DIR="$TMP_DIR/laintas-cli"
mkdir -p "$LINUX_PACKAGE_DIR"
cp "$PYI_DIST_DIR/laintas-cli" "$LINUX_PACKAGE_DIR/laintas-cli"
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

echo "Release assets updated in $RELEASE_DIR"
