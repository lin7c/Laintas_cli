#!/usr/bin/env python3
"""Small process entry point; defer the interactive runtime until needed."""
import sys


def main():
    if sys.argv[1:2] in (["--version"], ["-V"]):
        from version import __version__
        print(f"laintas-cli {__version__}")
        return
    if sys.argv[1:2] in (["--help"], ["-h"]):
        from cli_args import build_parser
        build_parser().parse_args()
        return

    # Running as __main__ keeps account selection, pow, module identity and
    # process teardown identical to the historical source entry. A module
    # loader can reuse bytecode instead of recompiling the large script.
    import runpy
    runpy.run_module("laintas_cli", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
