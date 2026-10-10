# Laintas CLI beta v3 (1.32.5b3)

Laintas CLI beta v3 (package version 1.32.5b3) is now available.

- Text colors now fit your terminal. /theme auto reads the terminal's background once and picks colors made for a dark or a light one; /theme dark, light and mono set it by hand. The terminal's own background and body text are left alone.
- New launch options apply to a single run without changing your saved settings: --model, --route, --mode, --plan, --auto-approve, --effort, --max-loops, --config KEY=VALUE, --theme, --no-color, --tools, --disallow-tools, --mcp-config, --append-system-prompt, --cwd and --resume.
- A task on the command line opens a session with it as the first message. When output is piped the task runs once, and - reads the task from standard input.
- With --execute in text mode, start-up notices go to standard error, so standard output holds only the task's output. --output-format json returns one JSON object.
- The /agents view uses one color meaning per state, a shorter side panel, and a list-first layout in narrow terminals.
- /resume no longer adds a duplicate entry when you quit without new messages, and long rows scroll while their tree prefix stays in place.
- CLI updates, extensions and the install scripts share one download progress display.
- Faster menus and tool handling in long sessions.

Download and install: https://cli.laintas.com/
Existing users can update with /v update.
