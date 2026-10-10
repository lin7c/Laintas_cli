#!/bin/sh
# Open a web link in the Windows default browser. The launcher points
# BROWSER at this file, which is how the CLI's sign-in page gets opened.
case "$1" in
    http://*|https://*) ;;
    *) exit 1 ;;
esac
# The link is passed through untouched, never rewritten as a path.
MSYS2_ARG_CONV_EXCL='*' exec rundll32.exe url.dll,FileProtocolHandler "$1"
