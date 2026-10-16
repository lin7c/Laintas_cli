# Laintas CLI beta v4 (1.32.5b4)

Laintas CLI beta v4 (package version 1.32.5b4) is now available.

- Request a progress report during a running task with Alt+P or /progress. A separate model call summarizes completed work, current activity, remaining work and known blockers while the task continues. Reports use the auxiliary model when configured and have their own output-token budget.
- Keep reasoning across tool steps and resumed sessions. Reasoning is replayed to the model that produced it; after a model switch, its text is carried as quoted context while opaque provider state stays private to its original model.
- The think tool lets agents pause to reason and search full local step records for details that have left the active context.
- Context compaction includes reasoning when preparing summaries and more consistently respects the available context budget.
- Critic reviews cover every step since the previous review.
- Fix task-state cleanup, session handling, customization validation and terminal rendering regressions.
- Fix update option parsing and verification of hidden manifest paths. Account information shows the membership plan by name.

Packages: Linux amd64/arm64, macOS Intel/Apple Silicon, Debian amd64 and source. Windows installers use their separate release channel.

Download and install: https://cli.laintas.com/
Existing users can update with /v update.
