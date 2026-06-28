# Headless-Browser / WebRTC / Snapshot — Packaging Plan

How the three "heavy / optional / platform-locked" subsystem modules must be
handled across the 5 release artifacts. This is the authoritative spec for the
`package_manifest.json` fields and the per-platform PyInstaller spec changes.

Read this before touching `package_manifest.json`, either `laintas_cli.spec`,
or `build_download_assets.sh`.

## The three modules at a glance

| Module | Python deps | System binaries | Platform | Import style | Backend dep |
|---|---|---|---|---|---|
| `browser_session.py` | `websockets` (WS↔RFB), `playwright` (CDP) — both lazy | Xvfb / x11vnc / chrome\|chromium | **Unix-only** (no Xvfb on Windows) | `laintas_cli.py:141` **top-level** + `tools.py` lazy-in-fn | Helpwo `/vnc` relay (retries, no crash) |
| `webrtc_channel.py` | `aiortc` (top-level try/except) | none | cross-platform | `laintas_cli.py:3357` lazy-in-fn | agent protocol carries SDP |
| `snapshot.py` | none (stdlib + `git`) | `git` | cross-platform | `laintas_cli.py:6043`, `agent_loop.py:3149` lazy-in-fn | none |

## Key finding: the code is already designed right — don't touch it

All three modules already do optional + lazy + graceful-degrade. Evidence:

- `browser_session.py` imports only stdlib at module top; `websockets`/`playwright`
  are imported inside methods (`websockets` at `:583` returns silently on
  ImportError; `playwright` at `:315` inside `get_page()`). System binaries are
  probed via `shutil.which` in `_check_host_deps()` (`:65-79`) which returns a
  message string instead of raising.
- `webrtc_channel.py:30-38` wraps `from aiortc import ...` in try/except and
  exposes `AIORTC_AVAILABLE` + `WebrtcManager.available()` for callers to check.
- `snapshot.py` is pure stdlib + `subprocess.run(["git",...])` with `_git()`
  that never raises (`:28-37`).
- `tools.py` browser.* tools all do `import browser_session as _bs` inside the
  function body wrapped in try/except (`:2394-2396`, etc.) returning a clean
  `{"ok": False, "error": "browser_session module not available"}`.

**One hard constraint**: `laintas_cli.py:141` is a **top-level**
`import browser_session as browser_mod`, and 7 shutdown sites call
`browser_mod.close_all_browser_sessions()`. So every artifact MUST make
`import browser_session` succeed. Because that module's top is pure stdlib,
this always holds as long as the file ships. **Do not convert this to a lazy
import** — the shutdown cascade depends on the name being bound at module
load, and the import itself is free (no heavy deps at top level).

## The problems are all in packaging

| Current defect | Effect |
|---|---|
| `build_download_assets.sh` `SOURCE_FILES` omits all 3 modules | source/macOS install `ImportError`s on `browser_session` at startup, `webrtc_channel`/`snapshot` on first use |
| Both `laintas_cli.spec` `datas`/`hiddenimports` omit all 3 modules | frozen binary crashes at startup on `import browser_session` (top-level) |
| `requirements.txt` mixes core + optional deps | `pip install -r` pulls heavyweight playwright/aiortc even when the user only wants the CLI |
| `setup.py` has no `extras_require` | `pip install laintas-cli` can't opt into browser/webrtc/mcp selectively |

## Strategy: ship source everywhere, prune frozen deps per-platform

Matches the existing graceful-degrade design. **Zero code changes**; only
packaging manifests move.

### Source / macOS / .deb (ship all `.py`)

- `package_manifest.json` `modules` includes `browser_session`, `webrtc_channel`,
  `snapshot`.
- `requirements.txt` is split:
  - Core four (`requests`, `certifi`, `rich`, `prompt_toolkit`) → `setup.py`
    `install_requires`.
  - Optional deps → `extras_require`:
    - `laintas-cli[browser]` → `websockets`, `playwright`
    - `laintas-cli[webrtc]` → `aiortc`
    - `laintas-cli[mcp]` → `mcp`
    - `laintas-cli[all]` → union
  - Keep `requirements.txt` for dev (`pip install -e .[all]`); `install.sh`
    changes to `pip install .` (core only) + prints the optional-extras hint.
- User opts in: `pip install laintas-cli[browser]` + `apt install xvfb x11vnc
  chromium` (documented in the post-install message).

### Linux frozen binary (ship all + collect heavy deps)

- spec `datas` adds the 3 modules' `.py`.
- spec `hiddenimports` adds `websockets`, `playwright`, `aiortc`, `av`, `cffi`,
  `snapshot`, `webrtc_channel`.
- `collect_submodules('websockets')` — pure Python, trivial.
- `collect_submodules('aiortc')` + `collect_submodules('av')` + `collect_submodules('cffi')`
  — aiortc depends on PyAV (C extension) and cffi; without collecting these the
  WebRTC handshake raises ImportError at runtime.
- `playwright`: use `collect_data_files('playwright')` for the Python package,
  **but do NOT bundle the chromium browser binary** (~300 MB). Users run
  `playwright install chromium` separately. Rationale: keeps the Linux tarball
  small; `get_page()`'s `from playwright.sync_api import sync_playwright`
  resolves from the bundled package, the browser comes from
  `~/.cache/ms-playwright`.

### Windows frozen binary (prune VNC deps, keep webrtc + snapshot)

- `browser_session.py` **is still bundled** (top-level import must succeed),
  but `hiddenimports` does **NOT** list `websockets` / `playwright`. At runtime:
  - `_ws_bridge_loop` hits `ImportError` on `from websockets.sync.client import
    connect` and `return`s (`:583-584`) — VNC relay silently off.
  - `get_page()` hits `ImportError` on `from playwright.sync_api import
    sync_playwright` — `tools.py`'s try/except returns
    `{"ok": False, "error": "browser_session module not available"}` (`:2396`).
  - `_check_host_deps()` returns the Windows-specific message (`:67-69`).
  Net: browser.* tools degrade cleanly, no crash, no exe bloat.
- `webrtc_channel.py` is bundled; `hiddenimports` adds `aiortc` + `av` + `cffi`
  (cross-platform, worth the ~15 MB).
- `snapshot.py` is bundled (pure stdlib, zero cost).

### Self-updater (`/v update`)

- `manifest.json` covers the sha256 of all 3 modules' `.py`. Source-install
  users get browser/webrtc/snapshot code on update automatically.
- Optional pip deps are NOT auto-installed by the updater. When
  `AIORTC_AVAILABLE is False` or `find_chrome() is None`, the relevant tool
  prints the exact `pip install laintas-cli[<extra>]` / `apt install ...` hint.
- Frozen-binary updates replace the whole platform tarball/exe, which already
  carries the platform-pruned content.

## PyInstaller pitfalls (learned the hard way)

- **playwright's `driver/` is a Node subprocess.** PyInstaller does not collect
  it by default. If you want playwright fully working inside the frozen exe you
  must add `('path/to/playwright/driver', 'playwright/driver')` to `binaries`.
  We deliberately skip this — users `pip install playwright` into the
  environment instead. The frozen exe only needs the Python API stub to import.
- **PyAV (`av`) is a C extension.** `collect_submodules('av')` alone is not
  enough; also `collect_data_files('av')` for its codec metadata. Without it
  aiortc's `from av import ...` fails on first RTCPeerConnection.
- **cffi** ships compiled `_cffi_backend`; PyInstaller usually finds it, but
  add to `hiddenimports` as belt-and-suspenders.
- **websockets** is pure Python but splits across many submodules; use
  `collect_submodules('websockets')` not a bare `'websockets'` hidden import.
- **Do NOT add `playwright` to `excludes`** on Linux — that defeats CDP. Only
  omit it from `hiddenimports` on Windows.

## Minimal change surface (lands in Phase 0)

`package_manifest.json` gains per-module platform/deps metadata so the spec
generator can prune per platform. Source packages ignore these fields and ship
everything.

```json
{
  "modules": [
    {"name": "browser_session", "platform": "all",
     "frozen_deps": {"linux": ["websockets", "playwright"], "windows": []}},
    {"name": "webrtc_channel", "platform": "all",
     "frozen_deps": {"linux": ["aiortc", "av", "cffi"], "windows": ["aiortc", "av", "cffi"]}},
    {"name": "snapshot", "platform": "all", "frozen_deps": {}}
  ],
  "extras_require": {
    "browser": ["websockets>=12.0", "playwright>=1.40"],
    "webrtc":  ["aiortc>=1.14"],
    "mcp":     ["mcp>=1.0"],
    "all":     ["websockets>=12.0", "playwright>=1.40", "aiortc>=1.14", "mcp>=1.0"]
  }
}
```

The spec generator reads `frozen_deps[<platform>]` to decide `hiddenimports` +
`collect_*` calls. Source/manifest paths always include all 3 modules.

## Acceptance smoke test (per artifact)

Run on each of the 5 artifacts after build:

```bash
# 1. startup doesn't crash (top-level browser_session import)
laintas-cli --version          # must print the version and exit 0

# 2. graceful degrade for unavailable extras
laintas-cli --execute "list browser sessions"   # returns "not available" or empty, never traceback

# 3. snapshot is always available (stdlib only)
laintas-cli --execute "create a snapshot"       # succeeds on any git repo

# 4. webrtc optional
python -c "from webrtc_channel import AIORTC_AVAILABLE; print(AIORTC_AVAILABLE)"
# Linux frozen: True ; Windows frozen: True ; source without [webrtc]: False
```

A failure of test 1 or 3 on any artifact blocks the release.
