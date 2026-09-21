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

## 3. Sync the release to cli.laintas.com and deploy the page

**The release is NOT complete once section 2's workflow finishes.**
cli.laintas.com is the primary download channel: the repository may go
private one day, and every installed CLI's `/v update` must keep working when
it does. The GitHub release is the build origin and a public mirror; the
site is what users actually update from.

Two things must land on the server after every release:

1. **The release assets**, under `dist/releases/latest/` and
   `dist/releases/v<version>/` (the flat layout `LAINTAS_DOWNLOAD_BASE`
   reads).
2. **The page itself**, whose nginx document root is:

```text
/root/laintas_cli/laintas_cli_download/dist
```

`npm run build` from section 1 produces the page. It serves the site,
`install.sh` and `install.ps1`; `dist/releases` serves the binaries.

### Syncing the release assets

`scripts/build_release_assets.py` copies the GitHub release into
`dist/releases/latest/` and `dist/releases/v<version>/`. It needs `gh`
logged in and reads the version from `version.py`.

**Migration status (plan A, decided 2026-09-21):** the sync is to become a
job in `.github/workflows/release.yml` that pushes the assets to the server
right after the GitHub release is published, so the mirror can never starve
again — that starvation is what drove `/v` to GitHub-only in the first
place. Until that job exists, run the sync BY HAND as part of EVERY release;
a release that skips it is incomplete:

```bash
python3 scripts/build_release_assets.py
```

Vite empties `dist` on every build, so around any later `npm run build` move
the assets aside and restore them afterwards:

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

**Target state (plan A):** `updater.py` reads cli.laintas.com:

```python
DEFAULT_DOWNLOAD_BASE = "https://cli.laintas.com"
```

so `/v` reads the mirror section 3 feeds:

```text
https://cli.laintas.com/releases/latest/manifest.json
https://cli.laintas.com/releases/latest/src_manifest.zip
https://cli.laintas.com/releases/latest/SHA256SUMS.txt
https://cli.laintas.com/releases/latest/laintas-cli_linux_amd64.tar.gz
https://cli.laintas.com/releases/latest/laintas-cli_linux_arm64.tar.gz
```

**Migration status:** `DEFAULT_DOWNLOAD_BASE` still points at
`https://github.com/lin7c/Laintas_cli` and flips to cli.laintas.com in the
NEXT release — the same release that ships the CI sync job. Order matters:
the mirror must be fed before the default moves, or every installed CLI
404s on its next `/v`. Until that release, `/v` reads the GitHub release this
workflow publishes.

History: the site used to self-host these under
`cli.laintas.com/releases/<channel>/`, written by
`scripts/build_release_assets.py` during a manual release. Nothing
repopulated that directory once releasing moved into CI, so `/v` was pointed
at GitHub. That made the repository's public-ness a load-bearing dependency;
plan A removes it.

A **git checkout is never an update target.** `/v update` on a source install
compares each file's sha256 against the release manifest, and that comparison
cannot tell "this file is outdated" from "you edited this file an hour ago" —
applied to the repo it is developed in, it silently reverts local work and
reports success. On a work tree the command now refuses, lists the files that
differ and points at `git pull`; `/v update --overwrite-local` is the way to
do it on purpose. A successful ordinary update keeps no backup — it only replaced files that
already match the release. `--overwrite-local` does keep one, under
`.laintas-update-backup/<version>-<stamp>/` (last three sets), because there
the replaced file may be the only copy of an edit.

A frozen install downloads the **Linux** archive for its architecture on every
platform, Windows included: there the CLI runs as that same binary inside its
private WSL distribution. `laintas-cli.exe` and the distribution are replaced
by re-running the installer, not by `/v`.

To pin a version:

```bash
LAINTAS_UPDATE_CHANNEL=v1.23.2 laintas-cli
```

which reads (after the plan-A release, from the mirror's flat layout):

```text
https://cli.laintas.com/releases/v1.23.2/manifest.json
```

`LAINTAS_DOWNLOAD_BASE` points at a test mirror, read with the flat
`<base>/releases/<channel>/<asset>` layout so any static directory serves.

## 5. Post-release verification

```bash
base=https://github.com/lin7c/Laintas_cli/releases/latest/download
mirror=https://cli.laintas.com/releases/latest
curl -fsSL "$base/manifest.json" | python3 -m json.tool
curl -fsSL "$mirror/manifest.json" | python3 -m json.tool
curl -fsSIL "$base/laintas-cli_linux_amd64.tar.gz"
curl -fsSIL "$mirror/laintas-cli_linux_amd64.tar.gz"
curl -fsSIL "$mirror/laintas-cli_windows_amd64_setup.exe"
curl -fsSIL https://cli.laintas.com/install.sh
curl -fsSIL https://cli.laintas.com/install.ps1
curl -fsSIL https://helpwo.laintas.com/downloads/latest.json
```

Confirm that:

- BOTH manifests report the new version — the mirror's is the one `/v` reads
  after the plan-A release lands
- amd64, arm64, the source bundle, the Windows installer and the .deb all
  return `200` on BOTH channels
- the download page shows the new version and its cards link at the new tag
- `src_manifest.zip` matches the file checksums in the manifest
- the mirror's `SHA256SUMS.txt` verifies (section 3)
- `downloads/latest.json` still resolves — the Windows build's `/windows
  install` reads it, and it is published by a different repository

`/v` reads whatever its `DEFAULT_DOWNLOAD_BASE` names — GitHub until the
plan-A release lands, cli.laintas.com after. The mirror checks above must
pass either way.

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

The Vite build empties `dist`, so move `dist/releases` aside before the
build and restore it afterwards (section 3). A published release is on
GitHub and is unaffected — but once `/v` reads cli.laintas.com (plan A), an
empty `dist/releases` IS a broken update channel for every installed CLI.
