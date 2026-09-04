---
name: shell-linux
description: Shell and Linux command discipline for laintas-cli terminal work.
version: 1.2.0
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

- Prefer `rg`/`rg --files` over `grep`/`find` for source search. Where neither is installed, `grep`/`glob` are native tools and need no package.
- Keep commands specific. Avoid broad destructive globs and unbounded scans of vendor/cache directories.
- **Bound a recursive command, or know the size of what you are recursing into.**
  `find` with no `-maxdepth`, `du -sh` on a directory you have not sized, `grep -r`
  from a home or drive root, `ls -R`: each costs one filesystem call per entry,
  and neither the depth nor the speed of the tree is visible from the command.
  A tree that is deep, or on a slow filesystem -- a network share, a mounted
  disk, a virtualized or remote path -- turns that into a command that does not
  return, and there is no output to tell you which. The rule is about the shape
  of the command, not about any particular directory: no bound plus unknown
  scale is a hang. Bound it (`-maxdepth 2`, a named subdirectory) or measure
  one level at a time with `ls` until you know where to look. The native
  `grep`/`glob` tools stop themselves and report a partial search; a shell
  command has nobody to stop it.
- For the primary agent, bare `shell.exec("cd path")` updates the CLI process
  working directory. Sub-agents should pass `cwd` or use `cd path && command`
  so concurrent agents never mutate one process-global directory.
- A non-zero exit code means the command ran and failed; inspect its preserved
  stdout/stderr before concluding that the shell executor itself failed.
- Read command errors carefully. Change arguments or approach before retrying.
- Use project-native scripts when obvious: `npm run build`, `npm test`, `pytest`, `cargo test`, etc.
- Long-running servers should be started intentionally; report the URL and leave the process running only when useful.
- Never pipe untrusted downloads into a shell.
