---
name: shell-linux
description: Shell and Linux command discipline for laintas-cli terminal work.
version: 1.0.0
triggers:
  - shell
  - bash
  - command line
  - terminal command
  - linux
  - install
  - package
  - permission denied
  - service
  - systemd
  - process
  - port
  - disk
  - log file
---

# Shell And Linux

Use shell commands when they are the clearest tool:

- Prefer `rg`/`rg --files` over `grep`/`find` for source search.
- Keep commands specific. Avoid broad destructive globs and unbounded scans of vendor/cache directories.
- For the primary agent, bare `shell.exec("cd path")` updates the CLI process
  working directory. Sub-agents should pass `cwd` or use `cd path && command`
  so concurrent agents never mutate one process-global directory.
- A non-zero exit code means the command ran and failed; inspect its preserved
  stdout/stderr before concluding that the shell executor itself failed.
- Read command errors carefully. Change arguments or approach before retrying.
- Use project-native scripts when obvious: `npm run build`, `npm test`, `pytest`, `cargo test`, etc.
- Long-running servers should be started intentionally; report the URL and leave the process running only when useful.
- Never pipe untrusted downloads into a shell.
