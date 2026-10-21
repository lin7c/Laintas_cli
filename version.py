"""Single source of truth for the laintas-cli version.

Bump this on every release. The download/release flow (build/RELEASE.md) and the
self-updater (updater.py, `/v` command) both read this value, so it must match
the version published in the release `manifest.json`.
"""

__version__ = "1.32.5b5"  # PEP 440 package version for build tools.
RELEASE_NAME = "laintas-cli-beta v4"
RELEASE_TAG = "laintas-cli-beta-v4"
