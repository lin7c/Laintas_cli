#!/usr/bin/env bash
# release.sh — one-command release for laintas-cli.
#
# Usage:  ./scripts/release.sh 1.3.0
#
# What it does:
#   1. Verify the working tree is clean (no uncommitted changes).
#   2. Bump version.py to the given version (single source of truth).
#   3. Commit + tag + push to main (triggers .github/workflows/release.yml).
#   4. Watch the CI run and print the GitHub Release URL when done.
#
# CI then builds Linux amd64/arm64, the single-file Windows amd64 installer
# (private WSL runtime), and source artifacts, publishes them to a GitHub
# Release, and uploads the self-update src/ manifest.
# No binaries are committed to the repo — they live in the Release assets.
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <version>   (e.g. 1.3.0)"
  exit 1
fi

VERSION="$1"
TAG="v${VERSION}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

cd "$PROJECT_DIR"

# 1. Clean tree check
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "[!] Working tree has uncommitted changes. Commit or stash first."
  git status --short
  exit 1
fi

# 2. Bump version.py
echo "[*] Bumping version.py → $VERSION"
python3 -c "
import re, sys
v = sys.argv[1]
p = 'version.py'
s = open(p).read()
s = re.sub(r'__version__\s*=\s*[\"\\'][^\"\\']+[\"\\']', f'__version__ = \"{v}\"', s)
open(p, 'w').write(s)
" "$VERSION"
git add version.py

# 3. Commit + tag + push
echo "[*] Committing release $TAG"
git commit -m "release: $TAG" >/dev/null

echo "[*] Tagging $TAG"
git tag "$TAG"

echo "[*] Pushing to origin main + tags"
git push origin main
git push origin "$TAG"

# 3b. Publish the self-hosted /v update assets to the cli.laintas.com docroot.
# /v reads from cli.laintas.com (not GitHub) at runtime, so regenerate the
# manifest.json + src_manifest.zip for `latest/` and `v$VERSION/` on this box.
echo "[*] Publishing self-hosted update assets to cli.laintas.com"
python3 "$PROJECT_DIR/scripts/build_release_assets.py"

# 4. Watch CI
echo "[*] CI triggered. Watching run..."
if command -v gh >/dev/null 2>&1; then
  sleep 5
  gh run watch "$(gh run list --workflow=release.yml --limit 1 --json databaseId -q '.[0].databaseId')" \
    --exit-status 2>/dev/null || true
  echo ""
  echo "[+] Release $TAG created:"
  gh release view "$TAG" --json url -q '.url' 2>/dev/null || echo "    https://github.com/lin7c/Laintas_cli/releases/tag/$TAG"
else
  echo "[i] gh CLI not found. Monitor manually:"
  echo "    https://github.com/lin7c/Laintas_cli/actions"
  echo "    Release: https://github.com/lin7c/Laintas_cli/releases/tag/$TAG"
fi
