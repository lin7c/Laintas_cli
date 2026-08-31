# Laintas CLI release and sync procedure

How to publish a version to GitHub and sync it to `cli.laintas.com`, so that
the download page, the install script and the CLI's `/v` update command all
use the same set of release assets.

## 1. Pre-release checks

From the repository root:

```bash
git status
python3 -m py_compile version.py
```

**Top-level module registration.** `package_manifest.json` is the single
source of truth for every packaging path (setup.py, the PyInstaller spec, the
CI source bundle, and the `/v` self-update manifest). A new top-level `.py`
module must be added to `modules`, or it ships in no release artifact at all
and an installed CLI raises `ImportError` at runtime. Check before releasing:

```bash
python3 - <<'PY'
import json, os
pm = json.load(open("package_manifest.json"))
modules = set(pm["modules"])
top_py = sorted(f[:-3] for f in os.listdir(".") if f.endswith(".py") and os.path.isfile(f))
missing = [m for m in top_py if m not in modules and m != "setup"]
assert not missing, f"top-level modules not registered in package_manifest.json: {missing}"
print("package_manifest.json is complete: every top-level module is registered")
PY
```

The local diagnostic modules currently registered (`event_log` / `critic` /
`precheck` / `rag_signals` / `mem_signals` / `stuck_signals` / `redactor`)
serve CLI recovery and local diagnostics only; they upload nothing to the
training pipeline.

Set the single version number in [version.py](../version.py):

```python
__version__ = "1.8.1"
```

The download page's version and download URLs live in:

```text
laintas_cli_download/src/components/DownloadSection.jsx
```

Releasing a new version means updating:

- `RELEASE_FALLBACK`: `v1.23.4` — the version shown until the page has
  answered from the GitHub API, so keep it in step with `version.py`
- the version shown in the page's compatibility section

`RELEASE_BASE` does not move between releases: it is the release channel's
rolling `latest/download` pointer, and the cards build their filenames from
the tag the page looked up.

Then build the download page:

```bash
cd laintas_cli_download
mv dist/releases /tmp/laintas-release-assets
npm run build
mv /tmp/laintas-release-assets dist/releases
cd ..
```

`dist/releases` must be moved aside and restored around the build: Vite's
clean step deletes the self-hosted installers and the `/v` update packages
otherwise.

## 2. Create the GitHub release

Commit and push the version tag:

```bash
git add version.py laintas_cli_download/src/components/DownloadSection.jsx
git commit -m "release: v1.8.1"
git tag v1.8.1
git push origin main
git push origin v1.8.1
```

`.github/workflows/release.yml` builds and publishes on the tag push:

- `laintas-cli_linux_amd64.tar.gz`
- `laintas-cli_linux_arm64.tar.gz`
- `laintas-cli_windows_amd64_setup.exe`
- `laintas-cli_source.zip`
- `laintas-cli_<version>_amd64.deb`
- `manifest.json`
- `src_manifest.zip`
- `SHA256SUMS.txt`

Confirm the release finished and is not a draft:

```bash
gh release view v1.8.1
```

## 3. Sync to cli.laintas.com

The nginx document root must be:

```text
/root/laintas_cli/laintas_cli_download/dist
```

Use the sync script to generate the source manifest and pull the binaries
from the GitHub release:

```bash
python3 scripts/build_release_assets.py
```

It needs `gh` to be logged in, reads the version from `version.py`, and
writes:

```text
dist/releases/v1.8.1/
dist/releases/latest/
```

If the `gh` login has expired, download every asset from the GitHub release
by hand and place it in both directories. Both must contain at least:

```text
laintas-cli_linux_amd64.tar.gz
laintas-cli_linux_arm64.tar.gz
laintas-cli_windows_amd64_setup.exe
laintas-cli_source.zip
laintas-cli_<version>_amd64.deb
manifest.json
src_manifest.zip
SHA256SUMS.txt
```

Verify after syncing:

```bash
for dir in \
  laintas_cli_download/dist/releases/v1.8.1 \
  laintas_cli_download/dist/releases/latest; do
  (cd "$dir" && sha256sum -c SHA256SUMS.txt)
  python3 -c "import json; print(json.load(open('$dir/manifest.json'))['version'])"
done
```

Both manifests must report the version being released, e.g. `1.8.1`.

## 4. Where `/v` updates from

`updater.py` is configured with:

```python
DEFAULT_DOWNLOAD_BASE = "https://github.com/lin7c/Laintas_cli"
```

so `/v` reads the same release this workflow publishes:

```text
https://github.com/lin7c/Laintas_cli/releases/latest/download/manifest.json
https://github.com/lin7c/Laintas_cli/releases/latest/download/src_manifest.zip
https://github.com/lin7c/Laintas_cli/releases/latest/download/SHA256SUMS.txt
https://github.com/lin7c/Laintas_cli/releases/latest/download/laintas-cli_linux_amd64.tar.gz
https://github.com/lin7c/Laintas_cli/releases/latest/download/laintas-cli_linux_arm64.tar.gz
```

The site used to self-host these under `cli.laintas.com/releases/<channel>/`,
written by `scripts/build_release_assets.py` during a manual release. Nothing
repopulated that directory once releasing moved into CI, so `/v update`, the
page's download buttons and both install scripts all resolved to 404s against
a channel that had stopped being fed. One channel now, the one CI writes.

A frozen install downloads the **Linux** archive for its architecture on every
platform, Windows included: there the CLI runs as that same binary inside its
private WSL distribution. `laintas-cli.exe` and the distribution are replaced
by re-running the installer, not by `/v`.

To pin a version:

```bash
LAINTAS_UPDATE_CHANNEL=v1.23.2 laintas-cli
```

which reads (GitHub spells a pinned tag differently from `latest`):

```text
https://github.com/lin7c/Laintas_cli/releases/download/v1.23.2/manifest.json
```

`LAINTAS_DOWNLOAD_BASE` points at a test mirror, read with the flat
`<base>/releases/<channel>/<asset>` layout so any static directory serves.

## 5. Post-release verification

```bash
base=https://github.com/lin7c/Laintas_cli/releases/latest/download
curl -fsSL "$base/manifest.json" | python3 -m json.tool
curl -fsSIL "$base/laintas-cli_linux_amd64.tar.gz"
curl -fsSIL "$base/laintas-cli_windows_amd64_setup.exe"
curl -fsSIL https://cli.laintas.com/install.sh
curl -fsSIL https://cli.laintas.com/install.ps1
```

Confirm that:

- the version in `latest/manifest.json` is the new one
- amd64, arm64, the source bundle and the .deb all return `200`
- the download page shows the new version and links into the right version
  directory
- `/v` checks `cli.laintas.com` for updates
- `src_manifest.zip` matches the file checksums in the manifest

Static file updates need no nginx reload; only a configuration change does:

```bash
nginx -t && nginx -s reload
```

## 6. Troubleshooting

### The page loads but the download links point at the old version

Check `DOWNLOAD_BASE` and `RELEASE_VERSION` in `DownloadSection.jsx`, then
rebuild the download page.

### `/v` reports an old manifest version

Check `dist/releases/latest/manifest.json` — updating only the `v1.8.1`
directory is not enough, since `latest` is the default update channel.

### The install script returns 404

Make sure `latest/` uses the architecture-suffixed filenames:

```text
laintas-cli_linux_amd64.tar.gz
laintas-cli_linux_arm64.tar.gz
laintas-cli_windows_amd64_setup.exe
```

### The release assets vanish after a build

The Vite build empties `dist`. Move `dist/releases` aside before the build and
restore it afterwards, or build into a separate directory that does not clear
the release folder.
