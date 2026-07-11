#!/usr/bin/env bash
# build_download_assets.sh — build the 3 local download artifacts + self-update
# manifest.
#
# Reads package_manifest.json as the single source of truth for which modules
# and packages to ship. See build/HEADLESS_BROWSER_PACKAGING.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
RELEASE_DIR="$PROJECT_DIR/laintas_cli_download/public/releases/latest"
TMP_DIR="$(mktemp -d)"
PYI_DIST_DIR="$TMP_DIR/pyinstaller-dist"
PYI_BUILD_DIR="$TMP_DIR/pyinstaller-build"
VENV_PYINSTALLER="$PROJECT_DIR/venv/bin/pyinstaller"
MANIFEST="$PROJECT_DIR/package_manifest.json"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

if [ ! -x "$VENV_PYINSTALLER" ]; then
  echo "Missing venv tooling. Expected: $VENV_PYINSTALLER"
  exit 1
fi

if [ ! -f "$MANIFEST" ]; then
  echo "Missing $MANIFEST"
  exit 1
fi

# ── Derive file lists from package_manifest.json ────────────────────────
# Flat .py modules + non-py extra_files → top-level files to bundle.
mapfile -t TOP_FILES < <(python3 -c "
import json, sys
m = json.load(open('$MANIFEST'))
for x in m['modules']:
    print(x + '.py')
for x in m['extra_files']:
    print(x)
")
# Sub-packages (directories) + data_dirs to bundle recursively.
mapfile -t PKG_DIRS < <(python3 -c "
import json
m = json.load(open('$MANIFEST'))
for x in m['packages'] + m['data_dirs']:
    print(x)
")

echo "Top-level files (${#TOP_FILES[@]}):"
printf '  %s\n' "${TOP_FILES[@]}"
echo "Package/data dirs (${#PKG_DIRS[@]}):"
printf '  %s\n' "${PKG_DIRS[@]}"

mkdir -p "$RELEASE_DIR"
rm -f \
  "$RELEASE_DIR/laintas-cli_linux.tar.gz" \
  "$RELEASE_DIR/laintas-cli_macos.tar.gz" \
  "$RELEASE_DIR/laintas-cli_source.zip"

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

# ── Helper: copy a full bundle (top files + pkg dirs) into a dest dir ──
copy_bundle() {
  local dest="$1"
  for file in "${TOP_FILES[@]}"; do
    cp "$PROJECT_DIR/$file" "$dest/"
  done
  for d in "${PKG_DIRS[@]}"; do
    cp -r "$PROJECT_DIR/$d" "$dest/"
  done
}

echo "Building macOS source bundle..."
MAC_PACKAGE_DIR="$TMP_DIR/laintas-cli-macos/laintas-cli"
mkdir -p "$MAC_PACKAGE_DIR"
copy_bundle "$MAC_PACKAGE_DIR"
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
  cp -r "$SCRIPT_DIR"/*.py "$INSTALL_DIR/"
  cp -r "$SCRIPT_DIR"/agent_tools "$SCRIPT_DIR"/context_policy \
        "$SCRIPT_DIR"/diagnostics_adapter "$SCRIPT_DIR"/format_adapter \
        "$SCRIPT_DIR"/patch_adapter "$SCRIPT_DIR"/default_skills "$INSTALL_DIR/" 2>/dev/null || true
  cp "$SCRIPT_DIR"/requirements.txt "$INSTALL_DIR/"
  cat > "$BIN_PATH" <<'LAUNCHER'
#!/usr/bin/env bash
exec python3 /usr/local/lib/laintas_cli/laintas_cli.py "$@"
LAUNCHER
  chmod 755 "$BIN_PATH"
else
  sudo mkdir -p "$INSTALL_DIR"
  sudo cp -r "$SCRIPT_DIR"/*.py "$INSTALL_DIR/"
  sudo cp -r "$SCRIPT_DIR"/agent_tools "$SCRIPT_DIR"/context_policy \
           "$SCRIPT_DIR"/diagnostics_adapter "$SCRIPT_DIR"/format_adapter \
           "$SCRIPT_DIR"/patch_adapter "$SCRIPT_DIR"/default_skills "$INSTALL_DIR/" 2>/dev/null || true
  sudo cp "$SCRIPT_DIR"/requirements.txt "$INSTALL_DIR/"
  printf '%s\n' '#!/usr/bin/env bash' 'exec python3 /usr/local/lib/laintas_cli/laintas_cli.py "$@"' | sudo tee "$BIN_PATH" >/dev/null
  sudo chmod 755 "$BIN_PATH"
fi

python3 -m pip install -r "$SCRIPT_DIR"/requirements.txt

echo "Installed to $BIN_PATH"
echo "Run: laintas-cli"
EOF
chmod 755 "$MAC_PACKAGE_DIR/install.sh"
tar -C "$TMP_DIR/laintas-cli-macos" -czf "$RELEASE_DIR/laintas-cli_macos.tar.gz" laintas-cli

echo "Building source zip..."
SOURCE_PACKAGE_DIR="$TMP_DIR/laintas-cli-source"
mkdir -p "$SOURCE_PACKAGE_DIR"
copy_bundle "$SOURCE_PACKAGE_DIR"
(cd "$TMP_DIR" && zip -qr "$RELEASE_DIR/laintas-cli_source.zip" laintas-cli-source)

echo "Publishing loose source files + manifest for partial self-update (/v update)..."
# The self-updater downloads only the .py files whose sha256 changed. To make
# that possible we publish each module individually under src/ plus a manifest
# that lists version + per-file sha256. Sub-package .py files are published
# under src/<pkg>/<module>.py to preserve their import path.
SRC_DIR="$RELEASE_DIR/src"
rm -rf "$SRC_DIR"
mkdir -p "$SRC_DIR"

# Top-level .py modules
for file in "${TOP_FILES[@]}"; do
  case "$file" in *.py) cp "$PROJECT_DIR/$file" "$SRC_DIR/";; esac
done
# Sub-package .py + their data files (.json)
for d in "${PKG_DIRS[@]}"; do
  if [ -d "$PROJECT_DIR/$d" ]; then
    mkdir -p "$SRC_DIR/$d"
    cp "$PROJECT_DIR"/$d/*.py "$SRC_DIR/$d/" 2>/dev/null || true
    cp "$PROJECT_DIR"/$d/*.json "$SRC_DIR/$d/" 2>/dev/null || true
  fi
done

VERSION="$(python3 -c "import sys; sys.path.insert(0, '$PROJECT_DIR'); from version import __version__; print(__version__)")"
python3 - "$PROJECT_DIR" "$SRC_DIR/manifest.json" "$VERSION" "${TOP_FILES[@]}" "${PKG_DIRS[@]}" <<'PY'
import hashlib, json, os, sys, datetime
project_dir, out_path, version, *items = sys.argv[1:]
manifest = {
    "version": version,
    "released": datetime.date.today().isoformat(),
    "files": {},
}

def add_file(rel, abs_path):
    data = open(abs_path, "rb").read()
    manifest["files"][rel] = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }

# Top-level entries: items may be .py modules or non-py extra_files.
# Only .py (and .json/.txt) are tracked for partial update; binaries are not.
for name in items:
    p = os.path.join(project_dir, name)
    if not os.path.isfile(p):
        continue
    if name.endswith((".py", ".json", ".txt")):
        add_file(name, p)

# Walk sub-packages and data dirs for .py + .json (relative path pkg/file).
for name in items:
    d = os.path.join(project_dir, name)
    if not os.path.isdir(d):
        continue
    for root, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        for f in files:
            if f.endswith((".py", ".json")):
                abs_p = os.path.join(root, f)
                rel = os.path.relpath(abs_p, project_dir)
                add_file(rel, abs_p)

with open(out_path, "w") as fh:
    json.dump(manifest, fh, indent=2)
print(f"  manifest: v{version}, {len(manifest['files'])} files")
PY
# The manifest is also expected at the release-dir root (the updater reads
# releases/<channel>/manifest.json).
cp "$SRC_DIR/manifest.json" "$RELEASE_DIR/manifest.json"

echo "Release assets updated in $RELEASE_DIR"
