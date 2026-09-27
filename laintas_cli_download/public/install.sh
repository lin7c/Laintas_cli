#!/usr/bin/env bash
set -euo pipefail

# Where this script is served from — the Windows hand-off below fetches its
# sibling from here.
BASE_URL="https://cli.laintas.com"
# Where the packages are. The site's own self-hosted release path is retired:
# its "latest" pointer 404s, which broke this installer on every platform
# after the move to GitHub Releases.
RELEASE_BASE="https://github.com/lin7c/Laintas_cli/releases/latest/download"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

echo ""
echo "── Laintas CLI Installer ─────────────────────────────────────"

# Detect OS and CPU architecture before downloading a native package. The
# existing Linux command remains unchanged; when the same script is run from
# Git Bash/MSYS/Cygwin on Windows it hands off to the native PowerShell
# installer, which creates the private Laintas-CLI WSL distribution.
UNAME_S=$(uname -s)
case "$UNAME_S" in
    Linux) INSTALL_MODE="linux" ;;
    Darwin) INSTALL_MODE="mac" ;;
    MINGW*|MSYS*|CYGWIN*) INSTALL_MODE="windows" ;;
    *)
        echo "Unsupported OS: $UNAME_S"
        echo "Supported platforms: Linux, macOS and 64-bit Windows with WSL 2"
        exit 1
        ;;
esac

if [ "$INSTALL_MODE" = "linux" ]; then
    case "$(uname -m)" in
        x86_64|amd64) ARCH="amd64" ;;
        aarch64|arm64) ARCH="arm64" ;;
        *)
            echo "Unsupported Linux architecture: $(uname -m)"
            echo "Supported architectures: x86_64/amd64 and aarch64/arm64"
            exit 1
            ;;
    esac
    echo "  Detected: Linux $ARCH"

    # Download tarball. Keep progress visible and fail instead of hanging forever
    # when DNS, IPv6, or the route to the download host is unhealthy.
    ASSET="laintas-cli_linux_${ARCH}.tar.gz"
    echo "  Downloading $ASSET…"
    curl --fail --location --show-error --progress-bar \
        --retry 2 --retry-delay 2 --connect-timeout 15 --max-time 900 \
        "$RELEASE_BASE/$ASSET" -o "$TMP_DIR/$ASSET"
    echo "  Download complete."

    # Extract
    echo "  Extracting package…"
    tar xzf "$TMP_DIR/$ASSET" -C "$TMP_DIR"

    echo "  Installing to /usr/local/bin…"
    # v1.7+ archives place install.sh beside the binary; older archives
    # nested both files under a laintas-cli directory.
    if [ -x "$TMP_DIR/install.sh" ]; then
        "$TMP_DIR/install.sh"
    elif [ -x "$TMP_DIR/laintas-cli/install.sh" ]; then
        "$TMP_DIR/laintas-cli/install.sh"
    else
        echo "Invalid package: install.sh was not found after extraction"
        exit 1
    fi

    printf '\n  Done! Run `laintas-cli` to start.\n\n'
elif [ "$INSTALL_MODE" = "mac" ]; then
    case "$(uname -m)" in
        x86_64) ARCH="amd64" ;;
        arm64) ARCH="arm64" ;;
        *) echo "Unsupported Mac architecture: $(uname -m)"; exit 1 ;;
    esac
    # Prereleases do not replace GitHub's stable /latest pointer. The beta
    # install command supplies LAINTAS_INSTALL_TAG explicitly.
    RELEASE_TAG="${LAINTAS_INSTALL_TAG:-}"
    if [ -z "$RELEASE_TAG" ]; then
        echo "macOS is in beta. Set LAINTAS_INSTALL_TAG to the beta release tag."
        exit 1
    fi
    case "$RELEASE_TAG" in
        *[!a-zA-Z0-9._-]*) echo "Invalid release tag"; exit 1 ;;
    esac
    TAG_BASE="https://github.com/lin7c/Laintas_cli/releases/download/$RELEASE_TAG"
    ASSET="laintas-cli_darwin_${ARCH}.tar.gz"
    curl --fail --location --show-error --progress-bar \
        --retry 2 --connect-timeout 15 --max-time 900 \
        "$TAG_BASE/$ASSET" -o "$TMP_DIR/$ASSET"
    curl --fail --location --show-error --silent \
        --retry 2 --connect-timeout 15 --max-time 60 \
        "$TAG_BASE/SHA256SUMS.txt" -o "$TMP_DIR/SHA256SUMS.txt"
    expected="$(awk -v asset="$ASSET" '$2 == asset {print $1}' "$TMP_DIR/SHA256SUMS.txt")"
    actual="$(shasum -a 256 "$TMP_DIR/$ASSET" | awk '{print $1}')"
    if [ -z "$expected" ] || [ "$actual" != "$expected" ]; then
        echo "Checksum verification failed for $ASSET" >&2
        exit 1
    fi
    tar xzf "$TMP_DIR/$ASSET" -C "$TMP_DIR"
    bash "$TMP_DIR/install.sh"
    printf '\n  Done! Run `laintas-cli` to start.\n\n'
else
    echo "  Detected: Windows amd64"
    if ! command -v powershell.exe >/dev/null 2>&1; then
        echo "PowerShell is required to install Laintas CLI for Windows."
        exit 1
    fi
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command \
        "Invoke-RestMethod '$BASE_URL/install.ps1' | Invoke-Expression"
fi

echo "── ────────────────────────────────────────────────────────────"
