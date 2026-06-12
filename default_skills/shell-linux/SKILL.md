---
name: shell-linux
description: Shell and Linux command discipline for laintas-cli terminal work.
version: 1.0.0
---

# Shell And Linux

Use shell commands when they are the clearest tool:

- Prefer `rg`/`rg --files` over `grep`/`find` for source search.
- Keep commands specific. Avoid broad destructive globs and unbounded scans of vendor/cache directories.
- For persistent directory changes, `shell.exec("cd path")` updates the stationed terminal state.
- Read command errors carefully. Change arguments or approach before retrying.
- Use project-native scripts when obvious: `npm run build`, `npm test`, `pytest`, `cargo test`, etc.
- Long-running servers should be started intentionally; report the URL and leave the process running only when useful.
- Never pipe untrusted downloads into a shell.
