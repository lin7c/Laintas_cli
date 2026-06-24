# Build & Release Guide

How to package and publish Laintas CLI to the download page. Written for agents
and humans picking up the release process. Read this before touching anything
under `build/` or `laintas_cli_download/releases/`.

## The 4 download-page artifacts

The download page serves these from `laintas_cli_download/{public,dist}/releases/latest/`:

| Artifact | Platform | Built by | Contains |
|---|---|---|---|
| `laintas-cli_linux.tar.gz` | Linux | **local** PyInstaller (onefile binary) | compiled binary |
| `laintas-cli_macos.tar.gz` | macOS | **local** (source bundle + `install.sh`) | `.py` source |
| `laintas-cli_source.zip` | any | **local** (source bundle) | `.py` source |
| `laintas_cli.exe` + `laintas-cli_windows.zip` | Windows | **CI only** (`windows-latest`) | compiled exe |

There is also a `.deb` (`laintas-cli_0.1.x_amd64.deb`) built separately via
`build/linux/build_deb.sh` (needs `fpm`); it is listed in `SHA256SUMS.txt` but is
not part of the routine "rebuild all 4" flow.

### Why Windows can only be built by CI

We develop on Linux. PyInstaller produces a binary for the OS it runs on, so a
Windows `.exe` requires a Windows runner. The local build script
(`build/release/build_download_assets.sh`) only *copies* the checked-in
`build/windows/laintas_cli.exe` — it does **not** rebuild it. To get fresh
Windows code you must trigger the CI workflow (below).

## Release procedure (the flow that works)

This is the exact sequence used for the remote-login fix release.

### 1. Build the 3 local artifacts

Requires Docker (for the Linux binary) and the venv.

```bash
bash build/release/build_download_assets.sh
```

This rebuilds `laintas-cli_linux.tar.gz`, `laintas-cli_macos.tar.gz`, and
`laintas-cli_source.zip` into `laintas_cli_download/public/releases/latest/`,
and copies the (stale) checked-in `.exe` over the release `.exe` — that's fine,
CI overwrites it in step 4. Takes a few minutes; run it in the background.

> **glibc: the Linux binary MUST be built in an old-glibc container.**
> PyInstaller binaries are only *forward* compatible across glibc — build on the
> oldest glibc you want to support. The build host here is glibc 2.39; a binary
> built directly on it dies on older servers with
> `Failed to load Python shared library ... GLIBC_2.38 not found`.
> `build_download_assets.sh` therefore delegates the Linux build to
> `build/linux/build_linux_compat.sh`, which builds inside `python:3.11-slim-buster`
> (Debian 10, **glibc 2.28**) — covers CentOS 8/Anolis/Aliyun Linux 3/Ubuntu 20.04+.
> Official python images ship a shared libpython + ssl, so no compiling; the slim
> image only needs `binutils` (the script apt-installs it from archive.debian.org
> since buster is EOL). To support even older boxes (CentOS 7, glibc 2.17) use
> `build/linux/build_in_manylinux.sh` instead (slower: compiles CPython
> `--enable-shared`).

### 2. Sync to `dist/` and regenerate checksums

The build script only writes `public/`. Both `public/` and `dist/` are served,
so mirror the 3 rebuilt files into `dist/releases/latest/`, then regenerate
`SHA256SUMS.txt` (same 5-entry set the repo commits: the 3 tarballs + `.exe` +
the current `.deb`) and copy it to `dist/` too.

```bash
cd laintas_cli_download
PUB=public/releases/latest; DIST=dist/releases/latest
for f in laintas-cli_linux.tar.gz laintas-cli_macos.tar.gz laintas-cli_source.zip; do
  cp "$PUB/$f" "$DIST/$f"
done
( cd "$PUB" && sha256sum laintas-cli_linux.tar.gz laintas-cli_macos.tar.gz \
    laintas-cli_source.zip laintas_cli.exe laintas-cli_0.1.4_amd64.deb > SHA256SUMS.txt )
cp "$PUB/SHA256SUMS.txt" "$DIST/SHA256SUMS.txt"   # dist copy is gitignored; harmless
```

### 3. Commit + push to `main`

> `laintas_cli_download/dist/` is gitignored (Vite build output), but the
> release tarballs inside it were force-tracked historically. Stage `public/`
> files normally; stage the tracked `dist/` tarballs with `git add -f`. The
> `dist/SHA256SUMS.txt` stays untracked — leave it.

```bash
git add laintas_cli.py \
  laintas_cli_download/public/releases/latest/laintas-cli_linux.tar.gz \
  laintas_cli_download/public/releases/latest/laintas-cli_macos.tar.gz \
  laintas_cli_download/public/releases/latest/laintas-cli_source.zip \
  laintas_cli_download/public/releases/latest/SHA256SUMS.txt
git add -f \
  laintas_cli_download/dist/releases/latest/laintas-cli_linux.tar.gz \
  laintas_cli_download/dist/releases/latest/laintas-cli_macos.tar.gz \
  laintas_cli_download/dist/releases/latest/laintas-cli_source.zip
git commit -m "release: ..." && git push origin main
```

Releases are committed directly to `main` — that is the repo's convention and is
required for the Windows CI trigger (below). Don't branch for a release.

GitHub warns about >50 MB files (the Linux tarball). That's an expected warning,
not an error.

### 4. Windows CI rebuilds the exe automatically

`.github/workflows/windows-release.yml` triggers on push to `main` when any of
these change: `laintas_cli.py`, `build/windows/**`,
`.github/workflows/windows-release.yml`. A normal release touches
`laintas_cli.py`, so the push in step 3 triggers it.

The workflow: installs deps → `pyinstaller --noconfirm build/windows/laintas_cli.spec`
→ copies the exe into `build/windows/`, `public/releases/latest/`, and
`dist/releases/latest/` → builds `laintas-cli_windows.zip` → regenerates
`SHA256SUMS.txt` (6 entries: adds `laintas-cli_windows.zip`) → **commits and
pushes back to `main`** as `release: rebuild Windows CLI`.

So after CI finishes, `git pull` to get the fresh `.exe`, `.zip`, and
checksums. All 4 artifacts are then current.

### 5. Verify

```bash
gh run list --workflow=windows-release.yml --limit 1     # success?
git pull --no-edit origin main
ls -la laintas_cli_download/public/releases/latest/      # exe/zip timestamps fresh
cat laintas_cli_download/public/releases/latest/SHA256SUMS.txt
```

Sanity-check the fix is actually in the artifacts:

```bash
unzip -p laintas_cli_download/public/releases/latest/laintas-cli_source.zip \
  laintas-cli-source/laintas_cli.py | grep -c <your-new-symbol>
```

## Gotchas learned the hard way

- **PyInstaller spec files have no `__file__`.** `build/windows/laintas_cli.spec`
  computes `PROJECT_DIR` from the `SPECPATH` global PyInstaller injects, not
  `__file__` (that raised `NameError` and the exe never built). The Linux spec
  (`build/linux/laintas_cli.spec`) uses relative `../../` paths instead.
- **The local script never rebuilds the exe** — only CI does. If you skip the
  push/CI step, the Windows download stays stale even though the other 3 updated.
- **CI commits back to `main`.** Expect an extra `release: rebuild Windows CLI`
  commit after your push; pull before doing more work.
- **Don't commit pyinstaller leftovers** (`build/linux/build/`, `build/linux/dist/`,
  top-level `dist/`) — they're untracked working files.
