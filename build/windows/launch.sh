#!/bin/bash
# Start the CLI inside the bundled runtime for the terminal window.
# Arguments: rows cols. The window passes its own environment; nothing here
# depends on a login shell having run.
PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
export PATH
self="$(cygpath -u "$0")"
app="$(cd "$(dirname "$self")/../.." && pwd)"

# Account data lives in the Windows user profile, not inside the install
# directory, so it survives upgrades and reinstalls.
HOME="$(cygpath -u "$USERPROFILE")"
export HOME
export SHELL=/usr/bin/bash
export LANG="${LANG:-C.UTF-8}"
export TERM=xterm-256color
export COLORTERM=truecolor
# Links the CLI opens (the sign-in page first of all) go to the Windows
# default browser.
export BROWSER="$app/build/windows/open_url.sh"

if [ -n "$LAINTAS_START_DIR" ] && [ -d "$(cygpath -u "$LAINTAS_START_DIR")" ]; then
    cd "$(cygpath -u "$LAINTAS_START_DIR")" || exit 1
else
    workspace="${LAINTAS_WORKSPACE:-$HOME/laintas_workspace}"
    mkdir -p "$workspace" && cd "$workspace" || cd "$HOME" || exit 1
fi

# The window test sets this to check the window, host and runtime without
# an account: it runs a bare shell where the CLI would be.
if [ -n "$LAINTAS_TERMINAL_SHELL" ]; then
    set -- "${1:-30}" "${2:-100}" bash --norc --noprofile
else
    set -- "${1:-30}" "${2:-100}" python3 "$app/laintas_cli.py"
fi
rows="$1"; cols="$2"; shift 2
exec python3 "$app/build/windows/term_host.py" --rows "$rows" --cols "$cols" -- "$@"
