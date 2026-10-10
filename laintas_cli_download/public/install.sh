#!/usr/bin/env bash
set -euo pipefail

# Where this script is served from — the Windows hand-off below fetches its
# sibling from here.
BASE_URL="https://cli.laintas.com"
# Linux uses GitHub assets; macOS uses the verified site mirror.
# Windows hands off to the native desktop bootstrap on the same site.
RELEASE_BASE="https://github.com/lin7c/Laintas_cli/releases/latest/download"
TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/laintas-install.XXXXXXXX")
DOWNLOAD_PID=""
cleanup() {
    if [ -n "$DOWNLOAD_PID" ]; then
        kill "$DOWNLOAD_PID" 2>/dev/null || true
        wait "$DOWNLOAD_PID" 2>/dev/null || true
    fi
    # Only the directory created by this invocation is removed.
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Same two-line rail as download_progress.py and the PowerShell bootstrap.
# Bash 3.2 + curl + awk are available on both Linux and stock macOS.
download_with_progress() {
    local url="$1" target="$2" label="$3"
    local headers="$TMP_DIR/download.headers" errors="$TMP_DIR/download.errors"
    local interactive=0 width=80 tick=0 bytes=0 total=0 code=0 started elapsed
    local green="" muted="" track="" red="" foreground="" bold="" reset="" line="" display_label="$label"
    if [ -t 2 ] && [ "${TERM:-}" != dumb ]; then
        interactive=1
        # Read the actual output TTY; inherited COLUMNS may be stale.
        width=$(stty size <&2 2>/dev/null | awk '{print $2}') || width=80
        case "$width" in ''|*[!0-9]*) width=80 ;; esac
        if [ "$width" -lt 10 ]; then width=10; fi
        if [ -z "${NO_COLOR+x}" ]; then
            green=$'\033[38;2;63;185;80m'
            muted=$'\033[38;2;139;148;158m'
            track=$'\033[38;2;72;79;88m'
            red=$'\033[38;2;248;81;73m'
            foreground=$'\033[38;2;201;209;217m'
            bold=$'\033[1;38;2;230;237;243m'
            reset=$'\033[0m'
        fi
    fi
    if [ "$interactive" -eq 1 ] && [ "${#display_label}" -gt "$((width - 5))" ]; then
        display_label="${display_label:0:$((width > 5 ? width - 6 : 0))}…"
    fi
    printf '  %s↓ %s%s%s\n' "$green" "$foreground" "$display_label" "$reset" >&2
    started=$(date +%s)
    curl --fail --location --show-error --silent \
        --retry 2 --retry-delay 2 --connect-timeout 15 --max-time 900 \
        --dump-header "$headers" "$url" -o "$target" 2>"$errors" &
    DOWNLOAD_PID=$!
    while kill -0 "$DOWNLOAD_PID" 2>/dev/null; do
        if [ "$interactive" -eq 1 ]; then
            bytes=0
            if [ -f "$target" ]; then bytes=$(wc -c < "$target"); fi
            total=0
            if [ -f "$headers" ]; then
                total=$(awk '
                    /^HTTP\// { n=0; ok=($2 >= 200 && $2 < 300) }
                    tolower($1)=="content-length:" && ok { n=$2+0 }
                    END { printf "%.0f", n }
                ' "$headers")
            fi
            elapsed=$(( $(date +%s) - started ))
            line=$(awk -v done="$bytes" -v total="$total" -v seconds="$elapsed" \
                -v tick="$tick" -v width="$width" -v green="$green" -v muted="$muted" \
                -v track="$track" -v bold="$bold" -v reset="$reset" '
                function size(n, u,i) {
                    split("B KB MB GB",u," "); i=1
                    while(n>=1024 && i<4) { n/=1024; i++ }
                    return (i==1 ? sprintf("%.0f",n) : sprintf("%.1f",n)) " " u[i]
                }
                function clock(n) { return sprintf("%02d:%02d",int(n/60),int(n)%60) }
                BEGIN {
                    speed=seconds>0 ? done/seconds : 0
                    pct=total>0 ? sprintf("%3d%%", (done>=total ? 99 : int(done*100/total))) : "  --%"
                    if(total>0) {
                        divisor=1; unit="B"; split("KB MB GB",units," ")
                        for(i=1;i<=3 && total>=divisor*1024;i++) { divisor*=1024; unit=units[i] }
                        amount=(divisor==1 ? sprintf("%.0f/%.0f",done,total) : sprintf("%.1f/%.1f",done/divisor,total/divisor)) " " unit
                    } else amount=size(done)
                    metrics=total>0 ? pct "  " amount : amount
                    velocity=size(speed) "/s"; eta=(total>0 && speed>0 ? clock((total-done)/speed) : "--:--")
                    if(width>=4+8+length(metrics)+2+length(velocity)) metrics=metrics "  " velocity
                    if(total>0 && width>=4+8+length(metrics)+2+length(eta)) metrics=metrics "  " eta
                    rail=width-5-length(metrics); if(rail>32) rail=32; if(rail<1) rail=1
                    if(total>0) {
                        filled=int(done*rail/total); if(filled>rail) filled=rail
                        for(i=0;i<rail;i++) printf "%s%s", (i<=filled ? green : track), (i<filled ? "━" : (i==filled ? "╸" : "─"))
                    } else {
                        pulse=(rail<4 ? rail : 4); span=rail-pulse
                        position=(span>0 ? tick%(2*span) : 0); if(position>span) position=2*span-position
                        for(i=0;i<rail;i++) printf "%s%s", (i>=position && i<position+pulse ? green : track), (i>=position && i<position+pulse ? "━" : "─")
                    }
                    room=width-5-rail; if(room<0) room=0
                    if(total>0) printf "%s  %s%s%s%s%s", reset, bold, substr(metrics,1,4), reset, muted, substr(metrics,5,room>4 ? room-4 : 0)
                    else printf "%s  %s%s", reset, muted, substr(metrics,1,room)
                    printf "%s", reset
                }')
            printf '\r\033[2K  %s' "$line" >&2
        fi
        tick=$((tick + 1))
        sleep 0.15
    done
    wait "$DOWNLOAD_PID" || code=$?
    DOWNLOAD_PID=""
    elapsed=$(( $(date +%s) - started ))
    bytes=0
    if [ -f "$target" ]; then bytes=$(wc -c < "$target"); fi
    if [ "$interactive" -eq 1 ]; then printf '\r\033[2K' >&2; fi
    if [ "$code" -ne 0 ]; then
        printf '  %s✗ Failed%s\n' "$red" "$reset" >&2
        cat "$errors" >&2
        return "$code"
    fi
    line=$(awk -v n="$bytes" -v s="$elapsed" 'BEGIN {
        split("B KB MB GB",u," "); i=1
        while(n>=1024 && i<4) { n/=1024; i++ }
        amount=(i==1 ? sprintf("%.0f",n) : sprintf("%.1f",n)) " " u[i]
        printf "%s · %02d:%02d", amount, int(s/60), s%60
    }')
    if [ "$interactive" -eq 1 ] && [ "$((10 + ${#line}))" -ge "$width" ]; then
        printf '  %s✓ %s100%%%s\n' "$green" "$foreground" "$reset" >&2
        return 0
    fi
    printf '  %s✓ %s100%%%s  %s%s%s\n' "$green" "$foreground" "$reset" "$muted" "$line" "$reset" >&2
}

echo ""
echo "── Laintas CLI Installer ─────────────────────────────────────"

# Detect OS and CPU architecture before downloading a native package. The
# existing Linux command remains unchanged; when the same script is run from
# Git Bash/MSYS/Cygwin on Windows it hands off to the native PowerShell
# installer, which downloads and verifies the native desktop setup wizard.
UNAME_S=$(uname -s)
case "$UNAME_S" in
    Linux) INSTALL_MODE="linux" ;;
    Darwin) INSTALL_MODE="mac" ;;
    MINGW*|MSYS*|CYGWIN*) INSTALL_MODE="windows" ;;
    *)
        echo "Unsupported OS: $UNAME_S"
        echo "  Supported platforms: Linux, macOS and 64-bit Windows"
        exit 1
        ;;
esac

if [ "$INSTALL_MODE" = "linux" ]; then
    case "$(uname -m)" in
        x86_64|amd64) ARCH="amd64" ;;
        aarch64|arm64) ARCH="arm64" ;;
        *)
            echo "Unsupported Linux architecture: $(uname -m)"
            echo "  Supported architectures: x86_64/amd64 and aarch64/arm64"
            exit 1
            ;;
    esac
    echo "  Detected: Linux $ARCH"

    # Download tarball. Keep progress visible and fail instead of hanging forever
    # when DNS, IPv6, or the route to the download host is unhealthy.
    ASSET="laintas-cli_linux_${ARCH}.tar.gz"
    download_with_progress "$RELEASE_BASE/$ASSET" "$TMP_DIR/$ASSET" "$ASSET"

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

    echo "  Done!"
    echo '  Run `laintas-cli` to start.'
elif [ "$INSTALL_MODE" = "mac" ]; then
    case "$(uname -m)" in
        x86_64) ARCH="amd64" ;;
        arm64) ARCH="arm64" ;;
        *) echo "Unsupported Mac architecture: $(uname -m)"; exit 1 ;;
    esac
    echo "  Detected: macOS $ARCH"
    # LAINTAS_INSTALL_TAG optionally pins an exact release. Mac packages and
    # checksums are served from the cli.laintas.com release mirror.
    RELEASE_TAG="${LAINTAS_INSTALL_TAG:-}"
    if [ -n "$RELEASE_TAG" ]; then
        case "$RELEASE_TAG" in
            *[!a-zA-Z0-9._-]*) echo "Invalid release tag"; exit 1 ;;
        esac
        TAG_BASE="$BASE_URL/releases/$RELEASE_TAG"
    else
        TAG_BASE="$BASE_URL/releases/latest"
    fi
    ASSET="laintas-cli_darwin_${ARCH}.tar.gz"
    download_with_progress "$TAG_BASE/$ASSET" "$TMP_DIR/$ASSET" "$ASSET"
    download_with_progress "$TAG_BASE/SHA256SUMS.txt" "$TMP_DIR/SHA256SUMS.txt" "SHA256SUMS.txt"
    expected="$(awk -v asset="$ASSET" '$2 == asset {print $1}' "$TMP_DIR/SHA256SUMS.txt")"
    actual="$(shasum -a 256 "$TMP_DIR/$ASSET" | awk '{print $1}')"
    if [ -z "$expected" ] || [ "$actual" != "$expected" ]; then
        echo "Checksum verification failed for $ASSET" >&2
        exit 1
    fi
    tar xzf "$TMP_DIR/$ASSET" -C "$TMP_DIR"
    bash "$TMP_DIR/install.sh"
    echo "  Done!"
    echo '  Run `laintas-cli` to start.'
else
    echo "  Detected: Windows amd64"
    if ! command -v powershell.exe >/dev/null 2>&1; then
        echo "PowerShell is required to install Laintas CLI for Windows."
        exit 1
    fi
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command \
        "Invoke-RestMethod '$BASE_URL/install.ps1' | Invoke-Expression"
fi
