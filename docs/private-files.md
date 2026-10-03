# Private file permissions

The local owner manages rules with `/private`; it is not a model-facing tool.

```text
/private set "./credentials" hidden
/private set "./config.json" no-read no-write
/private set "./reference" no-write
/private list
/private remove "./config.json"
```

`hidden` removes the file/folder and its descendants from tool listings and searches and denies reading/writing. `no-read` permits visible names but denies contents. `no-write` permits reads but denies modifications/deletion. Restrictions combine across ancestors, and both lexical and resolved symlink paths are checked. Recursive deletions are checked before mutation. Existing write/edit tools return old-content diffs and therefore also need read permission.

Rules are absolute paths in `~/.laintas/private-files.json`, atomically written with mode 0600. The policy file and password-vault directory are hidden from model file tools. Corrupt or inaccessible settings fail closed. Rules remain attached to a path; after an owner moves a file outside the CLI, review its rule.

Native commands could bypass these application checks. While custom rules exist, native AI execution is denied, including audit-mode policy, terminal/session tools, browser/Windows tools, app tools, executable custom commands and extensions. Native hooks and extension callbacks are skipped. Use the protected file tools. Existing processes and OS-level user access are outside this boundary; this feature does not encrypt private file contents or implement an OS sandbox.

Automatic prompt/memory/skill loading and cached references check read permission. Hidden current directories are omitted from generated model context. Local storage transfers and evidence collection also check file permissions.

The local `/helpwo` file bridge also enforces these rules. Its restricted files can be managed by changing rules from the local owner terminal. Cloud Helpwo maintains separate browser rules with the same semantics; rules are not automatically synchronized between devices.

See `/root/Helpwo/docs/private-files-and-passwords.md` in the joint workspace for Helpwo's UI and password-vault contract.
