"""Shared launch arguments; safe to load without the interactive runtime."""
from version import __version__
from terminal_theme import THEMES


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        description="Laintas CLI — Autonomous AI agent",
        epilog="Options given at launch apply to this run only; nothing is "
               "saved to your preferences.")
    parser.add_argument(
        "task", nargs="?", default=None,
        help="Task to start with. At a terminal it becomes the first message "
             "of an interactive session; with piped output it runs like "
             "--execute. Use - to read the task from standard input.")
    parser.add_argument("--version", "-V", action="version",
                        version=f"laintas-cli {__version__}")
    parser.add_argument("--name", type=str, help="Set agent name (shows in Helpwow AGENTS)")
    parser.add_argument("--cwd", type=str, default=None, metavar="DIR",
                        help="Work in DIR instead of the current directory")
    parser.add_argument("--model", type=str, default=None, metavar="ID",
                        help="Model to use for this run")
    parser.add_argument("--route", type=str, default=None, metavar="NAME",
                        help="Direct model route to use for this run (see /model route list)")
    parser.add_argument("--mode", type=str, default=None, metavar="NAME",
                        help="Working mode for this run (see /mode); plan starts in PLAN mode")
    parser.add_argument("--plan", action="store_true", default=False,
                        help="Start in PLAN mode (same as --mode plan)")
    parser.add_argument("--auto-approve", type=str, default=None,
                        choices=["none", "writes", "commands", "all"],
                        help="What to approve without asking in this run")
    parser.add_argument("--yes", "-y", action="store_true", default=False,
                        help="Approve file writes and commands without asking "
                             "(same as --auto-approve all); the command policy still applies")
    parser.add_argument("--effort", type=str, default=None, metavar="LEVEL",
                        help="Reasoning effort: auto, none, low, medium, high, max")
    parser.add_argument("--max-loops", type=int, default=None, metavar="N",
                        help="Maximum agent loop iterations per task")
    parser.add_argument("--config", "-c", action="append", default=[], metavar="KEY=VALUE",
                        help="Set a /config value for this run; repeat for several")
    parser.add_argument("--theme", type=str, default=None,
                        choices=list(THEMES),
                        help="Text colors for your terminal background")
    parser.add_argument("--no-color", action="store_true", default=False,
                        help="Print without color")
    parser.add_argument("--verbose", "-v", action="store_true", default=False,
                        help="Record tool calls and outputs for /detail trace")
    parser.add_argument("--append-system-prompt", type=str, default=None, metavar="TEXT",
                        help="Extra instructions added after the system prompt")
    parser.add_argument("--append-system-prompt-file", type=str, default=None, metavar="FILE",
                        help="Read extra system-prompt instructions from FILE")
    parser.add_argument("--tools", type=str, default=None, metavar="LIST",
                        help="Only offer these tools (comma-separated names or globs, e.g. file.*,web.search)")
    parser.add_argument("--disallow-tools", type=str, default=None, metavar="LIST",
                        help="Never offer these tools (comma-separated names or globs)")
    parser.add_argument("--mcp-config", type=str, default=None, metavar="FILE",
                        help="Load MCP servers from FILE instead of ~/.laintas/mcp.json")
    parser.add_argument("--output-format", type=str, default="text",
                        choices=["text", "json"],
                        help="Result format for --execute: json prints one object "
                             "on standard output and everything else on standard error")
    parser.add_argument("--account", type=str, default=None,
                        help="Use a saved account alias, email or userId")
    parser.add_argument("--account-return", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--backend", type=str,
        help="Custom backend URL (enters external/unmetered mode; Laintas credentials are stripped)",
        default=None)
    parser.add_argument("--laintas", type=str, help=argparse.SUPPRESS, default=None)
    parser.add_argument("--acp", action="store_true", default=False,
                        help="Serve the Agent Client Protocol (ACP) over stdio "
                             "— lets ACP editors (Zed & friends) drive this CLI. "
                             "Optional: pip install agent-client-protocol")
    parser.add_argument("--execute", "-e", type=str, default=None, metavar="TASK",
                        help="Execute a single task non-interactively and exit; "
                             "- reads the task from standard input")
    parser.add_argument("--session-id", type=str, default=None,
                        help="Persist/resume --execute context under this logical session id")
    # Internal: set by the CLI when it launches itself as a sub-terminal.
    parser.add_argument("--depth", "-d", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--simple-prompt", action="store_true", default=False,
                        help="Use plain input() instead of prompt_toolkit")
    parser.add_argument("--monitor-only", action="store_true", default=False,
                        help="Start as a remote executor only — register, heartbeat, "
                             "and poll HelpwoAI for tasks. No local REPL.")
    parser.add_argument("--resume", nargs="?", const=True, default=False, metavar="SESSION_ID",
                        help="Resume the saved conversation for this directory on "
                             "startup, or the one saved under SESSION_ID")
    parser.add_argument("--continue", dest="continue_session", action="store_true", default=False,
                        help="Alias for --resume (session continuation)")
    parser.add_argument("--agent-id", type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--agent-name", type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--agent-role", type=str, default=None,
                        choices=["pool", "deployed", "primary", "subagent"],
                        help=argparse.SUPPRESS)
    parser.add_argument("--terminal-name", type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--parent-terminal", type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--parent-agent-id", type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--remote-parent-id", type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--connect", action="store_true", default=False, help=argparse.SUPPRESS)
    # Internal: this sub-terminal hosts an application (/helpwo, /app).
    parser.add_argument("--app", type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--app-state", type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--app-launch-id", type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--app-options", type=str, default=None, help=argparse.SUPPRESS)
    return parser
