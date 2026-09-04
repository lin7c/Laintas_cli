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

**The Windows build depends on a second channel.** `/windows install`
downloads `helpwo-kernel.exe` from `helpwo.laintas.com/downloads/`, which is
published by the *Helpwo* build, not this one. Nothing in a laintas_cli
release contains the kernel, so a release cannot break it — but a Helpwo
deploy that drops `latest.json` breaks `/windows install` for every installed
CLI, and this is the only document that would tell you where to look. Check
it is answering before and after a release:

```bash
curl -fsSL https://helpwo.laintas.com/downloads/latest.json | python3 -m json.tool
```

It must name an `asset` that exists beside it and a 64-character `sha256`;
the CLI refuses anything else rather than running an unverified installer.
`Helpwo`'s `npm run build` regenerates the file from what is actually in
`public/downloads/`, so the fix is almost always a redeploy of that site
rather than a change here.

Set the single version number in [version.py](../version.py):

```python
__version__ = "1.23.4"
```

Every version in this document is the one that is current as of writing.
`latest` on GitHub is the highest tag, so publishing an example version
literally — `v1.8.1` against a released `v1.23.4` — creates a release that
never becomes `latest` and that no installed CLI will ever offer.

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
npm run build
cd ..
```

The page links straight at the GitHub release, so the build needs nothing
preserved across it. Release binaries are never committed to this repository
either — `laintas_cli_download/public/releases/` is ignored, because `public/`
is copied into `dist/` wholesale and every sub-agent worktree is a full
checkout, which turned two committed tarballs into gigabytes of duplicates.
Only if you run the optional mirror in section 3 does `dist/releases` hold
anything worth keeping, and that section says what to do about it.

## 2. Create the GitHub release

Commit and push the version tag:

```bash
git add version.py laintas_cli_download/src/components/DownloadSection.jsx
git commit -m "release: v1.23.4"
git tag v1.23.4
git push origin main
git push origin v1.23.4
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
gh release view v1.23.4
```

## 3. Deploy the download page (the release is already published)

**The release is complete once section 2's workflow finishes.** Nothing below
is required to ship a version, and nothing consumes it: `/v`, the download
page and both install scripts all read the GitHub release directly. See
section 4 for why there is only one channel.

What still needs deploying is the *page*, whose nginx document root is:

```text
/root/laintas_cli/laintas_cli_download/dist
```

`npm run build` from section 1 produces it. It serves the site, `install.sh`
and `install.ps1` — not release binaries.

### The optional cli.laintas.com mirror

`scripts/build_release_assets.py` still exists and still works. It needs `gh`
logged in, reads the version from `version.py`, and copies the GitHub release
into `dist/releases/latest/` and `dist/releases/v<version>/`.

Nothing reads those directories today. Run it only to stage a deliberate test
mirror for `LAINTAS_DOWNLOAD_BASE` (section 4), and if you do, move
`dist/releases` aside around any later `npm run build` — Vite empties `dist`
and will delete it:

```bash
python3 scripts/build_release_assets.py
# ...and around a later page rebuild:
mv laintas_cli_download/dist/releases /tmp/laintas-release-assets
(cd laintas_cli_download && npm run build)
mv /tmp/laintas-release-assets laintas_cli_download/dist/releases
```

Verify a mirror the same way the release itself is verified:

```bash
for dir in \
  laintas_cli_download/dist/releases/v1.23.4 \
  laintas_cli_download/dist/releases/latest; do
  (cd "$dir" && sha256sum -c SHA256SUMS.txt)
  python3 -c "import json; print(json.load(open('$dir/manifest.json'))['version'])"
done
```

Both manifests must report the version being released, e.g. `1.23.4`.

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
curl -fsSIL https://helpwo.laintas.com/downloads/latest.json
```

Confirm that:

- the version in `latest/manifest.json` is the new one
- amd64, arm64, the source bundle, the Windows installer and the .deb all
  return `200`
- the download page shows the new version and its cards link at the new tag
- `src_manifest.zip` matches the file checksums in the manifest
- `downloads/latest.json` still resolves — the Windows build's `/windows
  install` reads it, and it is published by a different repository

`/v` reads the GitHub release, not `cli.laintas.com` — the two `curl` checks
against that host above cover the install scripts the site serves, and
nothing else.

Static file updates need no nginx reload; only a configuration change does:

```bash
nginx -t && nginx -s reload
```

## 6. Troubleshooting

### The page loads but the download links point at the old version

The cards build their URLs from the tag the page looks up through the GitHub
API, and fall back to `RELEASE_FALLBACK` until that answers. Check
`RELEASE_FALLBACK` (not `RELEASE_VERSION` — no such constant) and
`RELEASE_BASE` in `DownloadSection.jsx`, then rebuild the download page.

### `/v` reports an old manifest version

Check the release's own `manifest.json`:

```bash
curl -fsSL https://github.com/lin7c/Laintas_cli/releases/latest/download/manifest.json
```

If that is the new version and `/v` still is not, the CLI is pinned:
`LAINTAS_UPDATE_CHANNEL` or `LAINTAS_DOWNLOAD_BASE` is set in its environment.

### The install script returns 404

Make sure `latest/` uses the architecture-suffixed filenames:

```text
laintas-cli_linux_amd64.tar.gz
laintas-cli_linux_arm64.tar.gz
laintas-cli_windows_amd64_setup.exe
```

### The release assets vanish after a build

Only applies to the optional mirror in section 3. The Vite build empties
`dist`, so move `dist/releases` aside before the build and restore it
afterwards. A published release is on GitHub and is unaffected by any local
build.
