# Laintas CLI beta v5 (1.32.5b5)

Laintas CLI beta v5 (package version 1.32.5b5) is now available.

- The /model picker groups models by where they come from — Laintas Supply, Activated and Third-party Supply — and models served through your own activated keys appear under their short names. The auxiliary model slot behind compaction, critic reviews and memory extraction is named AUX; choose its model with /model aux.
- An auto model can route each task to a suitable model; /model routing sets which models it may pick, with separate choices for everyday and complex tasks.
- A secret-input guard screens text submitted to the CLI for API keys and other credentials and flags matches before they are sent to a model or written into the conversation.
- Generate video from the terminal with the built-in video tool; rendering runs asynchronously on the gateway.
- Ctrl+C pressed right after Enter, while a command is still starting, now interrupts that command instead of being lost, and an interrupted command returns the prompt even when your shell configuration has replaced the CLI's prompt hooks.
- The /helpwo command and its local companion are removed. Remote sub-terminals and hosted /app applications are unchanged.
- Fix agents mode session handling and critic review regressions.

Packages: Linux amd64/arm64, macOS Intel/Apple Silicon, Debian amd64 and source. Windows installers use their separate release channel.

Download and install: https://cli.laintas.com/
Existing users can update with /v update.
