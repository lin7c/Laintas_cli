# CLI fixed-text inventory


> 本清单为当时的源码快照。文案分类与模板仍可参考；后续源码变更会移动行号，定位时请同时按文案或函数名搜索。
This inventory covers user-output templates and static UI text in registered CLI runtime modules and installer scripts. It excludes website text, tests, build tooling and external extensions. Dynamic expressions remain in `{...}`; no runtime values are read.

The list is a static source inventory. Error/message candidates may be internal. Values assembled in dictionaries, returned by helpers, or supplied by servers/extensions require follow-up tracing. The JSON also records output arguments that could not be resolved statically.


4021 source entries across 88 files; 149 variable-driven output arguments recorded for follow-up.

[Machine-readable inventory](cli-static-text-inventory.json)

## The version/update examples

Both example lines are rendered by `handle_version_command`. The local version comes from [version.py:8](/root/laintas_cli/version.py:8), via `updater.LOCAL_VERSION`; the remote version comes from the fetched manifest. Neither output template has a hardcoded version number.

| Source | Fixed template |
|---|---|
| [laintas_cli.py:13582](/root/laintas_cli/laintas_cli.py:13582) | `Usage: /v  \|  /v check  \|  /v update [--force] [--overwrite-local]  \|  /v enterprise [on\|off\|gateway]` |
| [laintas_cli.py:13586](/root/laintas_cli/laintas_cli.py:13586) | `laintas-cli v{updater.LOCAL_VERSION} ({'binary' if updater.is_frozen() else 'source'} install)` |
| [laintas_cli.py:13593](/root/laintas_cli/laintas_cli.py:13593) | `Could not reach the update server: {e}` |
| [laintas_cli.py:13601](/root/laintas_cli/laintas_cli.py:13601) | `Update available: v{remote_ver}` |
| [laintas_cli.py:13603](/root/laintas_cli/laintas_cli.py:13603) | `  {manifest['notes']}` |
| [laintas_cli.py:13605](/root/laintas_cli/laintas_cli.py:13605) | `Latest is v{remote_ver} — you are up to date.` |
| [laintas_cli.py:13609](/root/laintas_cli/laintas_cli.py:13609) | `Run /v update to install it.` |
| [laintas_cli.py:13613](/root/laintas_cli/laintas_cli.py:13613) | `Nothing to update. Use /v update --force to re-apply the latest.` |
| [laintas_cli.py:13618](/root/laintas_cli/laintas_cli.py:13618) | `Binary install — replacing the whole executable (partial update only applies to source installs).` |
| [laintas_cli.py:13622](/root/laintas_cli/laintas_cli.py:13622) | `Updated binary at {new_path}. Restarting...` |
| [laintas_cli.py:13634](/root/laintas_cli/laintas_cli.py:13634) | `All files already match the latest — nothing to download.` |
| [laintas_cli.py:13644](/root/laintas_cli/laintas_cli.py:13644) | `{updater.install_dir()} is a git checkout — not updating it from the release.` |
| [laintas_cli.py:13646](/root/laintas_cli/laintas_cli.py:13646) | `These {len(changed)} file(s) differ from v{remote_ver}; applying the release would overwrite them:` |
| [laintas_cli.py:13649](/root/laintas_cli/laintas_cli.py:13649) | `  {escape(name)}` |
| [laintas_cli.py:13651](/root/laintas_cli/laintas_cli.py:13651) | `  … and {len(changed) - 20} more` |
| [laintas_cli.py:13652](/root/laintas_cli/laintas_cli.py:13652) | `Update this checkout with git pull. To overwrite it with the release anyway: /v update --overwrite-local — that one keeps a copy of the replaced files under .laintas-update-backup/.` |
| [laintas_cli.py:13658](/root/laintas_cli/laintas_cli.py:13658) | `Downloading {len(changed)} changed file(s):` |
| [laintas_cli.py:13662](/root/laintas_cli/laintas_cli.py:13662) | `Update failed — no changes applied.` |
| [laintas_cli.py:13665](/root/laintas_cli/laintas_cli.py:13665) | `Updated to v{remote_ver} ({len(changed)} file(s) replaced). Restarting...` |
| [laintas_cli.py:13709](/root/laintas_cli/laintas_cli.py:13709) | `Usage: /v enterprise [on\|off\|gateway] [--force] ⏎   on      — Install or update the organisation layer (default) ⏎   off     — Remove it and go back to a personal CLI ⏎   gateway — Download the self-hosted gateway bundle ⏎   --force — Allow replacing the installed version with an older one` |
| [laintas_cli.py:13721](/root/laintas_cli/laintas_cli.py:13721) | `Organisation layer removed — /org and organisation rules are gone.` |
| [laintas_cli.py:13721](/root/laintas_cli/laintas_cli.py:13721) | `No organisation layer was installed.` |
| [laintas_cli.py:13735](/root/laintas_cli/laintas_cli.py:13735) | ` ⏎ Gateway bundle downloaded. ⏎   Run the following inside the extracted directory: ⏎     ./install.sh` |
| [laintas_cli.py:13741](/root/laintas_cli/laintas_cli.py:13741) | `Enterprise installation failed: {escape(str(exc))}` |

## Categories

| Category | Source entries |
|---|---:|
| output | 1445 |
| error candidate | 726 |
| message candidate | 549 |
| title/menu/help | 410 |
| command/help | 389 |
| tool/help | 145 |
| label/render | 129 |
| resolved output candidate | 76 |
| installer source | 60 |
| status/prompt | 51 |
| startup/notice | 31 |
| display constant candidate | 10 |

## Files

| File | Source entries |
|---|---:|
| [laintas_cli.py](/root/laintas_cli/laintas_cli.py) | 2089 |
| [tools.py](/root/laintas_cli/tools.py) | 680 |
| [agent_loop.py](/root/laintas_cli/agent_loop.py) | 145 |
| [password_vault.py](/root/laintas_cli/password_vault.py) | 79 |
| [password_vault_ui.py](/root/laintas_cli/password_vault_ui.py) | 60 |
| [helpwo_server.py](/root/laintas_cli/helpwo_server.py) | 57 |
| [terminal_link.py](/root/laintas_cli/terminal_link.py) | 57 |
| [extension_manager.py](/root/laintas_cli/extension_manager.py) | 49 |
| [context_snapshot.py](/root/laintas_cli/context_snapshot.py) | 42 |
| [browser_session.py](/root/laintas_cli/browser_session.py) | 38 |
| [updater.py](/root/laintas_cli/updater.py) | 36 |
| [web_search.py](/root/laintas_cli/web_search.py) | 34 |
| [workgraph.py](/root/laintas_cli/workgraph.py) | 32 |
| [laintas_cli_download/public/install.sh](/root/laintas_cli/laintas_cli_download/public/install.sh) | 31 |
| [extension_runtime.py](/root/laintas_cli/extension_runtime.py) | 29 |
| [hwg_adapter/adapter.py](/root/laintas_cli/hwg_adapter/adapter.py) | 29 |
| [handoff.py](/root/laintas_cli/handoff.py) | 27 |
| [retask.py](/root/laintas_cli/retask.py) | 27 |
| [laintas_cli_download/public/install.ps1](/root/laintas_cli/laintas_cli_download/public/install.ps1) | 26 |
| [hwo_runner.py](/root/laintas_cli/hwo_runner.py) | 18 |
| [contract_store.py](/root/laintas_cli/contract_store.py) | 17 |
| [account_tasks.py](/root/laintas_cli/account_tasks.py) | 16 |
| [station_ui.py](/root/laintas_cli/station_ui.py) | 16 |
| [vision.py](/root/laintas_cli/vision.py) | 16 |
| [workflow_engine.py](/root/laintas_cli/workflow_engine.py) | 16 |
| [enterprise_installer.py](/root/laintas_cli/enterprise_installer.py) | 14 |
| [hwo_adapter/adapter.py](/root/laintas_cli/hwo_adapter/adapter.py) | 14 |
| [terminal_link_runtime.py](/root/laintas_cli/terminal_link_runtime.py) | 14 |
| [local_runtime.py](/root/laintas_cli/local_runtime.py) | 13 |
| [precheck.py](/root/laintas_cli/precheck.py) | 13 |
| [agents_mode.py](/root/laintas_cli/agents_mode.py) | 12 |
| [mem_signals.py](/root/laintas_cli/mem_signals.py) | 12 |
| [redactor.py](/root/laintas_cli/redactor.py) | 11 |
| [shared_storage.py](/root/laintas_cli/shared_storage.py) | 11 |
| [skills.py](/root/laintas_cli/skills.py) | 11 |
| [task_handoff.py](/root/laintas_cli/task_handoff.py) | 11 |
| [private_files.py](/root/laintas_cli/private_files.py) | 10 |
| [stuck_signals.py](/root/laintas_cli/stuck_signals.py) | 10 |
| [agent_contract.py](/root/laintas_cli/agent_contract.py) | 9 |
| [config_file.py](/root/laintas_cli/config_file.py) | 9 |
| [rag_signals.py](/root/laintas_cli/rag_signals.py) | 9 |
| [release.py](/root/laintas_cli/release.py) | 9 |
| [account_store.py](/root/laintas_cli/account_store.py) | 8 |
| [download_progress.py](/root/laintas_cli/download_progress.py) | 8 |
| [extension_scanner.py](/root/laintas_cli/extension_scanner.py) | 7 |
| [model_route.py](/root/laintas_cli/model_route.py) | 7 |
| [agent_roles.py](/root/laintas_cli/agent_roles.py) | 6 |
| [evolution_lab.py](/root/laintas_cli/evolution_lab.py) | 6 |
| [mem_extract.py](/root/laintas_cli/mem_extract.py) | 6 |
| [paths.py](/root/laintas_cli/paths.py) | 6 |
| [prompt_budget.py](/root/laintas_cli/prompt_budget.py) | 6 |
| [prop_ui.py](/root/laintas_cli/prop_ui.py) | 6 |
| [backend_profiles.py](/root/laintas_cli/backend_profiles.py) | 5 |
| [branch.py](/root/laintas_cli/branch.py) | 5 |
| [hwg_runner.py](/root/laintas_cli/hwg_runner.py) | 5 |
| [agents_transcript.py](/root/laintas_cli/agents_transcript.py) | 4 |
| [durable_rules.py](/root/laintas_cli/durable_rules.py) | 4 |
| [hwg_view.py](/root/laintas_cli/hwg_view.py) | 4 |
| [hwo_ui.py](/root/laintas_cli/hwo_ui.py) | 4 |
| [hwo_view.py](/root/laintas_cli/hwo_view.py) | 4 |
| [mcp_client.py](/root/laintas_cli/mcp_client.py) | 4 |
| [prompt_lab.py](/root/laintas_cli/prompt_lab.py) | 4 |
| [station_service.py](/root/laintas_cli/station_service.py) | 4 |
| [worktree_manager.py](/root/laintas_cli/worktree_manager.py) | 4 |
| [app_host.py](/root/laintas_cli/app_host.py) | 3 |
| [build/mac/install.sh](/root/laintas_cli/build/mac/install.sh) | 3 |
| [memory_system.py](/root/laintas_cli/memory_system.py) | 3 |
| [migrate.py](/root/laintas_cli/migrate.py) | 3 |
| [prompt_opt.py](/root/laintas_cli/prompt_opt.py) | 3 |
| [resource_ui.py](/root/laintas_cli/resource_ui.py) | 3 |
| [transcript_view.py](/root/laintas_cli/transcript_view.py) | 3 |
| [enterprise.py](/root/laintas_cli/enterprise.py) | 2 |
| [file_lock.py](/root/laintas_cli/file_lock.py) | 2 |
| [patch_adapter/adapter.py](/root/laintas_cli/patch_adapter/adapter.py) | 2 |
| [peer_coordination.py](/root/laintas_cli/peer_coordination.py) | 2 |
| [runtime_host.py](/root/laintas_cli/runtime_host.py) | 2 |
| [trust_store.py](/root/laintas_cli/trust_store.py) | 2 |
| [acp_agent.py](/root/laintas_cli/acp_agent.py) | 1 |
| [auto_pilot.py](/root/laintas_cli/auto_pilot.py) | 1 |
| [context_router.py](/root/laintas_cli/context_router.py) | 1 |
| [identity_store.py](/root/laintas_cli/identity_store.py) | 1 |
| [mode_manager.py](/root/laintas_cli/mode_manager.py) | 1 |
| [policy.py](/root/laintas_cli/policy.py) | 1 |
| [retask_view.py](/root/laintas_cli/retask_view.py) | 1 |
| [session_store.py](/root/laintas_cli/session_store.py) | 1 |
| [terminal_preferences.py](/root/laintas_cli/terminal_preferences.py) | 1 |
| [usage_tracker.py](/root/laintas_cli/usage_tracker.py) | 1 |
| [version.py](/root/laintas_cli/version.py) | 3 |

## Complete source entries

### account_store.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [27](/root/laintas_cli/account_store.py:27) | error candidate | freeze_admissions | `An Agent is running; account switch cancelled` |
| [57](/root/laintas_cli/account_store.py:57) | error candidate | profile_dir | `An account needs a verified userId` |
| [79](/root/laintas_cli/account_store.py:79) | error candidate | resolve | `{'Ambiguous' if matches else 'Unknown'} account {value!r}; use /account list` |
| [87](/root/laintas_cli/account_store.py:87) | error candidate | remember | `Account aliases use 1-64 letters, digits, '.', '-' or '_'` |
| [92](/root/laintas_cli/account_store.py:92) | error candidate | remember | `Account alias {alias!r} is already in use` |
| [161](/root/laintas_cli/account_store.py:161) | error candidate | launch_account | `--account needs an account alias, email or userId` |
| [196](/root/laintas_cli/account_store.py:196) | error candidate | stamp | `Task belongs to another account; accept a handoff to continue it` |
| [207](/root/laintas_cli/account_store.py:207) | error candidate | frozen_auth | `Authentication does not match the task's execution account` |

### account_tasks.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [20](/root/laintas_cli/account_tasks.py:20) | error candidate | save_received | `This received task was deleted; create a new handoff` |
| [26](/root/laintas_cli/account_tasks.py:26) | error candidate | save_received | `Received task conflicts with an existing session` |
| [37](/root/laintas_cli/account_tasks.py:37) | error candidate | activate | `Task acceptance needs an interactive CLI` |
| [39](/root/laintas_cli/account_tasks.py:39) | output | activate / 0 | `This received task is already current.` |
| [71](/root/laintas_cli/account_tasks.py:71) | error candidate | _primary_receiver | `Select /agent primary before accepting a task` |
| [84](/root/laintas_cli/account_tasks.py:84) | error candidate | accept | `Open the source repository before accepting this task` |
| [87](/root/laintas_cli/account_tasks.py:87) | error candidate | accept | `This handoff is closed; reopen it before accepting` |
| [89](/root/laintas_cli/account_tasks.py:89) | error candidate | accept | `Another account holds this task; it must release it first` |
| [107](/root/laintas_cli/account_tasks.py:107) | output | accept / 0 | `Task accepted into a new session for this account.` |
| [109](/root/laintas_cli/account_tasks.py:109) | output | accept / 0 | `Task is ready. Enter /continue to begin.` |
| [142](/root/laintas_cli/account_tasks.py:142) | error candidate | adopt | `Select an account before adopting a legacy task` |
| [148](/root/laintas_cli/account_tasks.py:148) | error candidate | adopt | `Unknown legacy task; use /account legacy` |
| [150](/root/laintas_cli/account_tasks.py:150) | error candidate | adopt | `The legacy task is still running in another CLI` |
| [156](/root/laintas_cli/account_tasks.py:156) | error candidate | adopt | `Legacy ownership record is unreadable; adoption cancelled` |
| [158](/root/laintas_cli/account_tasks.py:158) | error candidate | adopt | `This legacy task has already been adopted by another account` |
| [186](/root/laintas_cli/account_tasks.py:186) | output | adopt / 0 | `Legacy task adopted; its original files are preserved.` |

### acp_agent.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [214](/root/laintas_cli/acp_agent.py:214) | title/menu/help | LaintasAcpAgent.initialize / title | `Laintas CLI` |

### agent_contract.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [78](/root/laintas_cli/agent_contract.py:78) | error candidate | normalize | `contract must be an object` |
| [85](/root/laintas_cli/agent_contract.py:85) | error candidate | normalize | `each output needs a name` |
| [89](/root/laintas_cli/agent_contract.py:89) | error candidate | normalize | `unknown output type {typ!r}; use one of {', '.join(_OUTPUT_TYPES)}` |
| [100](/root/laintas_cli/agent_contract.py:100) | error candidate | normalize | `each acceptance check must be an object` |
| [104](/root/laintas_cli/agent_contract.py:104) | error candidate | normalize | `unknown acceptance check {kind!r}; use one of {', '.join(_CHECK_KINDS)}` |
| [107](/root/laintas_cli/agent_contract.py:107) | error candidate | normalize | `a check's ˋwhenˋ must be an object` |
| [122](/root/laintas_cli/agent_contract.py:122) | error candidate | normalize | `scope must be an object` |
| [139](/root/laintas_cli/agent_contract.py:139) | error candidate | normalize | `a contract with no declared outputs cannot be verified; declare at least one output or spawn without a contract` |
| [478](/root/laintas_cli/agent_contract.py:478) | message candidate | scope_violation / error | `{abs_path} is outside the scope your contract declares ({allowed}). Reading anywhere is fine; changing anything outside that list is not. If the work genuinely needs it, say so in your outputs and let your caller decide.` |

### agent_loop.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [551](/root/laintas_cli/agent_loop.py:551) | label/render | _ActivityStatus._renderable._Wrapper.__rich__ | ` {elapsed:.1f}s` |
| [554](/root/laintas_cli/agent_loop.py:554) | label/render | _ActivityStatus._renderable._Wrapper.__rich__ | ` {symbols.BULLET} {_live_status_model() or '—'} {symbols.BULLET} {_active_mode_label()}` |
| [844](/root/laintas_cli/agent_loop.py:844) | output | _close_failed_turn / 0 | `Your question was not answered ({label}). “{preview}…” — it will not resume by itself; ask again to retry it.` |
| [1132](/root/laintas_cli/agent_loop.py:1132) | error candidate | _coerce_runtime_config_value | `Unknown config key: {key}` |
| [1147](/root/laintas_cli/agent_loop.py:1147) | error candidate | _coerce_runtime_config_value | `{key} expects a boolean: true/false, yes/no, on/off, or 1/0` |
| [1149](/root/laintas_cli/agent_loop.py:1149) | error candidate | _coerce_runtime_config_value | `{key} expects a boolean` |
| [1154](/root/laintas_cli/agent_loop.py:1154) | error candidate | _coerce_runtime_config_value | `{key} expects an integer, got {value!r}` |
| [1159](/root/laintas_cli/agent_loop.py:1159) | error candidate | _coerce_runtime_config_value | `{key} expects a number, got {value!r}` |
| [1168](/root/laintas_cli/agent_loop.py:1168) | error candidate | _coerce_runtime_config_value | `critic_prompt_file cannot contain a null byte` |
| [1185](/root/laintas_cli/agent_loop.py:1185) | error candidate | _coerce_runtime_config_value | `{key} contains unknown slot {token!r}; expected {', '.join(_RPROMPT_SLOT_IDS)}` |
| [1195](/root/laintas_cli/agent_loop.py:1195) | error candidate | _coerce_runtime_config_value | `{key} expects {', '.join(_RUNTIME_ENUM_CHOICES[key])}` |
| [1198](/root/laintas_cli/agent_loop.py:1198) | error candidate | _coerce_runtime_config_value | `{key} must be greater than 0` |
| [1200](/root/laintas_cli/agent_loop.py:1200) | error candidate | _coerce_runtime_config_value | `{key} must be 0 or greater` |
| [1202](/root/laintas_cli/agent_loop.py:1202) | error candidate | _coerce_runtime_config_value | `output_similarity must be between 0 and 1` |
| [1206](/root/laintas_cli/agent_loop.py:1206) | error candidate | _coerce_runtime_config_value | `{key} must be between {low} and {high}` |
| [1209](/root/laintas_cli/agent_loop.py:1209) | error candidate | _coerce_runtime_config_value | `browser_action_delay_min cannot exceed browser_action_delay_max` |
| [1212](/root/laintas_cli/agent_loop.py:1212) | error candidate | _coerce_runtime_config_value | `browser_action_delay_max cannot be below browser_action_delay_min` |
| [1222](/root/laintas_cli/agent_loop.py:1222) | error candidate | _check_compaction_order | `Compaction requires target < background < foreground <= 1` |
| [1500](/root/laintas_cli/agent_loop.py:1500) | output | _persist_warn / 0 | `(persistence warning: {context}: {type(exc).__name__}: {exc})` |
| [1502](/root/laintas_cli/agent_loop.py:1502) | output | _persist_warn / 0 | `(persistence warning: {context}: {type(exc).__name__}: {exc})` |
| [1641](/root/laintas_cli/agent_loop.py:1641) | error candidate | register_terminal | `Terminal '{name}' already exists` |
| [1649](/root/laintas_cli/agent_loop.py:1649) | error candidate | register_terminal | `Parent terminal '{parent_terminal or '(none)'}' does not exist` |
| [1654](/root/laintas_cli/agent_loop.py:1654) | error candidate | register_terminal | `Parent terminal '{parent_terminal}' is not running` |
| [1656](/root/laintas_cli/agent_loop.py:1656) | error candidate | register_terminal | `A terminal cannot be its own parent` |
| [3173](/root/laintas_cli/agent_loop.py:3173) | error candidate | delete_resume_state | `Saved task belongs to another account` |
| [3175](/root/laintas_cli/agent_loop.py:3175) | error candidate | delete_resume_state | `Saved session belongs to a different directory` |
| [3276](/root/laintas_cli/agent_loop.py:3276) | error candidate | delete_resume_state | `Session {session_id} is open (pid {owner.get('pid')}). Switch away or close it before deleting this session tree.` |
| [3570](/root/laintas_cli/agent_loop.py:3570) | error candidate | register_agent | `Agent '{agent_id}' already exists` |
| [3626](/root/laintas_cli/agent_loop.py:3626) | error candidate | register_agent | `Terminal '{inherited_terminal}' is already deployed to agent '{owner_id}'` |
| [3987](/root/laintas_cli/agent_loop.py:3987) | error candidate | get_or_hire_pool_agent | `No deployed employee is available; hire one from a live terminal first` |
| [4131](/root/laintas_cli/agent_loop.py:4131) | resolved output candidate | start_agent_assignment._finish | `agent_aborted` |
| [4131](/root/laintas_cli/agent_loop.py:4131) | resolved output candidate | start_agent_assignment._finish | `agent_error` |
| [4131](/root/laintas_cli/agent_loop.py:4131) | resolved output candidate | start_agent_assignment._finish | `agent_done` |
| [4156](/root/laintas_cli/agent_loop.py:4156) | output | start_agent_assignment._runner / 0 | `agent_started` |
| [4219](/root/laintas_cli/agent_loop.py:4219) | output | start_agent_assignment / 0 | `user_message` |
| [4827](/root/laintas_cli/agent_loop.py:4827) | output | send_to_agent / 0 | `agent_message` |
| [4846](/root/laintas_cli/agent_loop.py:4846) | output | send_to_agent / 0 | `agent_message_failed` |
| [5198](/root/laintas_cli/agent_loop.py:5198) | message candidate | spawn_subagent / error | `Cannot spawn: maximum agent depth (3) reached.` |
| [5201](/root/laintas_cli/agent_loop.py:5201) | message candidate | spawn_subagent / message | `maximum agent depth reached` |
| [5266](/root/laintas_cli/agent_loop.py:5266) | output | spawn_subagent / 0 | `agent_spawned` |
| [5270](/root/laintas_cli/agent_loop.py:5270) | output | spawn_subagent / 0 | `agent.spawn` |
| [5299](/root/laintas_cli/agent_loop.py:5299) | error candidate | spawn_subagent | `Automatic writing task requires an isolated Git worktree.` |
| [5416](/root/laintas_cli/agent_loop.py:5416) | output | spawn_subagent._runner / 0 | `agent_error` |
| [5445](/root/laintas_cli/agent_loop.py:5445) | output | spawn_subagent._runner / 0 | `agent_started` |
| [5561](/root/laintas_cli/agent_loop.py:5561) | output | spawn_subagent._runner / 0 | `agent_done` |
| [5607](/root/laintas_cli/agent_loop.py:5607) | output | spawn_subagent._runner / 0 | `agent_error` |
| [5659](/root/laintas_cli/agent_loop.py:5659) | message candidate | resume_subagent / error | `agent '{agent_id}' not found` |
| [5662](/root/laintas_cli/agent_loop.py:5662) | message candidate | resume_subagent / error | `agent '{agent_id}' is not a spawn sub-agent (role={child.role})` |
| [5667](/root/laintas_cli/agent_loop.py:5667) | message candidate | resume_subagent / error | `agent '{agent_id}' has no saved runner (not a spawn child or already cleaned up)` |
| [5671](/root/laintas_cli/agent_loop.py:5671) | message candidate | resume_subagent / error | `agent '{agent_id}' is not finished (status={child.status}); tell it directly instead` |
| [5675](/root/laintas_cli/agent_loop.py:5675) | message candidate | resume_subagent / error | `agent '{agent_id}' lifecycle is terminated` |
| [5711](/root/laintas_cli/agent_loop.py:5711) | message candidate | resume_subagent / error | `could not restart child thread: {exc}` |
| [5840](/root/laintas_cli/agent_loop.py:5840) | output | _print_markdown_safely / 0 | `Markdown highlighting failed; output was shown as plain text. The installed binary may be damaged. Run /v update --force or reinstall laintas-cli.` |
| [7143](/root/laintas_cli/agent_loop.py:7143) | output | _compaction_status.update / 0 | `{detail}` |
| [7439](/root/laintas_cli/agent_loop.py:7439) | output | _compact_thread_messages / 0 | `Context compacted: {before_tokens:,} → {_thread_tokens(candidate):,} tokens (trigger {usable:,}).` |
| [7825](/root/laintas_cli/agent_loop.py:7825) | output | publish_budget_report / 0 | `Context budget: {', '.join(fresh)} over its share — compressed in proportion. /prop budget shows what was sent.` |
| [8301](/root/laintas_cli/agent_loop.py:8301) | message candidate | compact_session_context / error | `background compaction is still cancelling; try again shortly` |
| [8304](/root/laintas_cli/agent_loop.py:8304) | message candidate | compact_session_context / error | `context compaction policy is unavailable` |
| [8307](/root/laintas_cli/agent_loop.py:8307) | message candidate | compact_session_context / error | `system prompt, tools and output reserve exhaust the context window` |
| [8344](/root/laintas_cli/agent_loop.py:8344) | message candidate | compact_session_context / error | `the context summarizer returned no usable summary` |
| [8507](/root/laintas_cli/agent_loop.py:8507) | message candidate | _write_cap_violation / error | `Rejected: {lines} lines written in one {name} call, over the current {cap}-line cap. Your previous response was cut off at the output token limit, so this call would be cut off too. Write at most {cap} lines per call: the first part now, the rest in a follow-up call.` |
| [10325](/root/laintas_cli/agent_loop.py:10325) | title/menu/help | _format_tool_result_for_loop / header | `{prefix} {err} ⏎ ` |
| [10326](/root/laintas_cli/agent_loop.py:10326) | title/menu/help | _format_tool_result_for_loop / header | `{prefix} ⏎ ` |
| [10337](/root/laintas_cli/agent_loop.py:10337) | title/menu/help | _format_tool_result_for_loop / header | `{prefix} {err} ⏎ ` |
| [10338](/root/laintas_cli/agent_loop.py:10338) | title/menu/help | _format_tool_result_for_loop / header | `{prefix} ⏎ ` |
| [10502](/root/laintas_cli/agent_loop.py:10502) | message candidate | _dispatch_read_only_batch / error | `parallel dispatch failed: {exc}` |
| [10548](/root/laintas_cli/agent_loop.py:10548) | message candidate | ask_parent_for_help / error | `no such agent` |
| [10551](/root/laintas_cli/agent_loop.py:10551) | message candidate | ask_parent_for_help / error | `you have no caller to ask; decide yourself and record the decision in your result` |
| [10621](/root/laintas_cli/agent_loop.py:10621) | message candidate | answer_child_help / error | `no such agent '{child_id}'` |
| [10624](/root/laintas_cli/agent_loop.py:10624) | message candidate | answer_child_help / error | `'{child_id}' is not your child; only the agent that spawned it can answer it` |
| [10913](/root/laintas_cli/agent_loop.py:10913) | message candidate | _authorize_tool_call / error | `BLOCKED: you have already called ˋ{name}ˋ on ˋ{salient[:100]}ˋ {_prev_n} times and it failed every time with the same deterministic error: ⏎ {_last_err[:300]} ⏎ Re-running it will fail identically. Stop repeating this call — either fix the underlying cause with a DIFFERENT action, or if this sub-goal is impossible, move on / call task_complete and report it.` |
| [10933](/root/laintas_cli/agent_loop.py:10933) | message candidate | _authorize_tool_call / error | `BLOCKED: tool '{name}' is disabled in the Prompt Lab no-side-effects test sandbox.` |
| [10942](/root/laintas_cli/agent_loop.py:10942) | message candidate | _authorize_tool_call / error | `BLOCKED: tool '{name}' is disabled while an Evolution Lab worker is designing a candidate.` |
| [10960](/root/laintas_cli/agent_loop.py:10960) | message candidate | _authorize_tool_call / error | `BLOCKED: tool '{name}' cannot be used while this agent has a scope.paths write boundary. Use structured fs tools, whose targets are checked against the declared paths.` |
| [10972](/root/laintas_cli/agent_loop.py:10972) | message candidate | _authorize_tool_call / error | `BLOCKED: tool '{name}' is not available to agent '{agent_id or 'current'}'.` |
| [10982](/root/laintas_cli/agent_loop.py:10982) | message candidate | _authorize_tool_call / error | `BLOCKED: tool '{name}' is not allowed in Plan Mode. Use read-only exploration or plan.update, then obtain user approval before implementation.` |
| [11008](/root/laintas_cli/agent_loop.py:11008) | message candidate | _authorize_tool_call / error | `BLOCKED: tool '{name}' is not allowed in {active_mode_name.upper()} mode.{blocked_hint}` |
| [11016](/root/laintas_cli/agent_loop.py:11016) | message candidate | _authorize_tool_call / error | `BLOCKED: tool '{name}' not allowed for role '{state.get('_role_name')}'` |
| [11024](/root/laintas_cli/agent_loop.py:11024) | message candidate | _authorize_tool_call / error | `BLOCKED: tool '{name}' not allowed in current workflow phase` |
| [11032](/root/laintas_cli/agent_loop.py:11032) | message candidate | _authorize_tool_call / error | `BLOCKED: tool '{name}' is outside this workflow node's tool scope ({', '.join(tool_scope)}). The scope is declared in the .hwg node policy and cannot be widened from here. Do the task with the tools listed, or report why it cannot be done.` |
| [11143](/root/laintas_cli/agent_loop.py:11143) | output | _observed_turn.wrapped / 0 | `turn.end` |
| [11261](/root/laintas_cli/agent_loop.py:11261) | error candidate | run_agent_loop | `A linked Agent cannot execute locally; use /station.` |
| [11459](/root/laintas_cli/agent_loop.py:11459) | output | run_agent_loop / 0 | `turn.start` |
| [11495](/root/laintas_cli/agent_loop.py:11495) | error candidate | run_agent_loop | `max_loops_override must be greater than 0` |
| [11673](/root/laintas_cli/agent_loop.py:11673) | output | run_agent_loop / 0 | ` ⏎ Interrupted by user (Ctrl+C).` |
| [11738](/root/laintas_cli/agent_loop.py:11738) | output | run_agent_loop / 0 | ` ⏎ Evolution Lab branch {_branch['id']} created; worker {_lab_child} started.` |
| [11738](/root/laintas_cli/agent_loop.py:11738) | output | run_agent_loop / 0 | ` ⏎ Evolution Lab branch {_branch['id']} created.` |
| [11742](/root/laintas_cli/agent_loop.py:11742) | output | run_agent_loop / 0 | ` ⏎ Evolution Lab error: {exc}` |
| [11797](/root/laintas_cli/agent_loop.py:11797) | output | run_agent_loop / 0 | ` ⏎ Prompt Lab branch {_branch['id']} captured; the main task is unchanged.` |
| [11801](/root/laintas_cli/agent_loop.py:11801) | output | run_agent_loop / 0 | ` ⏎ Could not create Prompt Lab branch: {_lab_error}` |
| [11816](/root/laintas_cli/agent_loop.py:11816) | output | run_agent_loop / 0 | ` ⏎ │ › {_esc(supp_text)}` |
| [12067](/root/laintas_cli/agent_loop.py:12067) | output | run_agent_loop / 0 | `{symbols.WARN} Intent check: unresolved after {_intent_st['debate_round']} rounds — the agent has been asked to put this to you: {_question}` |
| [12747](/root/laintas_cli/agent_loop.py:12747) | output | run_agent_loop / 0 | `Context window exhausted by system prompt, tools and output reserve; reduce them or select a larger model.` |
| [12757](/root/laintas_cli/agent_loop.py:12757) | output | run_agent_loop / 0 | `Context compaction is still cancelling; retry this turn shortly.` |
| [12760](/root/laintas_cli/agent_loop.py:12760) | output | run_agent_loop / 0 | `Context still exceeds the model budget after compaction; history was retained. Retry /compact or select a larger model.` |
| [12843](/root/laintas_cli/agent_loop.py:12843) | output | run_agent_loop / 0 | `{symbols.WARN} Intent check: the work so far does not match the request as read — corrective guidance injected` |
| [12891](/root/laintas_cli/agent_loop.py:12891) | output | run_agent_loop / 0 | `{symbols.WARN} Progress critic disabled after {_critic_fail_streak} consecutive failures ({_crit_fail}); goal-drift checks are off for the rest of this task.` |
| [12937](/root/laintas_cli/agent_loop.py:12937) | output | run_agent_loop / 0 | `{symbols.WARN} Progress check: still off track (score {_verdict.get('score')}/100) after repeated corrections — escalated.` |
| [12958](/root/laintas_cli/agent_loop.py:12958) | output | run_agent_loop / 0 | `{symbols.WARN} Progress check: off track (score {_verdict.get('score')}/100) — corrective guidance injected` |
| [12991](/root/laintas_cli/agent_loop.py:12991) | output | run_agent_loop / 0 | `{symbols.WARN} Critic prompt file ignored: {_crit_prompt_error}` |
| [13204](/root/laintas_cli/agent_loop.py:13204) | output | run_agent_loop._invoke_backend / 0 | `{symbols.WARN} Backend does not accept the full request; retrying without {_what}.` |
| [13236](/root/laintas_cli/agent_loop.py:13236) | error candidate | run_agent_loop._on_chunk | `user interrupt during streaming` |
| [13293](/root/laintas_cli/agent_loop.py:13293) | label/render | run_agent_loop._render | ` {_elapsed:.1f}s` |
| [13296](/root/laintas_cli/agent_loop.py:13296) | label/render | run_agent_loop._render | ` {symbols.BULLET} {symbols.ARROW_U}{_fmt_tokens(_cur_in_est)} {symbols.ARROW_D}{_fmt_tokens(_cur_out_est)}` |
| [13311](/root/laintas_cli/agent_loop.py:13311) | label/render | run_agent_loop._render | ` {symbols.BULLET} {_spin_model}{_gear} {symbols.BULLET} {_spin_mode}` |
| [13337](/root/laintas_cli/agent_loop.py:13337) | label/render | run_agent_loop._render | `→ {cmd_preview}` |
| [13441](/root/laintas_cli/agent_loop.py:13441) | output | run_agent_loop / 0 | ` ⏎ Partial response preserved: {_partial_reply[:300]}` |
| [13489](/root/laintas_cli/agent_loop.py:13489) | output | run_agent_loop / 0 | `(empty response — asking AI to retry)` |
| [13511](/root/laintas_cli/agent_loop.py:13511) | output | run_agent_loop / 0 | `(connection to the model dropped — retrying {silent_count}/2; the interrupted attempt used {_spent} credits)` |
| [13511](/root/laintas_cli/agent_loop.py:13511) | output | run_agent_loop / 0 | `(connection to the model dropped — retrying {silent_count}/2)` |
| [13533](/root/laintas_cli/agent_loop.py:13533) | output | run_agent_loop / 0 | `(context overflow persists after 2 compaction rounds — giving up)` |
| [13538](/root/laintas_cli/agent_loop.py:13538) | output | run_agent_loop / 0 | `(context overflow — compacting and retrying)` |
| [13549](/root/laintas_cli/agent_loop.py:13549) | output | run_agent_loop / 0 | `{_esc(_err_text)}` |
| [13599](/root/laintas_cli/agent_loop.py:13599) | output | run_agent_loop / 0 | `(empty response — asking AI to retry)` |
| [13604](/root/laintas_cli/agent_loop.py:13604) | output | run_agent_loop / 0 | `{msg}` |
| [13636](/root/laintas_cli/agent_loop.py:13636) | output | run_agent_loop / 0 | `assistant.reply` |
| [13651](/root/laintas_cli/agent_loop.py:13651) | output | run_agent_loop / 0 | `{symbols.BULLET} {_esc(_stripped)}` |
| [13762](/root/laintas_cli/agent_loop.py:13762) | output | run_agent_loop / 0 | `{symbols.WARN} Response cut off {_trunc_count} consecutive times ({_kind}) - {_why} - stopping.` |
| [13843](/root/laintas_cli/agent_loop.py:13843) | output | run_agent_loop / 0 | `{symbols.WARN} {_msg}` |
| [13866](/root/laintas_cli/agent_loop.py:13866) | output | run_agent_loop / 0 | `({billing_text})` |
| [13924](/root/laintas_cli/agent_loop.py:13924) | output | run_agent_loop / 0 | `Creating undo checkpoint…` |
| [14006](/root/laintas_cli/agent_loop.py:14006) | output | run_agent_loop / 0 | ` ⏎ Interrupted — skipping remaining {len(tool_calls) - idx} tool call(s).` |
| [14048](/root/laintas_cli/agent_loop.py:14048) | output | run_agent_loop / 0 | `tool.call` |
| [14087](/root/laintas_cli/agent_loop.py:14087) | output | run_agent_loop / 0 | `{symbols.WARN} {_warning}` |
| [14125](/root/laintas_cli/agent_loop.py:14125) | output | run_agent_loop / 0 | `{symbols.WARN} Blocked repeated failing call ˋ{name} {salient[:60]}ˋ (failed {result.get('_repeat_count')}× already).` |
| [14142](/root/laintas_cli/agent_loop.py:14142) | message candidate | run_agent_loop / error | `blocked by pre_tool hook` |
| [14153](/root/laintas_cli/agent_loop.py:14153) | message candidate | run_agent_loop / error | `BLOCKED: {policy_reason}` |
| [14167](/root/laintas_cli/agent_loop.py:14167) | message candidate | run_agent_loop / error | `BLOCKED by pre_command hook` |
| [14199](/root/laintas_cli/agent_loop.py:14199) | output | run_agent_loop / 0 | `.laintas/loop.py error: {e}` |
| [14217](/root/laintas_cli/agent_loop.py:14217) | output | run_agent_loop / 0 | `Extension loop error: {e}` |
| [14588](/root/laintas_cli/agent_loop.py:14588) | output | run_agent_loop / 0 | ` ⏎ User denied approval — terminating task.` |
| [14608](/root/laintas_cli/agent_loop.py:14608) | output | run_agent_loop / 0 | `tool.result` |
| [14612](/root/laintas_cli/agent_loop.py:14612) | output | run_agent_loop / 0 | `tool.batch_end` |
| [14745](/root/laintas_cli/agent_loop.py:14745) | output | run_agent_loop / 0 | `{symbols.WARN} {_warning}` |
| [14879](/root/laintas_cli/agent_loop.py:14879) | output | run_agent_loop / 0 | `{symbols.WARN} Exiting: a tool call kept failing identically and was blocked. The task appears stuck.` |
| [14900](/root/laintas_cli/agent_loop.py:14900) | output | run_agent_loop / 0 | `{symbols.WARN} Output repetition detected: last {_no_progress_count} steps produced highly similar output. Change strategy or the loop will exit.` |
| [14912](/root/laintas_cli/agent_loop.py:14912) | output | run_agent_loop / 0 | `{symbols.WARN} Output repetition detected: last {_no_progress_count} steps produced highly similar output. Exiting to prevent infinite loop.` |
| [14930](/root/laintas_cli/agent_loop.py:14930) | output | run_agent_loop / 0 | `{symbols.WARN} {_warning}` |
| [14954](/root/laintas_cli/agent_loop.py:14954) | output | run_agent_loop / 0 | `[dark_orange]{symbols.WARN} Warning '{wk}' fired {_new_streaks[wk]} consecutive times. Force-exiting to prevent infinite loop.[/dark_orange]` |
| [15052](/root/laintas_cli/agent_loop.py:15052) | output | run_agent_loop / 0 | `{symbols.WARN} {_wrapup_msg}` |
| [15072](/root/laintas_cli/agent_loop.py:15072) | output | run_agent_loop / 0 | `{symbols.WARN} No reply or tool call for {stale_count} idle steps. Monitoring is advisory; the task will continue.` |
| [15082](/root/laintas_cli/agent_loop.py:15082) | output | run_agent_loop / 0 | `Last backend response: {_esc(_raw_text)}` |
| [15113](/root/laintas_cli/agent_loop.py:15113) | output | run_agent_loop / 0 | ` ⏎ Agent loop interrupted during delay.` |
| [15170](/root/laintas_cli/agent_loop.py:15170) | output | run_agent_loop / 0 | `{symbols.WARN} {_exhaustion_msg}` |
| [15240](/root/laintas_cli/agent_loop.py:15240) | output | run_agent_loop / 0 | ` ⏎ {symbols.INFO} Partial response preserved ({len(reply)} chars)` |

### agent_roles.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [286](/root/laintas_cli/agent_roles.py:286) | title/menu/help | _register_builtin_roles / description | `Code exploration expert — traces execution paths, maps architecture, identifies key files` |
| [318](/root/laintas_cli/agent_roles.py:318) | title/menu/help | _register_builtin_roles / description | `Architecture designer — analyzes patterns, designs components, creates implementation blueprints` |
| [332](/root/laintas_cli/agent_roles.py:332) | title/menu/help | _register_builtin_roles / description | `Code review expert — confidence-scored bug detection, quality analysis, convention compliance` |
| [362](/root/laintas_cli/agent_roles.py:362) | title/menu/help | _register_builtin_roles / description | `Error handling auditor — detects silent failures, empty catch blocks, inadequate error propagation` |
| [392](/root/laintas_cli/agent_roles.py:392) | title/menu/help | _register_builtin_roles / description | `Code simplification expert — improves clarity and maintainability while preserving functionality` |
| [405](/root/laintas_cli/agent_roles.py:405) | title/menu/help | _register_builtin_roles / description | `Test coverage analyst — behavioral coverage analysis, critical gap identification, test quality review` |

### agents_mode.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [201](/root/laintas_cli/agents_mode.py:201) | message candidate | <module> / error | `bold #f85149` |
| [237](/root/laintas_cli/agents_mode.py:237) | message candidate | <module> / message | `#a78bfa` |
| [2048](/root/laintas_cli/agents_mode.py:2048) | output | AgentsModeController.dispatch / 0 | `user_message` |
| [2054](/root/laintas_cli/agents_mode.py:2054) | output | AgentsModeController.dispatch / 0 | `user_message_failed` |
| [2060](/root/laintas_cli/agents_mode.py:2060) | output | AgentsModeController.dispatch / 0 | `user_message_failed` |
| [2069](/root/laintas_cli/agents_mode.py:2069) | output | AgentsModeController.dispatch / 0 | `user_message` |
| [2073](/root/laintas_cli/agents_mode.py:2073) | output | AgentsModeController.dispatch / 0 | `input_rejected` |
| [2177](/root/laintas_cli/agents_mode.py:2177) | output | AgentsModeController._request_approval / 0 | `approval_requested` |
| [2183](/root/laintas_cli/agents_mode.py:2183) | output | AgentsModeController._request_approval / 0 | `approval_resolved` |
| [2209](/root/laintas_cli/agents_mode.py:2209) | output | AgentsModeController._request_approval / 0 | `approval_resolved` |
| [2329](/root/laintas_cli/agents_mode.py:2329) | output | AgentsModeController.resolve_approval / 0 | `approval_resolved` |
| [2347](/root/laintas_cli/agents_mode.py:2347) | output | AgentsModeController.deny_pending_approvals / 0 | `approval_resolved` |

### agents_transcript.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [455](/root/laintas_cli/agents_transcript.py:455) | title/menu/help | build_blocks / title | `output` |
| [490](/root/laintas_cli/agents_transcript.py:490) | title/menu/help | build_blocks / title | `action` |
| [500](/root/laintas_cli/agents_transcript.py:500) | title/menu/help | build_blocks / title | `Interrupted` |
| [558](/root/laintas_cli/agents_transcript.py:558) | message candidate | _tool_rows / error | `class:tool.error` |

### app_host.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [429](/root/laintas_cli/app_host.py:429) | error candidate | configure_runtime | `no primary agent in the application` |
| [462](/root/laintas_cli/app_host.py:462) | error candidate | configure_runtime | `terminal '{name}' already exists` |
| [466](/root/laintas_cli/app_host.py:466) | error candidate | configure_runtime | `terminal '{name}' did not start` |

### auto_pilot.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [151](/root/laintas_cli/auto_pilot.py:151) | error candidate | _run_with_timeout | `decompose_task timed out after {timeout}s` |

### backend_profiles.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [58](/root/laintas_cli/backend_profiles.py:58) | error candidate | _normalize_url | `backend URL must be an absolute http(s) URL` |
| [60](/root/laintas_cli/backend_profiles.py:60) | error candidate | _normalize_url | `backend URL must not contain embedded credentials` |
| [62](/root/laintas_cli/backend_profiles.py:62) | error candidate | _normalize_url | `backend URL must not contain query parameters or fragments` |
| [71](/root/laintas_cli/backend_profiles.py:71) | error candidate | _kind_for_url | `official backends require HTTPS` |
| [117](/root/laintas_cli/backend_profiles.py:117) | error candidate | resolve | `invalid backend kind: {requested}` |

### branch.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [210](/root/laintas_cli/branch.py:210) | output | open_branch / 0 | `branch_opened` |
| [218](/root/laintas_cli/branch.py:218) | error candidate | add_member | `Cannot add a member to a sealed or closed branch.` |
| [269](/root/laintas_cli/branch.py:269) | output | _supervise / 0 | `branch_opened` |
| [421](/root/laintas_cli/branch.py:421) | output | _settle / 0 | `member_settled` |
| [502](/root/laintas_cli/branch.py:502) | output | close / 0 | `branch_closed` |

### browser_session.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [86](/root/laintas_cli/browser_session.py:86) | error candidate | validate_browse_url | `a URL is required` |
| [91](/root/laintas_cli/browser_session.py:91) | error candidate | validate_browse_url | `only http(s) URLs may be browsed (got '{u.scheme}')` |
| [94](/root/laintas_cli/browser_session.py:94) | error candidate | validate_browse_url | `not a valid URL: {raw!r}` |
| [100](/root/laintas_cli/browser_session.py:100) | error candidate | validate_browse_url | `cannot resolve host {host!r}: {e}` |
| [112](/root/laintas_cli/browser_session.py:112) | error candidate | validate_browse_url | `refusing to browse {host!r}: resolves to non-public address {ip} (loopback/private/link-local/metadata are blocked)` |
| [177](/root/laintas_cli/browser_session.py:177) | error candidate | _free_tcp_port | `no free TCP port in [{start},{end})` |
| [187](/root/laintas_cli/browser_session.py:187) | error candidate | _free_display | `no free X display in :{start}..:{end}` |
| [613](/root/laintas_cli/browser_session.py:613) | error candidate | ProxyAuthRelay.__init__ | `upstream proxy must be host:port (got {upstream!r})` |
| [684](/root/laintas_cli/browser_session.py:684) | error candidate | ProxyAuthRelay._read_head | `request head too large` |
| [687](/root/laintas_cli/browser_session.py:687) | error candidate | ProxyAuthRelay._read_head | `client closed before sending a full request` |
| [904](/root/laintas_cli/browser_session.py:904) | error candidate | _install_pw_sync_guard._guarded_sync | `Event loop is closed! Is Playwright already stopped?` |
| [928](/root/laintas_cli/browser_session.py:928) | error candidate | _install_pw_sync_guard._guarded_sync | `Playwright call made no progress for {cap:.0f}s and was abandoned (raise LAINTAS_PW_SYNC_CAP if a legitimate call needs longer).` |
| [1225](/root/laintas_cli/browser_session.py:1225) | error candidate | BrowserSession._ensure_worker | `browser session is closed` |
| [1255](/root/laintas_cli/browser_session.py:1255) | error candidate | BrowserSession.run | `browser session is closed` |
| [1268](/root/laintas_cli/browser_session.py:1268) | error candidate | BrowserSession.run | `browser session '{self.session_id}' did not answer in time; its worker thread is wedged and the session should be closed` |
| [1283](/root/laintas_cli/browser_session.py:1283) | error candidate | BrowserSession._get_page | `browser session is closed` |
| [1292](/root/laintas_cli/browser_session.py:1292) | error candidate | BrowserSession._get_page | `browser session '{self.session_id}' is owned by thread {self._pw_tid}; use session.run(fn) instead of touching the page directly` |
| [1313](/root/laintas_cli/browser_session.py:1313) | error candidate | BrowserSession._get_page | `Playwright CDP connection failed` |
| [1524](/root/laintas_cli/browser_session.py:1524) | error candidate | BrowserSession.start | `Xvfb :{self.display_n} did not start (see {self._log('xvfb')})` |
| [1543](/root/laintas_cli/browser_session.py:1543) | error candidate | BrowserSession.start | `Chrome CDP port {self.cdp_port} did not open (see {self._log('chrome')})` |
| [1547](/root/laintas_cli/browser_session.py:1547) | error candidate | BrowserSession.start | `x11vnc on RFB port {self.rfb_port} never sent its banner (see {self._log('x11vnc')})` |
| [1689](/root/laintas_cli/browser_session.py:1689) | error candidate | BrowserSession._start_chrome | `no Chrome/Chromium binary on PATH` |
| [1950](/root/laintas_cli/browser_session.py:1950) | output | _self_test / 0 | `[!] {err}` |
| [1952](/root/laintas_cli/browser_session.py:1952) | output | _self_test / 0 | `[*] starting headless-browser stack for {url}` |
| [1962](/root/laintas_cli/browser_session.py:1962) | output | _self_test / 0 | `[!] start failed: {e}` |
| [1967](/root/laintas_cli/browser_session.py:1967) | output | _self_test / 0 | `[+] session '{name}' up:` |
| [1968](/root/laintas_cli/browser_session.py:1968) | output | _self_test / 0 | `    url         : {sess.url}` |
| [1969](/root/laintas_cli/browser_session.py:1969) | output | _self_test / 0 | `    display     :{sess.display_n}` |
| [1970](/root/laintas_cli/browser_session.py:1970) | output | _self_test / 0 | `    cdp         : {sess.cdp_endpoint()}` |
| [1971](/root/laintas_cli/browser_session.py:1971) | output | _self_test / 0 | `    rfb (VNC)   : 127.0.0.1:{sess.rfb_port}` |
| [1972](/root/laintas_cli/browser_session.py:1972) | output | _self_test / 0 | `    chrome pid  : {sess._chrome.pid if sess._chrome else '-'}` |
| [1973](/root/laintas_cli/browser_session.py:1973) | output | _self_test / 0 | `[*] Ctrl-C to tear down` |
| [1980](/root/laintas_cli/browser_session.py:1980) | output | _self_test / 0 | `    cdp/version : {ver.get('Browser', '?')} (protocol {ver.get('Protocol-Version', '?')})` |
| [1982](/root/laintas_cli/browser_session.py:1982) | output | _self_test / 0 | `    cdp/version : probe failed: {e}` |
| [1987](/root/laintas_cli/browser_session.py:1987) | output | _self_test / 0 | `[!] chrome exited` |
| [1990](/root/laintas_cli/browser_session.py:1990) | output | _self_test / 0 | ` ⏎ [*] tearing down` |
| [1993](/root/laintas_cli/browser_session.py:1993) | output | _self_test / 0 | `[+] done` |
| [1999](/root/laintas_cli/browser_session.py:1999) | title/menu/help | <module> / description | `headless-browser session self-test` |

### build/mac/install.sh

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [5](/root/laintas_cli/build/mac/install.sh:5) | installer source | <bootstrap> | `echo "This package is for macOS" >&2` |
| [13](/root/laintas_cli/build/mac/install.sh:13) | installer source | <bootstrap> | `echo "Installed $target"` |
| [14](/root/laintas_cli/build/mac/install.sh:14) | installer source | <bootstrap> | `echo 'Add ~/.local/bin to PATH if needed, then run: laintas-cli'` |

### config_file.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [30](/root/laintas_cli/config_file.py:30) | error candidate | resolve | `empty setting` |
| [37](/root/laintas_cli/config_file.py:37) | error candidate | resolve | `{' '.join(words)}: missing a value` |
| [38](/root/laintas_cli/config_file.py:38) | error candidate | resolve | `unknown budget level: {' '.join(words[:-1]) or words[0]}` |
| [41](/root/laintas_cli/config_file.py:41) | error candidate | resolve | `unknown setting: {key}` |
| [43](/root/laintas_cli/config_file.py:43) | error candidate | resolve | `{key}: missing a value` |
| [58](/root/laintas_cli/config_file.py:58) | error candidate | parse | `line {number}: {exc}` |
| [66](/root/laintas_cli/config_file.py:66) | error candidate | parse | `line {number}: {exc}` |
| [69](/root/laintas_cli/config_file.py:69) | error candidate | parse | `the file holds no settings` |
| [117](/root/laintas_cli/config_file.py:117) | error candidate | apply | `line {number} ({display_key(key)}): {message}` |

### context_router.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [165](/root/laintas_cli/context_router.py:165) | error candidate | register_group | `register_group needs triggers and selectors` |

### context_snapshot.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [68](/root/laintas_cli/context_snapshot.py:68) | error candidate | _json_bytes | `context material is not JSON serializable: {exc}` |
| [100](/root/laintas_cli/context_snapshot.py:100) | error candidate | ContextSnapshotStore._limit | `{name} must be a non-negative integer or None` |
| [117](/root/laintas_cli/context_snapshot.py:117) | error candidate | ContextSnapshotStore._ensure_dir | `snapshot directory is not a real directory: {directory}` |
| [124](/root/laintas_cli/context_snapshot.py:124) | error candidate | ContextSnapshotStore._ensure_dir | `cannot secure snapshot directory {directory}: {exc}` |
| [144](/root/laintas_cli/context_snapshot.py:144) | error candidate | ContextSnapshotStore._check_identifier | `{name} must be a non-empty string` |
| [151](/root/laintas_cli/context_snapshot.py:151) | error candidate | ContextSnapshotStore._read_bytes | `snapshot record is missing: {path.name}` |
| [153](/root/laintas_cli/context_snapshot.py:153) | error candidate | ContextSnapshotStore._read_bytes | `cannot inspect snapshot record {path.name}: {exc}` |
| [155](/root/laintas_cli/context_snapshot.py:155) | error candidate | ContextSnapshotStore._read_bytes | `refusing unsafe snapshot record: {path}` |
| [162](/root/laintas_cli/context_snapshot.py:162) | error candidate | ContextSnapshotStore._read_bytes | `refusing unsafe snapshot record: {path}` |
| [167](/root/laintas_cli/context_snapshot.py:167) | error candidate | ContextSnapshotStore._read_bytes | `cannot read snapshot record {path.name}: {exc}` |
| [175](/root/laintas_cli/context_snapshot.py:175) | error candidate | ContextSnapshotStore._read_json | `invalid snapshot JSON in {path.name}` |
| [183](/root/laintas_cli/context_snapshot.py:183) | error candidate | ContextSnapshotStore._atomic_write | `cannot inspect destination {path}` |
| [185](/root/laintas_cli/context_snapshot.py:185) | error candidate | ContextSnapshotStore._atomic_write | `refusing to replace unsafe path: {path}` |
| [201](/root/laintas_cli/context_snapshot.py:201) | error candidate | ContextSnapshotStore._atomic_write | `refusing to replace symlink: {path}` |
| [214](/root/laintas_cli/context_snapshot.py:214) | error candidate | ContextSnapshotStore._atomic_write | `cannot write snapshot record {path}: {exc}` |
| [229](/root/laintas_cli/context_snapshot.py:229) | error candidate | ContextSnapshotStore._put_blob | `content-addressed blob {digest} is invalid` |
| [233](/root/laintas_cli/context_snapshot.py:233) | error candidate | ContextSnapshotStore._put_blob | `cannot secure blob {digest}: {exc}` |
| [241](/root/laintas_cli/context_snapshot.py:241) | error candidate | ContextSnapshotStore._load_blob | `manifest contains an invalid blob hash` |
| [246](/root/laintas_cli/context_snapshot.py:246) | error candidate | ContextSnapshotStore._load_blob | `snapshot blob {digest} is missing` |
| [248](/root/laintas_cli/context_snapshot.py:248) | error candidate | ContextSnapshotStore._load_blob | `snapshot blob {digest} failed verification` |
| [252](/root/laintas_cli/context_snapshot.py:252) | error candidate | ContextSnapshotStore._load_blob | `snapshot blob {digest} is invalid JSON` |
| [259](/root/laintas_cli/context_snapshot.py:259) | error candidate | ContextSnapshotStore._validate_manifest | `invalid context conversation manifest` |
| [262](/root/laintas_cli/context_snapshot.py:262) | error candidate | ContextSnapshotStore._validate_manifest | `manifest identifiers are invalid` |
| [264](/root/laintas_cli/context_snapshot.py:264) | error candidate | ContextSnapshotStore._validate_manifest | `manifest session identifier does not match its path` |
| [266](/root/laintas_cli/context_snapshot.py:266) | error candidate | ContextSnapshotStore._validate_manifest | `manifest conversation identifier does not match its path` |
| [269](/root/laintas_cli/context_snapshot.py:269) | error candidate | ContextSnapshotStore._validate_manifest | `manifest calls are invalid` |
| [297](/root/laintas_cli/context_snapshot.py:297) | error candidate | ContextSnapshotStore.append_call | `conversation_id and turn_id disagree` |
| [300](/root/laintas_cli/context_snapshot.py:300) | error candidate | ContextSnapshotStore.append_call | `system_prompt and messages are required` |
| [304](/root/laintas_cli/context_snapshot.py:304) | error candidate | ContextSnapshotStore.append_call | `tool_schemas and tools disagree` |
| [306](/root/laintas_cli/context_snapshot.py:306) | error candidate | ContextSnapshotStore.append_call | `tool_schemas (or tools) is required` |
| [309](/root/laintas_cli/context_snapshot.py:309) | error candidate | ContextSnapshotStore.append_call | `system_sections aliases disagree` |
| [337](/root/laintas_cli/context_snapshot.py:337) | error candidate | ContextSnapshotStore.append_call | `call_id already exists: {call_id}` |
| [373](/root/laintas_cli/context_snapshot.py:373) | error candidate | ContextSnapshotStore._manifest_paths | `unsafe snapshot session directory: {base}` |
| [380](/root/laintas_cli/context_snapshot.py:380) | error candidate | ContextSnapshotStore._manifest_paths | `unsafe snapshot session directory: {directory}` |
| [388](/root/laintas_cli/context_snapshot.py:388) | error candidate | ContextSnapshotStore._manifest_paths | `cannot enumerate snapshot store: {exc}` |
| [426](/root/laintas_cli/context_snapshot.py:426) | error candidate | ContextSnapshotStore._expand_call | `call is missing content references` |
| [450](/root/laintas_cli/context_snapshot.py:450) | error candidate | ContextSnapshotStore.expand_call | `call record is invalid` |
| [465](/root/laintas_cli/context_snapshot.py:465) | error candidate | ContextSnapshotStore.load_conversation | `newest_index must be a positive integer` |
| [470](/root/laintas_cli/context_snapshot.py:470) | error candidate | ContextSnapshotStore.load_conversation | `conversation {newest_index} does not exist for session {session_id}` |
| [485](/root/laintas_cli/context_snapshot.py:485) | error candidate | ContextSnapshotStore.load_call | `call_index must be a positive integer` |
| [493](/root/laintas_cli/context_snapshot.py:493) | error candidate | ContextSnapshotStore.load_call | `call does not exist: {call_id}` |
| [497](/root/laintas_cli/context_snapshot.py:497) | error candidate | ContextSnapshotStore.load_call | `call {call_index} does not exist` |

### contract_store.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [119](/root/laintas_cli/contract_store.py:119) | error candidate | _read_json | `{path} is not readable JSON: {e}` |
| [121](/root/laintas_cli/contract_store.py:121) | error candidate | _read_json | `{path} must contain a JSON object` |
| [188](/root/laintas_cli/contract_store.py:188) | error candidate | normalize_operation | `operation must be '<METHOD> <path>', e.g. 'GET /api/orders' (got {operation!r})` |
| [193](/root/laintas_cli/contract_store.py:193) | error candidate | normalize_operation | `operation must be '<METHOD> <path>' with an absolute path (got {operation!r})` |
| [261](/root/laintas_cli/contract_store.py:261) | error candidate | propose | `definition must be a non-empty OpenAPI operation object` |
| [267](/root/laintas_cli/contract_store.py:267) | error candidate | propose | `paths in the spec is not an object` |
| [287](/root/laintas_cli/contract_store.py:287) | error candidate | agree | `{key} is not in the spec — propose it first` |
| [291](/root/laintas_cli/contract_store.py:291) | error candidate | agree | `{key} is {entry.get('state')}, not proposed — re-propose it to change the shape` |
| [309](/root/laintas_cli/contract_store.py:309) | error candidate | implement | `name at least one file that implements this operation` |
| [312](/root/laintas_cli/contract_store.py:312) | error candidate | implement | `{key} is not in the spec` |
| [316](/root/laintas_cli/contract_store.py:316) | error candidate | implement | `{key} has not been agreed yet — agree to the shape before building it` |
| [321](/root/laintas_cli/contract_store.py:321) | error candidate | implement | `these files do not exist: {', '.join(missing)}` |
| [456](/root/laintas_cli/contract_store.py:456) | error candidate | verify | `{key} is not in the contract` |
| [472](/root/laintas_cli/contract_store.py:472) | message candidate | verify / error | `no base URL — pass base_url or set servers[0].url` |
| [476](/root/laintas_cli/contract_store.py:476) | message candidate | verify / error | `path has parameters; verify it with an explicit base_url and a concrete path instead` |
| [567](/root/laintas_cli/contract_store.py:567) | error candidate | read | `{key} is not in the contract` |
| [598](/root/laintas_cli/contract_store.py:598) | error candidate | mock_response | `{key} is not in the spec` |

### download_progress.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [89](/root/laintas_cli/download_progress.py:89) | label/render | DownloadMeter.header | `  ↓ ` |
| [99](/root/laintas_cli/download_progress.py:99) | label/render | DownloadMeter.row | `  {mark} {word}` |
| [100](/root/laintas_cli/download_progress.py:100) | label/render | DownloadMeter.row | `  {format_size(self.done)} · {format_time(elapsed)}` |
| [118](/root/laintas_cli/download_progress.py:118) | label/render | DownloadMeter.row | `╸` |
| [129](/root/laintas_cli/download_progress.py:129) | label/render | DownloadMeter.row | `  {fields[0]}` |
| [130](/root/laintas_cli/download_progress.py:130) | label/render | DownloadMeter.row | `  {'  '.join(fields[1:])}` |
| [157](/root/laintas_cli/download_progress.py:157) | title/menu/help | _display / label | `Downloading…` |
| [228](/root/laintas_cli/download_progress.py:228) | error candidate | stream_download | `{label} download is incomplete ({len(buf)}/{total} bytes).` |

### durable_rules.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [59](/root/laintas_cli/durable_rules.py:59) | error candidate | save_rule | `rule text is required` |
| [61](/root/laintas_cli/durable_rules.py:61) | error candidate | save_rule | `invalid rule scope: {scope}` |
| [63](/root/laintas_cli/durable_rules.py:63) | error candidate | save_rule | `invalid rule kind: {kind}` |
| [65](/root/laintas_cli/durable_rules.py:65) | error candidate | save_rule | `invalid rule trigger: {trigger}` |

### enterprise.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [354](/root/laintas_cli/enterprise.py:354) | error candidate | _canonical_json | `policy documents may only contain integers within ±{MAX_SAFE_INTEGER}; got {value!r}` |
| [366](/root/laintas_cli/enterprise.py:366) | error candidate | _canonical_json | `policy documents may not contain {type(value).__name__}` |

### enterprise_installer.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [67](/root/laintas_cli/enterprise_installer.py:67) | error candidate | _authenticated_session | `Sign in with /login before installing Enterprise CLI.` |
| [128](/root/laintas_cli/enterprise_installer.py:128) | error candidate | _refuse_downgrade | `Refusing to replace Laintas Enterprise v{held} with the older v{offered}. ⏎   A signature proves who published a package, not that it is the current one. ⏎   If this downgrade is intended, run:  /v enterprise --force` |
| [205](/root/laintas_cli/enterprise_installer.py:205) | title/menu/help | _verify_enterprise / label | `Enterprise release` |
| [208](/root/laintas_cli/enterprise_installer.py:208) | title/menu/help | _verify_enterprise / label | `Enterprise release` |
| [220](/root/laintas_cli/enterprise_installer.py:220) | error candidate | _download_payload | `Enterprise release manifest contains an invalid size.` |
| [224](/root/laintas_cli/enterprise_installer.py:224) | error candidate | _download_payload | `Enterprise release manifest has no download URL.` |
| [230](/root/laintas_cli/enterprise_installer.py:230) | error candidate | _download_payload | `Refusing a release download that points outside {API_BASE}.` |
| [232](/root/laintas_cli/enterprise_installer.py:232) | output | _download_payload / 0 | `Downloading enterprise release v{manifest.get('version')} ({_format_size(size)})…` |
| [234](/root/laintas_cli/enterprise_installer.py:234) | title/menu/help | _download_payload / label | `enterprise release` |
| [238](/root/laintas_cli/enterprise_installer.py:238) | error candidate | _download_payload | `Enterprise release download is incomplete.` |
| [278](/root/laintas_cli/enterprise_installer.py:278) | error candidate | install_extension | `Enterprise package is missing extension.json — refusing it.` |
| [303](/root/laintas_cli/enterprise_installer.py:303) | error candidate | install_extension | `Enterprise package failed to load: {message}` |
| [306](/root/laintas_cli/enterprise_installer.py:306) | output | install_extension / 0 | `Laintas Enterprise v{release['version']} enabled — try /org status` |
| [373](/root/laintas_cli/enterprise_installer.py:373) | output | install_gateway / 0 | `Enterprise gateway v{manifest['version']} extracted to {target}` |

### evolution_lab.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [205](/root/laintas_cli/evolution_lab.py:205) | error candidate | draft_candidate | `Evolution branch not found: {branch_id}` |
| [208](/root/laintas_cli/evolution_lab.py:208) | error candidate | draft_candidate | `target_type must be extension, commands, or loop` |
| [210](/root/laintas_cli/evolution_lab.py:210) | error candidate | draft_candidate | `invalid extension/candidate name` |
| [214](/root/laintas_cli/evolution_lab.py:214) | error candidate | draft_candidate | `candidate files must be objects` |
| [218](/root/laintas_cli/evolution_lab.py:218) | error candidate | draft_candidate | `invalid candidate file path: {path}` |
| [689](/root/laintas_cli/evolution_lab.py:689) | error candidate | build_design_task | `Branch not found: {branch_id}` |

### extension_manager.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [213](/root/laintas_cli/extension_manager.py:213) | error candidate | _safe_extract_zip | `Extension archive contains more than {MAX_ARCHIVE_FILES} entries.` |
| [215](/root/laintas_cli/extension_manager.py:215) | error candidate | _safe_extract_zip | `Extension archive exceeds the unpacked size limit.` |
| [219](/root/laintas_cli/extension_manager.py:219) | error candidate | _safe_extract_zip | `Unsafe archive path: {entry.filename}` |
| [285](/root/laintas_cli/extension_manager.py:285) | error candidate | _fetch_registry_json | `Extension registry exceeds the 1 MB limit.` |
| [293](/root/laintas_cli/extension_manager.py:293) | error candidate | _fetch_registry_json | `Extension registry exceeds the 1 MB limit.` |
| [297](/root/laintas_cli/extension_manager.py:297) | error candidate | _fetch_registry_json | `Extension registry returned invalid JSON.` |
| [299](/root/laintas_cli/extension_manager.py:299) | error candidate | _fetch_registry_json | `Extension registry returned an invalid response.` |
| [301](/root/laintas_cli/extension_manager.py:301) | error candidate | _fetch_registry_json | `Extension registry has an invalid document shape.` |
| [413](/root/laintas_cli/extension_manager.py:413) | status/prompt | ExtensionManager._confirm | `{prompt} [y/N] ` |
| [520](/root/laintas_cli/extension_manager.py:520) | error candidate | ExtensionManager.list_available | `Source must be all, official, or community.` |
| [673](/root/laintas_cli/extension_manager.py:673) | error candidate | ExtensionManager.pack | `Extension {name!r} is not installed.` |
| [683](/root/laintas_cli/extension_manager.py:683) | error candidate | ExtensionManager.publish | `Extension {name!r} is not installed.` |
| [687](/root/laintas_cli/extension_manager.py:687) | error candidate | ExtensionManager.publish | `Manifest validation failed: ⏎   {'\n  '.join(errors)}` |
| [727](/root/laintas_cli/extension_manager.py:727) | error candidate | ExtensionManager.create | `Invalid extension name: {name!r} ⏎   Must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}` |
| [731](/root/laintas_cli/extension_manager.py:731) | error candidate | ExtensionManager.create | `Directory is not empty: {target}` |
| [770](/root/laintas_cli/extension_manager.py:770) | title/menu/help | ExtensionManager._install_staged / message | `No extension.json found in the package.` |
| [774](/root/laintas_cli/extension_manager.py:774) | title/menu/help | ExtensionManager._install_staged / message | `Invalid manifest: {exc}` |
| [780](/root/laintas_cli/extension_manager.py:780) | title/menu/help | ExtensionManager._install_staged / message | `Manifest validation failed: ⏎   {'\n  '.join(errors)}` |
| [788](/root/laintas_cli/extension_manager.py:788) | title/menu/help | ExtensionManager._install_staged / message | `main.py entrypoint is missing.` |
| [806](/root/laintas_cli/extension_manager.py:806) | title/menu/help | ExtensionManager._install_staged / message | `{name} v{existing_ver} is already installed. ⏎   Use --force to replace it.` |
| [821](/root/laintas_cli/extension_manager.py:821) | output | ExtensionManager._install_staged / 0 | `Extension: {name} v{version} ⏎   description : {desc} ⏎   author       : {author_name} ⏎   integrity    : {integrity[:16]} ⏎   source       : {_escape_markup(source_label)} ⏎   This extension will execute Python code with your permissions.` |
| [828](/root/laintas_cli/extension_manager.py:828) | status/prompt | ExtensionManager._install_staged | `Install and trust this extension?` |
| [829](/root/laintas_cli/extension_manager.py:829) | title/menu/help | ExtensionManager._install_staged / message | `Installation cancelled.` |
| [890](/root/laintas_cli/extension_manager.py:890) | title/menu/help | ExtensionManager._install_staged / message | `Extension failed to load and was rolled back: {msg}` |
| [895](/root/laintas_cli/extension_manager.py:895) | title/menu/help | ExtensionManager._install_staged / message | `Installed {name} v{version} ({integrity[:12]}).` |
| [902](/root/laintas_cli/extension_manager.py:902) | title/menu/help | ExtensionManager._install_from_directory / message | `Not a directory: {source}` |
| [913](/root/laintas_cli/extension_manager.py:913) | title/menu/help | ExtensionManager._install_from_archive / message | `Not a file: {source}` |
| [932](/root/laintas_cli/extension_manager.py:932) | output | ExtensionManager._install_from_url / 0 | `Downloading {url}…` |
| [940](/root/laintas_cli/extension_manager.py:940) | error candidate | ExtensionManager._install_from_url | `Extension download exceeds the 10 MB limit.` |
| [961](/root/laintas_cli/extension_manager.py:961) | title/menu/help | ExtensionManager._install_community / message | `Community extension IDs must use @author/slug.` |
| [965](/root/laintas_cli/extension_manager.py:965) | title/menu/help | ExtensionManager._install_community / message | `AI source review is unavailable; community installation was stopped.` |
| [970](/root/laintas_cli/extension_manager.py:970) | status/prompt | ExtensionManager._install_community | `Looking up {identifier} in the community registry…` |
| [982](/root/laintas_cli/extension_manager.py:982) | error candidate | ExtensionManager._install_community | `Community registry returned an invalid artifact hash.` |
| [983](/root/laintas_cli/extension_manager.py:983) | output | ExtensionManager._install_community / 0 | `Downloading {identifier} v{version}…` |
| [991](/root/laintas_cli/extension_manager.py:991) | error candidate | ExtensionManager._install_community | `Extension download exceeds the 10 MB limit.` |
| [995](/root/laintas_cli/extension_manager.py:995) | error candidate | ExtensionManager._install_community | `Downloaded extension does not match the published SHA-256.` |
| [1004](/root/laintas_cli/extension_manager.py:1004) | error candidate | ExtensionManager._install_community | `Downloaded package identity does not match the registry.` |
| [1008](/root/laintas_cli/extension_manager.py:1008) | status/prompt | ExtensionManager._install_community | `AI is reviewing {identifier} for risky code… this can take a minute` |
| [1013](/root/laintas_cli/extension_manager.py:1013) | error candidate | ExtensionManager._install_community | `AI source review returned an invalid risk level.` |
| [1015](/root/laintas_cli/extension_manager.py:1015) | output | ExtensionManager._install_community / 0 | `COMMUNITY EXTENSION — NOT REVIEWED BY LAINTAS ⏎  ⏎   Extension: {identifier} v{version} ⏎   Artifact:  {expected_sha[:16]} ⏎   Risk:      {risk.upper()} ⏎   Review:    {_escape_markup(report.get('summary', ''))} ⏎  ⏎   This extension executes Python code with your user permissions. ⏎   AI-assisted analysis cannot guarantee that the code is safe.` |
| [1025](/root/laintas_cli/extension_manager.py:1025) | output | ExtensionManager._install_community / 0 | `  {severity} {_escape_markup(finding.get('file', 'unknown'))}:{finding.get('line', 1)} — {_escape_markup(finding.get('description', ''))}` |
| [1031](/root/laintas_cli/extension_manager.py:1031) | title/menu/help | ExtensionManager._install_community / message | `Critical-risk community extensions cannot be installed.` |
| [1037](/root/laintas_cli/extension_manager.py:1037) | status/prompt | ExtensionManager._install_community | `{prompt} ` |
| [1045](/root/laintas_cli/extension_manager.py:1045) | title/menu/help | ExtensionManager._install_community / message | `Installation cancelled.` |
| [1064](/root/laintas_cli/extension_manager.py:1064) | title/menu/help | ExtensionManager._install_official / message | `No official extension named {official_id!r} was found. Community extensions must use @author/slug.` |
| [1069](/root/laintas_cli/extension_manager.py:1069) | error candidate | ExtensionManager._install_official | `Official extension registry entry is invalid.` |
| [1077](/root/laintas_cli/extension_manager.py:1077) | error candidate | ExtensionManager._install_official | `Extension download exceeds the 10 MB limit.` |
| [1081](/root/laintas_cli/extension_manager.py:1081) | error candidate | ExtensionManager._install_official | `Official extension does not match the registry SHA-256.` |
| [1090](/root/laintas_cli/extension_manager.py:1090) | error candidate | ExtensionManager._install_official | `Official package identity does not match the registry.` |

### extension_runtime.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [195](/root/laintas_cli/extension_runtime.py:195) | message candidate | TaskRunner.run / error | `task execution is not available in this session` |
| [277](/root/laintas_cli/extension_runtime.py:277) | error candidate | ExtensionContext.register_tool | `pass either a Tool or (name, handler, spec)` |
| [281](/root/laintas_cli/extension_runtime.py:281) | error candidate | ExtensionContext.register_tool | `register_tool(name, handler, spec) needs a callable handler` |
| [294](/root/laintas_cli/extension_runtime.py:294) | message candidate | ExtensionContext.register_tool._invoke / error | `invalid parameters: {exc}` |
| [300](/root/laintas_cli/extension_runtime.py:300) | message candidate | ExtensionContext.register_tool._invoke / error | `{type(exc).__name__}: {exc}` |
| [320](/root/laintas_cli/extension_runtime.py:320) | error candidate | ExtensionContext.register_skills | `extension directory is unknown` |
| [323](/root/laintas_cli/extension_runtime.py:323) | error candidate | ExtensionContext.register_skills | `skills directory must be inside the extension` |
| [325](/root/laintas_cli/extension_runtime.py:325) | error candidate | ExtensionContext.register_skills | `no skills directory at {subdirectory!r}` |
| [406](/root/laintas_cli/extension_runtime.py:406) | error candidate | ExtensionRuntime.register_command | `invalid extension command: {name}` |
| [408](/root/laintas_cli/extension_runtime.py:408) | error candidate | ExtensionRuntime.register_command | `command handler must be callable` |
| [410](/root/laintas_cli/extension_runtime.py:410) | error candidate | ExtensionRuntime.register_command | `command is reserved by laintas-cli: {normalized}` |
| [413](/root/laintas_cli/extension_runtime.py:413) | error candidate | ExtensionRuntime.register_command | `extension command already registered: {normalized}` |
| [425](/root/laintas_cli/extension_runtime.py:425) | error candidate | ExtensionRuntime.register_command | `arg_rules entries are (subcommand, max_args, usage)` |
| [437](/root/laintas_cli/extension_runtime.py:437) | error candidate | ExtensionRuntime.register_command | `file_arguments entries are (subcommand, suffixes)` |
| [447](/root/laintas_cli/extension_runtime.py:447) | error candidate | ExtensionRuntime.register_tool | `extension tool must be a Tool` |
| [454](/root/laintas_cli/extension_runtime.py:454) | error candidate | ExtensionRuntime.register_tool | `tool name already registered: {tool.name}` |
| [458](/root/laintas_cli/extension_runtime.py:458) | error candidate | ExtensionRuntime.register_loop | `loop handler must be callable` |
| [463](/root/laintas_cli/extension_runtime.py:463) | error candidate | ExtensionRuntime.subscribe | `unknown event: {event}` |
| [465](/root/laintas_cli/extension_runtime.py:465) | error candidate | ExtensionRuntime.subscribe | `event handler must be callable` |
| [468](/root/laintas_cli/extension_runtime.py:468) | error candidate | ExtensionRuntime.subscribe | `{owner} must declare the 'observe' capability to use ctx.on` |
| [492](/root/laintas_cli/extension_runtime.py:492) | error candidate | ExtensionRuntime._manifest | `invalid extension manifest: {exc}` |
| [498](/root/laintas_cli/extension_runtime.py:498) | error candidate | ExtensionRuntime._manifest | `extension manifest fields are invalid` |
| [500](/root/laintas_cli/extension_runtime.py:500) | error candidate | ExtensionRuntime._manifest | `manifest name must match extension directory` |
| [505](/root/laintas_cli/extension_runtime.py:505) | error candidate | ExtensionRuntime._manifest | `toolPrefix must look like 'org.' — lower-case, dot-terminated` |
| [531](/root/laintas_cli/extension_runtime.py:531) | error candidate | ExtensionRuntime.load | `missing main.py entrypoint` |
| [547](/root/laintas_cli/extension_runtime.py:547) | error candidate | ExtensionRuntime.load | `extension {name} is not trusted ({status.get('reason', 'unknown')}). Run /extensions trust {name} to approve it.` |
| [557](/root/laintas_cli/extension_runtime.py:557) | error candidate | ExtensionRuntime.load | `could not create module loader` |
| [563](/root/laintas_cli/extension_runtime.py:563) | error candidate | ExtensionRuntime.load | `extension must define setup(ctx)` |
| [706](/root/laintas_cli/extension_runtime.py:706) | error candidate | ExtensionRuntime.intercept_loop | `{owner} loop handler returned non-string` |

### extension_scanner.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [137](/root/laintas_cli/extension_scanner.py:137) | error candidate | collect_sources | `Community extension source exceeds the {MAX_AI_SOURCE_BYTES} byte scan limit.` |
| [141](/root/laintas_cli/extension_scanner.py:141) | error candidate | collect_sources | `Source file is not UTF-8: {path.name}` |
| [144](/root/laintas_cli/extension_scanner.py:144) | error candidate | collect_sources | `Community extension contains no reviewable source files.` |
| [192](/root/laintas_cli/extension_scanner.py:192) | error candidate | _json_object | `AI scanner returned no JSON object.` |
| [195](/root/laintas_cli/extension_scanner.py:195) | error candidate | _json_object | `AI scanner returned an invalid report.` |
| [211](/root/laintas_cli/extension_scanner.py:211) | error candidate | ai_scan | `AI source review is unavailable; community installation was stopped.` |
| [215](/root/laintas_cli/extension_scanner.py:215) | error candidate | ai_scan | `AI scanner returned an invalid risk level.` |

### file_lock.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [46](/root/laintas_cli/file_lock.py:46) | error candidate | guard | `The same lock was requested with a different rank` |
| [51](/root/laintas_cli/file_lock.py:51) | error candidate | guard | `Lock order must be STORE -> SESSION -> CLAIM, then canonical path order` |

### handoff.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [121](/root/laintas_cli/handoff.py:121) | error candidate | valid_id | `invalid handoff id {hid!r} — use 3-64 chars of a-z, 0-9 and '-'` |
| [236](/root/laintas_cli/handoff.py:236) | error candidate | make_event | `unknown event {kind!r}; use one of {', '.join(EVENT_KINDS)}` |
| [302](/root/laintas_cli/handoff.py:302) | error candidate | create | `a handoff needs a one-line title saying what it is` |
| [309](/root/laintas_cli/handoff.py:309) | error candidate | create | `handoff contract: {exc}` |
| [335](/root/laintas_cli/handoff.py:335) | error candidate | create | `handoff {env['id']} already exists` |
| [363](/root/laintas_cli/handoff.py:363) | error candidate | _write | `could not write handoff: {exc}` |
| [369](/root/laintas_cli/handoff.py:369) | error candidate | load | `no handoff {valid_id(hid)!r} here — /handoff list` |
| [378](/root/laintas_cli/handoff.py:378) | error candidate | parse | `not a handoff envelope{where}` |
| [387](/root/laintas_cli/handoff.py:387) | error candidate | parse | `handoff format v{version or '?'} is not v{VERSION}{where} — upgrade laintas_cli` |
| [397](/root/laintas_cli/handoff.py:397) | error candidate | parse | `Task checkpoints require handoff v2 — upgrade laintas_cli` |
| [401](/root/laintas_cli/handoff.py:401) | error candidate | parse | `{exc}{where}` |
| [420](/root/laintas_cli/handoff.py:420) | error candidate | parse | `handoff contract is not checkable{where}: {exc}` |
| [447](/root/laintas_cli/handoff.py:447) | error candidate | append | `This task is addressed to another account` |
| [467](/root/laintas_cli/handoff.py:467) | error candidate | merge | `refusing to merge different handoffs: {local.get('id')} vs {remote.get('id')}` |
| [471](/root/laintas_cli/handoff.py:471) | error candidate | merge | `handoff {local.get('id')} has two different headers ({lh} vs {rh}) — the same id was created twice; rename one and re-share it` |
| [645](/root/laintas_cli/handoff.py:645) | error candidate | fetch | `{remote} is not a handoff envelope` |
| [660](/root/laintas_cli/handoff.py:660) | error candidate | _fetch_locked | `{remote} is not a handoff envelope` |
| [674](/root/laintas_cli/handoff.py:674) | error candidate | _fetch_locked | `{remote} contains handoff {incoming['id']}, not {hid}` |
| [742](/root/laintas_cli/handoff.py:742) | error candidate | export_token | `Handoff is too large to export; compact its context first` |
| [748](/root/laintas_cli/handoff.py:748) | error candidate | export_token | `Handoff token is too large; compact its context first` |
| [762](/root/laintas_cli/handoff.py:762) | error candidate | decode_token | `that is far too large to be a handoff token` |
| [766](/root/laintas_cli/handoff.py:766) | error candidate | decode_token | `that is not a handoff token` |
| [769](/root/laintas_cli/handoff.py:769) | error candidate | decode_token | `handoff token format v{parts[1]} is not v{TOKEN_VERSION} — upgrade laintas_cli` |
| [775](/root/laintas_cli/handoff.py:775) | error candidate | decode_token | `the token is damaged and cannot be decoded: {exc}` |
| [780](/root/laintas_cli/handoff.py:780) | error candidate | decode_token | `the token is incomplete or was altered in transit — copy the whole thing, or send it as a file with /handoff export <id> <file>` |
| [790](/root/laintas_cli/handoff.py:790) | error candidate | decode_token | `that token expands to far more than a handoff` |
| [795](/root/laintas_cli/handoff.py:795) | error candidate | decode_token | `the token does not contain a handoff: {exc}` |

### helpwo_server.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [375](/root/laintas_cli/helpwo_server.py:375) | message candidate | _HelpwoHandler._trusted_host / error | `local Helpwo bridge: host not allowed` |
| [421](/root/laintas_cli/helpwo_server.py:421) | message candidate | _HelpwoHandler._require_auth / error | `authentication required` |
| [467](/root/laintas_cli/helpwo_server.py:467) | message candidate | _HelpwoHandler._guard_write / error | `cross-site request refused` |
| [471](/root/laintas_cli/helpwo_server.py:471) | message candidate | _HelpwoHandler._guard_write / error | `Content-Type must be application/json` |
| [478](/root/laintas_cli/helpwo_server.py:478) | message candidate | _HelpwoHandler._static / error | `dist directory not configured` |
| [518](/root/laintas_cli/helpwo_server.py:518) | message candidate | _HelpwoHandler._content_length_ok / error | `request body too large` |
| [656](/root/laintas_cli/helpwo_server.py:656) | message candidate | _HelpwoHandler.do_POST / error | `not found` |
| [690](/root/laintas_cli/helpwo_server.py:690) | message candidate | _HelpwoHandler._proxy_only_method / error | `method not allowed` |
| [736](/root/laintas_cli/helpwo_server.py:736) | message candidate | _HelpwoHandler._handle_updates / error | `unknown agent` |
| [744](/root/laintas_cli/helpwo_server.py:744) | message candidate | _HelpwoHandler._handle_send / error | `local runtime unavailable` |
| [747](/root/laintas_cli/helpwo_server.py:747) | message candidate | _HelpwoHandler._handle_send / error | `unknown agent` |
| [755](/root/laintas_cli/helpwo_server.py:755) | message candidate | _HelpwoHandler._handle_send / error | `kind '{kind}' is not available to applications` |
| [776](/root/laintas_cli/helpwo_server.py:776) | message candidate | _HelpwoHandler._handle_send / error | `executor not ready` |
| [780](/root/laintas_cli/helpwo_server.py:780) | message candidate | _HelpwoHandler._handle_send / error | `local runtime is busy` |
| [799](/root/laintas_cli/helpwo_server.py:799) | message candidate | _HelpwoHandler._handle_send / error | `executor is shutting down` |
| [853](/root/laintas_cli/helpwo_server.py:853) | message candidate | _HelpwoHandler._proxy_json / error | `backend unreachable: {e}` |
| [884](/root/laintas_cli/helpwo_server.py:884) | message candidate | _HelpwoHandler._proxy_stream / error | `backend unreachable: {e}` |
| [950](/root/laintas_cli/helpwo_server.py:950) | message candidate | _HelpwoHandler._handle_models / error | `model catalog unavailable — backend unreachable` |
| [1052](/root/laintas_cli/helpwo_server.py:1052) | message candidate | _HelpwoHandler._handle_vnc / error | `expected a WebSocket upgrade` |
| [1065](/root/laintas_cli/helpwo_server.py:1065) | message candidate | _HelpwoHandler._handle_vnc / error | `cannot reach the live view: {e}` |
| [1206](/root/laintas_cli/helpwo_server.py:1206) | message candidate | _HelpwoHandler._handle_local_exec_approval / error | `no such pending request` |
| [1212](/root/laintas_cli/helpwo_server.py:1212) | message candidate | _HelpwoHandler._handle_local_exec_abort / error | `no such request` |
| [1224](/root/laintas_cli/helpwo_server.py:1224) | message candidate | _HelpwoHandler._handle_local_pty / error | `expected a WebSocket upgrade` |
| [1227](/root/laintas_cli/helpwo_server.py:1227) | message candidate | _HelpwoHandler._handle_local_pty / error | `local runtime unavailable` |
| [1233](/root/laintas_cli/helpwo_server.py:1233) | message candidate | _HelpwoHandler._handle_local_pty / error | `invalid session id` |
| [1279](/root/laintas_cli/helpwo_server.py:1279) | message candidate | _HelpwoHandler._handle_local_proxy / error | `cross-site request refused` |
| [1282](/root/laintas_cli/helpwo_server.py:1282) | message candidate | _HelpwoHandler._handle_local_proxy / error | `local runtime unavailable` |
| [1287](/root/laintas_cli/helpwo_server.py:1287) | message candidate | _HelpwoHandler._handle_local_proxy / error | `expected /api/local-proxy/<port>/<path>` |
| [1301](/root/laintas_cli/helpwo_server.py:1301) | message candidate | _HelpwoHandler._handle_local_proxy / error | `request body too large` |
| [1345](/root/laintas_cli/helpwo_server.py:1345) | message candidate | _HelpwoHandler._handle_search / error | `web_search module unavailable` |
| [1349](/root/laintas_cli/helpwo_server.py:1349) | message candidate | _HelpwoHandler._handle_search / error | `a query is required` |
| [1369](/root/laintas_cli/helpwo_server.py:1369) | message candidate | _HelpwoHandler._handle_search / error | `search failed: {type(e).__name__}` |
| [1400](/root/laintas_cli/helpwo_server.py:1400) | message candidate | _HelpwoHandler._handle_fetch / error | `web_search module unavailable` |
| [1404](/root/laintas_cli/helpwo_server.py:1404) | message candidate | _HelpwoHandler._handle_fetch / error | `a URL is required` |
| [1422](/root/laintas_cli/helpwo_server.py:1422) | message candidate | _HelpwoHandler._handle_fetch / error | `fetch failed: {type(e).__name__}` |
| [1456](/root/laintas_cli/helpwo_server.py:1456) | message candidate | _HelpwoHandler._private_access / error | `Access denied by private file permissions.` |
| [1485](/root/laintas_cli/helpwo_server.py:1485) | message candidate | _HelpwoHandler._handle_local_fs_list / error | `path outside local workspace root` |
| [1488](/root/laintas_cli/helpwo_server.py:1488) | message candidate | _HelpwoHandler._handle_local_fs_list / error | `not a directory` |
| [1503](/root/laintas_cli/helpwo_server.py:1503) | message candidate | _HelpwoHandler._handle_local_fs_stat / error | `not found` |
| [1512](/root/laintas_cli/helpwo_server.py:1512) | message candidate | _HelpwoHandler._handle_local_fs_read / error | `not found` |
| [1517](/root/laintas_cli/helpwo_server.py:1517) | message candidate | _HelpwoHandler._handle_local_fs_read / error | `file too large ({size} bytes, cap {_MAX_LOCAL_FILE_BYTES})` |
| [1540](/root/laintas_cli/helpwo_server.py:1540) | message candidate | _HelpwoHandler._handle_local_fs_write / error | `path outside local workspace root` |
| [1543](/root/laintas_cli/helpwo_server.py:1543) | message candidate | _HelpwoHandler._handle_local_fs_write / error | `parent directory does not exist` |
| [1547](/root/laintas_cli/helpwo_server.py:1547) | message candidate | _HelpwoHandler._handle_local_fs_write / error | `Blocked by cross-writer coordination: {stale}` |
| [1554](/root/laintas_cli/helpwo_server.py:1554) | message candidate | _HelpwoHandler._handle_local_fs_write / error | `content too large (cap {_MAX_LOCAL_FILE_BYTES})` |
| [1568](/root/laintas_cli/helpwo_server.py:1568) | message candidate | _HelpwoHandler._handle_local_fs_mkdir / error | `path outside local workspace root` |
| [1583](/root/laintas_cli/helpwo_server.py:1583) | message candidate | _HelpwoHandler._handle_local_fs_delete / error | `path outside local workspace root` |
| [1586](/root/laintas_cli/helpwo_server.py:1586) | message candidate | _HelpwoHandler._handle_local_fs_delete / error | `cannot delete the workspace root` |
| [1590](/root/laintas_cli/helpwo_server.py:1590) | message candidate | _HelpwoHandler._handle_local_fs_delete / error | `Blocked by cross-writer coordination: {stale}` |
| [1598](/root/laintas_cli/helpwo_server.py:1598) | message candidate | _HelpwoHandler._handle_local_fs_delete / error | `not found` |
| [1614](/root/laintas_cli/helpwo_server.py:1614) | message candidate | _HelpwoHandler._handle_local_fs_rename / error | `invalid path or name` |
| [1618](/root/laintas_cli/helpwo_server.py:1618) | message candidate | _HelpwoHandler._handle_local_fs_rename / error | `target path outside local workspace root` |
| [1623](/root/laintas_cli/helpwo_server.py:1623) | message candidate | _HelpwoHandler._handle_local_fs_rename / error | `target already exists` |
| [1627](/root/laintas_cli/helpwo_server.py:1627) | message candidate | _HelpwoHandler._handle_local_fs_rename / error | `Blocked by cross-writer coordination: {stale}` |
| [1646](/root/laintas_cli/helpwo_server.py:1646) | message candidate | _HelpwoHandler._handle_local_fs_move / error | `invalid source or destination` |
| [1652](/root/laintas_cli/helpwo_server.py:1652) | message candidate | _HelpwoHandler._handle_local_fs_move / error | `target already exists` |
| [1656](/root/laintas_cli/helpwo_server.py:1656) | message candidate | _HelpwoHandler._handle_local_fs_move / error | `Blocked by cross-writer coordination: {stale}` |

### hwg_adapter/adapter.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [370](/root/laintas_cli/hwg_adapter/adapter.py:370) | error candidate | _tokenize_condition | `unexpected character {on[i]!r}` |
| [399](/root/laintas_cli/hwg_adapter/adapter.py:399) | error candidate | _CondParser.parse | `unexpected trailing input` |
| [436](/root/laintas_cli/hwg_adapter/adapter.py:436) | error candidate | _CondParser._parse_primary | `missing )` |
| [440](/root/laintas_cli/hwg_adapter/adapter.py:440) | error candidate | _CondParser._parse_primary | `unexpected token {value!r}` |
| [445](/root/laintas_cli/hwg_adapter/adapter.py:445) | error candidate | _CondParser._parse_primary | `exists() needs a path` |
| [447](/root/laintas_cli/hwg_adapter/adapter.py:447) | error candidate | _CondParser._parse_primary | `missing ) after exists(` |
| [453](/root/laintas_cli/hwg_adapter/adapter.py:453) | error candidate | _CondParser._parse_primary | `{value!r} is not a field name` |
| [457](/root/laintas_cli/hwg_adapter/adapter.py:457) | error candidate | _CondParser._parse_primary | `comparison needs a value` |
| [461](/root/laintas_cli/hwg_adapter/adapter.py:461) | error candidate | _CondParser._parse_primary | `{value!r} is not a field name` |
| [465](/root/laintas_cli/hwg_adapter/adapter.py:465) | error candidate | _CondParser._parse_primary | `in needs a [list]` |
| [470](/root/laintas_cli/hwg_adapter/adapter.py:470) | error candidate | _CondParser._parse_primary | `{value!r} is a keyword, not a verdict` |
| [583](/root/laintas_cli/hwg_adapter/adapter.py:583) | error candidate | _Parser.parse | `Expected #name# or { after ({label})` |
| [590](/root/laintas_cli/hwg_adapter/adapter.py:590) | error candidate | _Parser.parse | `Unexpected token "{snippet}"` |
| [599](/root/laintas_cli/hwg_adapter/adapter.py:599) | error candidate | _Parser._parse_manual_node | `A manual node must be !(file.hwo)#name#` |
| [605](/root/laintas_cli/hwg_adapter/adapter.py:605) | error candidate | _Parser._parse_manual_node | `A manual node must bind a .hwo file, not a tool` |
| [615](/root/laintas_cli/hwg_adapter/adapter.py:615) | error candidate | _Parser._parse_node_tail | `Empty file binding — use (file.hwo)#name#` |
| [638](/root/laintas_cli/hwg_adapter/adapter.py:638) | error candidate | _Parser._parse_graph | `@graph must be followed by [in(...), out(...)]` |
| [647](/root/laintas_cli/hwg_adapter/adapter.py:647) | error candidate | _Parser._parse_include | `@include must be followed by a quoted path, e.g. @include "contracts.hwg"` |
| [653](/root/laintas_cli/hwg_adapter/adapter.py:653) | error candidate | _Parser._parse_include | `Unterminated @include path` |
| [673](/root/laintas_cli/hwg_adapter/adapter.py:673) | error candidate | _Parser._parse_edge | `Expected -> or => after #{from_id}# (a bare #name# must start an edge: #from# -> #to#)` |
| [689](/root/laintas_cli/hwg_adapter/adapter.py:689) | error candidate | _Parser._parse_edge | `Expected target #name# after {arrow} in edge from #{from_id}#` |
| [705](/root/laintas_cli/hwg_adapter/adapter.py:705) | error candidate | _Parser._parse_schedule_block | `Unknown block "({label})" — only (schedule) { ... } is recognised` |
| [725](/root/laintas_cli/hwg_adapter/adapter.py:725) | error candidate | _Parser._read_paren | `Unclosed (, expected )` |
| [752](/root/laintas_cli/hwg_adapter/adapter.py:752) | error candidate | _Parser._read_brace_content | `Unclosed {, expected }` |
| [779](/root/laintas_cli/hwg_adapter/adapter.py:779) | error candidate | _Parser._read_bracket_content | `Unclosed [, expected ]` |
| [786](/root/laintas_cli/hwg_adapter/adapter.py:786) | error candidate | _Parser._read_name | `Unclosed name, expected #` |
| [789](/root/laintas_cli/hwg_adapter/adapter.py:789) | error candidate | _Parser._read_name | `Empty name` |
| [796](/root/laintas_cli/hwg_adapter/adapter.py:796) | error candidate | _Parser._skip_comment_block | `Unclosed comment block, expected ˋˋˋ` |
| [809](/root/laintas_cli/hwg_adapter/adapter.py:809) | error candidate | _Parser._expect | `Expected "{text}"` |

### hwg_runner.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [827](/root/laintas_cli/hwg_runner.py:827) | message candidate | run_hwg_file / message | `Manual node #{current['id']}# requires human action.` |
| [1127](/root/laintas_cli/hwg_runner.py:1127) | message candidate | _open_fanout / error | `HWG stopped at #{node['id']}#: verdict {verdict!r} matched none of its => branches.` |
| [1132](/root/laintas_cli/hwg_runner.py:1132) | message candidate | _open_fanout / error | `HWG stopped at #{node['id']}#: its => branches do not converge on a join node.` |
| [1145](/root/laintas_cli/hwg_runner.py:1145) | message candidate | _choose_next / error | `HWG stopped at #{current['id']}#: verdict {verdict!r} matched no outgoing edge.` |
| [1156](/root/laintas_cli/hwg_runner.py:1156) | message candidate | _choose_next / error | `HWG stopped at #{current['id']}#: all matching edges exhausted maxLoops.` |

### hwg_view.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [440](/root/laintas_cli/hwg_view.py:440) | output | open_viewer / 0 | `hwg view: cannot read '{path}': {e}` |
| [445](/root/laintas_cli/hwg_view.py:445) | output | open_viewer / 0 | `hwg view: parse error - {e}` |
| [449](/root/laintas_cli/hwg_view.py:449) | output | open_viewer / 0 | `hwg view: validation errors: ⏎ {'\n'.join(errors)}` |
| [455](/root/laintas_cli/hwg_view.py:455) | output | open_viewer / 0 | `hwg view: graph has no nodes` |

### hwo_adapter/adapter.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [134](/root/laintas_cli/hwo_adapter/adapter.py:134) | error candidate | _split_effort | `Empty thinking gear after ':'` |
| [137](/root/laintas_cli/hwo_adapter/adapter.py:137) | error candidate | _split_effort | `Unknown thinking gear '{gear}' — use one of: {', '.join(EFFORT_GEARS)}` |
| [178](/root/laintas_cli/hwo_adapter/adapter.py:178) | error candidate | _Parser.parse | `Unexpected token "{snippet}"` |
| [220](/root/laintas_cli/hwo_adapter/adapter.py:220) | error candidate | _Parser._parse_parallel | `Unclosed parallel block, expected // (preceded by whitespace or a brace)` |
| [230](/root/laintas_cli/hwo_adapter/adapter.py:230) | error candidate | _Parser._parse_workflow | `@line must be followed by [in(...), out(...)]` |
| [247](/root/laintas_cli/hwo_adapter/adapter.py:247) | error candidate | _Parser._parse_agent | `Unclosed agent name, expected #` |
| [264](/root/laintas_cli/hwo_adapter/adapter.py:264) | error candidate | _Parser._parse_agent | `Empty agent name` |
| [266](/root/laintas_cli/hwo_adapter/adapter.py:266) | error candidate | _Parser._parse_agent | `Empty model after '@' in agent name` |
| [283](/root/laintas_cli/hwo_adapter/adapter.py:283) | error candidate | _Parser._parse_agent | `Agent "{name}" must be followed by { — the prompt prefix goes either before or right after the name: (file)#{name}# { ... } or #{name}#(file) { ... }` |
| [290](/root/laintas_cli/hwo_adapter/adapter.py:290) | error candidate | _Parser._parse_agent | `Unclosed body for agent "{name}", expected }` |
| [320](/root/laintas_cli/hwo_adapter/adapter.py:320) | error candidate | _Parser._read_bracket_content | `Unclosed [, expected ]` |
| [328](/root/laintas_cli/hwo_adapter/adapter.py:328) | error candidate | _Parser._read_prompt_prefix | `Unclosed prompt prefix, expected )` |
| [355](/root/laintas_cli/hwo_adapter/adapter.py:355) | error candidate | _Parser._skip_comment_block | `Unclosed comment block, expected ˋˋˋ` |
| [389](/root/laintas_cli/hwo_adapter/adapter.py:389) | error candidate | _Parser._expect | `Expected "{text}"` |

### hwo_runner.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [99](/root/laintas_cli/hwo_runner.py:99) | error candidate | _to_node | `Unknown HWO node type: {t!r}` |
| [774](/root/laintas_cli/hwo_runner.py:774) | output | _step_started / 1 | `step_started` |
| [781](/root/laintas_cli/hwo_runner.py:781) | output | _step_finished / 1 | `step_completed` |
| [781](/root/laintas_cli/hwo_runner.py:781) | output | _step_finished / 1 | `step_failed` |
| [909](/root/laintas_cli/hwo_runner.py:909) | output | _run_task_group / 1 | `agent_spawned` |
| [920](/root/laintas_cli/hwo_runner.py:920) | output | _run_task_group._runner / 1 | `agent_error` |
| [927](/root/laintas_cli/hwo_runner.py:927) | output | _run_task_group._runner / 1 | `agent_started` |
| [942](/root/laintas_cli/hwo_runner.py:942) | output | _run_task_group._runner / 1 | `agent_done` |
| [949](/root/laintas_cli/hwo_runner.py:949) | output | _run_task_group._runner / 1 | `agent_error` |
| [1063](/root/laintas_cli/hwo_runner.py:1063) | output | _run_agent / 1 | `agent_spawned` |
| [1076](/root/laintas_cli/hwo_runner.py:1076) | output | _run_agent._runner / 1 | `agent_error` |
| [1084](/root/laintas_cli/hwo_runner.py:1084) | output | _run_agent._runner / 1 | `agent_started` |
| [1205](/root/laintas_cli/hwo_runner.py:1205) | output | _run_agent._runner / 1 | `agent_error` |
| [1205](/root/laintas_cli/hwo_runner.py:1205) | output | _run_agent._runner / 1 | `agent_done` |
| [1366](/root/laintas_cli/hwo_runner.py:1366) | output | run_hwo_file / 1 | `workflow_started` |
| [1386](/root/laintas_cli/hwo_runner.py:1386) | output | run_hwo_file / 1 | `workflow_cancelled` |
| [1386](/root/laintas_cli/hwo_runner.py:1386) | output | run_hwo_file / 1 | `workflow_completed` |
| [1386](/root/laintas_cli/hwo_runner.py:1386) | output | run_hwo_file / 1 | `workflow_failed` |

### hwo_ui.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [319](/root/laintas_cli/hwo_ui.py:319) | error candidate | _bind_runtime_step_ids | `HWO UI/runner task mismatch ({len(tasks)} != {len(task_steps)})` |
| [435](/root/laintas_cli/hwo_ui.py:435) | error candidate | _runner_agent_to_ui | `Nested parallel block inside #{ra.name}# is not editable in Workflow Studio yet; the source was not modified` |
| [480](/root/laintas_cli/hwo_ui.py:480) | error candidate | load_hwo_file | `Parallel blocks containing non-Agent nodes are not editable` |
| [701](/root/laintas_cli/hwo_ui.py:701) | message candidate | <module> / warning | `bold #e3b341` |

### hwo_view.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [149](/root/laintas_cli/hwo_view.py:149) | output | open_lane_viewer / 0 | `hwo view: cannot read '{path}': {e}` |
| [154](/root/laintas_cli/hwo_view.py:154) | output | open_lane_viewer / 0 | `hwo view: parse error - {e}` |
| [158](/root/laintas_cli/hwo_view.py:158) | output | open_lane_viewer / 0 | `hwo view: validation errors: ⏎ {'\n'.join(errors)}` |
| [164](/root/laintas_cli/hwo_view.py:164) | output | open_lane_viewer / 0 | `hwo view: workflow has no steps` |

### identity_store.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [83](/root/laintas_cli/identity_store.py:83) | error candidate | validate_name | `invalid identity name {name!r}: use letters, digits, '.', '-', '_'` |

### laintas_cli.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [60](/root/laintas_cli/laintas_cli.py:60) | output | _run_pow_command / 0 | `AI-PoW: the ai-pow extension is not available ({message}). Install it with: /extensions install ai-pow` |
| [80](/root/laintas_cli/laintas_cli.py:80) | output | <module> / 0 | `laintas-cli {_v}` |
| [118](/root/laintas_cli/laintas_cli.py:118) | title/menu/help | <module> / message | `.*use of fork\(\) may lead to deadlocks in the child.*` |
| [224](/root/laintas_cli/laintas_cli.py:224) | error candidate | _restart_command | `restart script does not exist: {script}` |
| [227](/root/laintas_cli/laintas_cli.py:227) | error candidate | _restart_command | `restart executable does not exist: {target}` |
| [229](/root/laintas_cli/laintas_cli.py:229) | error candidate | _restart_command | `restart executable is not executable: {target}` |
| [367](/root/laintas_cli/laintas_cli.py:367) | message candidate | <module> / error | `bold #f85149` |
| [368](/root/laintas_cli/laintas_cli.py:368) | message candidate | <module> / warning | `#e3b341` |
| [495](/root/laintas_cli/laintas_cli.py:495) | error candidate | <module> | `markdown theme {_preset_name!r} is missing keys: {sorted(_MARKDOWN_REQUIRED_KEYS - set(_preset))}` |
| [503](/root/laintas_cli/laintas_cli.py:503) | message candidate | <module> / error | `bold #cf222e` |
| [503](/root/laintas_cli/laintas_cli.py:503) | message candidate | <module> / warning | `#9a6700` |
| [511](/root/laintas_cli/laintas_cli.py:511) | message candidate | <module> / error | `bold reverse` |
| [511](/root/laintas_cli/laintas_cli.py:511) | message candidate | <module> / warning | `bold` |
| [538](/root/laintas_cli/laintas_cli.py:538) | error candidate | _load_markdown_palette | `top-level JSON value must be an object` |
| [553](/root/laintas_cli/laintas_cli.py:553) | output | _load_markdown_palette / 0 | `markdown_theme.json: ignored invalid style(s) for {', '.join(sorted(invalid))}.` |
| [559](/root/laintas_cli/laintas_cli.py:559) | output | _load_markdown_palette / 0 | `markdown_theme.json ignored ({exc}); using default Markdown palette.` |
| [582](/root/laintas_cli/laintas_cli.py:582) | error candidate | _apply_ui_theme | `theme expects dark, light, or mono` |
| [780](/root/laintas_cli/laintas_cli.py:780) | resolved output candidate | _press_enter_to_continue | ` ⏎ {message}...` |
| [782](/root/laintas_cli/laintas_cli.py:782) | resolved output candidate | _press_enter_to_continue | ` ⏎ {message}...` |
| [1921](/root/laintas_cli/laintas_cli.py:1921) | output | _recover_deleted_cwd / 0 | `The working directory was deleted and no replacement could be entered.` |
| [1925](/root/laintas_cli/laintas_cli.py:1925) | output | _recover_deleted_cwd / 0 | `{symbols.WARN} {escape(left_behind)} no longer exists — moved to {escape(cwd)}.` |
| [1978](/root/laintas_cli/laintas_cli.py:1978) | error candidate | _ensure_term0_alive | `replacement term0 exited during startup` |
| [2042](/root/laintas_cli/laintas_cli.py:2042) | output | _finish_attached / 0 | `{symbols.DOT} Detached — {escape(command)} keeps running in term0. /fg returns to it.` |
| [2057](/root/laintas_cli/laintas_cli.py:2057) | output | _finish_attached / 0 | `{symbols.DOT} The shell exited; started a new one in {escape(os.getcwd())}.` |
| [2081](/root/laintas_cli/laintas_cli.py:2081) | output | _run_attached_command / 0 | `{escape(term_attach.busy_message(session))}` |
| [2102](/root/laintas_cli/laintas_cli.py:2102) | output | _run_attached_command / 0 | `term0: {escape(type(exc).__name__)}: {escape(str(exc))}` |
| [2132](/root/laintas_cli/laintas_cli.py:2132) | output | _report_detached_finish / 0 | `{symbols.DOT} term0: {escape(command)} finished (exit {res.returncode}). /term shows its output.` |
| [2136](/root/laintas_cli/laintas_cli.py:2136) | output | _report_detached_finish / 0 | `{symbols.DOT} term0: the shell running {escape(command)} exited.` |
| [2145](/root/laintas_cli/laintas_cli.py:2145) | output | _cmd_fg / 0 | `Nothing is running in the background of term0.` |
| [2154](/root/laintas_cli/laintas_cli.py:2154) | output | _cmd_fg / 0 | `Cannot take the terminal: {escape(str(exc))}` |
| [2157](/root/laintas_cli/laintas_cli.py:2157) | output | _cmd_fg / 0 | `Could not reattach to term0.` |
| [2666](/root/laintas_cli/laintas_cli.py:2666) | error candidate | _build_connected_subterminal_cmd | `invalid environment variable name: {key!r}` |
| [3585](/root/laintas_cli/laintas_cli.py:3585) | command/help | <module> / /help:category | `Basics` |
| [3585](/root/laintas_cli/laintas_cli.py:3585) | command/help | <module> / /help:command | `/help` |
| [3585](/root/laintas_cli/laintas_cli.py:3585) | command/help | <module> / /help:description | `Show command help` |
| [3585](/root/laintas_cli/laintas_cli.py:3585) | command/help | <module> / /help:usage | `/help [command]` |
| [3586](/root/laintas_cli/laintas_cli.py:3586) | command/help | <module> / /cwd:category | `Basics` |
| [3586](/root/laintas_cli/laintas_cli.py:3586) | command/help | <module> / /cwd:command | `/cwd` |
| [3586](/root/laintas_cli/laintas_cli.py:3586) | command/help | <module> / /cwd:description | `Show the working directory` |
| [3587](/root/laintas_cli/laintas_cli.py:3587) | command/help | <module> / /fg:category | `Basics` |
| [3587](/root/laintas_cli/laintas_cli.py:3587) | command/help | <module> / /fg:command | `/fg` |
| [3587](/root/laintas_cli/laintas_cli.py:3587) | command/help | <module> / /fg:description | `Return to the command detached from term0 with Ctrl+]` |
| [3589](/root/laintas_cli/laintas_cli.py:3589) | command/help | <module> / /messages:category | `Basics` |
| [3589](/root/laintas_cli/laintas_cli.py:3589) | command/help | <module> / /messages:command | `/messages` |
| [3589](/root/laintas_cli/laintas_cli.py:3589) | command/help | <module> / /messages:description | `Read the notices behind the L> mark` |
| [3590](/root/laintas_cli/laintas_cli.py:3590) | command/help | <module> / /messages:usage | `/messages [list\|read <n>\|seen\|dismiss <n>\|clear]` |
| [3594](/root/laintas_cli/laintas_cli.py:3594) | command/help | <module> / /messages:completion | `Print every message without marking any read` |
| [3595](/root/laintas_cli/laintas_cli.py:3595) | command/help | <module> / /messages:completion | `Print one message in full and mark it read` |
| [3596](/root/laintas_cli/laintas_cli.py:3596) | command/help | <module> / /messages:completion | `Mark every message read` |
| [3597](/root/laintas_cli/laintas_cli.py:3597) | command/help | <module> / /messages:completion | `Remove one message` |
| [3598](/root/laintas_cli/laintas_cli.py:3598) | command/help | <module> / /messages:completion | `Remove every message` |
| [3601](/root/laintas_cli/laintas_cli.py:3601) | command/help | <module> / /messages:help_text | `Startup advisories - usage tips, the training-data opt-in, an available update, a resumable session, extension load failures - are collected behind the L> mark on the prompt instead of being printed above it. With no argument this opens the reader, the same one Alt+0 reaches, where arrows move, Enter reads, d dismisses and c clears. The subcommands do the same things without a full-screen terminal, which is what a sub-terminal, a piped session or --execute has. <n> is the position shown by /messages list; a message key works too.` |
| [3611](/root/laintas_cli/laintas_cli.py:3611) | command/help | <module> / /scan:category | `Basics` |
| [3611](/root/laintas_cli/laintas_cli.py:3611) | command/help | <module> / /scan:command | `/scan` |
| [3611](/root/laintas_cli/laintas_cli.py:3611) | command/help | <module> / /scan:description | `List user-facing PATH commands` |
| [3613](/root/laintas_cli/laintas_cli.py:3613) | command/help | <module> / /img:category | `Basics` |
| [3613](/root/laintas_cli/laintas_cli.py:3613) | command/help | <module> / /img:command | `/img` |
| [3613](/root/laintas_cli/laintas_cli.py:3613) | command/help | <module> / /img:description | `Read an image: ask about it, or transcribe it` |
| [3614](/root/laintas_cli/laintas_cli.py:3614) | command/help | <module> / /img:usage | `/img [<path> [question] \| text <path> \| list]` |
| [3617](/root/laintas_cli/laintas_cli.py:3617) | command/help | <module> / /img:help_text | `With a question, a vision model looks at the image and answers — use it for screenshots, mockups and diagrams. With --text, OCR transcribes the whole thing instead, keeping headings and tables; that path also accepts a .pdf. With no arguments, lists images in the working directory and recent browser screenshots. ˋ--textˋ is accepted as an alias for the ˋtextˋ action.` |
| [3623](/root/laintas_cli/laintas_cli.py:3623) | command/help | <module> / /login:category | `Account & Session` |
| [3623](/root/laintas_cli/laintas_cli.py:3623) | command/help | <module> / /login:command | `/login` |
| [3623](/root/laintas_cli/laintas_cli.py:3623) | command/help | <module> / /login:description | `Re-authenticate with Laintas` |
| [3625](/root/laintas_cli/laintas_cli.py:3625) | command/help | <module> / /account:category | `Account & Session` |
| [3625](/root/laintas_cli/laintas_cli.py:3625) | command/help | <module> / /account:command | `/account` |
| [3625](/root/laintas_cli/laintas_cli.py:3625) | command/help | <module> / /account:description | `List, add or switch account profiles` |
| [3626](/root/laintas_cli/laintas_cli.py:3626) | command/help | <module> / /account:usage | `/account [list\|add <alias>\|switch <account>\|alias <name>\|legacy\|adopt <session-id>]` |
| [3628](/root/laintas_cli/laintas_cli.py:3628) | command/help | <module> / /account:help_text | `Accounts keep separate tasks and credentials. Switching saves the current task and restarts into the selected account. Legacy tasks require explicit adoption.` |
| [3629](/root/laintas_cli/laintas_cli.py:3629) | command/help | <module> / /private:category | `Account & Session` |
| [3629](/root/laintas_cli/laintas_cli.py:3629) | command/help | <module> / /private:command | `/private` |
| [3629](/root/laintas_cli/laintas_cli.py:3629) | command/help | <module> / /private:description | `Manage private file permissions` |
| [3630](/root/laintas_cli/laintas_cli.py:3630) | command/help | <module> / /private:usage | `/private [list\|set <path> hidden\|no-read\|no-write [...]\|remove <path>]` |
| [3633](/root/laintas_cli/laintas_cli.py:3633) | command/help | <module> / /password:category | `Account & Session` |
| [3633](/root/laintas_cli/laintas_cli.py:3633) | command/help | <module> / /password:command | `/password` |
| [3633](/root/laintas_cli/laintas_cli.py:3633) | command/help | <module> / /password:description | `Open the local password vault` |
| [3634](/root/laintas_cli/laintas_cli.py:3634) | command/help | <module> / /password:usage | `/password` |
| [3636](/root/laintas_cli/laintas_cli.py:3636) | command/help | <module> / /password:help_text | `Opens a local interactive vault on this terminal. Entries are either logins (username + password for an approved site) or secrets (a single key/token in any format, multi-line supported). Descriptions and approved origins are visible to the AI assistant; usernames, passwords, secret values, and notes stay encrypted and never enter the chat. Takes no arguments — secrets must never be typed as command text. The AI can list entries (password.list) and request a protected fill (password.fill); fills require a trusted credential broker and fail closed when none is deployed.` |
| [3647](/root/laintas_cli/laintas_cli.py:3647) | command/help | <module> / /training:command | `/training` |
| [3647](/root/laintas_cli/laintas_cli.py:3647) | command/help | <module> / /training:description | `Manage training-data sharing (cloud) and capture (local)` |
| [3648](/root/laintas_cli/laintas_cli.py:3648) | command/help | <module> / /training:category | `Account & Session` |
| [3649](/root/laintas_cli/laintas_cli.py:3649) | command/help | <module> / /training:usage | `/training [status\|on\|off\|local [status\|on\|off\|purge\|export <file>]]` |
| [3652](/root/laintas_cli/laintas_cli.py:3652) | command/help | <module> / /training:completion | `Show cloud sharing state` |
| [3653](/root/laintas_cli/laintas_cli.py:3653) | command/help | <module> / /training:completion | `Let the Gateway retain what it serves` |
| [3654](/root/laintas_cli/laintas_cli.py:3654) | command/help | <module> / /training:completion | `Stop Gateway collection` |
| [3655](/root/laintas_cli/laintas_cli.py:3655) | command/help | <module> / /training:completion | `Keep a copy on this machine instead/as well` |
| [3658](/root/laintas_cli/laintas_cli.py:3658) | command/help | <module> / /training:help_text | `Two independent switches. Cloud sharing is off by default: 'on' explicitly allows Laintas to retain gateway-observed CLI model inputs and outputs, 'off' stops future collection (already collected data is retained). '/training local on' writes the same interactions to ~/.laintas/training/ on this machine and uploads nothing; it works with any backend and needs no login. Neither switch reads the other — local capture does not enable or suppress cloud collection.` |
| [3667](/root/laintas_cli/laintas_cli.py:3667) | command/help | <module> / /usage:command | `/usage` |
| [3667](/root/laintas_cli/laintas_cli.py:3667) | command/help | <module> / /usage:description | `Show AI usage — local token stats + Laintas backend usage` |
| [3668](/root/laintas_cli/laintas_cli.py:3668) | command/help | <module> / /usage:category | `Account & Session` |
| [3668](/root/laintas_cli/laintas_cli.py:3668) | command/help | <module> / /usage:usage | `/usage [7d\|30d\|90d] [local] \| buy <calls\|storage>` |
| [3671](/root/laintas_cli/laintas_cli.py:3671) | command/help | <module> / /usage:completion | `Backend usage for the last 7 days` |
| [3672](/root/laintas_cli/laintas_cli.py:3672) | command/help | <module> / /usage:completion | `Backend usage for the last 30 days (default)` |
| [3673](/root/laintas_cli/laintas_cli.py:3673) | command/help | <module> / /usage:completion | `Backend usage for the last 90 days` |
| [3674](/root/laintas_cli/laintas_cli.py:3674) | command/help | <module> / /usage:completion | `Local token stats only, no network` |
| [3675](/root/laintas_cli/laintas_cli.py:3675) | command/help | <module> / /usage:completion | `Buy a call or storage pack` |
| [3678](/root/laintas_cli/laintas_cli.py:3678) | command/help | <module> / /resume:command | `/resume` |
| [3678](/root/laintas_cli/laintas_cli.py:3678) | command/help | <module> / /resume:description | `Switch to a saved session (picker; echo last N events, default 20)` |
| [3679](/root/laintas_cli/laintas_cli.py:3679) | command/help | <module> / /resume:category | `Account & Session` |
| [3679](/root/laintas_cli/laintas_cli.py:3679) | command/help | <module> / /resume:usage | `/resume [N\|all\|latest]` |
| [3682](/root/laintas_cli/laintas_cli.py:3682) | command/help | <module> / /resume:completion | `Restore the newest session without the picker` |
| [3683](/root/laintas_cli/laintas_cli.py:3683) | command/help | <module> / /resume:completion | `Echo the whole conversation after restoring` |
| [3685](/root/laintas_cli/laintas_cli.py:3685) | command/help | <module> / /fork:category | `Account & Session` |
| [3685](/root/laintas_cli/laintas_cli.py:3685) | command/help | <module> / /fork:command | `/fork` |
| [3685](/root/laintas_cli/laintas_cli.py:3685) | command/help | <module> / /fork:description | `Fork current context into a named branch, or start a new session from current context` |
| [3685](/root/laintas_cli/laintas_cli.py:3685) | command/help | <module> / /fork:usage | `/fork [name]` |
| [3686](/root/laintas_cli/laintas_cli.py:3686) | command/help | <module> / /new:category | `Account & Session` |
| [3686](/root/laintas_cli/laintas_cli.py:3686) | command/help | <module> / /new:command | `/new` |
| [3686](/root/laintas_cli/laintas_cli.py:3686) | command/help | <module> / /new:description | `Start a new live session` |
| [3686](/root/laintas_cli/laintas_cli.py:3686) | command/help | <module> / /new:usage | `/new` |
| [3688](/root/laintas_cli/laintas_cli.py:3688) | command/help | <module> / /exit:category | `Account & Session` |
| [3688](/root/laintas_cli/laintas_cli.py:3688) | command/help | <module> / /exit:command | `/exit` |
| [3688](/root/laintas_cli/laintas_cli.py:3688) | command/help | <module> / /exit:description | `Log out and exit` |
| [3689](/root/laintas_cli/laintas_cli.py:3689) | command/help | <module> / /quit:category | `Account & Session` |
| [3689](/root/laintas_cli/laintas_cli.py:3689) | command/help | <module> / /quit:command | `/quit` |
| [3689](/root/laintas_cli/laintas_cli.py:3689) | command/help | <module> / /quit:description | `Exit without logging out` |
| [3690](/root/laintas_cli/laintas_cli.py:3690) | command/help | <module> / /back:category | `Account & Session` |
| [3690](/root/laintas_cli/laintas_cli.py:3690) | command/help | <module> / /back:command | `/back` |
| [3690](/root/laintas_cli/laintas_cli.py:3690) | command/help | <module> / /back:description | `Detach from a sub-terminal` |
| [3692](/root/laintas_cli/laintas_cli.py:3692) | command/help | <module> / /version:category | `Account & Session` |
| [3692](/root/laintas_cli/laintas_cli.py:3692) | command/help | <module> / /version:command | `/version` |
| [3692](/root/laintas_cli/laintas_cli.py:3692) | command/help | <module> / /version:description | `Show version or update` |
| [3693](/root/laintas_cli/laintas_cli.py:3693) | command/help | <module> / /version:usage | `/version [check\|update [--force]\|enterprise [on\|off\|gateway]]` |
| [3696](/root/laintas_cli/laintas_cli.py:3696) | command/help | <module> / /version:completion | `Check whether a newer version is available` |
| [3697](/root/laintas_cli/laintas_cli.py:3697) | command/help | <module> / /version:completion | `Download, install, and restart on the latest version` |
| [3698](/root/laintas_cli/laintas_cli.py:3698) | command/help | <module> / /version:completion | `Turn your organisation layer on or off in this CLI` |
| [3701](/root/laintas_cli/laintas_cli.py:3701) | command/help | <module> / /extensions:command | `/extensions` |
| [3701](/root/laintas_cli/laintas_cli.py:3701) | command/help | <module> / /extensions:description | `Install, manage, and publish community extensions` |
| [3702](/root/laintas_cli/laintas_cli.py:3702) | command/help | <module> / /extensions:category | `Account & Session` |
| [3703](/root/laintas_cli/laintas_cli.py:3703) | command/help | <module> / /extensions:usage | `/extensions [list\|available [all\|official\|community]\|search <keyword>\|install <src>\|publish <name>\|remove <name>\|trust <name>\|untrust <name>\|info <name>\|create <name>\|pack <name>]` |
| [3706](/root/laintas_cli/laintas_cli.py:3706) | command/help | <module> / /extensions:completion | `List installed extensions` |
| [3707](/root/laintas_cli/laintas_cli.py:3707) | command/help | <module> / /extensions:completion | `Browse installable official and community extensions` |
| [3708](/root/laintas_cli/laintas_cli.py:3708) | command/help | <module> / /extensions:completion | `Search the extension marketplace` |
| [3709](/root/laintas_cli/laintas_cli.py:3709) | command/help | <module> / /extensions:completion | `Install from a local path, URL, or @author/slug` |
| [3710](/root/laintas_cli/laintas_cli.py:3710) | command/help | <module> / /extensions:completion | `Publish an installed extension from Laintas Storage` |
| [3711](/root/laintas_cli/laintas_cli.py:3711) | command/help | <module> / /extensions:completion | `Uninstall an extension` |
| [3712](/root/laintas_cli/laintas_cli.py:3712) | command/help | <module> / /extensions:completion | `Approve an extension's hash` |
| [3713](/root/laintas_cli/laintas_cli.py:3713) | command/help | <module> / /extensions:completion | `Revoke trust for an extension` |
| [3714](/root/laintas_cli/laintas_cli.py:3714) | command/help | <module> / /extensions:completion | `Show details about an installed extension` |
| [3715](/root/laintas_cli/laintas_cli.py:3715) | command/help | <module> / /extensions:completion | `Scaffold a new extension` |
| [3716](/root/laintas_cli/laintas_cli.py:3716) | command/help | <module> / /extensions:completion | `Pack an extension into a .lext archive` |
| [3718](/root/laintas_cli/laintas_cli.py:3718) | command/help | <module> / /name:category | `Agents & Terminals` |
| [3718](/root/laintas_cli/laintas_cli.py:3718) | command/help | <module> / /name:command | `/name` |
| [3718](/root/laintas_cli/laintas_cli.py:3718) | command/help | <module> / /name:description | `Show or set the current agent name` |
| [3718](/root/laintas_cli/laintas_cli.py:3718) | command/help | <module> / /name:usage | `/name [new-name]` |
| [3720](/root/laintas_cli/laintas_cli.py:3720) | command/help | <module> / /hire:command | `/hire` |
| [3720](/root/laintas_cli/laintas_cli.py:3720) | command/help | <module> / /hire:description | `Hire an undeployed employee; does not start an assignment` |
| [3721](/root/laintas_cli/laintas_cli.py:3721) | command/help | <module> / /hire:category | `Agents & Terminals` |
| [3722](/root/laintas_cli/laintas_cli.py:3722) | command/help | <module> / /hire:usage | `/hire [name] [--profile role] [--prompt file] [--tools name,...\|inherit] [--model [id]] [--terminal name]` |
| [3725](/root/laintas_cli/laintas_cli.py:3725) | command/help | <module> / /hire:help_text | `Creates a persistent employee identity with its own prompt and tool policy, base model, and logical home terminal. It is not auto-deployed. Use /station <agent> --task <work> for a private temporary terminal, or /station <agent> <terminal> to deploy it explicitly.` |
| [3731](/root/laintas_cli/laintas_cli.py:3731) | command/help | <module> / /agent:command | `/agent` |
| [3731](/root/laintas_cli/laintas_cli.py:3731) | command/help | <module> / /agent:description | `Switch the REPL's current Agent focus without redeploying` |
| [3732](/root/laintas_cli/laintas_cli.py:3732) | command/help | <module> / /agent:category | `Agents & Terminals` |
| [3732](/root/laintas_cli/laintas_cli.py:3732) | command/help | <module> / /agent:usage | `/agent [agent-id-or-name]` |
| [3734](/root/laintas_cli/laintas_cli.py:3734) | command/help | <module> / /agent:help_text | `Switches the main REPL's current Agent conversation/execution identity in place: no terminal is moved, no deployment is reissued, and the focused Agent keeps its existing state, history, and bound terminal. With no argument, prints the current Agent and lists every other Agent you can switch to. Accepts either an Agent ID or an Agent name (case-insensitive name match). Use /agents for the full-screen multi-Agent view; /agent only changes which Agent the main REPL drives.` |
| [3744](/root/laintas_cli/laintas_cli.py:3744) | command/help | <module> / /agents:command | `/agents` |
| [3744](/root/laintas_cli/laintas_cli.py:3744) | command/help | <module> / /agents:description | `Open the full-screen multi-Agent focus and activity view` |
| [3745](/root/laintas_cli/laintas_cli.py:3745) | command/help | <module> / /agents:category | `Agents & Terminals` |
| [3745](/root/laintas_cli/laintas_cli.py:3745) | command/help | <module> / /agents:usage | `/agents [agent-id\|tree\|--plain]` |
| [3748](/root/laintas_cli/laintas_cli.py:3748) | command/help | <module> / /agents:help_text | `Opens a dedicated Focus + Agent Rail + Event Feed interface. Passing an Agent ID selects its conversation view without changing deployment or shell ownership. Use --plain for a script-friendly snapshot. Inside Agents Mode, Enter sends to the focused Agent, @Agent performs one-shot routing, Tab opens the Agent rail, Alt+arrow switches Agent/terminal, PageUp/PageDown scrolls, and Esc exits.` |
| [3755](/root/laintas_cli/laintas_cli.py:3755) | command/help | <module> / /term:category | `Agents & Terminals` |
| [3755](/root/laintas_cli/laintas_cli.py:3755) | command/help | <module> / /term:command | `/term` |
| [3755](/root/laintas_cli/laintas_cli.py:3755) | command/help | <module> / /term:description | `Manage terminals and two-level adoption` |
| [3755](/root/laintas_cli/laintas_cli.py:3755) | command/help | <module> / /term:usage | `/term [name\|peers\|adopt <id> --name <name>\|accept <invite>\|release [name]\|rename <old> <new>]` |
| [3756](/root/laintas_cli/laintas_cli.py:3756) | command/help | <module> / /helpwo:category | `Agents & Terminals` |
| [3756](/root/laintas_cli/laintas_cli.py:3756) | command/help | <module> / /helpwo:command | `/helpwo` |
| [3756](/root/laintas_cli/laintas_cli.py:3756) | command/help | <module> / /helpwo:description | `Start the local Helpwo companion in its own sub-terminal; /helpwo stop closes it` |
| [3756](/root/laintas_cli/laintas_cli.py:3756) | command/help | <module> / /helpwo:usage | `/helpwo [stop]` |
| [3758](/root/laintas_cli/laintas_cli.py:3758) | command/help | <module> / /app:command | `/app` |
| [3758](/root/laintas_cli/laintas_cli.py:3758) | command/help | <module> / /app:description | `Run a registered application in its own sub-terminal with its own agent` |
| [3759](/root/laintas_cli/laintas_cli.py:3759) | command/help | <module> / /app:category | `Agents & Terminals` |
| [3759](/root/laintas_cli/laintas_cli.py:3759) | command/help | <module> / /app:usage | `/app [list\|start <name>\|stop <name>\|trust <name>\|revoke <name>]` |
| [3762](/root/laintas_cli/laintas_cli.py:3762) | command/help | <module> / /app:completion | `List registered applications and their state` |
| [3763](/root/laintas_cli/laintas_cli.py:3763) | command/help | <module> / /app:completion | `Start a trusted application in its own sub-terminal` |
| [3764](/root/laintas_cli/laintas_cli.py:3764) | command/help | <module> / /app:completion | `Stop an application and close its sub-terminal` |
| [3765](/root/laintas_cli/laintas_cli.py:3765) | command/help | <module> / /app:completion | `Trust an application (required before first start)` |
| [3766](/root/laintas_cli/laintas_cli.py:3766) | command/help | <module> / /app:completion | `Revoke an application's trust` |
| [3769](/root/laintas_cli/laintas_cli.py:3769) | command/help | <module> / /app:help_text | `Applications are described by manifests in ~/.laintas/apps/*.json or ./.laintas/apps/*.json: name, description, command, prompt, persistence (none\|workspace), port. Starting one creates a sub-terminal of the same name whose agent serves only that application, and a loopback bridge that exposes nothing but the conversation (send chat/abort/approval-response, poll updates). The manifest's command is started with LAINTAS_APP_BRIDGE_URL, LAINTAS_APP_TOKEN and LAINTAS_APP_AGENT_ID in its environment. A manifest must be trusted before it can start, and again after it changes. Helpwo is not an /app; use /helpwo.` |
| [3781](/root/laintas_cli/laintas_cli.py:3781) | command/help | <module> / /station:command | `/station` |
| [3781](/root/laintas_cli/laintas_cli.py:3781) | command/help | <module> / /station:description | `Manage agents and terminals; route or assign work` |
| [3782](/root/laintas_cli/laintas_cli.py:3782) | command/help | <module> / /station:category | `Agents & Terminals` |
| [3782](/root/laintas_cli/laintas_cli.py:3782) | command/help | <module> / /station:usage | `/station <agent-id> [terminal] [--task <work>]` |
| [3785](/root/laintas_cli/laintas_cli.py:3785) | command/help | <module> / /station:help_text | `With no arguments, opens Station. Use /station auto --task <work> to route an isolated child, or /station suggest --task <work> to preview. Without --task, deploys or moves the employee. With --task, starts a fresh background Assignment with isolated state/history. For an undeployed employee, omitting the terminal with --task uses a private temporary terminal; deployment always requires an explicit target.` |
| [3793](/root/laintas_cli/laintas_cli.py:3793) | command/help | <module> / /handoff:command | `/handoff` |
| [3793](/root/laintas_cli/laintas_cli.py:3793) | command/help | <module> / /handoff:description | `Create a task checkpoint for the next account` |
| [3794](/root/laintas_cli/laintas_cli.py:3794) | command/help | <module> / /handoff:category | `Agents & Terminals` |
| [3795](/root/laintas_cli/laintas_cli.py:3795) | command/help | <module> / /handoff:usage | `/handoff [list\|new <title>\|accept <id>\|show <id>\|claim <id>\|release <id>\|note <id> <text>\|close <id>\|reopen <id>\|sync [id]\|export <id> [file]\|import <token\|file>\|remote\|fetch <remote-path>]` |
| [3799](/root/laintas_cli/laintas_cli.py:3799) | command/help | <module> / /handoff:completion | `Every handoff in this workspace and what is left on it` |
| [3800](/root/laintas_cli/laintas_cli.py:3800) | command/help | <module> / /handoff:completion | `Write one before you stop for the day` |
| [3801](/root/laintas_cli/laintas_cli.py:3801) | command/help | <module> / /handoff:completion | `Create and continue this account's receiving session` |
| [3802](/root/laintas_cli/laintas_cli.py:3802) | command/help | <module> / /handoff:completion | `Open one: baseline, what is outstanding, what not to retry` |
| [3803](/root/laintas_cli/laintas_cli.py:3803) | command/help | <module> / /handoff:completion | `Say you are picking it up` |
| [3804](/root/laintas_cli/laintas_cli.py:3804) | command/help | <module> / /handoff:completion | `Put it back without closing it` |
| [3805](/root/laintas_cli/laintas_cli.py:3805) | command/help | <module> / /handoff:completion | `Record something the next person needs to know` |
| [3806](/root/laintas_cli/laintas_cli.py:3806) | command/help | <module> / /handoff:completion | `Mark it handed over and done` |
| [3807](/root/laintas_cli/laintas_cli.py:3807) | command/help | <module> / /handoff:completion | `Undo a close` |
| [3808](/root/laintas_cli/laintas_cli.py:3808) | command/help | <module> / /handoff:completion | `Merge with the shared copy through Laintas storage` |
| [3809](/root/laintas_cli/laintas_cli.py:3809) | command/help | <module> / /handoff:completion | `Render it as one string to send over any channel` |
| [3810](/root/laintas_cli/laintas_cli.py:3810) | command/help | <module> / /handoff:completion | `Take a handoff somebody pasted or mailed you` |
| [3811](/root/laintas_cli/laintas_cli.py:3811) | command/help | <module> / /handoff:completion | `List handoffs other machines shared for this repository` |
| [3812](/root/laintas_cli/laintas_cli.py:3812) | command/help | <module> / /handoff:completion | `Take a handoff somebody else created` |
| [3815](/root/laintas_cli/laintas_cli.py:3815) | command/help | <module> / /handoff:help_text | `An envelope in .laintas/handoff/. During a signed-in task, new captures its context and plan. --to selects a saved account; --to-user-id names a remote recipient. Export/import crosses accounts; sync uses only the current account's cloud storage. accept creates a separate receiving session and continues it. Sync code and environment separately; credentials and live processes are never transferred.` |
| [3823](/root/laintas_cli/laintas_cli.py:3823) | command/help | <module> / /shared:command | `/shared` |
| [3823](/root/laintas_cli/laintas_cli.py:3823) | command/help | <module> / /shared:description | `Share files with Helpwo through Laintas storage` |
| [3824](/root/laintas_cli/laintas_cli.py:3824) | command/help | <module> / /shared:category | `Agents & Terminals` |
| [3825](/root/laintas_cli/laintas_cli.py:3825) | command/help | <module> / /shared:usage | `/shared [list [path]\|push <local> [remote]\|pull <remote> [local]\|rm <path>\|mkdir <path>\|mv <from> <to>\|cp <from> <to>\|usage]` |
| [3828](/root/laintas_cli/laintas_cli.py:3828) | command/help | <module> / /shared:completion | `List what is in shared storage` |
| [3829](/root/laintas_cli/laintas_cli.py:3829) | command/help | <module> / /shared:completion | `Upload a local file or folder` |
| [3830](/root/laintas_cli/laintas_cli.py:3830) | command/help | <module> / /shared:completion | `Download a stored file` |
| [3831](/root/laintas_cli/laintas_cli.py:3831) | command/help | <module> / /shared:completion | `Delete a stored file or folder` |
| [3832](/root/laintas_cli/laintas_cli.py:3832) | command/help | <module> / /shared:completion | `Create a folder` |
| [3833](/root/laintas_cli/laintas_cli.py:3833) | command/help | <module> / /shared:completion | `Move or rename a stored entry` |
| [3834](/root/laintas_cli/laintas_cli.py:3834) | command/help | <module> / /shared:completion | `Copy a stored entry` |
| [3835](/root/laintas_cli/laintas_cli.py:3835) | command/help | <module> / /shared:completion | `Show storage used, free allowance and cost` |
| [3838](/root/laintas_cli/laintas_cli.py:3838) | command/help | <module> / /shared:help_text | `Reads and writes the same per-account storage Helpwo mounts as its "Laintas Storage" cloud folder: push a file here and it is in the Helpwo file tree, pull one that Helpwo wrote. Unlike /helpwo --remote, it does not need a live link and it outlives this process — the files sit on the server. Storage above the plan's free allowance is billed monthly; /shared usage shows where you stand.` |
| [3845](/root/laintas_cli/laintas_cli.py:3845) | command/help | <module> / /terminate:category | `Agents & Terminals` |
| [3845](/root/laintas_cli/laintas_cli.py:3845) | command/help | <module> / /terminate:command | `/terminate` |
| [3845](/root/laintas_cli/laintas_cli.py:3845) | command/help | <module> / /terminate:description | `Close a terminal and all child resources` |
| [3845](/root/laintas_cli/laintas_cli.py:3845) | command/help | <module> / /terminate:usage | `/terminate <name>` |
| [3846](/root/laintas_cli/laintas_cli.py:3846) | command/help | <module> / /send:category | `Agents & Terminals` |
| [3846](/root/laintas_cli/laintas_cli.py:3846) | command/help | <module> / /send:command | `/send` |
| [3846](/root/laintas_cli/laintas_cli.py:3846) | command/help | <module> / /send:description | `Send input to a terminal` |
| [3846](/root/laintas_cli/laintas_cli.py:3846) | command/help | <module> / /send:usage | `/send <name> [--wait <seconds>] <command>` |
| [3847](/root/laintas_cli/laintas_cli.py:3847) | command/help | <module> / /spawn:category | `Agents & Terminals` |
| [3847](/root/laintas_cli/laintas_cli.py:3847) | command/help | <module> / /spawn:command | `/spawn` |
| [3847](/root/laintas_cli/laintas_cli.py:3847) | command/help | <module> / /spawn:description | `Spawn a sub-agent` |
| [3847](/root/laintas_cli/laintas_cli.py:3847) | command/help | <module> / /spawn:usage | `/spawn [name:] <task>` |
| [3848](/root/laintas_cli/laintas_cli.py:3848) | command/help | <module> / /tell:category | `Agents & Terminals` |
| [3848](/root/laintas_cli/laintas_cli.py:3848) | command/help | <module> / /tell:command | `/tell` |
| [3848](/root/laintas_cli/laintas_cli.py:3848) | command/help | <module> / /tell:description | `Send a message to an agent` |
| [3848](/root/laintas_cli/laintas_cli.py:3848) | command/help | <module> / /tell:usage | `/tell <agent-id> <message\|json>` |
| [3849](/root/laintas_cli/laintas_cli.py:3849) | command/help | <module> / /abort:category | `Agents & Terminals` |
| [3849](/root/laintas_cli/laintas_cli.py:3849) | command/help | <module> / /abort:command | `/abort` |
| [3849](/root/laintas_cli/laintas_cli.py:3849) | command/help | <module> / /abort:description | `Abort an agent` |
| [3849](/root/laintas_cli/laintas_cli.py:3849) | command/help | <module> / /abort:usage | `/abort <agent-id>` |
| [3850](/root/laintas_cli/laintas_cli.py:3850) | command/help | <module> / /hwo:category | `Planning & Tasks` |
| [3850](/root/laintas_cli/laintas_cli.py:3850) | command/help | <module> / /hwo:command | `/hwo` |
| [3850](/root/laintas_cli/laintas_cli.py:3850) | command/help | <module> / /hwo:description | `Open or run an orchestration workflow` |
| [3850](/root/laintas_cli/laintas_cli.py:3850) | command/help | <module> / /hwo:usage | `/hwo [file\|run <file>\|compile <file>\|status\|view <file>]` |
| [3851](/root/laintas_cli/laintas_cli.py:3851) | command/help | <module> / /hwg:category | `Planning & Tasks` |
| [3851](/root/laintas_cli/laintas_cli.py:3851) | command/help | <module> / /hwg:command | `/hwg` |
| [3851](/root/laintas_cli/laintas_cli.py:3851) | command/help | <module> / /hwg:description | `Compile, run, visualize, or resume a graph workflow` |
| [3851](/root/laintas_cli/laintas_cli.py:3851) | command/help | <module> / /hwg:usage | `/hwg {<file.hwg>\|run\|compile\|resume\|status\|gantt\|cancel} ...` |
| [3852](/root/laintas_cli/laintas_cli.py:3852) | command/help | <module> / /retask:category | `Planning & Tasks` |
| [3852](/root/laintas_cli/laintas_cli.py:3852) | command/help | <module> / /retask:command | `/retask` |
| [3852](/root/laintas_cli/laintas_cli.py:3852) | command/help | <module> / /retask:description | `Open the checklist of work the AI handed to you (Alt+R)` |
| [3852](/root/laintas_cli/laintas_cli.py:3852) | command/help | <module> / /retask:usage | `/retask [<file.retask>\|list\|done <id> [note]]` |
| [3853](/root/laintas_cli/laintas_cli.py:3853) | command/help | <module> / /retask:completion | `List the .retask checklists in this workspace` |
| [3854](/root/laintas_cli/laintas_cli.py:3854) | command/help | <module> / /retask:completion | `Say a task is finished so the AI checks it` |
| [3856](/root/laintas_cli/laintas_cli.py:3856) | command/help | <module> / /mode:category | `Planning & Tasks` |
| [3856](/root/laintas_cli/laintas_cli.py:3856) | command/help | <module> / /mode:command | `/mode` |
| [3856](/root/laintas_cli/laintas_cli.py:3856) | command/help | <module> / /mode:description | `Show, switch, or create agent modes` |
| [3856](/root/laintas_cli/laintas_cli.py:3856) | command/help | <module> / /mode:usage | `/mode [list\|create\|delete\|<mode-name>]` |
| [3857](/root/laintas_cli/laintas_cli.py:3857) | command/help | <module> / /mode:completion | `List available modes` |
| [3858](/root/laintas_cli/laintas_cli.py:3858) | command/help | <module> / /mode:completion | `Create a custom mode` |
| [3859](/root/laintas_cli/laintas_cli.py:3859) | command/help | <module> / /mode:completion | `Delete a custom mode` |
| [3861](/root/laintas_cli/laintas_cli.py:3861) | command/help | <module> / /plan:category | `Planning & Tasks` |
| [3861](/root/laintas_cli/laintas_cli.py:3861) | command/help | <module> / /plan:command | `/plan` |
| [3861](/root/laintas_cli/laintas_cli.py:3861) | command/help | <module> / /plan:description | `Create, revise, review, or approve versioned plans` |
| [3861](/root/laintas_cli/laintas_cli.py:3861) | command/help | <module> / /plan:usage | `/plan [enter <task>\|submit\|revise <feedback>\|approve\|exit\|status\|list]` |
| [3862](/root/laintas_cli/laintas_cli.py:3862) | command/help | <module> / /plan:completion | `Enter plan mode for a task and draft a plan` |
| [3863](/root/laintas_cli/laintas_cli.py:3863) | command/help | <module> / /plan:completion | `Submit the current plan for review` |
| [3864](/root/laintas_cli/laintas_cli.py:3864) | command/help | <module> / /plan:completion | `Revise the current plan from feedback` |
| [3865](/root/laintas_cli/laintas_cli.py:3865) | command/help | <module> / /plan:completion | `Approve the current plan` |
| [3866](/root/laintas_cli/laintas_cli.py:3866) | command/help | <module> / /plan:completion | `Exit plan mode` |
| [3867](/root/laintas_cli/laintas_cli.py:3867) | command/help | <module> / /plan:completion | `Show current plan status` |
| [3868](/root/laintas_cli/laintas_cli.py:3868) | command/help | <module> / /plan:completion | `List saved plans` |
| [3870](/root/laintas_cli/laintas_cli.py:3870) | command/help | <module> / /prompt:category | `Planning & Tasks` |
| [3870](/root/laintas_cli/laintas_cli.py:3870) | command/help | <module> / /prompt:command | `/prompt` |
| [3870](/root/laintas_cli/laintas_cli.py:3870) | command/help | <module> / /prompt:description | `Open Prompt Lab or manage tested prompt overlays` |
| [3870](/root/laintas_cli/laintas_cli.py:3870) | command/help | <module> / /prompt:usage | `/prompt [issue\|subcommand]` |
| [3871](/root/laintas_cli/laintas_cli.py:3871) | command/help | <module> / /evolve:category | `Planning & Tasks` |
| [3871](/root/laintas_cli/laintas_cli.py:3871) | command/help | <module> / /evolve:command | `/evolve` |
| [3871](/root/laintas_cli/laintas_cli.py:3871) | command/help | <module> / /evolve:description | `Create, improve, test, and hot-load project extensions` |
| [3871](/root/laintas_cli/laintas_cli.py:3871) | command/help | <module> / /evolve:usage | `/evolve [idea\|subcommand]` |
| [3872](/root/laintas_cli/laintas_cli.py:3872) | command/help | <module> / /task:category | `Planning & Tasks` |
| [3872](/root/laintas_cli/laintas_cli.py:3872) | command/help | <module> / /task:command | `/task` |
| [3872](/root/laintas_cli/laintas_cli.py:3872) | command/help | <module> / /task:description | `Track project tasks` |
| [3872](/root/laintas_cli/laintas_cli.py:3872) | command/help | <module> / /task:usage | `/task [list\|mine\|agent\|add\|show\|start\|done\|del\|progress\|note\|subtask]` |
| [3873](/root/laintas_cli/laintas_cli.py:3873) | command/help | <module> / /task:completion | `List tasks` |
| [3874](/root/laintas_cli/laintas_cli.py:3874) | command/help | <module> / /task:completion | `List tasks owned by the current agent` |
| [3875](/root/laintas_cli/laintas_cli.py:3875) | command/help | <module> / /task:completion | `List tasks for a specific agent` |
| [3876](/root/laintas_cli/laintas_cli.py:3876) | command/help | <module> / /task:completion | `Create a new task` |
| [3877](/root/laintas_cli/laintas_cli.py:3877) | command/help | <module> / /task:completion | `Show task details` |
| [3878](/root/laintas_cli/laintas_cli.py:3878) | command/help | <module> / /task:completion | `Mark a task in progress` |
| [3879](/root/laintas_cli/laintas_cli.py:3879) | command/help | <module> / /task:completion | `Mark a task completed` |
| [3880](/root/laintas_cli/laintas_cli.py:3880) | command/help | <module> / /task:completion | `Delete a task` |
| [3881](/root/laintas_cli/laintas_cli.py:3881) | command/help | <module> / /task:completion | `Update completion progress` |
| [3882](/root/laintas_cli/laintas_cli.py:3882) | command/help | <module> / /task:completion | `Append a progress note` |
| [3883](/root/laintas_cli/laintas_cli.py:3883) | command/help | <module> / /task:completion | `Create a child task` |
| [3885](/root/laintas_cli/laintas_cli.py:3885) | command/help | <module> / /work:category | `Planning & Tasks` |
| [3885](/root/laintas_cli/laintas_cli.py:3885) | command/help | <module> / /work:command | `/work` |
| [3885](/root/laintas_cli/laintas_cli.py:3885) | command/help | <module> / /work:description | `Inspect or resume unified WorkGraph state` |
| [3885](/root/laintas_cli/laintas_cli.py:3885) | command/help | <module> / /work:usage | `/work [status\|list\|resume\|history]` |
| [3886](/root/laintas_cli/laintas_cli.py:3886) | command/help | <module> / /workflow:category | `Planning & Tasks` |
| [3886](/root/laintas_cli/laintas_cli.py:3886) | command/help | <module> / /workflow:command | `/workflow` |
| [3886](/root/laintas_cli/laintas_cli.py:3886) | command/help | <module> / /workflow:description | `Run a multi-phase workflow` |
| [3886](/root/laintas_cli/laintas_cli.py:3886) | command/help | <module> / /workflow:usage | `/workflow {start\|status\|advance\|approve\|end\|list}` |
| [3887](/root/laintas_cli/laintas_cli.py:3887) | command/help | <module> / /model:category | `Config & Tools` |
| [3887](/root/laintas_cli/laintas_cli.py:3887) | command/help | <module> / /model:command | `/model` |
| [3887](/root/laintas_cli/laintas_cli.py:3887) | command/help | <module> / /model:description | `Select the chat model: backend models or your own provider key` |
| [3887](/root/laintas_cli/laintas_cli.py:3887) | command/help | <module> / /model:usage | `/model [terminal\|aux\|@agent] [id\|reset] \| route [<route> [model] [window]\|list] \| add [provider] [key\|$VAR] [model] [name] \| remove <route>` |
| [3890](/root/laintas_cli/laintas_cli.py:3890) | command/help | <module> / /model:completion | `Pick the model for compaction, critic and memory extraction` |
| [3891](/root/laintas_cli/laintas_cli.py:3891) | command/help | <module> / /model:completion | `Clear this terminal's model override (use gateway auto-routing)` |
| [3892](/root/laintas_cli/laintas_cli.py:3892) | command/help | <module> / /model:completion | `Clear this terminal's model override (use gateway auto-routing)` |
| [3893](/root/laintas_cli/laintas_cli.py:3893) | command/help | <module> / /model:completion | `Clear this terminal's model override (use gateway auto-routing)` |
| [3894](/root/laintas_cli/laintas_cli.py:3894) | command/help | <module> / /model:completion | `Choose who serves the chat: the backend or your own provider key` |
| [3895](/root/laintas_cli/laintas_cli.py:3895) | command/help | <module> / /model:completion | `Add your own provider key as a direct model route` |
| [3896](/root/laintas_cli/laintas_cli.py:3896) | command/help | <module> / /model:completion | `Delete a provider route and its stored key` |
| [3898](/root/laintas_cli/laintas_cli.py:3898) | command/help | <module> / /config:category | `Config & Tools` |
| [3898](/root/laintas_cli/laintas_cli.py:3898) | command/help | <module> / /config:command | `/config` |
| [3898](/root/laintas_cli/laintas_cli.py:3898) | command/help | <module> / /config:description | `View or set runtime configuration` |
| [3898](/root/laintas_cli/laintas_cli.py:3898) | command/help | <module> / /config:usage | `/config [<key>\|<prefix> [<value>]\|import <file>\|export <file>\|reset]` |
| [3899](/root/laintas_cli/laintas_cli.py:3899) | command/help | <module> / /web:command | `/web` |
| [3899](/root/laintas_cli/laintas_cli.py:3899) | command/help | <module> / /web:description | `Inspect web search and fetch: engines, proxy, cookies, diagnostics` |
| [3900](/root/laintas_cli/laintas_cli.py:3900) | command/help | <module> / /web:category | `Config & Tools` |
| [3901](/root/laintas_cli/laintas_cli.py:3901) | command/help | <module> / /web:usage | `/web [status\|engines [init]\|test [engine]\|try <query>\|cookies [clear [domain]]]` |
| [3905](/root/laintas_cli/laintas_cli.py:3905) | command/help | <module> / /web:completion | `Show engines, proxy and search configuration` |
| [3906](/root/laintas_cli/laintas_cli.py:3906) | command/help | <module> / /web:completion | `List configured search engines (init writes a starter file)` |
| [3907](/root/laintas_cli/laintas_cli.py:3907) | command/help | <module> / /web:completion | `Probe one engine or the whole chain` |
| [3908](/root/laintas_cli/laintas_cli.py:3908) | command/help | <module> / /web:completion | `Run a real search query to see live results` |
| [3909](/root/laintas_cli/laintas_cli.py:3909) | command/help | <module> / /web:completion | `Inspect saved browser cookies (clear removes them)` |
| [3911](/root/laintas_cli/laintas_cli.py:3911) | command/help | <module> / /identity:command | `/identity` |
| [3911](/root/laintas_cli/laintas_cli.py:3911) | command/help | <module> / /identity:description | `Manage saved logins the agent may browse as` |
| [3912](/root/laintas_cli/laintas_cli.py:3912) | command/help | <module> / /identity:category | `Config & Tools` |
| [3913](/root/laintas_cli/laintas_cli.py:3913) | command/help | <module> / /identity:usage | `/identity [list\|check <name>\|capture <name> [domains]\|delete <name>]` |
| [3915](/root/laintas_cli/laintas_cli.py:3915) | command/help | <module> / /policy:category | `Config & Tools` |
| [3915](/root/laintas_cli/laintas_cli.py:3915) | command/help | <module> / /policy:command | `/policy` |
| [3915](/root/laintas_cli/laintas_cli.py:3915) | command/help | <module> / /policy:description | `Show or set security policy` |
| [3915](/root/laintas_cli/laintas_cli.py:3915) | command/help | <module> / /policy:usage | `/policy [audit\|enforce\|disabled [--yes]\|reset]` |
| [3916](/root/laintas_cli/laintas_cli.py:3916) | command/help | <module> / /trust:category | `Config & Tools` |
| [3916](/root/laintas_cli/laintas_cli.py:3916) | command/help | <module> / /trust:command | `/trust` |
| [3916](/root/laintas_cli/laintas_cli.py:3916) | command/help | <module> / /trust:description | `Review or change workspace trust` |
| [3916](/root/laintas_cli/laintas_cli.py:3916) | command/help | <module> / /trust:usage | `/trust [status\|allow\|revoke]` |
| [3917](/root/laintas_cli/laintas_cli.py:3917) | command/help | <module> / /hooks:category | `Config & Tools` |
| [3917](/root/laintas_cli/laintas_cli.py:3917) | command/help | <module> / /hooks:command | `/hooks` |
| [3917](/root/laintas_cli/laintas_cli.py:3917) | command/help | <module> / /hooks:description | `Manage executable hooks` |
| [3917](/root/laintas_cli/laintas_cli.py:3917) | command/help | <module> / /hooks:usage | `/hooks [status\|trust\|revoke\|reload]` |
| [3918](/root/laintas_cli/laintas_cli.py:3918) | command/help | <module> / /backend:category | `Config & Tools` |
| [3918](/root/laintas_cli/laintas_cli.py:3918) | command/help | <module> / /backend:command | `/backend` |
| [3918](/root/laintas_cli/laintas_cli.py:3918) | command/help | <module> / /backend:description | `Manage backend trust profiles` |
| [3918](/root/laintas_cli/laintas_cli.py:3918) | command/help | <module> / /backend:usage | `/backend [status\|list\|use <name>\|config]` |
| [3919](/root/laintas_cli/laintas_cli.py:3919) | command/help | <module> / /max:category | `Config & Tools` |
| [3919](/root/laintas_cli/laintas_cli.py:3919) | command/help | <module> / /max:command | `/max` |
| [3919](/root/laintas_cli/laintas_cli.py:3919) | command/help | <module> / /max:description | `Lift runtime limits for this process` |
| [3920](/root/laintas_cli/laintas_cli.py:3920) | command/help | <module> / /tools:category | `Config & Tools` |
| [3920](/root/laintas_cli/laintas_cli.py:3920) | command/help | <module> / /tools:command | `/tools` |
| [3920](/root/laintas_cli/laintas_cli.py:3920) | command/help | <module> / /tools:description | `List registered tools` |
| [3921](/root/laintas_cli/laintas_cli.py:3921) | command/help | <module> / /tool:category | `Config & Tools` |
| [3921](/root/laintas_cli/laintas_cli.py:3921) | command/help | <module> / /tool:command | `/tool` |
| [3921](/root/laintas_cli/laintas_cli.py:3921) | command/help | <module> / /tool:description | `Invoke a tool directly` |
| [3921](/root/laintas_cli/laintas_cli.py:3921) | command/help | <module> / /tool:usage | `/tool <name> [json-params]` |
| [3922](/root/laintas_cli/laintas_cli.py:3922) | command/help | <module> / /skill:category | `Config & Tools` |
| [3922](/root/laintas_cli/laintas_cli.py:3922) | command/help | <module> / /skill:command | `/skill` |
| [3922](/root/laintas_cli/laintas_cli.py:3922) | command/help | <module> / /skill:description | `Manage skills` |
| [3922](/root/laintas_cli/laintas_cli.py:3922) | command/help | <module> / /skill:usage | `/skill [manager\|list\|trust\|revoke\|load\|unload\|reload\|new\|dir]` |
| [3923](/root/laintas_cli/laintas_cli.py:3923) | command/help | <module> / /mcp:category | `Config & Tools` |
| [3923](/root/laintas_cli/laintas_cli.py:3923) | command/help | <module> / /mcp:command | `/mcp` |
| [3923](/root/laintas_cli/laintas_cli.py:3923) | command/help | <module> / /mcp:description | `Manage MCP servers` |
| [3923](/root/laintas_cli/laintas_cli.py:3923) | command/help | <module> / /mcp:usage | `/mcp {list\|trust\|revoke\|connect\|disconnect\|reload\|tools\|init\|config}` |
| [3924](/root/laintas_cli/laintas_cli.py:3924) | command/help | <module> / /bash:category | `Config & Tools` |
| [3924](/root/laintas_cli/laintas_cli.py:3924) | command/help | <module> / /bash:command | `/bash` |
| [3924](/root/laintas_cli/laintas_cli.py:3924) | command/help | <module> / /bash:description | `Run a command through term0` |
| [3924](/root/laintas_cli/laintas_cli.py:3924) | command/help | <module> / /bash:usage | `/bash <command>\|list\|add <command>\|remove <command>` |
| [3925](/root/laintas_cli/laintas_cli.py:3925) | command/help | <module> / /memory:category | `Config & Tools` |
| [3925](/root/laintas_cli/laintas_cli.py:3925) | command/help | <module> / /memory:command | `/memory` |
| [3925](/root/laintas_cli/laintas_cli.py:3925) | command/help | <module> / /memory:description | `Manage memory (interactive view/delete); split into global/local` |
| [3925](/root/laintas_cli/laintas_cli.py:3925) | command/help | <module> / /memory:usage | `/memory [global\|local\|persistent\|project\|show <id\|name>\|status\|compact [dry]]` |
| [3927](/root/laintas_cli/laintas_cli.py:3927) | command/help | <module> / /prop:category | `Config & Tools` |
| [3927](/root/laintas_cli/laintas_cli.py:3927) | command/help | <module> / /prop:command | `/prop` |
| [3927](/root/laintas_cli/laintas_cli.py:3927) | command/help | <module> / /prop:description | `Inspect complete model context and system prompts` |
| [3928](/root/laintas_cli/laintas_cli.py:3928) | command/help | <module> / /prop:usage | `/prop [sys\|budget] [N] \| /prop budget [N] output [path]` |
| [3931](/root/laintas_cli/laintas_cli.py:3931) | command/help | <module> / /prop:completion | `Only the effective system prompt and its sources` |
| [3932](/root/laintas_cli/laintas_cli.py:3932) | command/help | <module> / /prop:completion | `What the context budget compressed, sent vs original` |
| [3935](/root/laintas_cli/laintas_cli.py:3935) | command/help | <module> / /prop:help_text | `With no arguments, opens the most recent user conversation's complete provider context. N selects the Nth newest conversation (1 is newest); ˋsysˋ limits the view to the effective system prompt and its source sections. ˋ/ˋ performs exact local search. The separate AI input can translate, locate, explain, or propose a Prompt Lab draft; proposals never change the active prompt without later review and activation.` |
| [3941](/root/laintas_cli/laintas_cli.py:3941) | command/help | <module> / /debug:category | `Config & Tools` |
| [3941](/root/laintas_cli/laintas_cli.py:3941) | command/help | <module> / /debug:command | `/debug` |
| [3941](/root/laintas_cli/laintas_cli.py:3941) | command/help | <module> / /debug:description | `Browse or export debug entries` |
| [3941](/root/laintas_cli/laintas_cli.py:3941) | command/help | <module> / /debug:usage | `/debug [clear\|N\|N <file> [--raw]]` |
| [3942](/root/laintas_cli/laintas_cli.py:3942) | command/help | <module> / /why:category | `Config & Tools` |
| [3942](/root/laintas_cli/laintas_cli.py:3942) | command/help | <module> / /why:command | `/why` |
| [3942](/root/laintas_cli/laintas_cli.py:3942) | command/help | <module> / /why:description | `Explain a recent tool failure` |
| [3942](/root/laintas_cli/laintas_cli.py:3942) | command/help | <module> / /why:usage | `/why [N\|tool\|terminal\|agent]` |
| [3943](/root/laintas_cli/laintas_cli.py:3943) | command/help | <module> / /stream:category | `Config & Tools` |
| [3943](/root/laintas_cli/laintas_cli.py:3943) | command/help | <module> / /stream:command | `/stream` |
| [3943](/root/laintas_cli/laintas_cli.py:3943) | command/help | <module> / /stream:description | `Set bounded streaming preview` |
| [3943](/root/laintas_cli/laintas_cli.py:3943) | command/help | <module> / /stream:usage | `/stream [off\|one\|detail]` |
| [3944](/root/laintas_cli/laintas_cli.py:3944) | command/help | <module> / /theme:category | `Config & Tools` |
| [3944](/root/laintas_cli/laintas_cli.py:3944) | command/help | <module> / /theme:command | `/theme` |
| [3944](/root/laintas_cli/laintas_cli.py:3944) | command/help | <module> / /theme:description | `Set terminal color theme` |
| [3944](/root/laintas_cli/laintas_cli.py:3944) | command/help | <module> / /theme:usage | `/theme [dark\|light\|mono]` |
| [3945](/root/laintas_cli/laintas_cli.py:3945) | command/help | <module> / /detail:category | `Config & Tools` |
| [3945](/root/laintas_cli/laintas_cli.py:3945) | command/help | <module> / /detail:command | `/detail` |
| [3945](/root/laintas_cli/laintas_cli.py:3945) | command/help | <module> / /detail:description | `Record and browse conversation execution details` |
| [3945](/root/laintas_cli/laintas_cli.py:3945) | command/help | <module> / /detail:usage | `/detail [on\|off\|trace [N]]` |
| [3946](/root/laintas_cli/laintas_cli.py:3946) | command/help | <module> / /undo:category | `History` |
| [3946](/root/laintas_cli/laintas_cli.py:3946) | command/help | <module> / /undo:command | `/undo` |
| [3946](/root/laintas_cli/laintas_cli.py:3946) | command/help | <module> / /undo:description | `Restore a git checkpoint` |
| [3946](/root/laintas_cli/laintas_cli.py:3946) | command/help | <module> / /undo:usage | `/undo [sha]` |
| [3947](/root/laintas_cli/laintas_cli.py:3947) | command/help | <module> / /snapshot:category | `History` |
| [3947](/root/laintas_cli/laintas_cli.py:3947) | command/help | <module> / /snapshot:command | `/snapshot` |
| [3947](/root/laintas_cli/laintas_cli.py:3947) | command/help | <module> / /snapshot:description | `Create a git checkpoint` |
| [3947](/root/laintas_cli/laintas_cli.py:3947) | command/help | <module> / /snapshot:usage | `/snapshot [label]` |
| [3948](/root/laintas_cli/laintas_cli.py:3948) | command/help | <module> / /snapshots:category | `History` |
| [3948](/root/laintas_cli/laintas_cli.py:3948) | command/help | <module> / /snapshots:command | `/snapshots` |
| [3948](/root/laintas_cli/laintas_cli.py:3948) | command/help | <module> / /snapshots:description | `List git checkpoints` |
| [3949](/root/laintas_cli/laintas_cli.py:3949) | command/help | <module> / /compact:category | `History` |
| [3949](/root/laintas_cli/laintas_cli.py:3949) | command/help | <module> / /compact:command | `/compact` |
| [3949](/root/laintas_cli/laintas_cli.py:3949) | command/help | <module> / /compact:description | `Compact the current session context` |
| [3950](/root/laintas_cli/laintas_cli.py:3950) | command/help | <module> / /compact:usage | `/compact [status\|--force]` |
| [3952](/root/laintas_cli/laintas_cli.py:3952) | command/help | <module> / /compact:completion | `Show context size without compacting` |
| [3953](/root/laintas_cli/laintas_cli.py:3953) | command/help | <module> / /compact:completion | `Compact even below the usual threshold` |
| [3955](/root/laintas_cli/laintas_cli.py:3955) | command/help | <module> / /continue:category | `History` |
| [3955](/root/laintas_cli/laintas_cli.py:3955) | command/help | <module> / /continue:command | `/continue` |
| [3955](/root/laintas_cli/laintas_cli.py:3955) | command/help | <module> / /continue:description | `Continue the current live session` |
| [3956](/root/laintas_cli/laintas_cli.py:3956) | command/help | <module> / /told:category | `History` |
| [3956](/root/laintas_cli/laintas_cli.py:3956) | command/help | <module> / /told:command | `/told` |
| [3956](/root/laintas_cli/laintas_cli.py:3956) | command/help | <module> / /told:description | `Replay prompts or a selected Agent's conversation` |
| [3957](/root/laintas_cli/laintas_cli.py:3957) | command/help | <module> / /told:usage | `/told [agent-id [reply [N]\|all]\|N\|all\|reply [N]\|log [N]]` |
| [3960](/root/laintas_cli/laintas_cli.py:3960) | command/help | <module> / /reload:category | `History` |
| [3960](/root/laintas_cli/laintas_cli.py:3960) | command/help | <module> / /reload:command | `/reload` |
| [3960](/root/laintas_cli/laintas_cli.py:3960) | command/help | <module> / /reload:description | `Reload default files and restart` |
| [7155](/root/laintas_cli/laintas_cli.py:7155) | output | _pt_prompt_once / 0 | `{exc}` |
| [7167](/root/laintas_cli/laintas_cli.py:7167) | error candidate | _pt_prompt_once | `terminal input closed` |
| [7421](/root/laintas_cli/laintas_cli.py:7421) | error candidate | save_session | `Cannot persist an unverified account` |
| [7637](/root/laintas_cli/laintas_cli.py:7637) | error candidate | run_cancellable_blocking._pump | `Operation cancelled by Esc or Ctrl+C` |
| [7676](/root/laintas_cli/laintas_cli.py:7676) | error candidate | run_cancellable_blocking | `Operation cancelled by Ctrl+C` |
| [7808](/root/laintas_cli/laintas_cli.py:7808) | error candidate | fetch_available_models | `Model fetch cancelled` |
| [7821](/root/laintas_cli/laintas_cli.py:7821) | error candidate | fetch_available_models | `Model fetch cancelled` |
| [7902](/root/laintas_cli/laintas_cli.py:7902) | title/menu/help | show_model_selector / title | `Models — choose with {symbols.ARROW_U}{symbols.ARROW_D} and Enter` |
| [8095](/root/laintas_cli/laintas_cli.py:8095) | output | login_interactive / 0 | `Direct terminal password login is disabled. Use browser OAuth with PKCE instead.` |
| [8112](/root/laintas_cli/laintas_cli.py:8112) | output | _login_via_device / 0 | `Could not start browser login (HTTP {started.status_code}).` |
| [8119](/root/laintas_cli/laintas_cli.py:8119) | output | _login_via_device / 0 | `Browser login returned an incomplete device authorization.` |
| [8126](/root/laintas_cli/laintas_cli.py:8126) | output | _login_via_device / 0 | `Cannot start browser login: {exc}` |
| [8133](/root/laintas_cli/laintas_cli.py:8133) | output | _login_via_device / 0 | `Open this URL in any browser to sign in: ⏎ {login_url}[/link] ⏎ Confirm the page shows code {user_code} before approving. ⏎ Waiting for approval (expires in {expires // 60} minutes)…` |
| [8133](/root/laintas_cli/laintas_cli.py:8133) | output | _login_via_device / 0 | `Open this URL in any browser to sign in: ⏎ {login_url}[/link] ⏎ Waiting for approval (expires in {expires // 60} minutes)…` |
| [8164](/root/laintas_cli/laintas_cli.py:8164) | output | _login_via_device / 0 | `Browser login returned an incomplete session.` |
| [8176](/root/laintas_cli/laintas_cli.py:8176) | output | _login_via_device / 0 | `The new CLI session could not be verified.` |
| [8180](/root/laintas_cli/laintas_cli.py:8180) | output | _login_via_device / 0 | `Logged in as {display}` |
| [8183](/root/laintas_cli/laintas_cli.py:8183) | output | _login_via_device / 0 | `Browser login expired or was cancelled.` |
| [8194](/root/laintas_cli/laintas_cli.py:8194) | output | _login_via_device / 0 | `Browser login timed out.` |
| [8274](/root/laintas_cli/laintas_cli.py:8274) | resolved output candidate | _login_via_browser_local | `Opening browser for login ⏎  ⏎ If your browser doesn't open automatically, visit: ⏎ {login_url}[/link] ⏎  ⏎ Waiting for authentication... (timeout in 2 minutes)` |
| [8279](/root/laintas_cli/laintas_cli.py:8279) | title/menu/help | _login_via_browser_local / title | `Laintas Auth` |
| [8323](/root/laintas_cli/laintas_cli.py:8323) | output | _login_via_browser_local / 0 | `OAuth exchange returned no access token.` |
| [8334](/root/laintas_cli/laintas_cli.py:8334) | output | _login_via_browser_local / 0 | `CLI session exchange failed (HTTP {session_resp.status_code}).` |
| [8343](/root/laintas_cli/laintas_cli.py:8343) | output | _login_via_browser_local / 0 | `CLI session exchange returned an incomplete session.` |
| [8354](/root/laintas_cli/laintas_cli.py:8354) | output | _login_via_browser_local / 0 | `The new CLI session could not be verified.` |
| [8359](/root/laintas_cli/laintas_cli.py:8359) | output | _login_via_browser_local / 0 | `Logged in as {display}` |
| [8362](/root/laintas_cli/laintas_cli.py:8362) | output | _login_via_browser_local / 0 | `OAuth code exchange failed (HTTP {resp.status_code}).` |
| [8365](/root/laintas_cli/laintas_cli.py:8365) | output | _login_via_browser_local / 0 | `Cannot reach {ACCOUNTS_BASE} for token exchange: {e}` |
| [8369](/root/laintas_cli/laintas_cli.py:8369) | output | _login_via_browser_local / 0 | `{result['error']}` |
| [8371](/root/laintas_cli/laintas_cli.py:8371) | output | _login_via_browser_local / 0 | `Login timed out.` |
| [8385](/root/laintas_cli/laintas_cli.py:8385) | status/prompt | ensure_auth | `Checking sign-in…` |
| [8390](/root/laintas_cli/laintas_cli.py:8390) | output | ensure_auth / 0 | `Saved credentials identify another account. Use /login.` |
| [8399](/root/laintas_cli/laintas_cli.py:8399) | output | ensure_auth / 0 | `Could not reach {LAINTAS_BASE} to check your sign-in; continuing with the saved one.` |
| [8402](/root/laintas_cli/laintas_cli.py:8402) | output | ensure_auth / 0 | `Session expired. Use /login to re-authenticate.` |
| [8411](/root/laintas_cli/laintas_cli.py:8411) | output | ensure_auth / 0 | `Login cancelled.` |
| [8419](/root/laintas_cli/laintas_cli.py:8419) | output | ensure_auth / 0 | `Login cancelled.` |
| [8423](/root/laintas_cli/laintas_cli.py:8423) | output | ensure_auth / 0 | `Remote login failed. Run /login to retry.` |
| [8429](/root/laintas_cli/laintas_cli.py:8429) | output | ensure_auth / 0 | `Authentication unavailable. Continuing without login; run /login to retry.` |
| [9070](/root/laintas_cli/laintas_cli.py:9070) | output | _acquire_resume_lease / 0 | `This task belongs to another account. Accept a handoff instead.` |
| [9074](/root/laintas_cli/laintas_cli.py:9074) | output | _acquire_resume_lease / 0 | `This saved session was deleted. Refresh the picker.` |
| [9088](/root/laintas_cli/laintas_cli.py:9088) | output | _acquire_resume_lease_locked / 0 | `Could not lock the saved session: {exc}` |
| [9092](/root/laintas_cli/laintas_cli.py:9092) | output | _acquire_resume_lease_locked / 0 | `Could not lock the saved session: {result['error']}` |
| [9096](/root/laintas_cli/laintas_cli.py:9096) | output | _acquire_resume_lease_locked / 0 | `This session is already open in another running instance (pid {owner.get('pid', '?')}). Resuming it here would make two instances write the same session and overwrite each other's progress. Not resuming.` |
| [9109](/root/laintas_cli/laintas_cli.py:9109) | output | _archive_previous_session / 0 | `Leaving session open in another instance (pid {owner.get('pid', '?')}).` |
| [9127](/root/laintas_cli/laintas_cli.py:9127) | output | _switch_resume_session / 0 | `Saved session is unavailable. Refresh the picker.` |
| [9130](/root/laintas_cli/laintas_cli.py:9130) | output | _switch_resume_session / 0 | `This session is already current.` |
| [9143](/root/laintas_cli/laintas_cli.py:9143) | output | _switch_resume_session / 0 | `Saved snapshot no longer exists.` |
| [9153](/root/laintas_cli/laintas_cli.py:9153) | output | _switch_resume_session / 0 | `Saved session has no conversation to resume.` |
| [9179](/root/laintas_cli/laintas_cli.py:9179) | output | _switch_resume_session / 0 | `Could not restore session: {exc}` |
| [9190](/root/laintas_cli/laintas_cli.py:9190) | error candidate | _fork_resume_blob | `Saved task belongs to another account` |
| [9201](/root/laintas_cli/laintas_cli.py:9201) | output | _fork_resume_blob / 0 | `Could not create a branch from the saved session.` |
| [9213](/root/laintas_cli/laintas_cli.py:9213) | error candidate | _restore_resume_blob | `Saved task belongs to another account` |
| [9299](/root/laintas_cli/laintas_cli.py:9299) | output | _resolve_resume_selector / 0 | `Invalid resume number: {selector}` |
| [9301](/root/laintas_cli/laintas_cli.py:9301) | output | _resolve_resume_selector / 0 | `Invalid resume selector: {selector}` |
| [9478](/root/laintas_cli/laintas_cli.py:9478) | output | _build_fork_tree_rows._emit / 1 | `{prefix}   ` |
| [9478](/root/laintas_cli/laintas_cli.py:9478) | output | _build_fork_tree_rows._emit / 1 | `{prefix}{symbols.TREE_VERT}  ` |
| [9502](/root/laintas_cli/laintas_cli.py:9502) | output | show_resume_picker / 0 | `No saved session to resume in this directory.` |
| [9569](/root/laintas_cli/laintas_cli.py:9569) | title/menu/help | show_resume_picker / title | `Resume Saved Session` |
| [9599](/root/laintas_cli/laintas_cli.py:9599) | output | _show_resume_detail / 0 | `{excerpt}` |
| [9600](/root/laintas_cli/laintas_cli.py:9600) | output | _show_resume_detail / 0 | `Type: {item.get('kind', 'session')}   When: {_format_time_ago(item.get('timestamp', 0))}   Turns: {item.get('turn_count') or _resume_turn_count(item)}` |
| [9608](/root/laintas_cli/laintas_cli.py:9608) | output | _show_resume_detail / 0 | `Lineage: {lineage_str}` |
| [9610](/root/laintas_cli/laintas_cli.py:9610) | output | _show_resume_detail / 0 | `Parent: {_lineage[-2]}` |
| [9612](/root/laintas_cli/laintas_cli.py:9612) | output | _show_resume_detail / 0 | `Events: {len(history)} ⏎ ` |
| [9664](/root/laintas_cli/laintas_cli.py:9664) | output | _print_resume_transcript / 0 | `── conversation ── {len(window)}/{total} event(s) {'(all)' if at_start else '(most recent)'} ──` |
| [9669](/root/laintas_cli/laintas_cli.py:9669) | output | _print_resume_transcript / 0 | `[earlier session context] ⏎ {older}` |
| [9705](/root/laintas_cli/laintas_cli.py:9705) | output | ensure_files_exist / 0 | `Created {cli_prop_path}` |
| [9709](/root/laintas_cli/laintas_cli.py:9709) | output | ensure_files_exist / 0 | `Created {memory_path}` |
| [9713](/root/laintas_cli/laintas_cli.py:9713) | output | ensure_files_exist / 0 | `Created {commands_path}` |
| [9717](/root/laintas_cli/laintas_cli.py:9717) | output | ensure_files_exist / 0 | `Created {loop_path}` |
| [9734](/root/laintas_cli/laintas_cli.py:9734) | label/render | reload_default_files | `Blocked by policy: {decision.reason}` |
| [9734](/root/laintas_cli/laintas_cli.py:9734) | output | reload_default_files / 0 | `Blocked by policy: {decision.reason}` |
| [9741](/root/laintas_cli/laintas_cli.py:9741) | output | reload_default_files / 0 | `Reload cancelled; no files were deleted.` |
| [9747](/root/laintas_cli/laintas_cli.py:9747) | output | reload_default_files / 0 | `Deleted {f}` |
| [9754](/root/laintas_cli/laintas_cli.py:9754) | output | reload_default_files / 0 | `Restarting laintas_cli...` |
| [9972](/root/laintas_cli/laintas_cli.py:9972) | error candidate | _post_with_interrupt | `interrupted while waiting for the backend` |
| [10390](/root/laintas_cli/laintas_cli.py:10390) | error candidate | _call_backend_stream_impl | `interrupted before request` |
| [10417](/root/laintas_cli/laintas_cli.py:10417) | error candidate | _call_backend_stream_impl | `interrupted during retry delay` |
| [10429](/root/laintas_cli/laintas_cli.py:10429) | error candidate | _call_backend_stream_impl | `interrupted during retry delay` |
| [10466](/root/laintas_cli/laintas_cli.py:10466) | error candidate | _call_backend_stream_impl | `interrupted during retry delay` |
| [11134](/root/laintas_cli/laintas_cli.py:11134) | output | TerminalSession._run / 0 | `Terminal relay connect failed after retries: {last_err}` |
| [11393](/root/laintas_cli/laintas_cli.py:11393) | output | AgentRegistry.start_message_poll / 0 | `Listening for remote messages from Helpwo...` |
| [11482](/root/laintas_cli/laintas_cli.py:11482) | output | AgentRegistry._handle_remote_message / 0 | `Error handling remote message: {e}` |
| [11502](/root/laintas_cli/laintas_cli.py:11502) | resolved output candidate | AgentRegistry._handle_chat | `Remote message from {'the application' if self.app_mode is not None else 'Helpwo'}: ⏎ {escape(content)}` |
| [11506](/root/laintas_cli/laintas_cli.py:11506) | title/menu/help | AgentRegistry._handle_chat / title | `Incoming` |
| [11532](/root/laintas_cli/laintas_cli.py:11532) | output | AgentRegistry._handle_chat / 0 | `user_message` |
| [11573](/root/laintas_cli/laintas_cli.py:11573) | output | AgentRegistry._handle_chat / 0 | `user_message` |
| [11579](/root/laintas_cli/laintas_cli.py:11579) | error candidate | AgentRegistry._handle_chat._chat_events | `chat aborted` |
| [11656](/root/laintas_cli/laintas_cli.py:11656) | output | AgentRegistry._handle_exec / 0 | `BLOCKED remote exec: {cmd[:100]} — {decision.reason}` |
| [11657](/root/laintas_cli/laintas_cli.py:11657) | label/render | AgentRegistry._handle_exec | `BLOCKED remote exec: {cmd[:100]} — {decision.reason}` |
| [11671](/root/laintas_cli/laintas_cli.py:11671) | resolved output candidate | AgentRegistry._handle_exec | `remote_exec {cmd} ⏎ cwd={cwd} timeout={timeout}s` |
| [11673](/root/laintas_cli/laintas_cli.py:11673) | title/menu/help | AgentRegistry._handle_exec / title | `Incoming exec ({req_id})` |
| [11681](/root/laintas_cli/laintas_cli.py:11681) | error candidate | AgentRegistry._handle_exec | `not a directory: {cwd}` |
| [12171](/root/laintas_cli/laintas_cli.py:12171) | output | AgentRegistry._handle_term_open / 0 | `Remote terminal request ignored (disable_remote_terminal is set).` |
| [12228](/root/laintas_cli/laintas_cli.py:12228) | resolved output candidate | AgentRegistry._handle_term_open | `Browser opened a terminal ⏎ session {session_id} {symbols.BULLET} {cols}×{rows} ⏎ relay: {backend_url}` |
| [12230](/root/laintas_cli/laintas_cli.py:12230) | title/menu/help | AgentRegistry._handle_term_open / title | `Remote Terminal` |
| [12243](/root/laintas_cli/laintas_cli.py:12243) | output | AgentRegistry._handle_term_open / 0 | `Failed to open remote terminal: {e}` |
| [12301](/root/laintas_cli/laintas_cli.py:12301) | resolved output candidate | AgentRegistry._handle_term_new | `Helpwo created sub-terminal {name} ⏎ It will hand itself over to Helpwo once it finishes starting.` |
| [12304](/root/laintas_cli/laintas_cli.py:12304) | title/menu/help | AgentRegistry._handle_term_new / title | `Remote Terminal` |
| [12321](/root/laintas_cli/laintas_cli.py:12321) | output | AgentRegistry._handle_term_close / 0 | `Helpwo closed this sub-terminal.` |
| [12367](/root/laintas_cli/laintas_cli.py:12367) | resolved output candidate | AgentRegistry._handle_term_close | `Helpwo closed sub-terminal {name}` |
| [12369](/root/laintas_cli/laintas_cli.py:12369) | title/menu/help | AgentRegistry._handle_term_close / title | `Remote Terminal` |
| [12401](/root/laintas_cli/laintas_cli.py:12401) | resolved output candidate | AgentRegistry._handle_disconnect | `Helpwo disconnected this terminal. ⏎ The CLI keeps running locally. Run /helpwo to connect again.` |
| [12404](/root/laintas_cli/laintas_cli.py:12404) | title/menu/help | AgentRegistry._handle_disconnect / title | `Disconnected` |
| [12458](/root/laintas_cli/laintas_cli.py:12458) | resolved output candidate | AgentRegistry._handle_delegate | `delegate {goal[:200]} ⏎ maxLoops={max_loops_val}, context={len(context)} chars` |
| [12458](/root/laintas_cli/laintas_cli.py:12458) | resolved output candidate | AgentRegistry._handle_delegate | `delegate {goal[:200]} ⏎ maxLoops={max_loops_val}` |
| [12463](/root/laintas_cli/laintas_cli.py:12463) | title/menu/help | AgentRegistry._handle_delegate / title | `Incoming delegate ({req_id})` |
| [12480](/root/laintas_cli/laintas_cli.py:12480) | error candidate | AgentRegistry._handle_delegate._delegate_events | `delegate aborted` |
| [12543](/root/laintas_cli/laintas_cli.py:12543) | output | AgentRegistry._handle_abort / 0 | `Abort request for {target}: {reason or '(no reason)'}` |
| [12591](/root/laintas_cli/laintas_cli.py:12591) | output | AgentRegistry._handle_approval_response / 0 | `Approval response for {target}: {decision} — {feedback}` |
| [12591](/root/laintas_cli/laintas_cli.py:12591) | output | AgentRegistry._handle_approval_response / 0 | `Approval response for {target}: {decision}` |
| [12646](/root/laintas_cli/laintas_cli.py:12646) | output | AgentRegistry._request_approval / 0 | `AUTO mode: approval window expired after {int(auto_confirm_seconds)} seconds; approving.` |
| [12719](/root/laintas_cli/laintas_cli.py:12719) | title/menu/help | show_debug_browser_interactive._load_items / subtitle | `{entry.timestamp[-19:]} · {entry.current_path}` |
| [12757](/root/laintas_cli/laintas_cli.py:12757) | title/menu/help | show_debug_browser_interactive / title | `Debug Activity` |
| [12767](/root/laintas_cli/laintas_cli.py:12767) | output | show_debug_detail / 0 | `Entry #{index + 1} not found.` |
| [12778](/root/laintas_cli/laintas_cli.py:12778) | resolved output candidate | show_debug_detail | `#{e.loop} — {ts}   Path: {e.current_path}` |
| [12778](/root/laintas_cli/laintas_cli.py:12778) | title/menu/help | show_debug_detail / title | `Debug Entry #{index + 1}` |
| [12782](/root/laintas_cli/laintas_cli.py:12782) | title/menu/help | show_debug_detail / title | `User Input` |
| [12799](/root/laintas_cli/laintas_cli.py:12799) | title/menu/help | show_debug_detail / title | `Request Payload` |
| [12815](/root/laintas_cli/laintas_cli.py:12815) | title/menu/help | show_debug_detail / title | `AI Response` |
| [12823](/root/laintas_cli/laintas_cli.py:12823) | title/menu/help | show_debug_detail / title | `Raw Response JSON` |
| [12836](/root/laintas_cli/laintas_cli.py:12836) | title/menu/help | show_debug_detail / title | `Command Execution` |
| [12873](/root/laintas_cli/laintas_cli.py:12873) | resolved output candidate | _show_terminal_detail | `Name: {name} ⏎ Parent: {parent or '(root)'} ⏎ Child terminals: {', '.join(children) or '(none)'} ⏎ Deployed agents: {', '.join(agents) or '(none)'} ⏎ Command: {cmd[:200]} ⏎ Status: {'Alive' if alive else 'Dead'} ⏎ Uptime: {uptime_str} ⏎ Return code: {sess.returncode if sess else 'N/A'} ⏎ Output ({len(output)} bytes): ⏎ {output_preview}` |
| [12873](/root/laintas_cli/laintas_cli.py:12873) | title/menu/help | _show_terminal_detail / title | `Terminal: {name}` |
| [12929](/root/laintas_cli/laintas_cli.py:12929) | title/menu/help | _show_terminal_manager_once.load_items / title | `{_terminal_display_name(name)} (current)` |
| [12929](/root/laintas_cli/laintas_cli.py:12929) | title/menu/help | _show_terminal_manager_once.load_items / title | `{_terminal_display_name(name)}` |
| [12985](/root/laintas_cli/laintas_cli.py:12985) | title/menu/help | _show_terminal_manager_once.close / message | `The primary terminal cannot be closed here.` |
| [12991](/root/laintas_cli/laintas_cli.py:12991) | title/menu/help | _show_terminal_manager_once.close / message | `Press x again to close {name}` |
| [12995](/root/laintas_cli/laintas_cli.py:12995) | title/menu/help | _show_terminal_manager_once.close / message | `Closed {name}` |
| [12998](/root/laintas_cli/laintas_cli.py:12998) | title/menu/help | _show_terminal_manager_once / title | `Terminals` |
| [13018](/root/laintas_cli/laintas_cli.py:13018) | output | _show_terminal_manager_once / 0 | `Session has already ended.` |
| [13048](/root/laintas_cli/laintas_cli.py:13048) | title/menu/help | _show_skill_detail / title | `Skill: {name}` |
| [13202](/root/laintas_cli/laintas_cli.py:13202) | title/menu/help | show_skill_manager.reload_all / message | `Re-scanned {len(results)} skills` |
| [13205](/root/laintas_cli/laintas_cli.py:13205) | title/menu/help | show_skill_manager / title | `Skills` |
| [13233](/root/laintas_cli/laintas_cli.py:13233) | output | observe_session / 0 | `No active sub-terminal session.` |
| [13237](/root/laintas_cli/laintas_cli.py:13237) | output | observe_session / 0 | `Session has already ended.` |
| [13349](/root/laintas_cli/laintas_cli.py:13349) | output | _enter_session_raw / 0 | `No active sub-terminal session.` |
| [13353](/root/laintas_cli/laintas_cli.py:13353) | output | _enter_session_raw / 0 | `Session has already ended.` |
| [13358](/root/laintas_cli/laintas_cli.py:13358) | output | _enter_session_raw / 0 | `Session does not support raw PTY I/O (tmux mode).` |
| [13506](/root/laintas_cli/laintas_cli.py:13506) | output | _enter_session_raw / 0 | ` ⏎ Cannot take over: {exc}` |
| [13513](/root/laintas_cli/laintas_cli.py:13513) | output | _enter_session_raw / 0 | ` ⏎ {symbols.DOT} Sub-terminal exited. Returned to {escape(_terminal_display_name('term0'))}` |
| [13515](/root/laintas_cli/laintas_cli.py:13515) | output | _enter_session_raw / 0 | ` ⏎ {symbols.DOT} Detached. Returned to {escape(_terminal_display_name('term0'))}` |
| [13544](/root/laintas_cli/laintas_cli.py:13544) | output | _load_extra_commands / 0 | `Restricted Mode: not executing {path} ({reason}). Use /trust allow after reviewing it.` |
| [13559](/root/laintas_cli/laintas_cli.py:13559) | output | _load_extra_commands / 0 | `.laintas/commands.py failed to load: {type(exc).__name__}: {exc}` |
| [13582](/root/laintas_cli/laintas_cli.py:13582) | output | handle_version_command / 0 | `Usage: /v  \|  /v check  \|  /v update [--force] [--overwrite-local]  \|  /v enterprise [on\|off\|gateway]` |
| [13586](/root/laintas_cli/laintas_cli.py:13586) | output | handle_version_command / 0 | `laintas-cli v{updater.LOCAL_VERSION} ({'binary' if updater.is_frozen() else 'source'} install)` |
| [13593](/root/laintas_cli/laintas_cli.py:13593) | output | handle_version_command / 0 | `Could not reach the update server: {e}` |
| [13601](/root/laintas_cli/laintas_cli.py:13601) | output | handle_version_command / 0 | `Update available: v{remote_ver}` |
| [13603](/root/laintas_cli/laintas_cli.py:13603) | output | handle_version_command / 0 | `  {manifest['notes']}` |
| [13605](/root/laintas_cli/laintas_cli.py:13605) | output | handle_version_command / 0 | `Latest is v{remote_ver} — you are up to date.` |
| [13609](/root/laintas_cli/laintas_cli.py:13609) | output | handle_version_command / 0 | `Run /v update to install it.` |
| [13613](/root/laintas_cli/laintas_cli.py:13613) | output | handle_version_command / 0 | `Nothing to update. Use /v update --force to re-apply the latest.` |
| [13618](/root/laintas_cli/laintas_cli.py:13618) | output | handle_version_command / 0 | `Binary install — replacing the whole executable (partial update only applies to source installs).` |
| [13622](/root/laintas_cli/laintas_cli.py:13622) | output | handle_version_command / 0 | `Updated binary at {new_path}. Restarting...` |
| [13634](/root/laintas_cli/laintas_cli.py:13634) | output | handle_version_command / 0 | `All files already match the latest — nothing to download.` |
| [13644](/root/laintas_cli/laintas_cli.py:13644) | output | handle_version_command / 0 | `{updater.install_dir()} is a git checkout — not updating it from the release.` |
| [13646](/root/laintas_cli/laintas_cli.py:13646) | output | handle_version_command / 0 | `These {len(changed)} file(s) differ from v{remote_ver}; applying the release would overwrite them:` |
| [13649](/root/laintas_cli/laintas_cli.py:13649) | output | handle_version_command / 0 | `  {escape(name)}` |
| [13651](/root/laintas_cli/laintas_cli.py:13651) | output | handle_version_command / 0 | `  … and {len(changed) - 20} more` |
| [13652](/root/laintas_cli/laintas_cli.py:13652) | output | handle_version_command / 0 | `Update this checkout with git pull. To overwrite it with the release anyway: /v update --overwrite-local — that one keeps a copy of the replaced files under .laintas-update-backup/.` |
| [13658](/root/laintas_cli/laintas_cli.py:13658) | output | handle_version_command / 0 | `Downloading {len(changed)} changed file(s):` |
| [13662](/root/laintas_cli/laintas_cli.py:13662) | output | handle_version_command / 0 | `Update failed — no changes applied.` |
| [13665](/root/laintas_cli/laintas_cli.py:13665) | output | handle_version_command / 0 | `Updated to v{remote_ver} ({len(changed)} file(s) replaced). Restarting...` |
| [13709](/root/laintas_cli/laintas_cli.py:13709) | output | _handle_enterprise / 0 | `Usage: /v enterprise [on\|off\|gateway] [--force] ⏎   on      — Install or update the organisation layer (default) ⏎   off     — Remove it and go back to a personal CLI ⏎   gateway — Download the self-hosted gateway bundle ⏎   --force — Allow replacing the installed version with an older one` |
| [13721](/root/laintas_cli/laintas_cli.py:13721) | output | _handle_enterprise / 0 | `Organisation layer removed — /org and organisation rules are gone.` |
| [13721](/root/laintas_cli/laintas_cli.py:13721) | output | _handle_enterprise / 0 | `No organisation layer was installed.` |
| [13735](/root/laintas_cli/laintas_cli.py:13735) | output | _handle_enterprise / 0 | ` ⏎ Gateway bundle downloaded. ⏎   Run the following inside the extracted directory: ⏎     ./install.sh` |
| [13741](/root/laintas_cli/laintas_cli.py:13741) | output | _handle_enterprise / 0 | `Enterprise installation failed: {escape(str(exc))}` |
| [14019](/root/laintas_cli/laintas_cli.py:14019) | error candidate | _validate_slash_args | `Usage: {declared[1]}` |
| [14032](/root/laintas_cli/laintas_cli.py:14032) | error candidate | _validate_slash_args | `Usage: {rule.usage}` |
| [14038](/root/laintas_cli/laintas_cli.py:14038) | error candidate | _validate_slash_args | `Unexpected argument: {invalid[0]}. Usage: {rule.usage}` |
| [14052](/root/laintas_cli/laintas_cli.py:14052) | error candidate | _parse_slash_command | `Empty slash command. Run /help for available commands.` |
| [14055](/root/laintas_cli/laintas_cli.py:14055) | error candidate | _parse_slash_command | `Could not parse command. Run /help for usage.` |
| [14066](/root/laintas_cli/laintas_cli.py:14066) | error candidate | _parse_slash_command | `Invalid quoting: {exc}. Close the quote or escape it, then retry.` |
| [14141](/root/laintas_cli/laintas_cli.py:14141) | error candidate | _parse_hire_profile | `{token} requires a value. Usage: /hire [name] [--profile role] [--prompt file] [--tools name,...] [--model [id]] [--terminal name]` |
| [14155](/root/laintas_cli/laintas_cli.py:14155) | error candidate | _parse_hire_profile | `Could not read employee prompt {path}: {exc}` |
| [14158](/root/laintas_cli/laintas_cli.py:14158) | error candidate | _parse_hire_profile | `Employee prompt exceeds the 100,000 character limit.` |
| [14173](/root/laintas_cli/laintas_cli.py:14173) | error candidate | _parse_hire_profile | `Unknown /hire option: {token}` |
| [14176](/root/laintas_cli/laintas_cli.py:14176) | error candidate | _parse_hire_profile | `Usage: /hire [name] [--profile role] [--prompt file] [--tools name,...] [--model [id]] [--terminal name]` |
| [14185](/root/laintas_cli/laintas_cli.py:14185) | error candidate | _parse_hire_profile | `Unknown employee profile '{role_name}'. Available: {available}` |
| [14199](/root/laintas_cli/laintas_cli.py:14199) | error candidate | _parse_hire_profile | `Unknown employee tool(s): {', '.join(unknown)}` |
| [14202](/root/laintas_cli/laintas_cli.py:14202) | title/menu/help | _parse_hire_profile / title | `General Agent` |
| [14203](/root/laintas_cli/laintas_cli.py:14203) | title/menu/help | _parse_hire_profile / description | `General-purpose autonomous employee` |
| [14326](/root/laintas_cli/laintas_cli.py:14326) | resolved output candidate | _print_long_panel | `(empty)` |
| [14328](/root/laintas_cli/laintas_cli.py:14328) | resolved output candidate | _print_long_panel | `(empty)` |
| [14703](/root/laintas_cli/laintas_cli.py:14703) | error candidate | _prompt_lab_start_test._backend | `backend returned a non-object response` |
| [14720](/root/laintas_cli/laintas_cli.py:14720) | error candidate | _prompt_lab_start_test._parse_judgment | `test judge did not return valid JSON` |
| [14764](/root/laintas_cli/laintas_cli.py:14764) | output | _prompt_lab_start_test._runner / 0 | ` ⏎ [{'green' if passed else 'red'}]Prompt Lab test {patch_id}: {report}[/{'green' if passed else 'red'}]` |
| [14771](/root/laintas_cli/laintas_cli.py:14771) | output | _prompt_lab_start_test._runner / 0 | ` ⏎ Prompt Lab test {patch_id} failed: {exc}` |
| [14790](/root/laintas_cli/laintas_cli.py:14790) | output | _review_and_approve_current_plan / 0 | `The plan is not ready for review. Ask the AI to complete it first.` |
| [14824](/root/laintas_cli/laintas_cli.py:14824) | output | _review_and_approve_current_plan / 0 | `Plan not approved. It remains available for AI revision; use /plan revise <feedback>.` |
| [14830](/root/laintas_cli/laintas_cli.py:14830) | output | _review_and_approve_current_plan / 0 | `Plan changed during review; review the latest revision again.` |
| [14837](/root/laintas_cli/laintas_cli.py:14837) | output | _review_and_approve_current_plan / 0 | `Could not start approved execution: {exc}` |
| [14903](/root/laintas_cli/laintas_cli.py:14903) | label/render | _usage_allowance_bar | `  {figures}` |
| [14905](/root/laintas_cli/laintas_cli.py:14905) | label/render | _usage_allowance_bar | `  {note}` |
| [14993](/root/laintas_cli/laintas_cli.py:14993) | output | _usage_buy_pack / 0 | `Laintas Market` |
| [14994](/root/laintas_cli/laintas_cli.py:14994) | output | _usage_buy_pack / 0 | `https://market.laintas.com/?kind={kind}` |
| [14995](/root/laintas_cli/laintas_cli.py:14995) | output | _usage_buy_pack / 0 | `Buy with your Laintas balance. Valid for 30 days; no membership required. Review the current price and optional renewal in the checkout.` |
| [15036](/root/laintas_cli/laintas_cli.py:15036) | label/render | _usage_cache_cell | `—` |
| [15042](/root/laintas_cli/laintas_cli.py:15042) | label/render | _usage_cache_cell | `{pct:.0f}%` |
| [15047](/root/laintas_cli/laintas_cli.py:15047) | label/render | _usage_section | `▍` |
| [15048](/root/laintas_cli/laintas_cli.py:15048) | label/render | _usage_section | ` {title}` |
| [15050](/root/laintas_cli/laintas_cli.py:15050) | label/render | _usage_section | `   {note}` |
| [15097](/root/laintas_cli/laintas_cli.py:15097) | output | _training_control_request / 0 | `Training-data controls are available only with the official Laintas backend.` |
| [15102](/root/laintas_cli/laintas_cli.py:15102) | output | _training_control_request / 0 | `Log in with /login before changing training-data sharing.` |
| [15117](/root/laintas_cli/laintas_cli.py:15117) | error candidate | _training_control_request | `unsupported training control method: {method}` |
| [15120](/root/laintas_cli/laintas_cli.py:15120) | output | _training_control_request / 0 | `Could not reach training-data controls: {escape(str(exc))}` |
| [15133](/root/laintas_cli/laintas_cli.py:15133) | output | _training_control_request / 0 | `Training-data control failed: {escape(str(reason))}` |
| [15157](/root/laintas_cli/laintas_cli.py:15157) | error candidate | _cmd_training_local | `Usage: /training local [status\|on\|off\|purge\|export <file>]` |
| [15166](/root/laintas_cli/laintas_cli.py:15166) | output | _cmd_training_local / 0 | `Local training capture is on. ⏎ Model inputs and outputs from this CLI are written to {escape(training_local.db_path())} and are never uploaded by laintas_cli. This is independent of /training on\|off, which controls what the Gateway retains.` |
| [15173](/root/laintas_cli/laintas_cli.py:15173) | output | _cmd_training_local / 0 | `Local training capture is off. ⏎ Samples already on disk are kept — /training local purge deletes them.` |
| [15181](/root/laintas_cli/laintas_cli.py:15181) | output | _cmd_training_local / 0 | `No local training samples to delete.` |
| [15185](/root/laintas_cli/laintas_cli.py:15185) | status/prompt | _cmd_training_local | `Delete all {rows} local training samples? [y/N] ` |
| [15189](/root/laintas_cli/laintas_cli.py:15189) | output | _cmd_training_local / 0 | `Kept.` |
| [15191](/root/laintas_cli/laintas_cli.py:15191) | output | _cmd_training_local / 0 | `Deleted {training_local.purge()} samples.` |
| [15198](/root/laintas_cli/laintas_cli.py:15198) | error candidate | _cmd_training_local | `Usage: /training local export <file.jsonl>` |
| [15203](/root/laintas_cli/laintas_cli.py:15203) | output | _cmd_training_local / 0 | `Wrote {written} samples to {escape(target)}.` |
| [15206](/root/laintas_cli/laintas_cli.py:15206) | output | _cmd_training_local / 0 | `Nothing exported — the local store is empty or the path is not writable.` |
| [15214](/root/laintas_cli/laintas_cli.py:15214) | output | _cmd_training_local / 0 | `[{'green' if on else 'yellow'}]Local training capture: {'on' if on else 'off'}[/{'green' if on else 'yellow'}]` |
| [15216](/root/laintas_cli/laintas_cli.py:15216) | output | _cmd_training_local / 0 | `Store: {escape(info['path'])}` |
| [15218](/root/laintas_cli/laintas_cli.py:15218) | output | _cmd_training_local / 0 | `No samples captured yet.` |
| [15222](/root/laintas_cli/laintas_cli.py:15222) | output | _cmd_training_local / 0 | `{rows} sample{'' if rows == 1 else 's'} {symbols.BULLET} {trajs} {'trajectory' if trajs == 1 else 'trajectories'} {symbols.BULLET} {_fmt_bytes(info['bytes'])}` |
| [15228](/root/laintas_cli/laintas_cli.py:15228) | output | _cmd_training_local / 0 | `Span: {span}` |
| [15230](/root/laintas_cli/laintas_cli.py:15230) | output | _cmd_training_local / 0 | `  {kind:<20}{count}` |
| [15233](/root/laintas_cli/laintas_cli.py:15233) | output | _cmd_training_local / 0 | `{info['dropped']} samples dropped (write queue full).` |
| [15236](/root/laintas_cli/laintas_cli.py:15236) | output | _cmd_training_local / 0 | `Last capture error: {escape(info['error'])}` |
| [15247](/root/laintas_cli/laintas_cli.py:15247) | error candidate | _cmd_training | `Usage: /training [status\|on\|off\|local ...]` |
| [15249](/root/laintas_cli/laintas_cli.py:15249) | status/prompt | _cmd_training | `Checking training-data sharing…` |
| [15259](/root/laintas_cli/laintas_cli.py:15259) | output | _cmd_training / 0 | `Training-data sharing: {status}` |
| [15261](/root/laintas_cli/laintas_cli.py:15261) | output | _cmd_training / 0 | `Collection service: {'available' if available else 'unavailable'}` |
| [15264](/root/laintas_cli/laintas_cli.py:15264) | output | _cmd_training / 0 | `Scope: {escape(scope)}` |
| [15266](/root/laintas_cli/laintas_cli.py:15266) | output | _cmd_training / 0 | `Local capture (separate switch): {'on' if local_training_enabled() else 'off'} {symbols.BULLET} /training local` |
| [15273](/root/laintas_cli/laintas_cli.py:15273) | output | _cmd_training / 0 | `Training-data collection is not configured on the Gateway; sharing was not enabled.` |
| [15283](/root/laintas_cli/laintas_cli.py:15283) | status/prompt | _cmd_training | `Saving training-data preference…` |
| [15289](/root/laintas_cli/laintas_cli.py:15289) | output | _cmd_training / 0 | `Training-data sharing is on. ⏎ Laintas may retain model inputs and outputs actually observed by the Gateway for review and training. Use /training off to stop future collection.` |
| [15295](/root/laintas_cli/laintas_cli.py:15295) | output | _cmd_training / 0 | `Training-data sharing is off. No new interactions will be retained. Previously collected training data remains in the versioned dataset.` |
| [15313](/root/laintas_cli/laintas_cli.py:15313) | error candidate | _show_usage_command | `Usage: /usage buy <calls\|storage>` |
| [15326](/root/laintas_cli/laintas_cli.py:15326) | error candidate | _show_usage_command | `Usage: /usage [7d\|30d\|90d\|local\|buy <calls\|storage>]` |
| [15349](/root/laintas_cli/laintas_cli.py:15349) | error candidate | _show_usage_command._fetch_backend_usage | `Usage fetch cancelled` |
| [15356](/root/laintas_cli/laintas_cli.py:15356) | error candidate | _show_usage_command._fetch_backend_usage | `Usage fetch cancelled` |
| [15373](/root/laintas_cli/laintas_cli.py:15373) | status/prompt | _show_usage_command | `Fetching usage… {symbols.BULLET} Esc/Ctrl+C cancel` |
| [15377](/root/laintas_cli/laintas_cli.py:15377) | output | _show_usage_command / 0 | `Usage request cancelled.` |
| [15390](/root/laintas_cli/laintas_cli.py:15390) | label/render | _show_usage_command | `  no calls recorded yet — stats begin with your next AI request` |
| [15409](/root/laintas_cli/laintas_cli.py:15409) | label/render | _show_usage_command | `—` |
| [15421](/root/laintas_cli/laintas_cli.py:15421) | label/render | _show_usage_command | `model` |
| [15423](/root/laintas_cli/laintas_cli.py:15423) | label/render | _show_usage_command | `calls` |
| [15424](/root/laintas_cli/laintas_cli.py:15424) | label/render | _show_usage_command | `tokens` |
| [15425](/root/laintas_cli/laintas_cli.py:15425) | label/render | _show_usage_command | `cost` |
| [15426](/root/laintas_cli/laintas_cli.py:15426) | label/render | _show_usage_command | `tier` |
| [15441](/root/laintas_cli/laintas_cli.py:15441) | label/render | _show_usage_command | `  ~ estimated — backend sent no token counts (chars/4)` |
| [15462](/root/laintas_cli/laintas_cli.py:15462) | label/render | _show_usage_command | `  {_trunc} of {range_totals['calls']} calls hit the output limit ({_rate:.0f}%){_detail}` |
| [15473](/root/laintas_cli/laintas_cli.py:15473) | label/render | _show_usage_command | `  {profile.billing_label} — not billed by Laintas; local stats above are authoritative` |
| [15479](/root/laintas_cli/laintas_cli.py:15479) | label/render | _show_usage_command | `  run /login to see backend usage, balance and plan` |
| [15484](/root/laintas_cli/laintas_cli.py:15484) | label/render | _show_usage_command | `  backend usage unavailable ({fail})` |
| [15503](/root/laintas_cli/laintas_cli.py:15503) | label/render | _show_usage_command | ` {plan} ` |
| [15511](/root/laintas_cli/laintas_cli.py:15511) | label/render | _show_usage_command | ` balance` |
| [15512](/root/laintas_cli/laintas_cli.py:15512) | label/render | _show_usage_command | `  {symbols.BULLET}  ` |
| [15514](/root/laintas_cli/laintas_cli.py:15514) | label/render | _show_usage_command | ` subscription` |
| [15578](/root/laintas_cli/laintas_cli.py:15578) | label/render | _show_usage_command | `  peak {max(values)}/day` |
| [15608](/root/laintas_cli/laintas_cli.py:15608) | label/render | _show_usage_command | `{symbols.BULLET} {note}` |
| [15615](/root/laintas_cli/laintas_cli.py:15615) | title/menu/help | _show_usage_command / title | `AI usage` |
| [15617](/root/laintas_cli/laintas_cli.py:15617) | title/menu/help | _show_usage_command / subtitle | `/usage {symbols.BULLET} {rng}` |
| [15636](/root/laintas_cli/laintas_cli.py:15636) | output | _cmd_palette / 0 | `Could not queue the selected command. Please run it directly.` |
| [15642](/root/laintas_cli/laintas_cli.py:15642) | output | _cmd_exit / 0 | `Usage: /exit` |
| [15649](/root/laintas_cli/laintas_cli.py:15649) | output | _cmd_exit / 0 | `Logged out. Goodbye!` |
| [15655](/root/laintas_cli/laintas_cli.py:15655) | output | _cmd_quit / 0 | `Usage: {action}` |
| [15661](/root/laintas_cli/laintas_cli.py:15661) | output | _cmd_quit / 0 | `Detaching...` |
| [15667](/root/laintas_cli/laintas_cli.py:15667) | output | _cmd_quit / 0 | `Goodbye!` |
| [15673](/root/laintas_cli/laintas_cli.py:15673) | output | _cmd_back / 0 | `Usage: /back` |
| [15676](/root/laintas_cli/laintas_cli.py:15676) | output | _cmd_back / 0 | `/back only detaches from a sub-terminal.` |
| [15681](/root/laintas_cli/laintas_cli.py:15681) | output | _cmd_back / 0 | `Detaching...` |
| [15791](/root/laintas_cli/laintas_cli.py:15791) | output | _cmd_img / 0 | `/img — read an image` |
| [15792](/root/laintas_cli/laintas_cli.py:15792) | output | _cmd_img / 0 | `  /img                      [list] images here and recent screenshots` |
| [15793](/root/laintas_cli/laintas_cli.py:15793) | output | _cmd_img / 0 | `  /img <path> [question]    ask a vision model about it` |
| [15794](/root/laintas_cli/laintas_cli.py:15794) | output | _cmd_img / 0 | `  /img text <path>          transcribe it word for word (also .pdf)` |
| [15797](/root/laintas_cli/laintas_cli.py:15797) | output | _cmd_img / 0 | `/img: unknown action '{verb}'. Try /img help` |
| [15802](/root/laintas_cli/laintas_cli.py:15802) | output | _cmd_img / 0 | `Usage: /img <path> [question]   ·   /img <path> --text` |
| [15803](/root/laintas_cli/laintas_cli.py:15803) | output | _cmd_img / 0 | `No images found in the working directory or among recent screenshots.` |
| [15805](/root/laintas_cli/laintas_cli.py:15805) | output | _cmd_img / 0 | `Recent images (run /img <path> [question])` |
| [15813](/root/laintas_cli/laintas_cli.py:15813) | output | _cmd_img / 0 | `  {escape(shown)} {size / 1024:.0f} KB` |
| [15819](/root/laintas_cli/laintas_cli.py:15819) | output | _cmd_img / 0 | `/img text <path>  —  a file path is required` |
| [15826](/root/laintas_cli/laintas_cli.py:15826) | output | _cmd_img / 0 | `ˋtextˋ transcribes the whole document; the question is ignored. Drop it to ask about the image instead.` |
| [15829](/root/laintas_cli/laintas_cli.py:15829) | status/prompt | _cmd_img | `Transcribing…` |
| [15837](/root/laintas_cli/laintas_cli.py:15837) | status/prompt | _cmd_img | `Looking…` |
| [15845](/root/laintas_cli/laintas_cli.py:15845) | output | _cmd_img / 0 | `{e}` |
| [15849](/root/laintas_cli/laintas_cli.py:15849) | output | _cmd_img / 0 | `Try: /img {path} --text` |
| [15852](/root/laintas_cli/laintas_cli.py:15852) | output | _cmd_img / 0 | `{type(e).__name__}: {e}` |
| [15855](/root/laintas_cli/laintas_cli.py:15855) | resolved output candidate | _cmd_img | `Text of {escape(path)}` |
| [15855](/root/laintas_cli/laintas_cli.py:15855) | resolved output candidate | _cmd_img | `{escape(path)} read by {out.get('model', '?')}{' · cached' if out.get('cached') else ''}` |
| [15866](/root/laintas_cli/laintas_cli.py:15866) | output | _open_retask_view / 0 | `No .retask checklist here. The AI makes one when a step needs you.` |
| [15873](/root/laintas_cli/laintas_cli.py:15873) | output | _open_retask_view / 0 | `retask view failed: {type(exc).__name__}: {exc}` |
| [15884](/root/laintas_cli/laintas_cli.py:15884) | output | _cmd_retask / 0 | `/retask — checklists of work the AI hands to you` |
| [15885](/root/laintas_cli/laintas_cli.py:15885) | output | _cmd_retask / 0 | `  /retask                  open the current checklist (also Alt+R)` |
| [15886](/root/laintas_cli/laintas_cli.py:15886) | output | _cmd_retask / 0 | `  /retask <file.retask>    open a specific one` |
| [15887](/root/laintas_cli/laintas_cli.py:15887) | output | _cmd_retask / 0 | `  /retask list             every checklist in this workspace` |
| [15888](/root/laintas_cli/laintas_cli.py:15888) | output | _cmd_retask / 0 | `  /retask done <id> [note] say a task is finished; the AI checks it` |
| [15889](/root/laintas_cli/laintas_cli.py:15889) | output | _cmd_retask / 0 | `In the view: ↑↓ select · Enter fold · y copy the task · s I finished this · m mouse on/off · q close. Mouse is off so you can select and copy text directly.` |
| [15897](/root/laintas_cli/laintas_cli.py:15897) | output | _cmd_retask / 0 | `No such checklist: {path}` |
| [15905](/root/laintas_cli/laintas_cli.py:15905) | output | _cmd_retask / 0 | `No .retask checklists here.` |
| [15911](/root/laintas_cli/laintas_cli.py:15911) | output | _cmd_retask / 0 | `  {os.path.relpath(path, cwd)}  {exc}` |
| [15915](/root/laintas_cli/laintas_cli.py:15915) | label/render | _cmd_retask | `  {os.path.relpath(path, cwd)}` |
| [15916](/root/laintas_cli/laintas_cli.py:15916) | label/render | _cmd_retask | `  {doc.title}` |
| [15917](/root/laintas_cli/laintas_cli.py:15917) | label/render | _cmd_retask | `  {done}/{total} done` |
| [15919](/root/laintas_cli/laintas_cli.py:15919) | label/render | _cmd_retask | `  {symbols.BULLET} now: {current.id} {current.title}` |
| [15920](/root/laintas_cli/laintas_cli.py:15920) | resolved output candidate | _cmd_retask | `  {os.path.relpath(path, cwd)}` |
| [15926](/root/laintas_cli/laintas_cli.py:15926) | output | _cmd_retask / 0 | `Usage: /retask done <id> [note]` |
| [15930](/root/laintas_cli/laintas_cli.py:15930) | output | _cmd_retask / 0 | `No open .retask checklist here.` |
| [15937](/root/laintas_cli/laintas_cli.py:15937) | output | _cmd_retask / 0 | `{task_id} {block}.` |
| [15943](/root/laintas_cli/laintas_cli.py:15943) | output | _cmd_retask / 0 | `{outcome['error']}` |
| [15947](/root/laintas_cli/laintas_cli.py:15947) | output | _cmd_retask / 0 | `{exc}` |
| [15956](/root/laintas_cli/laintas_cli.py:15956) | output | _cmd_retask / 0 | `The AI checks it on your next message.` |
| [15978](/root/laintas_cli/laintas_cli.py:15978) | output | _cmd_resume / 0 | `/resume is handled by the main REPL. Type it at the prompt to resume a saved session.` |
| [15982](/root/laintas_cli/laintas_cli.py:15982) | output | _cmd_new_session_notice / 0 | `/new is handled by the main REPL. Type it at the prompt to start a new session.` |
| [15994](/root/laintas_cli/laintas_cli.py:15994) | output | _account_idle / 0 | `Pause or finish running Agents before changing accounts or accepting a task. You can use another account in a separate CLI.` |
| [16002](/root/laintas_cli/laintas_cli.py:16002) | output | _request_account_switch / 0 | `Switch accounts from the main CLI or launch a separate CLI with --account.` |
| [16063](/root/laintas_cli/laintas_cli.py:16063) | error candidate | _save_account_tasks_before_switch | `Could not save task for Agent {agent.id}; account switch cancelled` |
| [16067](/root/laintas_cli/laintas_cli.py:16067) | error candidate | _save_account_tasks_before_switch | `Could not save the current task; account switch cancelled` |
| [16109](/root/laintas_cli/laintas_cli.py:16109) | output | _restart_into_account / 0 | `Could not restore account selection: {type(rollback_error).__name__}` |
| [16115](/root/laintas_cli/laintas_cli.py:16115) | output | _restart_into_account / 0 | `Account restart failed ({type(exc).__name__}). Tasks were saved before shutdown. This CLI will exit; relaunch the previous account with --resume.` |
| [16133](/root/laintas_cli/laintas_cli.py:16133) | output | _perform_requested_account_restart / 0 | `Account switch cancelled: {switch_error}` |
| [16160](/root/laintas_cli/laintas_cli.py:16160) | output | _cmd_login / 0 | `Starting browser login… {symbols.BULLET} Esc/Ctrl+C cancel` |
| [16166](/root/laintas_cli/laintas_cli.py:16166) | output | _cmd_login / 0 | `Login cancelled.` |
| [16169](/root/laintas_cli/laintas_cli.py:16169) | output | _cmd_login / 0 | `Login cancelled.` |
| [16173](/root/laintas_cli/laintas_cli.py:16173) | output | _cmd_login / 0 | `Added account {escape(alias or new_session['userId'])}. Use /account switch to select it.` |
| [16183](/root/laintas_cli/laintas_cli.py:16183) | output | _cmd_login / 0 | `Logged in as {new_session.get('userEmail') or new_session.get('userName') or new_session['userId']}` |
| [16194](/root/laintas_cli/laintas_cli.py:16194) | output | _cmd_account / 0 | `{marker} {escape(row.get('alias') or row.get('email') or row['userId'])}  {escape(row['userId'])}` |
| [16196](/root/laintas_cli/laintas_cli.py:16196) | output | _cmd_account / 0 | `No saved accounts. Use /account add <alias>.` |
| [16202](/root/laintas_cli/laintas_cli.py:16202) | output | _cmd_account / 0 | `This account is already selected.` |
| [16208](/root/laintas_cli/laintas_cli.py:16208) | error candidate | _cmd_account | `Sign in before naming this account` |
| [16210](/root/laintas_cli/laintas_cli.py:16210) | output | _cmd_account / 0 | `Account alias saved.` |
| [16214](/root/laintas_cli/laintas_cli.py:16214) | error candidate | _cmd_account | `Usage: /account [list\|add <alias>\|switch <account>\|alias <name>\|legacy\|adopt <session-id>]` |
| [16223](/root/laintas_cli/laintas_cli.py:16223) | output | _cmd_account_legacy / 0 | `{escape(_resume_effective_session_id(blob))}  {escape(str(blob.get('title') or blob.get('objective') or 'Legacy task'))}` |
| [16224](/root/laintas_cli/laintas_cli.py:16224) | output | _cmd_account_legacy / 0 | `Assign a selected task with /account adopt <session-id>.` |
| [16228](/root/laintas_cli/laintas_cli.py:16228) | error candidate | _cmd_account_legacy | `Usage: /account legacy \| /account adopt <session-id>` |
| [16249](/root/laintas_cli/laintas_cli.py:16249) | output | _cmd_model_aux / 0 | `Auxiliary model cleared — compaction, the critic and memory extraction will use the terminal's own model again.` |
| [16254](/root/laintas_cli/laintas_cli.py:16254) | output | _cmd_model_aux / 0 | `Auxiliary model set to: {args[0]}` |
| [16259](/root/laintas_cli/laintas_cli.py:16259) | status/prompt | _cmd_model_aux | `Fetching available models… {symbols.BULLET} Esc/Ctrl+C cancel` |
| [16263](/root/laintas_cli/laintas_cli.py:16263) | output | _cmd_model_aux / 0 | `Selection cancelled.` |
| [16266](/root/laintas_cli/laintas_cli.py:16266) | output | _cmd_model_aux / 0 | `Failed to fetch models: {exc}` |
| [16267](/root/laintas_cli/laintas_cli.py:16267) | output | _cmd_model_aux / 0 | `Current auxiliary model: {current or 'same as terminal'}` |
| [16268](/root/laintas_cli/laintas_cli.py:16268) | output | _cmd_model_aux / 0 | `Usage: /model aux <model-id>  or  /model aux reset` |
| [16272](/root/laintas_cli/laintas_cli.py:16272) | output | _cmd_model_aux / 0 | `Current auxiliary model: {current or 'same as terminal'}` |
| [16276](/root/laintas_cli/laintas_cli.py:16276) | output | _cmd_model_aux / 0 | `Unchanged.` |
| [16283](/root/laintas_cli/laintas_cli.py:16283) | output | _cmd_model_aux / 0 | `Auxiliary model set to {model_id} (supplier priority routing)` |
| [16286](/root/laintas_cli/laintas_cli.py:16286) | output | _cmd_model_aux / 0 | `Applies to compaction, the critic and memory extraction. The terminal's own model is unchanged.` |
| [16299](/root/laintas_cli/laintas_cli.py:16299) | output | _cmd_model_agent_base / 0 | `{label} is a temporary sub-agent; it runs on its parent's model.` |
| [16311](/root/laintas_cli/laintas_cli.py:16311) | output | _cmd_model_agent_base / 0 | `{label} now uses the backend default model.` |
| [16315](/root/laintas_cli/laintas_cli.py:16315) | output | _cmd_model_agent_base / 0 | `Usage: /model @<agent> [id\|reset]` |
| [16318](/root/laintas_cli/laintas_cli.py:16318) | output | _cmd_model_agent_base / 0 | `Model for {label} set to: {escape(args[0])}{_model_tier_suffix(args[0])}` |
| [16323](/root/laintas_cli/laintas_cli.py:16323) | output | _cmd_model_agent_base / 0 | `Current model for {label}: {escape(agent.base_model or 'backend default')}` |
| [16328](/root/laintas_cli/laintas_cli.py:16328) | status/prompt | _cmd_model_agent_base | `Fetching available models… {symbols.BULLET} Esc/Ctrl+C cancel` |
| [16333](/root/laintas_cli/laintas_cli.py:16333) | output | _cmd_model_agent_base / 0 | `Model selection cancelled.` |
| [16336](/root/laintas_cli/laintas_cli.py:16336) | output | _cmd_model_agent_base / 0 | `Failed to fetch models: {escape(str(exc))}` |
| [16339](/root/laintas_cli/laintas_cli.py:16339) | output | _cmd_model_agent_base / 0 | `The backend offered no models.` |
| [16344](/root/laintas_cli/laintas_cli.py:16344) | output | _cmd_model_agent_base / 0 | `Model selection cancelled.` |
| [16351](/root/laintas_cli/laintas_cli.py:16351) | output | _cmd_model_agent_base / 0 | `Model for {label} set to: {escape(model_id)}{_model_tier_suffix(model_id)}` |
| [16384](/root/laintas_cli/laintas_cli.py:16384) | output | _cmd_model / 0 | `{escape(str(exc))}` |
| [16387](/root/laintas_cli/laintas_cli.py:16387) | output | _cmd_model / 0 | `No agent matches '{escape(reference)}'.` |
| [16399](/root/laintas_cli/laintas_cli.py:16399) | output | _cmd_model / 0 | `Terminal '{target_terminal}' not found.` |
| [16403](/root/laintas_cli/laintas_cli.py:16403) | output | _cmd_model / 0 | `Terminal '{target_terminal}' has no deployed agent; /model only changes a deployed terminal model.` |
| [16430](/root/laintas_cli/laintas_cli.py:16430) | output | _cmd_model / 0 | `Usage: /model [terminal] reset` |
| [16434](/root/laintas_cli/laintas_cli.py:16434) | output | _cmd_model / 0 | `Model override reset for {target_terminal}. Gateway auto-routing will be used (model shown as 'auto-routing').` |
| [16438](/root/laintas_cli/laintas_cli.py:16438) | output | _cmd_model / 0 | `Usage: /model [terminal] <model-id>` |
| [16443](/root/laintas_cli/laintas_cli.py:16443) | output | _cmd_model / 0 | `Model for {target_terminal} set to: {model}{_model_tier_suffix(model)}` |
| [16455](/root/laintas_cli/laintas_cli.py:16455) | status/prompt | _cmd_model | `Fetching available models… {symbols.BULLET} Esc/Ctrl+C cancel` |
| [16460](/root/laintas_cli/laintas_cli.py:16460) | output | _cmd_model / 0 | `Model selection cancelled.` |
| [16463](/root/laintas_cli/laintas_cli.py:16463) | output | _cmd_model / 0 | `Failed to fetch models: {e}` |
| [16465](/root/laintas_cli/laintas_cli.py:16465) | output | _cmd_model / 0 | `Current model: {current or 'backend default'}` |
| [16466](/root/laintas_cli/laintas_cli.py:16466) | output | _cmd_model / 0 | `Usage: /model <model-id>  or  /model reset` |
| [16488](/root/laintas_cli/laintas_cli.py:16488) | output | _cmd_model / 0 | `Model for {target_terminal} set to: {info}` |
| [16491](/root/laintas_cli/laintas_cli.py:16491) | output | _cmd_model / 0 | `Model selection cancelled.` |
| [16493](/root/laintas_cli/laintas_cli.py:16493) | title/menu/help | _cmd_model / title | `Available Models ({endpoint})` |
| [16494](/root/laintas_cli/laintas_cli.py:16494) | label/render | _cmd_model | `#` |
| [16495](/root/laintas_cli/laintas_cli.py:16495) | label/render | _cmd_model | `Current` |
| [16496](/root/laintas_cli/laintas_cli.py:16496) | label/render | _cmd_model | `Model ID` |
| [16497](/root/laintas_cli/laintas_cli.py:16497) | label/render | _cmd_model | `Tier` |
| [16498](/root/laintas_cli/laintas_cli.py:16498) | label/render | _cmd_model | `Name` |
| [16499](/root/laintas_cli/laintas_cli.py:16499) | label/render | _cmd_model | `Provider` |
| [16529](/root/laintas_cli/laintas_cli.py:16529) | output | _cmd_model / 0 | `Terminal {target_terminal} override: {current or '(none)'}{_model_tier_suffix(current)} ({_model_supply_label(current_provider)})` |
| [16529](/root/laintas_cli/laintas_cli.py:16529) | output | _cmd_model / 0 | `Terminal {target_terminal} override: {current or '(none)'}{_model_tier_suffix(current)}` |
| [16535](/root/laintas_cli/laintas_cli.py:16535) | output | _cmd_model / 0 | `Non-interactive terminal: select explicitly with /model <model-id>.` |
| [16538](/root/laintas_cli/laintas_cli.py:16538) | output | _cmd_model / 0 | `Set with /model [terminal] <model-id>, reset with /model [terminal] reset. This never changes the employee base model.` |
| [16543](/root/laintas_cli/laintas_cli.py:16543) | output | _cmd_model / 0 | `Your own provider key: /model add (walks you through provider, key and model); routes: /model route list.` |
| [16553](/root/laintas_cli/laintas_cli.py:16553) | output | _cmd_name / 0 | `Agent name set to: {name}` |
| [16569](/root/laintas_cli/laintas_cli.py:16569) | output | _cmd_name / 0 | `Current agent name: {current}` |
| [16570](/root/laintas_cli/laintas_cli.py:16570) | output | _cmd_name / 0 | `Usage: /name <new-name>` |
| [16571](/root/laintas_cli/laintas_cli.py:16571) | output | _cmd_name / 0 | `       /term rename <old> <new>  (rename a terminal)` |
| [16664](/root/laintas_cli/laintas_cli.py:16664) | title/menu/help | _memory_manager.delete / message | `Press x again to delete {item.title}` |
| [16669](/root/laintas_cli/laintas_cli.py:16669) | title/menu/help | _memory_manager.delete / message | `Deleted {item.title}` |
| [16669](/root/laintas_cli/laintas_cli.py:16669) | title/menu/help | _memory_manager.delete / message | `Delete failed: {message}` |
| [16674](/root/laintas_cli/laintas_cli.py:16674) | title/menu/help | _memory_manager / title | `Memory` |
| [16694](/root/laintas_cli/laintas_cli.py:16694) | output | _memory_status / 0 | `Persistent memories visible here: {total}  •  budget {budget}  (over)` |
| [16694](/root/laintas_cli/laintas_cli.py:16694) | output | _memory_status / 0 | `Persistent memories visible here: {total}  •  budget {budget}` |
| [16697](/root/laintas_cli/laintas_cli.py:16697) | output | _memory_status / 0 | `Used at least once since usage tracking began: {used}; not yet used: {never}.` |
| [16701](/root/laintas_cli/laintas_cli.py:16701) | output | _memory_status / 0 | `Most used: {', '.join((f'{e.get('name')} ({int(e.get('uses', 0) or 0)})' for e in top))}.` |
| [16707](/root/laintas_cli/laintas_cli.py:16707) | output | _memory_status / 0 | `Archived (kept, out of the live set): {count}.` |
| [16713](/root/laintas_cli/laintas_cli.py:16713) | output | _memory_status / 0 | `Semantic de-duplication and ranking are degraded — the embedding endpoint refused ({reason or 'no detail'}); retrying in {int(left)}s. New memories can only be matched by exact name until then.` |
| [16718](/root/laintas_cli/laintas_cli.py:16718) | output | _memory_status / 0 | `Embeddings unavailable (not signed in?); de-duplication falls back to exact-name matching.` |
| [16732](/root/laintas_cli/laintas_cli.py:16732) | output | _cmd_memory._persistent_table / 0 | `No persistent memories in this scope.` |
| [16735](/root/laintas_cli/laintas_cli.py:16735) | label/render | _cmd_memory._persistent_table | `Name` |
| [16736](/root/laintas_cli/laintas_cli.py:16736) | label/render | _cmd_memory._persistent_table | `Category` |
| [16737](/root/laintas_cli/laintas_cli.py:16737) | label/render | _cmd_memory._persistent_table | `Scope` |
| [16738](/root/laintas_cli/laintas_cli.py:16738) | label/render | _cmd_memory._persistent_table | `Description` |
| [16748](/root/laintas_cli/laintas_cli.py:16748) | output | _cmd_memory._persistent_table / 0 | `Run /memory show <name> for full text, or /memory to open the manager.` |
| [16756](/root/laintas_cli/laintas_cli.py:16756) | output | _cmd_memory / 0 | `Persistent memory: {len(persistent_entries)} visible. List with /memory global\|local\|persistent, or /memory show <name>.` |
| [16760](/root/laintas_cli/laintas_cli.py:16760) | output | _cmd_memory / 0 | `Project memory.json: {len(project_entries)} entries. See /memory project.` |
| [16773](/root/laintas_cli/laintas_cli.py:16773) | output | _cmd_memory / 0 | `Memory store is within its budget; nothing to archive.` |
| [16775](/root/laintas_cli/laintas_cli.py:16775) | output | _cmd_memory / 0 | `{len(chosen)} entries are over the budget ({len(persistent_entries)} visible, limit {memory_system.MEMORY_BUDGET}).` |
| [16779](/root/laintas_cli/laintas_cli.py:16779) | output | _cmd_memory / 0 | `  {entry.get('name')}  importance {float(entry.get('importance', 0.5) or 0.5):.1f}  uses {int(entry.get('uses', 0) or 0)}` |
| [16783](/root/laintas_cli/laintas_cli.py:16783) | output | _cmd_memory / 0 | `  … and {len(chosen) - 10} more` |
| [16785](/root/laintas_cli/laintas_cli.py:16785) | output | _cmd_memory / 0 | `Dry run. Run /memory compact to archive them (files are moved to memory/archive/, never deleted).` |
| [16789](/root/laintas_cli/laintas_cli.py:16789) | output | _cmd_memory / 0 | `Archived {len(archived)} entries to {memory_system.archive_dir()}` |
| [16791](/root/laintas_cli/laintas_cli.py:16791) | output | _cmd_memory / 0 | `Nothing was deleted; /memory show <name> still reads them.` |
| [16796](/root/laintas_cli/laintas_cli.py:16796) | resolved output candidate | _cmd_memory | `{'\n'.join((f'- {error}' for error in project_errors))} ⏎  ⏎ Fix {paths.project_file(paths.CWD_MEMORY)} and retry /memory project.` |
| [16799](/root/laintas_cli/laintas_cli.py:16799) | title/menu/help | _cmd_memory / title | `Project Memory: invalid` |
| [16808](/root/laintas_cli/laintas_cli.py:16808) | title/menu/help | _cmd_memory / title | `Project memory ({len(project_entries)} entries)` |
| [16812](/root/laintas_cli/laintas_cli.py:16812) | output | _cmd_memory / 0 | `Project memory is empty.` |
| [16844](/root/laintas_cli/laintas_cli.py:16844) | title/menu/help | _cmd_memory / title | `View Memory` |
| [16860](/root/laintas_cli/laintas_cli.py:16860) | output | _cmd_memory / 0 | `Memory selection cancelled.` |
| [16873](/root/laintas_cli/laintas_cli.py:16873) | output | _cmd_memory / 0 | `Memory '{selector}' not found.` |
| [16874](/root/laintas_cli/laintas_cli.py:16874) | output | _cmd_memory / 0 | `Use /memory project or /memory persistent to list valid entries.` |
| [16881](/root/laintas_cli/laintas_cli.py:16881) | output | _cmd_memory / 0 | `Usage: /memory [global\|local\|persistent\|project\|show <id\|name>]` |
| [16892](/root/laintas_cli/laintas_cli.py:16892) | error candidate | _shared_storage_client | `Not signed in. Run /login — shared storage is per Laintas account.` |
| [16904](/root/laintas_cli/laintas_cli.py:16904) | resolved output candidate | _shared_print_usage | `{tier} {symbols.BULLET} {ss.human_bytes(usage.used_bytes)} used {symbols.BULLET} {ss.human_bytes(usage.free_bytes)} free {symbols.BULLET} {ss.human_bytes(usage.max_bytes)} limit` |
| [16907](/root/laintas_cli/laintas_cli.py:16907) | output | _shared_print_usage / 0 | `{ss.human_bytes(usage.overage_bytes)} over the free allowance {symbols.BULLET} about ${usage.est_cost_cents / 100:.2f}/month, billed at month end.` |
| [16916](/root/laintas_cli/laintas_cli.py:16916) | output | _shared_print_listing / 0 | `Nothing stored{f' under {prefix}' if prefix else ''} yet. Use /shared push <file> to put something here.` |
| [16919](/root/laintas_cli/laintas_cli.py:16919) | title/menu/help | _shared_print_listing / title | `Laintas Storage{f' {symbols.BULLET} {prefix}' if prefix else ''} ({len(entries)})` |
| [16922](/root/laintas_cli/laintas_cli.py:16922) | label/render | _shared_print_listing | `Path` |
| [16923](/root/laintas_cli/laintas_cli.py:16923) | label/render | _shared_print_listing | `Size` |
| [16924](/root/laintas_cli/laintas_cli.py:16924) | label/render | _shared_print_listing | `Modified` |
| [16949](/root/laintas_cli/laintas_cli.py:16949) | output | _cmd_shared / 0 | `Usage: /shared [list [path]\|push <local> [remote]\|pull <remote> [local]\|rm <path>\|mkdir <path>\|mv <from> <to>\|cp <from> <to>\|usage] ⏎ Files land in the same storage Helpwo mounts as "Laintas Storage" — push here, open it there.` |
| [16960](/root/laintas_cli/laintas_cli.py:16960) | status/prompt | _cmd_shared | `Reading storage…` |
| [16969](/root/laintas_cli/laintas_cli.py:16969) | status/prompt | _cmd_shared | `Listing…` |
| [16976](/root/laintas_cli/laintas_cli.py:16976) | output | _cmd_shared / 0 | `Usage: /shared push <local-path> [remote-path]` |
| [16980](/root/laintas_cli/laintas_cli.py:16980) | output | _cmd_shared / 0 | `No such file or directory: {escape(local)}` |
| [16988](/root/laintas_cli/laintas_cli.py:16988) | output | _cmd_shared / 0 | `Nothing to push — the folder has no files.` |
| [16993](/root/laintas_cli/laintas_cli.py:16993) | status/prompt | _cmd_shared | `Pushing {index}/{len(files)} {symbols.BULLET} {escape(relative)}` |
| [16997](/root/laintas_cli/laintas_cli.py:16997) | output | _cmd_shared / 0 | `Pushed {len(files)} files ({ss.human_bytes(total)}) to {escape(base)}/` |
| [17002](/root/laintas_cli/laintas_cli.py:17002) | status/prompt | _cmd_shared | `Pushing {escape(os.path.basename(local))}…` |
| [17005](/root/laintas_cli/laintas_cli.py:17005) | output | _cmd_shared / 0 | `Pushed {escape(remote)} ({ss.human_bytes(size)}).` |
| [17006](/root/laintas_cli/laintas_cli.py:17006) | output | _cmd_shared / 0 | `Open it in Helpwo under Laintas Storage.` |
| [17011](/root/laintas_cli/laintas_cli.py:17011) | output | _cmd_shared / 0 | `Usage: /shared pull <remote-path> [local-path]` |
| [17017](/root/laintas_cli/laintas_cli.py:17017) | status/prompt | _cmd_shared | `Pulling {escape(remote)}…` |
| [17020](/root/laintas_cli/laintas_cli.py:17020) | output | _cmd_shared / 0 | `Pulled {escape(remote)} → {escape(local)} ({ss.human_bytes(size)}).` |
| [17026](/root/laintas_cli/laintas_cli.py:17026) | output | _cmd_shared / 0 | `Usage: /shared rm <remote-path>` |
| [17034](/root/laintas_cli/laintas_cli.py:17034) | output | _cmd_shared / 0 | `{escape(target)} contains {len(victims)} entries; deleting it removes all of them.` |
| [17037](/root/laintas_cli/laintas_cli.py:17037) | status/prompt | _cmd_shared | `Delete {target} and everything under it? [y/N] ` |
| [17041](/root/laintas_cli/laintas_cli.py:17041) | output | _cmd_shared / 0 | `Cancelled.` |
| [17043](/root/laintas_cli/laintas_cli.py:17043) | status/prompt | _cmd_shared | `Deleting…` |
| [17045](/root/laintas_cli/laintas_cli.py:17045) | output | _cmd_shared / 0 | `Deleted {escape(target)} ({removed} objects).` |
| [17050](/root/laintas_cli/laintas_cli.py:17050) | output | _cmd_shared / 0 | `Usage: /shared mkdir <remote-path>` |
| [17053](/root/laintas_cli/laintas_cli.py:17053) | output | _cmd_shared / 0 | `Created {escape(ss.clean_remote_path(args[0]))}/` |
| [17058](/root/laintas_cli/laintas_cli.py:17058) | output | _cmd_shared / 0 | `Usage: /shared mv <from> <to>` |
| [17061](/root/laintas_cli/laintas_cli.py:17061) | output | _cmd_shared / 0 | `Moved {escape(args[0])} → {escape(args[1])}` |
| [17066](/root/laintas_cli/laintas_cli.py:17066) | output | _cmd_shared / 0 | `Usage: /shared cp <from> <to>` |
| [17069](/root/laintas_cli/laintas_cli.py:17069) | output | _cmd_shared / 0 | `Copied {escape(args[0])} → {escape(args[1])}` |
| [17072](/root/laintas_cli/laintas_cli.py:17072) | output | _cmd_shared / 0 | `Unknown subcommand: {escape(sub)} — run /shared help` |
| [17075](/root/laintas_cli/laintas_cli.py:17075) | output | _cmd_shared / 0 | `{escape(str(exc))}` |
| [17077](/root/laintas_cli/laintas_cli.py:17077) | output | _cmd_shared / 0 | `Cancelled.` |
| [17116](/root/laintas_cli/laintas_cli.py:17116) | output | _handoff_print_one / 0 | ` ⏎ {escape(env['title'])}  {escape(env['id'])}` |
| [17125](/root/laintas_cli/laintas_cli.py:17125) | output | _handoff_print_one / 0 | `baseline: {escape(base)}` |
| [17131](/root/laintas_cli/laintas_cli.py:17131) | output | _handoff_print_one / 0 | `  the tree was dirty when this was written — the work is not in that commit, only on the author's disk` |
| [17136](/root/laintas_cli/laintas_cli.py:17136) | output | _handoff_print_one / 0 | `the API contract changed since this handoff was written — re-read .laintas/contract/` |
| [17139](/root/laintas_cli/laintas_cli.py:17139) | output | _handoff_print_one / 0 | `API contract unchanged since it was written` |
| [17142](/root/laintas_cli/laintas_cli.py:17142) | output | _handoff_print_one / 0 | `  {symbols.BULLET} don't: {escape(entry)}` |
| [17147](/root/laintas_cli/laintas_cli.py:17147) | output | _handoff_print_one / 0 | `all declared acceptance checks pass against the current workspace` |
| [17150](/root/laintas_cli/laintas_cli.py:17150) | output | _handoff_print_one / 0 | `outstanding ({len(result['gaps'])}):` |
| [17152](/root/laintas_cli/laintas_cli.py:17152) | output | _handoff_print_one / 0 | `  {symbols.BULLET} {escape(gap)}` |
| [17156](/root/laintas_cli/laintas_cli.py:17156) | output | _handoff_print_one / 0 | `also claimed by {who} — {escape(state['holder'])} holds it (earliest claim wins); agree before both of you work` |
| [17161](/root/laintas_cli/laintas_cli.py:17161) | output | _handoff_print_one / 0 | `log:` |
| [17165](/root/laintas_cli/laintas_cli.py:17165) | output | _handoff_print_one / 0 | `  {when} {escape(event['actor'])} {event['kind']}{note}` |
| [17168](/root/laintas_cli/laintas_cli.py:17168) | output | _handoff_print_one / 0 | `  … {len(events) - 8} earlier` |
| [17175](/root/laintas_cli/laintas_cli.py:17175) | output | _handoff_print_list / 0 | `No handoffs here yet. Use /handoff new <title> before you stop for the day.` |
| [17178](/root/laintas_cli/laintas_cli.py:17178) | title/menu/help | _handoff_print_list / title | `Handoffs ({len(envs)})` |
| [17179](/root/laintas_cli/laintas_cli.py:17179) | label/render | _handoff_print_list | `Id` |
| [17180](/root/laintas_cli/laintas_cli.py:17180) | label/render | _handoff_print_list | `Title` |
| [17181](/root/laintas_cli/laintas_cli.py:17181) | label/render | _handoff_print_list | `State` |
| [17182](/root/laintas_cli/laintas_cli.py:17182) | label/render | _handoff_print_list | `Left` |
| [17183](/root/laintas_cli/laintas_cli.py:17183) | label/render | _handoff_print_list | `Updated` |
| [17207](/root/laintas_cli/laintas_cli.py:17207) | output | _cmd_handoff / 0 | `Usage: /handoff [list\|new <title>\|accept <id>\|show <id>\|claim <id>\|release <id>\|note <id> <text>\|close <id>\|reopen <id>\|sync [id]\|export <id> [file]\|import <token\|file>\|remote\|fetch <remote-path>] ⏎ An envelope in .laintas/handoff/ — committable, mergeable, and re-verified against the workspace instead of believed. ⏎ new: add "don't do X" notes with --avoid "…" (repeatable), and name the successor with --to <who>.` |
| [17223](/root/laintas_cli/laintas_cli.py:17223) | error candidate | _cmd_handoff | `Usage: /handoff accept <id>` |
| [17234](/root/laintas_cli/laintas_cli.py:17234) | error candidate | _cmd_handoff | `{token} needs a value` |
| [17247](/root/laintas_cli/laintas_cli.py:17247) | output | _cmd_handoff / 0 | `Give it a title: /handoff new <what this is>` |
| [17251](/root/laintas_cli/laintas_cli.py:17251) | error candidate | _cmd_handoff | `Use either --to <saved-account> or --to-user-id <userId>` |
| [17268](/root/laintas_cli/laintas_cli.py:17268) | output | _cmd_handoff / 0 | `Created {escape(env['id'])}.` |
| [17271](/root/laintas_cli/laintas_cli.py:17271) | output | _cmd_handoff / 0 | `This handoff contains notes only. Create one during a task to capture a resumable checkpoint.` |
| [17274](/root/laintas_cli/laintas_cli.py:17274) | output | _cmd_handoff / 0 | ` ⏎ Commit .laintas/handoff/ so it reaches them through the repo, or use export/import across accounts. sync uses this account's storage. Sync project files separately; the receiver runs /handoff accept <id>.` |
| [17281](/root/laintas_cli/laintas_cli.py:17281) | output | _cmd_handoff / 0 | `Which handoff? /handoff {sub} <id>` |
| [17291](/root/laintas_cli/laintas_cli.py:17291) | output | _cmd_handoff / 0 | `Nothing to record: /handoff note <id> <text>` |
| [17299](/root/laintas_cli/laintas_cli.py:17299) | output | _cmd_handoff / 0 | `{escape(state['holder'])} claimed this first — your claim is recorded but does not hold it. Talk to them before working.` |
| [17302](/root/laintas_cli/laintas_cli.py:17302) | output | _cmd_handoff / 0 | `{sub} recorded on {escape(env['id'])}.` |
| [17311](/root/laintas_cli/laintas_cli.py:17311) | output | _cmd_handoff / 0 | `Nothing here to sync.` |
| [17314](/root/laintas_cli/laintas_cli.py:17314) | status/prompt | _cmd_handoff | `Syncing {env['id']}…` |
| [17319](/root/laintas_cli/laintas_cli.py:17319) | output | _cmd_handoff / 0 | `{escape(env['id'])} {symbols.BULLET} {detail}` |
| [17329](/root/laintas_cli/laintas_cli.py:17329) | output | _cmd_handoff / 0 | `Wrote {escape(target)} ({len(token)} chars).` |
| [17331](/root/laintas_cli/laintas_cli.py:17331) | output | _cmd_handoff / 0 | `Attach it to mail or chat. The other side runs /handoff import <file>.` |
| [17334](/root/laintas_cli/laintas_cli.py:17334) | output | _cmd_handoff / 0 | `Send this to them — any channel. {len(token)} chars, no account or link needed:` |
| [17340](/root/laintas_cli/laintas_cli.py:17340) | output | _cmd_handoff / 0 | `They run: /handoff import <paste>` |
| [17349](/root/laintas_cli/laintas_cli.py:17349) | output | _cmd_handoff / 0 | `Paste the token, or give a file: /handoff import <token\|file>` |
| [17357](/root/laintas_cli/laintas_cli.py:17357) | output | _cmd_handoff / 0 | `This handoff was written against a different repository — its outstanding checks are paths, and they will fail here for reasons that are not about the work.` |
| [17362](/root/laintas_cli/laintas_cli.py:17362) | output | _cmd_handoff / 0 | `{'Merged into' if existed else 'Imported'} {escape(env['id'])}.` |
| [17369](/root/laintas_cli/laintas_cli.py:17369) | status/prompt | _cmd_handoff | `Listing shared handoffs…` |
| [17372](/root/laintas_cli/laintas_cli.py:17372) | output | _cmd_handoff / 0 | `No handoffs in storage for this repository.` |
| [17375](/root/laintas_cli/laintas_cli.py:17375) | output | _cmd_handoff / 0 | `  {escape(path)}` |
| [17376](/root/laintas_cli/laintas_cli.py:17376) | output | _cmd_handoff / 0 | `/handoff fetch <path> to take one.` |
| [17381](/root/laintas_cli/laintas_cli.py:17381) | status/prompt | _cmd_handoff | `Fetching…` |
| [17383](/root/laintas_cli/laintas_cli.py:17383) | output | _cmd_handoff / 0 | `Fetched {escape(env['id'])}.` |
| [17387](/root/laintas_cli/laintas_cli.py:17387) | output | _cmd_handoff / 0 | `Unknown subcommand: {escape(sub)} — run /handoff help` |
| [17390](/root/laintas_cli/laintas_cli.py:17390) | output | _cmd_handoff / 0 | `{escape(str(exc))}` |
| [17392](/root/laintas_cli/laintas_cli.py:17392) | output | _cmd_handoff / 0 | `{escape(str(exc))}` |
| [17394](/root/laintas_cli/laintas_cli.py:17394) | output | _cmd_handoff / 0 | `Cancelled.` |
| [17405](/root/laintas_cli/laintas_cli.py:17405) | output | _cmd_prop / 0 | `{exc}` |
| [17427](/root/laintas_cli/laintas_cli.py:17427) | output | _cmd_prop / 0 | `No budget has been recorded yet. Run a prompt first.` |
| [17437](/root/laintas_cli/laintas_cli.py:17437) | output | _cmd_prop / 0 | `Could not write the page: {escape(str(exc))}` |
| [17439](/root/laintas_cli/laintas_cli.py:17439) | output | _cmd_prop / 0 | `Budget page written: {escape(str(written.resolve()))}` |
| [17440](/root/laintas_cli/laintas_cli.py:17440) | output | _cmd_prop / 0 | `Open it in a browser, drag the dividers or type percentages, download the .config file, then /config import <file>.` |
| [17449](/root/laintas_cli/laintas_cli.py:17449) | output | _cmd_prop / 0 | `{exc}` |
| [17460](/root/laintas_cli/laintas_cli.py:17460) | output | _cmd_prop / 0 | `No captured conversation at that position. Run a prompt first; N=1 is the newest conversation.` |
| [17564](/root/laintas_cli/laintas_cli.py:17564) | title/menu/help | _cmd_prop._assistant / subtitle | `Independent tool-less inspection` |
| [17574](/root/laintas_cli/laintas_cli.py:17574) | error candidate | _cmd_prop._assistant | `AI did not return a valid prompt patch proposal` |
| [17593](/root/laintas_cli/laintas_cli.py:17593) | title/menu/help | _cmd_prop._assistant / message | `Draft {patch['id']} created; active prompt unchanged` |
| [17596](/root/laintas_cli/laintas_cli.py:17596) | title/menu/help | _cmd_prop._assistant / subtitle | `Proposal only · explicit review and activation required` |
| [17605](/root/laintas_cli/laintas_cli.py:17605) | output | _cmd_scan / 0 | `{len(user_cmds)} user-facing commands on PATH: ⏎ ` |
| [17610](/root/laintas_cli/laintas_cli.py:17610) | output | _cmd_scan / 0 | `  {letter}: {', '.join(groups[letter][:40])}` |
| [17612](/root/laintas_cli/laintas_cli.py:17612) | output | _cmd_scan / 0 | `       (+{len(groups[letter]) - 40} more)` |
| [17616](/root/laintas_cli/laintas_cli.py:17616) | output | _cmd_cwd / 0 | `Working directory: {os.getcwd()}` |
| [17623](/root/laintas_cli/laintas_cli.py:17623) | resolved output candidate | _cmd_bash | `/bash <command>        run <command> directly via term0's real bash (marker-poll + cwd sync), bypassing the interactive whitelist below ⏎ /bash list              show the interactive-terminal whitelist ⏎ /bash add <command>     force <command> through full PTY passthrough (vim-style — use for full-screen/raw-keystroke programs) ⏎ /bash remove <command>  let <command> use term0/marker-poll instead ⏎  ⏎ Current whitelist: {wl}` |
| [17631](/root/laintas_cli/laintas_cli.py:17631) | title/menu/help | _cmd_bash / title | `/bash` |
| [17635](/root/laintas_cli/laintas_cli.py:17635) | resolved output candidate | _cmd_bash | `(empty)` |
| [17636](/root/laintas_cli/laintas_cli.py:17636) | title/menu/help | _cmd_bash / title | `Interactive-terminal whitelist` |
| [17639](/root/laintas_cli/laintas_cli.py:17639) | output | _cmd_bash / 0 | `Usage: /bash add <command>` |
| [17641](/root/laintas_cli/laintas_cli.py:17641) | output | _cmd_bash / 0 | `Command must be one executable token without shell operators.` |
| [17644](/root/laintas_cli/laintas_cli.py:17644) | output | _cmd_bash / 0 | `'{parts[2]}' now uses full PTY passthrough (native terminal).` |
| [17647](/root/laintas_cli/laintas_cli.py:17647) | output | _cmd_bash / 0 | `Usage: /bash remove <command>` |
| [17649](/root/laintas_cli/laintas_cli.py:17649) | output | _cmd_bash / 0 | `Command must be one executable token without shell operators.` |
| [17652](/root/laintas_cli/laintas_cli.py:17652) | output | _cmd_bash / 0 | `'{parts[2]}' now routes through term0/marker-poll.` |
| [17657](/root/laintas_cli/laintas_cli.py:17657) | output | _cmd_bash / 0 | `{denial}` |
| [17662](/root/laintas_cli/laintas_cli.py:17662) | output | _cmd_bash / 0 | `term0 session unavailable.` |
| [17674](/root/laintas_cli/laintas_cli.py:17674) | output | _cmd_bash / 0 | `cwd → {os.getcwd()}{rc_text}` |
| [17688](/root/laintas_cli/laintas_cli.py:17688) | output | _print_mode_activation_note / 0 | `↳ AUTO confirmation windows: {int(timeout)}s ordinary, {int(mode_manager.get_auto_confirm_timeout(destructive=True) or 0)}s deletion. Choose No before the timer expires to stop an action.` |
| [17696](/root/laintas_cli/laintas_cli.py:17696) | output | _print_mode_activation_note / 0 | `↳ {name.upper()}* — auto-approving {auto_approve} this session.` |
| [17704](/root/laintas_cli/laintas_cli.py:17704) | output | _print_mode_activation_note / 0 | `↳ {detail}Use /mode act to switch back.` |
| [17721](/root/laintas_cli/laintas_cli.py:17721) | output | _cmd_mode / 0 | `Already in PLAN mode.` |
| [17725](/root/laintas_cli/laintas_cli.py:17725) | resolved output candidate | _cmd_mode | `PLAN mode ⏎  ⏎ Describe the task in your next message. The agent will plan without modifying files or the system.` |
| [17729](/root/laintas_cli/laintas_cli.py:17729) | title/menu/help | _cmd_mode / title | `Mode changed` |
| [17734](/root/laintas_cli/laintas_cli.py:17734) | output | _cmd_mode / 0 | `A plan is already active. Use /plan status, /plan approve, or /plan exit before starting another.` |
| [17740](/root/laintas_cli/laintas_cli.py:17740) | resolved output candidate | _cmd_mode | `Plan Mode: ENTERED ⏎  ⏎ Task: {task} ⏎ Plan file: {plan['file']} ⏎  ⏎ The AI will explore and design — no code will be executed. ⏎ When ready, the review menu will offer execute, revise, or exit.` |
| [17746](/root/laintas_cli/laintas_cli.py:17746) | title/menu/help | _cmd_mode / title | `Plan Mode` |
| [17755](/root/laintas_cli/laintas_cli.py:17755) | output | _cmd_mode / 0 | `Usage: /mode act [always]` |
| [17766](/root/laintas_cli/laintas_cli.py:17766) | output | _cmd_mode / 0 | `ACT always-approve — file writes and commands are auto-approved this session (ACT*). ⏎ Run /mode act to turn confirmations back on.` |
| [17773](/root/laintas_cli/laintas_cli.py:17773) | output | _cmd_mode / 0 | `[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}] (draft plan saved)` |
| [17773](/root/laintas_cli/laintas_cli.py:17773) | output | _cmd_mode / 0 | `[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]` |
| [17779](/root/laintas_cli/laintas_cli.py:17779) | output | _cmd_mode / 0 | `/mode approve is deprecated; use /plan approve.` |
| [17781](/root/laintas_cli/laintas_cli.py:17781) | output | _cmd_mode / 0 | `No active plan to approve.` |
| [17792](/root/laintas_cli/laintas_cli.py:17792) | resolved output candidate | _cmd_mode | `Plan [{'green' if approved else 'yellow'}]{'APPROVED' if approved else 'NOT APPROVED'}[/{'green' if approved else 'yellow'}] ⏎  ⏎ File: {_cur_plan['file']} ⏎  ⏎ Execution request queued.` |
| [17792](/root/laintas_cli/laintas_cli.py:17792) | resolved output candidate | _cmd_mode | `Plan [{'green' if approved else 'yellow'}]{'APPROVED' if approved else 'NOT APPROVED'}[/{'green' if approved else 'yellow'}] ⏎  ⏎ File: {_cur_plan['file']} ⏎  ⏎ No execution was queued.` |
| [17799](/root/laintas_cli/laintas_cli.py:17799) | title/menu/help | _cmd_mode / title | `Plan Approved` |
| [17804](/root/laintas_cli/laintas_cli.py:17804) | output | _cmd_mode / 0 | `Agent modes` |
| [17806](/root/laintas_cli/laintas_cli.py:17806) | output | _cmd_mode / 0 | `{'*' if active_name == 'plan' else ' '} plan Reviewed, read-only planning` |
| [17830](/root/laintas_cli/laintas_cli.py:17830) | output | _cmd_mode / 0 | `{marker} {item['name']} {_escape(f' {symbols.BULLET} '.join(_bits))}` |
| [17836](/root/laintas_cli/laintas_cli.py:17836) | output | _cmd_mode / 0 | `Usage: /mode create <name> [--tools "fs.*,web.search"] [--deny "shell.*"] [--read-only] [--auto-approve none\|writes\|commands\|all] <instructions>` |
| [17865](/root/laintas_cli/laintas_cli.py:17865) | output | _cmd_mode / 0 | `[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]` |
| [17869](/root/laintas_cli/laintas_cli.py:17869) | output | _cmd_mode / 0 | `⚠ Mode '{name}' auto-approves {_auto} — file writes/commands run without confirmation while it's active (shown as {name.upper()}*). Hard deny rules still apply.` |
| [17882](/root/laintas_cli/laintas_cli.py:17882) | title/menu/help | _cmd_mode / title | `Delete Custom Mode` |
| [17889](/root/laintas_cli/laintas_cli.py:17889) | output | _cmd_mode / 0 | `Mode selection cancelled.` |
| [17893](/root/laintas_cli/laintas_cli.py:17893) | output | _cmd_mode / 0 | `[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]` |
| [17903](/root/laintas_cli/laintas_cli.py:17903) | resolved output candidate | _cmd_mode | `Mode: {_mode_name} ⏎ {_escape(_mode_desc[:120])}` |
| [17906](/root/laintas_cli/laintas_cli.py:17906) | title/menu/help | _cmd_mode / title | `Current Mode` |
| [17911](/root/laintas_cli/laintas_cli.py:17911) | output | _cmd_mode / 0 | `Usage: /mode {_escape(sub)}` |
| [17922](/root/laintas_cli/laintas_cli.py:17922) | output | _cmd_mode / 0 | `[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]` |
| [17950](/root/laintas_cli/laintas_cli.py:17950) | title/menu/help | _cmd_mode / title | `Select Agent Mode` |
| [17962](/root/laintas_cli/laintas_cli.py:17962) | output | _cmd_mode / 0 | `[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]` |
| [17968](/root/laintas_cli/laintas_cli.py:17968) | output | _cmd_mode / 0 | `Current mode: {'plan' if _in_plan else active['name']}` |
| [17994](/root/laintas_cli/laintas_cli.py:17994) | resolved output candidate | _cmd_trust | `Project: {status.get('realpath', os.getcwd())} ⏎ Trusted: {status.get('trusted', False)} ⏎ Reason: {status.get('reason', '')} ⏎ Executable hashes: ⏎ {details}` |
| [17999](/root/laintas_cli/laintas_cli.py:17999) | title/menu/help | _cmd_trust / title | `Workspace Trust` |
| [18018](/root/laintas_cli/laintas_cli.py:18018) | output | _cmd_trust / 0 | `Workspace not trusted. In non-interactive mode use /trust allow --yes after reviewing the files.` |
| [18026](/root/laintas_cli/laintas_cli.py:18026) | output | _cmd_trust / 0 | `Trusted executable customization for {trusted['realpath']} at the current hashes.` |
| [18034](/root/laintas_cli/laintas_cli.py:18034) | output | _cmd_trust / 0 | `Workspace trust revoked.` |
| [18034](/root/laintas_cli/laintas_cli.py:18034) | output | _cmd_trust / 0 | `Workspace was not explicitly trusted.` |
| [18037](/root/laintas_cli/laintas_cli.py:18037) | output | _cmd_trust / 0 | `Usage: /trust [status\|allow\|revoke]` |
| [18044](/root/laintas_cli/laintas_cli.py:18044) | resolved output candidate | _cmd_backend | `Profile: {profile.name} ⏎ Kind: {profile.kind} ⏎ URL: {profile.base_url} ⏎ Billing: {profile.billing_label} ⏎ Sends Laintas credentials: {profile.sends_laintas_credentials} ⏎ Model route: {model_route.selected_name()} (/model route)` |
| [18049](/root/laintas_cli/laintas_cli.py:18049) | title/menu/help | _cmd_backend / title | `Backend` |
| [18057](/root/laintas_cli/laintas_cli.py:18057) | output | _cmd_backend / 0 | `{marker} {profile.name}  {profile.base_url} {profile.billing_label}` |
| [18069](/root/laintas_cli/laintas_cli.py:18069) | title/menu/help | _cmd_backend / title | `Select Backend` |
| [18079](/root/laintas_cli/laintas_cli.py:18079) | output | _cmd_backend / 0 | `Backend selection cancelled.` |
| [18082](/root/laintas_cli/laintas_cli.py:18082) | output | _cmd_backend / 0 | `LAINTAS_BACKEND currently overrides profiles; unset it first.` |
| [18085](/root/laintas_cli/laintas_cli.py:18085) | output | _cmd_backend / 0 | `[{'green' if ok else 'red'}]{msg}[/{'green' if ok else 'red'}]` |
| [18089](/root/laintas_cli/laintas_cli.py:18089) | output | _cmd_backend / 0 | `Usage: /backend [status\|list\|use <name>\|config]` |
| [18108](/root/laintas_cli/laintas_cli.py:18108) | output | _leave_direct_route / 0 | `{model_route.ENV_OVERRIDE} still routes chat to {escape(model_route.selected_name())}; unset it to use this model.` |
| [18112](/root/laintas_cli/laintas_cli.py:18112) | output | _leave_direct_route / 0 | `Model route: back to {model_route.DEFAULT_ROUTE} (the backend serves the model).` |
| [18124](/root/laintas_cli/laintas_cli.py:18124) | output | _cmd_model_route / 0 | `Model route: {model_route.DEFAULT_ROUTE} — served by backend {get_backend_profile().base_url}` |
| [18127](/root/laintas_cli/laintas_cli.py:18127) | resolved output candidate | _cmd_model_route | `Route: {route.name} ⏎ Provider: {route.spec.label} ⏎ Model: {route.model} ⏎ URL: {route.base_url} ⏎ Key: {model_route.key_preview(route)} ⏎ Context window: {f'{route.context_window:,}' if route.context_window else 'unknown (/config budget assumed_window)'} ⏎ Thinking: {get_runtime_config('reasoning_effort')}{' (not accepted by this model)' if route.reasoning_unsupported else ''} ⏎ Billing: your provider account — not metered by Laintas ⏎ Other services (search, OCR, images…): backend {get_backend_profile().base_url}` |
| [18135](/root/laintas_cli/laintas_cli.py:18135) | title/menu/help | _cmd_model_route / title | `Model route (direct)` |
| [18137](/root/laintas_cli/laintas_cli.py:18137) | output | _cmd_model_route / 0 | `{escape(note)}` |
| [18142](/root/laintas_cli/laintas_cli.py:18142) | output | _cmd_model_route / 0 | `{marker} {model_route.DEFAULT_ROUTE} served by the backend` |
| [18146](/root/laintas_cli/laintas_cli.py:18146) | output | _cmd_model_route / 0 | `{marker} {route.name} {escape(_model_route_row(route))} ({escape(blocked)})` |
| [18146](/root/laintas_cli/laintas_cli.py:18146) | output | _cmd_model_route / 0 | `{marker} {route.name} {escape(_model_route_row(route))}` |
| [18151](/root/laintas_cli/laintas_cli.py:18151) | output | _cmd_model_route / 0 | `{_MODEL_ROUTE_USAGE}` |
| [18161](/root/laintas_cli/laintas_cli.py:18161) | output | _cmd_model_route / 0 | `Usage: /model remove <route>` |
| [18163](/root/laintas_cli/laintas_cli.py:18163) | output | _cmd_model_route / 0 | `Removed model route {name} and its stored key.` |
| [18165](/root/laintas_cli/laintas_cli.py:18165) | output | _cmd_model_route / 0 | `Unknown model route: {escape(name)}` |
| [18169](/root/laintas_cli/laintas_cli.py:18169) | output | _cmd_model_route / 0 | `{model_route.ENV_OVERRIDE} currently overrides the selection; unset it first.` |
| [18177](/root/laintas_cli/laintas_cli.py:18177) | title/menu/help | _cmd_model_route / title | `Model Route` |
| [18184](/root/laintas_cli/laintas_cli.py:18184) | output | _cmd_model_route / 0 | `Unchanged: {active}. /model route {model_route.DEFAULT_ROUTE} returns to the backend.` |
| [18193](/root/laintas_cli/laintas_cli.py:18193) | output | _use_model_route / 0 | `{model_route.ENV_OVERRIDE} currently overrides the selection; unset it first.` |
| [18197](/root/laintas_cli/laintas_cli.py:18197) | output | _use_model_route / 0 | `Model route: {name} — the backend serves the model.` |
| [18202](/root/laintas_cli/laintas_cli.py:18202) | output | _use_model_route / 0 | `Unknown model route: {escape(name)} (/model add)` |
| [18206](/root/laintas_cli/laintas_cli.py:18206) | output | _use_model_route / 0 | `Cannot use {escape(name)}: {escape(refusal)}` |
| [18214](/root/laintas_cli/laintas_cli.py:18214) | output | _use_model_route / 0 | `Model route: {name} — {escape(route.model)} via {escape(route.base_url)}` |
| [18215](/root/laintas_cli/laintas_cli.py:18215) | output | _use_model_route / 0 | `Context window: {route.context_window:,} tokens. Thinking: /config reasoning_effort (not accepted by this model)` |
| [18215](/root/laintas_cli/laintas_cli.py:18215) | output | _use_model_route / 0 | `Context window: {route.context_window:,} tokens. Thinking: /config reasoning_effort` |
| [18215](/root/laintas_cli/laintas_cli.py:18215) | output | _use_model_route / 0 | `Context window: unknown — budgeting with /config budget assumed_window; set it with /model route {name} {route.model} <tokens>. Thinking: /config reasoning_effort (not accepted by this model)` |
| [18215](/root/laintas_cli/laintas_cli.py:18215) | output | _use_model_route / 0 | `Context window: unknown — budgeting with /config budget assumed_window; set it with /model route {name} {route.model} <tokens>. Thinking: /config reasoning_effort` |
| [18222](/root/laintas_cli/laintas_cli.py:18222) | output | _use_model_route / 0 | `Chat goes directly to this provider with your key; Laintas does not meter it. Search, OCR and images still use the backend.` |
| [18311](/root/laintas_cli/laintas_cli.py:18311) | output | _model_add_step / 0 | `Step {step}/{total} {text}` |
| [18328](/root/laintas_cli/laintas_cli.py:18328) | output | _model_route_add / 0 | `Unknown provider: {escape(preset)}. Choose one of: {', '.join(model_route.PRESETS)}` |
| [18333](/root/laintas_cli/laintas_cli.py:18333) | output | _model_route_add / 0 | `Usage: {escape(_MODEL_ADD_USAGE)}` |
| [18335](/root/laintas_cli/laintas_cli.py:18335) | output | _model_route_add / 0 | `Add your own model provider: the chat then goes straight to it with your key (Laintas does not meter it). Esc in a list, or Ctrl+C, cancels.` |
| [18340](/root/laintas_cli/laintas_cli.py:18340) | title/menu/help | _model_route_add / title | `Step 1/4 · Provider — who serves the model (Custom = any OpenAI-compatible URL)` |
| [18344](/root/laintas_cli/laintas_cli.py:18344) | output | _model_route_add / 0 | `Cancelled; nothing saved.` |
| [18348](/root/laintas_cli/laintas_cli.py:18348) | output | _model_route_add / 0 | `Provider: {escape(spec.label)}` |
| [18357](/root/laintas_cli/laintas_cli.py:18357) | output | _model_route_add / 0 | `Usage: /model add custom <base-url> <key\|$VAR> [model] [name]` |
| [18359](/root/laintas_cli/laintas_cli.py:18359) | output | _model_route_add / 0 | `  The URL that ends before /chat/completions, e.g. https://api.example.com/v1` |
| [18361](/root/laintas_cli/laintas_cli.py:18361) | status/prompt | _model_route_add | `  Base URL: ` |
| [18368](/root/laintas_cli/laintas_cli.py:18368) | output | _model_route_add / 0 | `Missing the key. Usage: {escape(_MODEL_ADD_USAGE)}` |
| [18375](/root/laintas_cli/laintas_cli.py:18375) | output | _model_route_add / 0 | `{escape(str(exc)) or 'Cancelled; nothing saved.'}` |
| [18380](/root/laintas_cli/laintas_cli.py:18380) | output | _model_route_add / 0 | `{'$' + escape(key_env) + ' is empty or unset' if key_env else 'No key given'}; nothing saved.` |
| [18386](/root/laintas_cli/laintas_cli.py:18386) | output | _model_route_add / 0 | `Checking the key with {escape(base_url)}/models …` |
| [18390](/root/laintas_cli/laintas_cli.py:18390) | output | _model_route_add / 0 | `Key check failed: {escape(str(exc))}. Nothing saved.` |
| [18392](/root/laintas_cli/laintas_cli.py:18392) | output | _model_route_add / 0 | `Key works — {len(catalog)} model(s) listed.` |
| [18395](/root/laintas_cli/laintas_cli.py:18395) | output | _model_route_add / 0 | `{escape(model)} is not in the provider's list; saving it anyway (some providers list only part of what they serve).` |
| [18399](/root/laintas_cli/laintas_cli.py:18399) | output | _model_route_add / 0 | `Missing the model. Available: {escape(sample)}. Run again with the model id after the key.` |
| [18407](/root/laintas_cli/laintas_cli.py:18407) | title/menu/help | _model_route_add / title | `Step 3/4 · Model — type to search; change it later with /model route <name> <model>` |
| [18412](/root/laintas_cli/laintas_cli.py:18412) | status/prompt | _model_route_add | `  The provider listed no models; model id: ` |
| [18416](/root/laintas_cli/laintas_cli.py:18416) | output | _model_route_add / 0 | `Cancelled; nothing saved.` |
| [18427](/root/laintas_cli/laintas_cli.py:18427) | status/prompt | _model_route_add | `  Name : ` |
| [18429](/root/laintas_cli/laintas_cli.py:18429) | output | _model_route_add / 0 | `Cancelled; nothing saved.` |
| [18432](/root/laintas_cli/laintas_cli.py:18432) | output | _model_route_add / 0 | `Unexpected extra arguments. Usage: {escape(_MODEL_ADD_USAGE)}` |
| [18440](/root/laintas_cli/laintas_cli.py:18440) | output | _model_route_add / 0 | `{escape(str(exc))}` |
| [18442](/root/laintas_cli/laintas_cli.py:18442) | output | _model_route_add / 0 | `{'Replaced' if replacing else 'Saved'} model route {route.name}: {escape(route.model)} via {escape(route.base_url)}.` |
| [18446](/root/laintas_cli/laintas_cli.py:18446) | output | _model_route_add / 0 | `Note: {escape(refusal)} — it cannot be selected here.` |
| [18450](/root/laintas_cli/laintas_cli.py:18450) | title/menu/help | _model_route_add / title | `Use {route.name} now?` |
| [18457](/root/laintas_cli/laintas_cli.py:18457) | output | _model_route_add / 0 | `Switch to it any time: /model (it is listed at the bottom) or /model route {route.name}. Back to Laintas: /model route {model_route.DEFAULT_ROUTE}.` |
| [18477](/root/laintas_cli/laintas_cli.py:18477) | resolved output candidate | _cmd_hooks | `Python hooks: {hook_path} ⏎ Trusted: {py_status.get('trusted', False)} ⏎ Reason: {py_status.get('reason', '')} ⏎ Config hooks: {paths.HOOKS_FILE} ⏎ Trusted: {config_status.get('trusted', False)} ⏎ Reason: {config_status.get('reason', '')} ⏎ Loaded functions: {', '.join(info.get('python_hooks', )) or '(none)'} ⏎ Configured argv hooks: {info.get('shell_hooks', 0)}` |
| [18486](/root/laintas_cli/laintas_cli.py:18486) | title/menu/help | _cmd_hooks / title | `Hooks` |
| [18495](/root/laintas_cli/laintas_cli.py:18495) | output | _cmd_hooks / 0 | `No hooks.py or hooks.json exists.` |
| [18519](/root/laintas_cli/laintas_cli.py:18519) | output | _cmd_hooks / 0 | `Trusted {len(trusted)} hook file(s) at their current hashes.` |
| [18521](/root/laintas_cli/laintas_cli.py:18521) | output | _cmd_hooks / 0 | `Hooks not trusted.` |
| [18526](/root/laintas_cli/laintas_cli.py:18526) | output | _cmd_hooks / 0 | `Hook trust revoked.` |
| [18526](/root/laintas_cli/laintas_cli.py:18526) | output | _cmd_hooks / 0 | `Hooks were not trusted.` |
| [18532](/root/laintas_cli/laintas_cli.py:18532) | output | _cmd_hooks / 0 | `Usage: /hooks [status\|trust\|revoke\|reload]` |
| [18551](/root/laintas_cli/laintas_cli.py:18551) | title/menu/help | _cmd_policy / title | `Select Security Policy` |
| [18564](/root/laintas_cli/laintas_cli.py:18564) | output | _cmd_policy / 0 | `Disabling policy in a non-interactive shell requires /policy disabled --yes.` |
| [18573](/root/laintas_cli/laintas_cli.py:18573) | output | _cmd_policy / 0 | `Policy change cancelled.` |
| [18577](/root/laintas_cli/laintas_cli.py:18577) | output | _cmd_policy / 0 | `{msg}` |
| [18580](/root/laintas_cli/laintas_cli.py:18580) | output | _cmd_policy / 0 | `Commands matching approval rules will now prompt before execution.` |
| [18582](/root/laintas_cli/laintas_cli.py:18582) | output | _cmd_policy / 0 | `⚠ All policy checks bypassed — commands run without approval.` |
| [18584](/root/laintas_cli/laintas_cli.py:18584) | output | _cmd_policy / 0 | `Policy advisory only — deny rules still block, approvals are advisory.` |
| [18587](/root/laintas_cli/laintas_cli.py:18587) | output | _cmd_policy / 0 | `Session auto-approvals cleared.` |
| [18592](/root/laintas_cli/laintas_cli.py:18592) | output | _cmd_policy / 0 | `Usage: /policy [status\|audit\|enforce\|disabled [--yes]\|reset]` |
| [18601](/root/laintas_cli/laintas_cli.py:18601) | resolved output candidate | _cmd_policy | `Mode:           {_mode} ⏎ Allow rules:    {_n_allow} ⏎ Deny rules:     {_n_deny} ⏎ Approval rules: {_n_appr} ⏎ Config file:    {_pol_cmd.CONFIG_PATH} ⏎  ⏎ Session auto-approve:  commands={'on' if _session_approval_state['all_commands'] else 'off'}  writes={'on' if _session_approval_state['all_writes'] else 'off'} ⏎  ⏎ Run /policy to choose a mode, or pass audit/enforce/disabled directly. ⏎ Reset session approvals:  /policy reset` |
| [18612](/root/laintas_cli/laintas_cli.py:18612) | title/menu/help | _cmd_policy / title | `Security Policy` |
| [18650](/root/laintas_cli/laintas_cli.py:18650) | title/menu/help | show_plan_picker.delete / message | `Exit or approve the active plan before deleting it.` |
| [18656](/root/laintas_cli/laintas_cli.py:18656) | title/menu/help | show_plan_picker.delete / message | `Press d again to delete {item.payload.get('name', item.title)}` |
| [18662](/root/laintas_cli/laintas_cli.py:18662) | title/menu/help | show_plan_picker.delete / message | `Could not delete: {exc}` |
| [18665](/root/laintas_cli/laintas_cli.py:18665) | title/menu/help | show_plan_picker.delete / message | `Deleted {item.payload.get('name', item.title)}` |
| [18671](/root/laintas_cli/laintas_cli.py:18671) | title/menu/help | show_plan_picker.leave / message | `Exited plan mode; the draft remains saved.` |
| [18671](/root/laintas_cli/laintas_cli.py:18671) | title/menu/help | show_plan_picker.leave / message | `No active plan to exit.` |
| [18687](/root/laintas_cli/laintas_cli.py:18687) | title/menu/help | show_plan_picker / title | `Plans` |
| [18699](/root/laintas_cli/laintas_cli.py:18699) | output | show_plan_picker / 0 | `Approve or exit the active plan first.` |
| [18702](/root/laintas_cli/laintas_cli.py:18702) | status/prompt | show_plan_picker | `Task description: ` |
| [18725](/root/laintas_cli/laintas_cli.py:18725) | output | _cmd_plan / 0 | `A plan is already active. Approve or exit it before starting another.` |
| [18731](/root/laintas_cli/laintas_cli.py:18731) | resolved output candidate | _cmd_plan | `Plan Mode: ENTERED ⏎  ⏎ Task: {task} ⏎ Plan file: {plan['file']} ⏎  ⏎ The AI will now explore and design — no code will be executed. ⏎ When the plan is ready, run /plan approve.` |
| [18737](/root/laintas_cli/laintas_cli.py:18737) | title/menu/help | _cmd_plan / title | `Plan Mode` |
| [18749](/root/laintas_cli/laintas_cli.py:18749) | resolved output candidate | _cmd_plan | `Plan APPROVED ⏎  ⏎ File: {plan['file']} ⏎  ⏎ Execution request queued.` |
| [18749](/root/laintas_cli/laintas_cli.py:18749) | resolved output candidate | _cmd_plan | `Plan APPROVED ⏎  ⏎ File: {plan['file']} ⏎  ⏎ Could not queue execution; submit a task referencing this plan.` |
| [18754](/root/laintas_cli/laintas_cli.py:18754) | title/menu/help | _cmd_plan / title | `Plan Approved` |
| [18758](/root/laintas_cli/laintas_cli.py:18758) | output | _cmd_plan / 0 | `Plan was not approved.` |
| [18762](/root/laintas_cli/laintas_cli.py:18762) | output | _cmd_plan / 0 | `Plan is not ready to submit.` |
| [18771](/root/laintas_cli/laintas_cli.py:18771) | output | _cmd_plan / 0 | `Usage: /plan revise <feedback>` |
| [18781](/root/laintas_cli/laintas_cli.py:18781) | output | _cmd_plan / 0 | `Revision feedback queued for the planning AI.` |
| [18781](/root/laintas_cli/laintas_cli.py:18781) | output | _cmd_plan / 0 | `Could not queue revision feedback.` |
| [18786](/root/laintas_cli/laintas_cli.py:18786) | output | _cmd_plan / 0 | `Exited plan mode; the draft remains in the plans directory.` |
| [18788](/root/laintas_cli/laintas_cli.py:18788) | output | _cmd_plan / 0 | `No active plan to exit.` |
| [18795](/root/laintas_cli/laintas_cli.py:18795) | output | _cmd_plan / 0 | `Not in plan mode.` |
| [18799](/root/laintas_cli/laintas_cli.py:18799) | output | _cmd_plan / 0 | `Saved Plans:` |
| [18801](/root/laintas_cli/laintas_cli.py:18801) | output | _cmd_plan / 0 | `  {p['name']} — {p['title'][:80]}` |
| [18803](/root/laintas_cli/laintas_cli.py:18803) | output | _cmd_plan / 0 | `No saved plans.` |
| [18805](/root/laintas_cli/laintas_cli.py:18805) | output | _cmd_plan / 0 | `Usage: ⏎   /plan enter <task> — Enter plan mode ⏎   /plan submit       — Submit immutable revision for review ⏎   /plan revise <feedback> — Ask AI to revise ⏎   /plan approve      — Approve and execute ⏎   /plan exit         — Exit without approving ⏎   /plan status       — Show current plan ⏎   /plan list         — List saved plans` |
| [18831](/root/laintas_cli/laintas_cli.py:18831) | resolved output candidate | _cmd_evolve | `Evolution Lab branch created ⏎  ⏎ Branch: {branch['id']} ⏎ Intent: {branch.get('intent')} ⏎ Status: {branch.get('status')} ⏎ Idea: {branch.get('description')} ⏎ Worker: {worker}` |
| [18831](/root/laintas_cli/laintas_cli.py:18831) | resolved output candidate | _cmd_evolve | `Evolution Lab branch created ⏎  ⏎ Branch: {branch['id']} ⏎ Intent: {branch.get('intent')} ⏎ Status: {branch.get('status')} ⏎ Idea: {branch.get('description')} ⏎ Snapshot saved; no active parent agent was available.` |
| [18839](/root/laintas_cli/laintas_cli.py:18839) | title/menu/help | _cmd_evolve / title | `Evolution Lab` |
| [18845](/root/laintas_cli/laintas_cli.py:18845) | output | _cmd_evolve / 0 | `No active Evolution Lab branch.` |
| [18847](/root/laintas_cli/laintas_cli.py:18847) | resolved output candidate | _cmd_evolve | `Branch: {branch.get('id')} ⏎ Intent: {branch.get('intent')} ⏎ Status: {branch.get('status')} ⏎ Candidate: {branch.get('candidate_id') or '(none)'} ⏎ Profile: {profile.get('name', 'default')} ⏎ Loaded extensions: {len(runtime.list())}` |
| [18853](/root/laintas_cli/laintas_cli.py:18853) | title/menu/help | _cmd_evolve / title | `Evolution Lab Status` |
| [18857](/root/laintas_cli/laintas_cli.py:18857) | output | _cmd_evolve / 0 | `  {branch.get('id')}  {branch.get('status')} — {str(branch.get('description') or '')[:80]}` |
| [18865](/root/laintas_cli/laintas_cli.py:18865) | title/menu/help | _cmd_evolve / title | `Open Evolution Branch` |
| [18874](/root/laintas_cli/laintas_cli.py:18874) | output | _cmd_evolve / 0 | `Branch selection cancelled.` |
| [18877](/root/laintas_cli/laintas_cli.py:18877) | output | _cmd_evolve / 0 | `[{'green' if ok else 'red'}]{message}[/{'green' if ok else 'red'}]` |
| [18882](/root/laintas_cli/laintas_cli.py:18882) | output | _cmd_evolve / 0 | `Usage: /evolve chat <refinement>` |
| [18886](/root/laintas_cli/laintas_cli.py:18886) | output | _cmd_evolve / 0 | `Refinement worker: {worker}` |
| [18886](/root/laintas_cli/laintas_cli.py:18886) | output | _cmd_evolve / 0 | `Could not start refinement worker.` |
| [18891](/root/laintas_cli/laintas_cli.py:18891) | output | _cmd_evolve / 0 | `No Evolution Lab candidates.` |
| [18894](/root/laintas_cli/laintas_cli.py:18894) | output | _cmd_evolve / 0 | `  {candidate.get('id')}  {candidate.get('status')} — {candidate.get('target_type')}:{candidate.get('name')}` |
| [18901](/root/laintas_cli/laintas_cli.py:18901) | output | _cmd_evolve / 0 | `Evolution candidate not found.` |
| [18917](/root/laintas_cli/laintas_cli.py:18917) | title/menu/help | _cmd_evolve / title | `Evolution Candidate` |
| [18921](/root/laintas_cli/laintas_cli.py:18921) | output | _cmd_evolve / 0 | `No candidate to test.` |
| [18932](/root/laintas_cli/laintas_cli.py:18932) | output | _cmd_evolve / 0 | `Evolution candidate not found.` |
| [18944](/root/laintas_cli/laintas_cli.py:18944) | output | _cmd_evolve / 0 | `Evolution activation cancelled.` |
| [18948](/root/laintas_cli/laintas_cli.py:18948) | output | _cmd_evolve / 0 | `[{'green' if ok else 'red'}]{message}[/{'green' if ok else 'red'}]` |
| [18954](/root/laintas_cli/laintas_cli.py:18954) | title/menu/help | _cmd_evolve / title | `Disable Extension` |
| [18962](/root/laintas_cli/laintas_cli.py:18962) | output | _cmd_evolve / 0 | `Extension selection cancelled.` |
| [18969](/root/laintas_cli/laintas_cli.py:18969) | output | _cmd_evolve / 0 | `[{'green' if ok else 'red'}]{message}[/{'green' if ok else 'red'}]` |
| [18974](/root/laintas_cli/laintas_cli.py:18974) | output | _cmd_evolve / 0 | `{marker} {profile.get('name')} {len(profile.get('extensions') or {})} extension(s)` |
| [18978](/root/laintas_cli/laintas_cli.py:18978) | output | _cmd_evolve / 0 | `Usage: /evolve profile create <name> [candidate-id ...]` |
| [18981](/root/laintas_cli/laintas_cli.py:18981) | output | _cmd_evolve / 0 | `[{'green' if ok else 'red'}]{message}[/{'green' if ok else 'red'}]` |
| [18987](/root/laintas_cli/laintas_cli.py:18987) | title/menu/help | _cmd_evolve / title | `Select Evolution Profile` |
| [18995](/root/laintas_cli/laintas_cli.py:18995) | output | _cmd_evolve / 0 | `Profile selection cancelled.` |
| [19002](/root/laintas_cli/laintas_cli.py:19002) | output | _cmd_evolve / 0 | `[{'green' if ok else 'red'}]{message}[/{'green' if ok else 'red'}]` |
| [19009](/root/laintas_cli/laintas_cli.py:19009) | output | _cmd_evolve / 0 | `[{'green' if ok else 'red'}]{message}[/{'green' if ok else 'red'}]` |
| [19012](/root/laintas_cli/laintas_cli.py:19012) | output | _cmd_evolve / 0 | `Evolution Lab ⏎   /evolve <idea> ⏎   /evolve status\|branches\|candidates ⏎   /evolve chat <refinement> ⏎   /evolve review\|test\|activate [id] ⏎   /evolve disable <extension> ⏎   /evolve profiles\|profile create\|use\|rollback` |
| [19041](/root/laintas_cli/laintas_cli.py:19041) | resolved output candidate | _cmd_prompt | `Prompt Lab branch created ⏎  ⏎ Branch: {branch['id']} ⏎ Status: {branch.get('status', 'CAPTURED')} ⏎ Issue: {_escape(branch.get('description', ''))} ⏎ Worker: {worker} ⏎  ⏎ The main task and active prompt are unchanged. Use /prompt status, /prompt chat <message>, and /prompt review.` |
| [19041](/root/laintas_cli/laintas_cli.py:19041) | resolved output candidate | _cmd_prompt | `Prompt Lab branch created ⏎  ⏎ Branch: {branch['id']} ⏎ Status: {branch.get('status', 'CAPTURED')} ⏎ Issue: {_escape(branch.get('description', ''))} ⏎ No active parent agent; snapshot saved without a worker. ⏎  ⏎ The main task and active prompt are unchanged. Use /prompt status, /prompt chat <message>, and /prompt review.` |
| [19050](/root/laintas_cli/laintas_cli.py:19050) | title/menu/help | _cmd_prompt / title | `Prompt Lab` |
| [19055](/root/laintas_cli/laintas_cli.py:19055) | output | _cmd_prompt / 0 | `No Prompt Lab branches in this project.` |
| [19057](/root/laintas_cli/laintas_cli.py:19057) | output | _cmd_prompt / 0 | `Prompt Lab branches` |
| [19060](/root/laintas_cli/laintas_cli.py:19060) | output | _cmd_prompt / 0 | `  {branch.get('id')} {branch.get('status')} — {_escape(str(branch.get('description') or '')[:80])}` |
| [19068](/root/laintas_cli/laintas_cli.py:19068) | title/menu/help | _cmd_prompt / title | `Open Prompt Lab Branch` |
| [19077](/root/laintas_cli/laintas_cli.py:19077) | output | _cmd_prompt / 0 | `Branch selection cancelled.` |
| [19080](/root/laintas_cli/laintas_cli.py:19080) | output | _cmd_prompt / 0 | `[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]` |
| [19085](/root/laintas_cli/laintas_cli.py:19085) | output | _cmd_prompt / 0 | `No active Prompt Lab branch. Run /prompt <issue> first.` |
| [19087](/root/laintas_cli/laintas_cli.py:19087) | output | _cmd_prompt / 0 | `Usage: /prompt chat <feedback or refinement>` |
| [19092](/root/laintas_cli/laintas_cli.py:19092) | output | _cmd_prompt / 0 | `Prompt Lab refinement worker started: {child_id}` |
| [19094](/root/laintas_cli/laintas_cli.py:19094) | output | _cmd_prompt / 0 | `Could not start refinement worker; no active parent agent.` |
| [19102](/root/laintas_cli/laintas_cli.py:19102) | resolved output candidate | _cmd_prompt | `Branch: {branch.get('id', '?')} ⏎ Status: {branch.get('status', '?')} ⏎ Candidate: {branch.get('candidate_patch_id') or '(none)'} ⏎ Tests: {test_status} ⏎ Active profile: {profile.get('name', 'default')} ⏎ Active overlays: {len(profile.get('patches') or )}` |
| [19109](/root/laintas_cli/laintas_cli.py:19109) | title/menu/help | _cmd_prompt / title | `Prompt Lab Status` |
| [19114](/root/laintas_cli/laintas_cli.py:19114) | output | _cmd_prompt / 0 | `No Prompt Lab patches in this project.` |
| [19116](/root/laintas_cli/laintas_cli.py:19116) | output | _cmd_prompt / 0 | `Prompt Lab patches` |
| [19119](/root/laintas_cli/laintas_cli.py:19119) | output | _cmd_prompt / 0 | `  {patch.get('id')} {patch.get('status')} — {_escape(str(patch.get('title') or ''))}` |
| [19130](/root/laintas_cli/laintas_cli.py:19130) | output | _cmd_prompt / 0 | `No Prompt Lab or legacy candidate found.` |
| [19135](/root/laintas_cli/laintas_cli.py:19135) | title/menu/help | _cmd_prompt / title | `Legacy Prompt Candidate` |
| [19150](/root/laintas_cli/laintas_cli.py:19150) | title/menu/help | _cmd_prompt / title | `Prompt Lab Review` |
| [19154](/root/laintas_cli/laintas_cli.py:19154) | output | _cmd_prompt / 0 | `No candidate patch. Wait for diagnosis or run /prompt chat.` |
| [19157](/root/laintas_cli/laintas_cli.py:19157) | output | _cmd_prompt / 0 | `[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]` |
| [19163](/root/laintas_cli/laintas_cli.py:19163) | output | _cmd_prompt / 0 | `Prompt Lab patch not found.` |
| [19168](/root/laintas_cli/laintas_cli.py:19168) | output | _cmd_prompt / 0 | `The latest regression run has not passed. Run /prompt test first, or explicitly use --force.` |
| [19172](/root/laintas_cli/laintas_cli.py:19172) | output | _cmd_prompt / 0 | `{_escape(preview)}` |
| [19183](/root/laintas_cli/laintas_cli.py:19183) | output | _cmd_prompt / 0 | `Prompt activation cancelled.` |
| [19186](/root/laintas_cli/laintas_cli.py:19186) | output | _cmd_prompt / 0 | `[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]` |
| [19196](/root/laintas_cli/laintas_cli.py:19196) | title/menu/help | _cmd_prompt / title | `Disable Prompt Overlay` |
| [19203](/root/laintas_cli/laintas_cli.py:19203) | output | _cmd_prompt / 0 | `Prompt overlay selection cancelled.` |
| [19212](/root/laintas_cli/laintas_cli.py:19212) | output | _cmd_prompt / 0 | `Prompt change cancelled.` |
| [19215](/root/laintas_cli/laintas_cli.py:19215) | output | _cmd_prompt / 0 | `[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]` |
| [19220](/root/laintas_cli/laintas_cli.py:19220) | output | _cmd_prompt / 0 | `{marker} {profile.get('name')} {len(profile.get('patches') or )} patch(es)` |
| [19224](/root/laintas_cli/laintas_cli.py:19224) | output | _cmd_prompt / 0 | `Usage: /prompt profile create <name> [patch-id ...]` |
| [19227](/root/laintas_cli/laintas_cli.py:19227) | output | _cmd_prompt / 0 | `[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]` |
| [19233](/root/laintas_cli/laintas_cli.py:19233) | title/menu/help | _cmd_prompt / title | `Select Prompt Profile` |
| [19241](/root/laintas_cli/laintas_cli.py:19241) | output | _cmd_prompt / 0 | `Profile selection cancelled.` |
| [19246](/root/laintas_cli/laintas_cli.py:19246) | output | _cmd_prompt / 0 | `Profile {profile_name} not found.` |
| [19254](/root/laintas_cli/laintas_cli.py:19254) | output | _cmd_prompt / 0 | `Profile switch cancelled.` |
| [19257](/root/laintas_cli/laintas_cli.py:19257) | output | _cmd_prompt / 0 | `[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]` |
| [19266](/root/laintas_cli/laintas_cli.py:19266) | output | _cmd_prompt / 0 | `Prompt rollback cancelled.` |
| [19269](/root/laintas_cli/laintas_cli.py:19269) | output | _cmd_prompt / 0 | `[{'green' if ok else 'red'}]{_escape(msg)}[/{'green' if ok else 'red'}]` |
| [19272](/root/laintas_cli/laintas_cli.py:19272) | output | _cmd_prompt / 0 | `Prompt Lab ⏎   /prompt [issue]                  Capture an incident and start diagnosis ⏎   /prompt chat <message>           Refine with AI in the active branch ⏎   /prompt status\|branches\|patches  Inspect project state ⏎   /prompt review [patch-id]        Review diagnosis, overlay, and tests ⏎   /prompt test [patch-id]          Run an isolated AI regression evaluation ⏎   /prompt activate [id] [--force]  Confirm, activate, and hot-reload ⏎   /prompt disable <id>             Confirm and disable an overlay ⏎   /prompt profiles\|use <name>      Manage switchable prompt profiles ⏎   /prompt rollback                 Confirm and undo latest prompt change` |
| [19290](/root/laintas_cli/laintas_cli.py:19290) | resolved output candidate | _cmd_prompt | `Feedback captured. Optimizer spawned in background. ⏎  ⏎ Feedback ID: {entry['id']} ⏎ Optimizer agent: {child_id} ⏎  ⏎ The main task continues uninterrupted. The candidate will ⏎ arrive via inbox when ready. Run /prompt status to check.` |
| [19296](/root/laintas_cli/laintas_cli.py:19296) | title/menu/help | _cmd_prompt / title | `Prompt Optimization` |
| [19298](/root/laintas_cli/laintas_cli.py:19298) | resolved output candidate | _cmd_prompt | `Feedback captured. (ID: {entry['id']}) ⏎  ⏎ Optimizer spawn failed — max depth may be reached. ⏎ Run /prompt optimize {entry['id']} later from the REPL.` |
| [19302](/root/laintas_cli/laintas_cli.py:19302) | title/menu/help | _cmd_prompt / title | `Prompt Optimization` |
| [19304](/root/laintas_cli/laintas_cli.py:19304) | resolved output candidate | _cmd_prompt | `Feedback captured. (ID: {entry['id']}) ⏎  ⏎ No active agent to spawn the optimizer from. Run ⏎ /prompt optimize {entry['id']} when an agent is active.` |
| [19308](/root/laintas_cli/laintas_cli.py:19308) | title/menu/help | _cmd_prompt / title | `Prompt Optimization` |
| [19313](/root/laintas_cli/laintas_cli.py:19313) | output | _cmd_prompt / 0 | `No active agent. /hire one first.` |
| [19317](/root/laintas_cli/laintas_cli.py:19317) | output | _cmd_prompt / 0 | `Optimizer spawned: {child_id}` |
| [19319](/root/laintas_cli/laintas_cli.py:19319) | output | _cmd_prompt / 0 | `Spawn failed (max depth reached?)` |
| [19324](/root/laintas_cli/laintas_cli.py:19324) | output | _cmd_prompt / 0 | `No active prompt optimization.` |
| [19329](/root/laintas_cli/laintas_cli.py:19329) | resolved output candidate | _cmd_prompt | `Status: {status} ⏎ Feedback: {fid} ⏎ Candidate: {cid}` |
| [19333](/root/laintas_cli/laintas_cli.py:19333) | title/menu/help | _cmd_prompt / title | `Prompt Optimization Status` |
| [19338](/root/laintas_cli/laintas_cli.py:19338) | output | _cmd_prompt / 0 | `No candidate found. Run /prompt feedback first.` |
| [19346](/root/laintas_cli/laintas_cli.py:19346) | title/menu/help | _cmd_prompt / title | `Prompt Candidate Review` |
| [19347](/root/laintas_cli/laintas_cli.py:19347) | output | _cmd_prompt / 0 | ` ⏎ Run /prompt apply to activate, /prompt discard to reject.` |
| [19353](/root/laintas_cli/laintas_cli.py:19353) | output | _cmd_prompt / 0 | `No legacy candidate found to apply.` |
| [19362](/root/laintas_cli/laintas_cli.py:19362) | output | _cmd_prompt / 0 | `Legacy prompt activation cancelled.` |
| [19366](/root/laintas_cli/laintas_cli.py:19366) | output | _cmd_prompt / 0 | `{msg}` |
| [19376](/root/laintas_cli/laintas_cli.py:19376) | output | _cmd_prompt / 0 | `Legacy prompt rollback cancelled.` |
| [19380](/root/laintas_cli/laintas_cli.py:19380) | output | _cmd_prompt / 0 | `{msg}` |
| [19384](/root/laintas_cli/laintas_cli.py:19384) | output | _cmd_prompt / 0 | `All candidates:` |
| [19390](/root/laintas_cli/laintas_cli.py:19390) | output | _cmd_prompt / 0 | `  {c['id']} {c['status']} {ctype}{extra} — {c.get('feedback', '')[:40]}` |
| [19395](/root/laintas_cli/laintas_cli.py:19395) | output | _cmd_prompt / 0 | `No candidates. Run /prompt feedback or /prompt fail to start.` |
| [19401](/root/laintas_cli/laintas_cli.py:19401) | output | _cmd_prompt / 0 | `{res}` |
| [19406](/root/laintas_cli/laintas_cli.py:19406) | output | _cmd_prompt / 0 | `{msg}` |
| [19411](/root/laintas_cli/laintas_cli.py:19411) | output | _cmd_prompt / 0 | `{msg}` |
| [19427](/root/laintas_cli/laintas_cli.py:19427) | output | _cmd_prompt / 0 | `Invalid failure-report JSON: {exc}` |
| [19428](/root/laintas_cli/laintas_cli.py:19428) | output | _cmd_prompt / 0 | `Use /prompt fail with no arguments for an interactive form.` |
| [19431](/root/laintas_cli/laintas_cli.py:19431) | output | _cmd_prompt / 0 | `Failure report must be a JSON object.` |
| [19439](/root/laintas_cli/laintas_cli.py:19439) | resolved output candidate | _cmd_prompt | `Enter a concise failure report. Task and actual behavior are required.` |
| [19441](/root/laintas_cli/laintas_cli.py:19441) | title/menu/help | _cmd_prompt / title | `Prompt Failure Report` |
| [19442](/root/laintas_cli/laintas_cli.py:19442) | status/prompt | _cmd_prompt | `Task: ` |
| [19443](/root/laintas_cli/laintas_cli.py:19443) | status/prompt | _cmd_prompt | `Expected behavior: ` |
| [19444](/root/laintas_cli/laintas_cli.py:19444) | status/prompt | _cmd_prompt | `Actual behavior: ` |
| [19448](/root/laintas_cli/laintas_cli.py:19448) | title/menu/help | _cmd_prompt / title | `Failure category` |
| [19455](/root/laintas_cli/laintas_cli.py:19455) | status/prompt | _cmd_prompt | `Minimal fix (optional): ` |
| [19456](/root/laintas_cli/laintas_cli.py:19456) | status/prompt | _cmd_prompt | `Regression tests (optional): ` |
| [19471](/root/laintas_cli/laintas_cli.py:19471) | title/menu/help | _cmd_prompt / title | `Failure Report Template (v3)` |
| [19474](/root/laintas_cli/laintas_cli.py:19474) | output | _cmd_prompt / 0 | `In a non-interactive shell pass a JSON object, for example: /prompt fail '{"task":"...","actual":"..."}'` |
| [19479](/root/laintas_cli/laintas_cli.py:19479) | output | _cmd_prompt / 0 | `Task or actual behavior is required; report not saved.` |
| [19482](/root/laintas_cli/laintas_cli.py:19482) | output | _cmd_prompt / 0 | `Invalid category: {fields.get('category')}` |
| [19483](/root/laintas_cli/laintas_cli.py:19483) | output | _cmd_prompt / 0 | `Use one of: {'; '.join(_po.FAILURE_CATEGORIES)}` |
| [19490](/root/laintas_cli/laintas_cli.py:19490) | resolved output candidate | _cmd_prompt | `Feedback ID: {entry['id']} ⏎ Optimizer: {child_id}` |
| [19490](/root/laintas_cli/laintas_cli.py:19490) | resolved output candidate | _cmd_prompt | `Feedback ID: {entry['id']} ⏎ Optimizer not started (no active agent).` |
| [19494](/root/laintas_cli/laintas_cli.py:19494) | title/menu/help | _cmd_prompt / title | `Failure report saved` |
| [19501](/root/laintas_cli/laintas_cli.py:19501) | output | _cmd_prompt / 0 | `Skill patches:` |
| [19504](/root/laintas_cli/laintas_cli.py:19504) | output | _cmd_prompt / 0 | `  {p['id']} {p['status']} — {p.get('skill_name', '?')}/{p.get('skill_file', '?')} ({p.get('mode', '?')})` |
| [19510](/root/laintas_cli/laintas_cli.py:19510) | output | _cmd_prompt / 0 | `No skill patches. Run /prompt fail to start diagnosis.` |
| [19516](/root/laintas_cli/laintas_cli.py:19516) | title/menu/help | _cmd_prompt / title | `Review Skill Patch` |
| [19526](/root/laintas_cli/laintas_cli.py:19526) | output | _cmd_prompt / 0 | `No skill patch found. Run /prompt skill list for ids.` |
| [19542](/root/laintas_cli/laintas_cli.py:19542) | title/menu/help | _cmd_prompt / title | `Skill Patch Review` |
| [19543](/root/laintas_cli/laintas_cli.py:19543) | output | _cmd_prompt / 0 | ` ⏎ Run /prompt skill apply <id> to activate, /prompt skill discard <id> to reject.` |
| [19550](/root/laintas_cli/laintas_cli.py:19550) | title/menu/help | _cmd_prompt / title | `Apply Skill Patch` |
| [19559](/root/laintas_cli/laintas_cli.py:19559) | output | _cmd_prompt / 0 | `Skill patch selection cancelled.` |
| [19572](/root/laintas_cli/laintas_cli.py:19572) | output | _cmd_prompt / 0 | `Skill patch activation cancelled.` |
| [19577](/root/laintas_cli/laintas_cli.py:19577) | output | _cmd_prompt / 0 | `{msg}` |
| [19583](/root/laintas_cli/laintas_cli.py:19583) | title/menu/help | _cmd_prompt / title | `Discard Skill Patch` |
| [19592](/root/laintas_cli/laintas_cli.py:19592) | output | _cmd_prompt / 0 | `Skill patch selection cancelled.` |
| [19598](/root/laintas_cli/laintas_cli.py:19598) | output | _cmd_prompt / 0 | `Skill patch rollback cancelled.` |
| [19602](/root/laintas_cli/laintas_cli.py:19602) | output | _cmd_prompt / 0 | `{msg}` |
| [19604](/root/laintas_cli/laintas_cli.py:19604) | output | _cmd_prompt / 0 | `Usage: ⏎   /prompt skill list            — List skill patches ⏎   /prompt skill review <id>     — Review a skill patch ⏎   /prompt skill apply <id> [--force] — Apply a skill patch ⏎   /prompt skill discard <id>    — Discard a skill patch` |
| [19610](/root/laintas_cli/laintas_cli.py:19610) | output | _cmd_prompt / 0 | `Usage: ⏎   /prompt feedback <desc>    — Capture feedback & spawn optimizer ⏎   /prompt fail                — Show failure template (v3) ⏎   /prompt optimize <id>       — Spawn optimizer for a feedback id ⏎   /prompt status              — Show optimization status ⏎   /prompt review [id]         — Review cli.prop candidate patch ⏎   /prompt apply [id] [--force] — Apply candidate to cli.prop ⏎   /prompt discard [id]        — Strip applied cli.prop patch ⏎   /prompt list                — List all candidates (cli.prop + skill) ⏎   /prompt skill list\|review\|apply\|discard <id> — Manage skill patches ⏎   /prompt export <id> [path]  — Export portable pack ⏎   /prompt install <path\|url>  — Import a shared pack ⏎   /prompt publish <id>        — Publish to community` |
| [19633](/root/laintas_cli/laintas_cli.py:19633) | output | _cmd_work / 0 | `No active WorkGraph in this project.` |
| [19637](/root/laintas_cli/laintas_cli.py:19637) | resolved output candidate | _cmd_work | `ID: {work['id']} ⏎ Objective: {work['objective']} ⏎ Status: {work['status']} ⏎ Revision: {work.get('current_revision') or 0} ⏎ Approved: {work.get('approved_revision') or '(none)'} ⏎ Workflow: {work.get('workflow_template') or '(none)'} / {work.get('workflow_phase') or '(none)'} ⏎ Steps: {done}/{len(steps)} complete` |
| [19646](/root/laintas_cli/laintas_cli.py:19646) | title/menu/help | _cmd_work / title | `Active WorkGraph` |
| [19650](/root/laintas_cli/laintas_cli.py:19650) | output | _cmd_work / 0 | `No WorkGraph history in this project.` |
| [19653](/root/laintas_cli/laintas_cli.py:19653) | output | _cmd_work / 0 | `  {item['id']} {item['status']} — {item['objective'][:100]}` |
| [19691](/root/laintas_cli/laintas_cli.py:19691) | title/menu/help | _cmd_work / title | `Resume Work` |
| [19702](/root/laintas_cli/laintas_cli.py:19702) | output | _cmd_work / 0 | `WorkGraph selection cancelled.` |
| [19706](/root/laintas_cli/laintas_cli.py:19706) | output | _cmd_work / 0 | `WorkGraph {work_id} not found.` |
| [19712](/root/laintas_cli/laintas_cli.py:19712) | output | _cmd_work / 0 | `Resumed WorkGraph {work['id']}.` |
| [19718](/root/laintas_cli/laintas_cli.py:19718) | output | _cmd_work / 0 | `No active WorkGraph.` |
| [19722](/root/laintas_cli/laintas_cli.py:19722) | output | _cmd_work / 0 | `  {event['id']} {event['event_type']} {json.dumps(event['payload'], ensure_ascii=False)[:120]}` |
| [19725](/root/laintas_cli/laintas_cli.py:19725) | output | _cmd_work / 0 | `Usage: /work [status\|list\|resume <id>\|history]` |
| [19751](/root/laintas_cli/laintas_cli.py:19751) | label/render | _task_ui_progress | ` {progress:>3}%` |
| [19810](/root/laintas_cli/laintas_cli.py:19810) | label/render | display_live_task_list | `Tasks` |
| [19811](/root/laintas_cli/laintas_cli.py:19811) | label/render | display_live_task_list | ` {symbols.BULLET} {agent_id or 'current'}` |
| [19812](/root/laintas_cli/laintas_cli.py:19812) | label/render | display_live_task_list | `  {completed}/{len(current)} done` |
| [19813](/root/laintas_cli/laintas_cli.py:19813) | resolved output candidate | display_live_task_list | `Tasks` |
| [19820](/root/laintas_cli/laintas_cli.py:19820) | label/render | display_live_task_list | `  {mark} ` |
| [19823](/root/laintas_cli/laintas_cli.py:19823) | label/render | display_live_task_list | `  {progress}%` |
| [19824](/root/laintas_cli/laintas_cli.py:19824) | resolved output candidate | display_live_task_list | `  {mark} ` |
| [19848](/root/laintas_cli/laintas_cli.py:19848) | label/render | display_live_retask | `Retask` |
| [19849](/root/laintas_cli/laintas_cli.py:19849) | label/render | display_live_retask | ` {symbols.BULLET} {summary.get('title', '')}` |
| [19850](/root/laintas_cli/laintas_cli.py:19850) | label/render | display_live_retask | `  {summary.get('progress', '')} done` |
| [19851](/root/laintas_cli/laintas_cli.py:19851) | resolved output candidate | display_live_retask | `Retask` |
| [19855](/root/laintas_cli/laintas_cli.py:19855) | label/render | display_live_retask | `  {mark} ` |
| [19857](/root/laintas_cli/laintas_cli.py:19857) | resolved output candidate | display_live_retask | `  {mark} ` |
| [19861](/root/laintas_cli/laintas_cli.py:19861) | label/render | display_live_retask | `      └ {note}` |
| [19861](/root/laintas_cli/laintas_cli.py:19861) | output | display_live_retask / 0 | `      └ {note}` |
| [19877](/root/laintas_cli/laintas_cli.py:19877) | label/render | _render_task_todolist | `Tasks` |
| [19878](/root/laintas_cli/laintas_cli.py:19878) | label/render | _render_task_todolist | `  {cwd}` |
| [19879](/root/laintas_cli/laintas_cli.py:19879) | resolved output candidate | _render_task_todolist | `Tasks` |
| [19881](/root/laintas_cli/laintas_cli.py:19881) | label/render | _render_task_todolist | `{counts['in_progress']} active` |
| [19882](/root/laintas_cli/laintas_cli.py:19882) | label/render | _render_task_todolist | `  {symbols.BULLET}  ` |
| [19883](/root/laintas_cli/laintas_cli.py:19883) | label/render | _render_task_todolist | `{counts['pending']} pending` |
| [19885](/root/laintas_cli/laintas_cli.py:19885) | label/render | _render_task_todolist | `  {symbols.BULLET}  ` |
| [19886](/root/laintas_cli/laintas_cli.py:19886) | label/render | _render_task_todolist | `{counts['blocked']} blocked` |
| [19887](/root/laintas_cli/laintas_cli.py:19887) | label/render | _render_task_todolist | `  {symbols.BULLET}  ` |
| [19888](/root/laintas_cli/laintas_cli.py:19888) | label/render | _render_task_todolist | `{counts['completed']} done` |
| [19896](/root/laintas_cli/laintas_cli.py:19896) | label/render | _render_task_todolist | `ID` |
| [19897](/root/laintas_cli/laintas_cli.py:19897) | label/render | _render_task_todolist | `TYPE` |
| [19898](/root/laintas_cli/laintas_cli.py:19898) | label/render | _render_task_todolist | `TASK` |
| [19899](/root/laintas_cli/laintas_cli.py:19899) | label/render | _render_task_todolist | `PROGRESS` |
| [19921](/root/laintas_cli/laintas_cli.py:19921) | label/render | _render_task_todolist | `↳ ` |
| [19924](/root/laintas_cli/laintas_cli.py:19924) | label/render | _render_task_todolist | `  blocked by {', '.join(blocked)}` |
| [19983](/root/laintas_cli/laintas_cli.py:19983) | label/render | _render_task_agent_tree | `Tasks` |
| [19985](/root/laintas_cli/laintas_cli.py:19985) | label/render | _render_task_agent_tree | ` {symbols.BULLET} session {session_id[:12]}` |
| [19987](/root/laintas_cli/laintas_cli.py:19987) | label/render | _render_task_agent_tree | ` {symbols.BULLET} current {current_id}` |
| [19988](/root/laintas_cli/laintas_cli.py:19988) | label/render | _render_task_agent_tree | `  {cwd}` |
| [19989](/root/laintas_cli/laintas_cli.py:19989) | resolved output candidate | _render_task_agent_tree | `Tasks` |
| [19991](/root/laintas_cli/laintas_cli.py:19991) | output | _render_task_agent_tree / 0 | `{counts['in_progress']} active  {symbols.BULLET}  {counts['pending']} pending  {symbols.BULLET}  {counts['blocked']} blocked  {symbols.BULLET}  {counts['completed']} done` |
| [19998](/root/laintas_cli/laintas_cli.py:19998) | output | _render_task_agent_tree / 0 | `No tasks in this session and agent tree.` |
| [20004](/root/laintas_cli/laintas_cli.py:20004) | label/render | _render_task_agent_tree | `AGENT` |
| [20006](/root/laintas_cli/laintas_cli.py:20006) | label/render | _render_task_agent_tree | `ID` |
| [20007](/root/laintas_cli/laintas_cli.py:20007) | label/render | _render_task_agent_tree | `TYPE` |
| [20008](/root/laintas_cli/laintas_cli.py:20008) | label/render | _render_task_agent_tree | `TASK` |
| [20009](/root/laintas_cli/laintas_cli.py:20009) | label/render | _render_task_agent_tree | `PROGRESS` |
| [20133](/root/laintas_cli/laintas_cli.py:20133) | title/menu/help | _cmd_task._task_manager.load_items / subtitle | `{task.get('owner_agent_id') or 'unowned'}  •  {task.get('progress', 0)}%` |
| [20163](/root/laintas_cli/laintas_cli.py:20163) | title/menu/help | _cmd_task._task_manager.update / message | `Updated {task.get('id')}: {task.get('subject')}` |
| [20174](/root/laintas_cli/laintas_cli.py:20174) | title/menu/help | _cmd_task._task_manager.delete / message | `Press x again to delete task {item.key}` |
| [20180](/root/laintas_cli/laintas_cli.py:20180) | title/menu/help | _cmd_task._task_manager / title | `Tasks` |
| [20202](/root/laintas_cli/laintas_cli.py:20202) | output | _cmd_task / 0 | `No tasks. Use /task add <subject> to create one.` |
| [20219](/root/laintas_cli/laintas_cli.py:20219) | output | _cmd_task / 0 | `Agent must be the current agent or one of its descendants.` |
| [20232](/root/laintas_cli/laintas_cli.py:20232) | output | _cmd_task / 0 | `Usage: /task add <subject>` |
| [20238](/root/laintas_cli/laintas_cli.py:20238) | output | _cmd_task / 0 | `Created task {_tk['id']}: {subject}` |
| [20246](/root/laintas_cli/laintas_cli.py:20246) | output | _cmd_task / 0 | `Task selection cancelled.` |
| [20251](/root/laintas_cli/laintas_cli.py:20251) | output | _cmd_task / 0 | `Task '{task_id}' not found.` |
| [20254](/root/laintas_cli/laintas_cli.py:20254) | resolved output candidate | _cmd_task | `Status: {_tk.get('status', 'pending')} ⏎ Progress: {_tk.get('progress', 0)}% ⏎ Blocked by: {', '.join(_tk.get('blockedBy', )) or '(none)'} ⏎ Blocks: {', '.join(_tk.get('blocks', )) or '(none)'} ⏎  ⏎ {_tk.get('description', '') or '(no description)'} ⏎  ⏎ Notes ⏎ {notes}` |
| [20261](/root/laintas_cli/laintas_cli.py:20261) | title/menu/help | _cmd_task / title | `Task {_tk.get('id')}: {_tk.get('subject', '(untitled)')}` |
| [20271](/root/laintas_cli/laintas_cli.py:20271) | output | _cmd_task / 0 | `Task selection cancelled.` |
| [20275](/root/laintas_cli/laintas_cli.py:20275) | output | _cmd_task / 0 | `Started task {_tk['id']}: {_tk['subject']}` |
| [20277](/root/laintas_cli/laintas_cli.py:20277) | output | _cmd_task / 0 | `{msg}` |
| [20285](/root/laintas_cli/laintas_cli.py:20285) | output | _cmd_task / 0 | `Task selection cancelled.` |
| [20290](/root/laintas_cli/laintas_cli.py:20290) | output | _cmd_task / 0 | `Completed task {_tk['id']}: {_tk['subject']}` |
| [20292](/root/laintas_cli/laintas_cli.py:20292) | output | _cmd_task / 0 | `{msg}` |
| [20300](/root/laintas_cli/laintas_cli.py:20300) | output | _cmd_task / 0 | `Task selection cancelled.` |
| [20304](/root/laintas_cli/laintas_cli.py:20304) | output | _cmd_task / 0 | `Deleted task {_tk['id']}: {_tk['subject']}` |
| [20306](/root/laintas_cli/laintas_cli.py:20306) | output | _cmd_task / 0 | `{msg}` |
| [20316](/root/laintas_cli/laintas_cli.py:20316) | status/prompt | _cmd_task | `Progress (0-100): ` |
| [20320](/root/laintas_cli/laintas_cli.py:20320) | output | _cmd_task / 0 | `Progress update cancelled.` |
| [20326](/root/laintas_cli/laintas_cli.py:20326) | output | _cmd_task / 0 | `Task {_tk['id']} progress: {_tk.get('progress', 0)}%` |
| [20328](/root/laintas_cli/laintas_cli.py:20328) | output | _cmd_task / 0 | `{msg}` |
| [20338](/root/laintas_cli/laintas_cli.py:20338) | status/prompt | _cmd_task | `Note: ` |
| [20342](/root/laintas_cli/laintas_cli.py:20342) | output | _cmd_task / 0 | `Task note cancelled.` |
| [20346](/root/laintas_cli/laintas_cli.py:20346) | output | _cmd_task / 0 | `Note added to task {_tk['id']}.` |
| [20348](/root/laintas_cli/laintas_cli.py:20348) | output | _cmd_task / 0 | `{msg}` |
| [20358](/root/laintas_cli/laintas_cli.py:20358) | status/prompt | _cmd_task | `Subtask subject: ` |
| [20362](/root/laintas_cli/laintas_cli.py:20362) | output | _cmd_task / 0 | `Subtask creation cancelled.` |
| [20368](/root/laintas_cli/laintas_cli.py:20368) | output | _cmd_task / 0 | `Created subtask {child_id} under task {_tk['id']}.` |
| [20370](/root/laintas_cli/laintas_cli.py:20370) | output | _cmd_task / 0 | `{msg}` |
| [20373](/root/laintas_cli/laintas_cli.py:20373) | output | _cmd_task / 0 | `Usage: /task [list\|mine\|agent\|add\|show\|start\|done\|del\|progress\|note\|subtask] ⏎   /task               — current agent + descendants ⏎   /task mine          — current agent only ⏎   /task agent <id>    — one descendant agent ⏎   /task add <subject> — create a task ⏎   /task show <id>      — show task details ⏎   /task start <id>    — mark as in_progress ⏎   /task done <id>     — mark as completed ⏎   /task progress <id> <n> — update progress ⏎   /task note <id> <text>  — append a note ⏎   /task subtask <id> <subject> — create child task ⏎   /task del <id>      — delete a task` |
| [20404](/root/laintas_cli/laintas_cli.py:20404) | title/menu/help | _cmd_workflow / title | `Select Workflow Template` |
| [20412](/root/laintas_cli/laintas_cli.py:20412) | status/prompt | _cmd_workflow | `Workflow objective: ` |
| [20416](/root/laintas_cli/laintas_cli.py:20416) | output | _cmd_workflow / 0 | `Workflow creation cancelled. You can also use /workflow start [--replace] <name> "<description>".` |
| [20423](/root/laintas_cli/laintas_cli.py:20423) | output | _cmd_workflow / 0 | `Unknown workflow: {wf_name}` |
| [20424](/root/laintas_cli/laintas_cli.py:20424) | output | _cmd_workflow / 0 | `Available: {', '.join(_we.list_workflow_templates())}` |
| [20427](/root/laintas_cli/laintas_cli.py:20427) | output | _cmd_workflow / 0 | `Workflow '{existing.name}' is already active. End it first or pass --replace.` |
| [20432](/root/laintas_cli/laintas_cli.py:20432) | resolved output candidate | _cmd_workflow | `Workflow Started: {wf_name} ⏎  ⏎ Task: {wf_desc} ⏎ Phases: {len(wf.phases)} ⏎ Progress: {wf.progress_str} ⏎  ⏎ Current phase: {cur_name}` |
| [20438](/root/laintas_cli/laintas_cli.py:20438) | title/menu/help | _cmd_workflow / title | `Workflow` |
| [20444](/root/laintas_cli/laintas_cli.py:20444) | output | _cmd_workflow / 0 | `No active workflow.` |
| [20445](/root/laintas_cli/laintas_cli.py:20445) | output | _cmd_workflow / 0 | `Start one with: /workflow start <name> "description"` |
| [20446](/root/laintas_cli/laintas_cli.py:20446) | output | _cmd_workflow / 0 | `Available: {', '.join(_we.list_workflow_templates())}` |
| [20463](/root/laintas_cli/laintas_cli.py:20463) | resolved output candidate | _cmd_workflow | `{wf.name} — {wf.description} ⏎  ⏎ Progress: {wf.progress_str}{phase_info}{history_text}` |
| [20466](/root/laintas_cli/laintas_cli.py:20466) | title/menu/help | _cmd_workflow / title | `Active Workflow` |
| [20475](/root/laintas_cli/laintas_cli.py:20475) | output | _cmd_workflow / 0 | `{exc}` |
| [20480](/root/laintas_cli/laintas_cli.py:20480) | output | _cmd_workflow / 0 | `Workflow '{wf.name}' completed!` |
| [20482](/root/laintas_cli/laintas_cli.py:20482) | output | _cmd_workflow / 0 | `No active workflow or already completed.` |
| [20484](/root/laintas_cli/laintas_cli.py:20484) | output | _cmd_workflow / 0 | `Advanced to phase: {new_phase.name} — {new_phase.description}` |
| [20490](/root/laintas_cli/laintas_cli.py:20490) | output | _cmd_workflow / 0 | `No active workflow to approve.` |
| [20495](/root/laintas_cli/laintas_cli.py:20495) | output | _cmd_workflow / 0 | `{exc}` |
| [20498](/root/laintas_cli/laintas_cli.py:20498) | output | _cmd_workflow / 0 | `Workflow '{wf.name}' completed.` |
| [20501](/root/laintas_cli/laintas_cli.py:20501) | output | _cmd_workflow / 0 | `Approved; advanced to {new_phase.name} — {new_phase.description}` |
| [20509](/root/laintas_cli/laintas_cli.py:20509) | output | _cmd_workflow / 0 | `Workflow '{wf.name}' ended.` |
| [20511](/root/laintas_cli/laintas_cli.py:20511) | output | _cmd_workflow / 0 | `No active workflow.` |
| [20514](/root/laintas_cli/laintas_cli.py:20514) | output | _cmd_workflow / 0 | `Available workflow templates:` |
| [20516](/root/laintas_cli/laintas_cli.py:20516) | output | _cmd_workflow / 0 | `  {t}` |
| [20518](/root/laintas_cli/laintas_cli.py:20518) | output | _cmd_workflow / 0 | `Usage: ⏎   /workflow start <name> "<desc>" — Start a workflow ⏎   /workflow status                — Show current workflow ⏎   /workflow advance [summary]    — Advance to next phase ⏎   /workflow approve [summary]    — Confirm a gated phase ⏎   /workflow end [summary]        — End workflow ⏎   /workflow list                  — List available workflows` |
| [20533](/root/laintas_cli/laintas_cli.py:20533) | output | _cmd_debug / 0 | `Debug log cleared.` |
| [20539](/root/laintas_cli/laintas_cli.py:20539) | error candidate | _cmd_debug | `N must be greater than 0` |
| [20543](/root/laintas_cli/laintas_cli.py:20543) | error candidate | _cmd_debug | `unexpected argument {unexpected[0]!r}` |
| [20547](/root/laintas_cli/laintas_cli.py:20547) | output | _cmd_debug / 0 | `No debug entries to save.` |
| [20592](/root/laintas_cli/laintas_cli.py:20592) | output | _cmd_debug / 0 | `Could not save debug entries to {filepath}: {exc}` |
| [20595](/root/laintas_cli/laintas_cli.py:20595) | output | _cmd_debug / 0 | `Saved {len(entries)} debug entr{'y' if len(entries) == 1 else 'ies'} to {filepath.absolute()}` |
| [20598](/root/laintas_cli/laintas_cli.py:20598) | output | _cmd_debug / 0 | `Raw export may contain credentials or private content.` |
| [20600](/root/laintas_cli/laintas_cli.py:20600) | output | _cmd_debug / 0 | `Common credential fields were redacted. Use --raw only when necessary.` |
| [20602](/root/laintas_cli/laintas_cli.py:20602) | output | _cmd_debug / 0 | `Error: {exc}` |
| [20603](/root/laintas_cli/laintas_cli.py:20603) | output | _cmd_debug / 0 | `Usage: /debug <N> <filename>` |
| [20609](/root/laintas_cli/laintas_cli.py:20609) | output | _cmd_debug / 0 | `Invalid entry number: {sub}` |
| [20610](/root/laintas_cli/laintas_cli.py:20610) | output | _cmd_debug / 0 | `Use /debug to browse entries. /debug <N> <file> to save to file.` |
| [20637](/root/laintas_cli/laintas_cli.py:20637) | output | _show_detail_trace_item / 0 | `AI` |
| [20642](/root/laintas_cli/laintas_cli.py:20642) | output | _show_detail_trace_item / 0 | `{escape(title)}` |
| [20664](/root/laintas_cli/laintas_cli.py:20664) | resolved output candidate | _show_detail_trace_item | `$ {command}` |
| [20669](/root/laintas_cli/laintas_cli.py:20669) | output | _show_detail_trace_item / 0 | `{symbols.ARROW_U}{symbols.ARROW_D}/wheel/PgUp/PgDn scroll{'  n/N changes' if change_rows else ''}  q/Esc/Enter back` |
| [20758](/root/laintas_cli/laintas_cli.py:20758) | output | _browse_detail_trace / 0 | `No conversations are available.` |
| [20762](/root/laintas_cli/laintas_cli.py:20762) | output | _browse_detail_trace / 0 | `Conversation #{conversation_number} does not exist (available: 1-{len(turns)}).` |
| [20768](/root/laintas_cli/laintas_cli.py:20768) | output | _browse_detail_trace / 0 | `Conversation #{conversation_number} has no detail trace. Use /detail on before starting a conversation.` |
| [20774](/root/laintas_cli/laintas_cli.py:20774) | output | _browse_detail_trace / 0 | `Conversation #{conversation_number} contains no recorded tool calls or AI output.` |
| [20793](/root/laintas_cli/laintas_cli.py:20793) | title/menu/help | _browse_detail_trace._load_items / subtitle | `Assistant output` |
| [20793](/root/laintas_cli/laintas_cli.py:20793) | title/menu/help | _browse_detail_trace._load_items / subtitle | `{trace.get('elapsed_seconds', 0):g}s · exit {trace.get('returncode')}` |
| [20793](/root/laintas_cli/laintas_cli.py:20793) | title/menu/help | _browse_detail_trace._load_items / subtitle | `{trace.get('elapsed_seconds', 0):g}s` |
| [20845](/root/laintas_cli/laintas_cli.py:20845) | title/menu/help | _browse_detail_trace / title | `Detail Trace #{conversation_number} · {prompt}` |
| [20857](/root/laintas_cli/laintas_cli.py:20857) | output | _cmd_detail / 0 | `Detail recording is {'on' if current else 'off'}. Use /detail on\|off or /detail trace [N].` |
| [20867](/root/laintas_cli/laintas_cli.py:20867) | output | _cmd_detail / 0 | `Detail recording {sub}. Live tool display is unchanged.` |
| [20875](/root/laintas_cli/laintas_cli.py:20875) | output | _cmd_detail / 0 | `Usage: /detail trace [N]` |
| [20880](/root/laintas_cli/laintas_cli.py:20880) | output | _cmd_detail / 0 | `Usage: /detail [on\|off\|trace [N]]` |
| [20888](/root/laintas_cli/laintas_cli.py:20888) | output | _cmd_stream / 0 | `Streaming preview is {escape(current)}. Use /stream off\|one\|detail.` |
| [20893](/root/laintas_cli/laintas_cli.py:20893) | output | _cmd_stream / 0 | `Usage: /stream [off\|one\|detail]` |
| [20902](/root/laintas_cli/laintas_cli.py:20902) | output | _cmd_stream / 0 | `Streaming preview: {description}.` |
| [20908](/root/laintas_cli/laintas_cli.py:20908) | output | _cmd_theme / 0 | `Theme is {escape(str(get_runtime_config('theme')))}. Use /theme dark\|light\|mono.` |
| [20913](/root/laintas_cli/laintas_cli.py:20913) | output | _cmd_theme / 0 | `Usage: /theme [dark\|light\|mono]` |
| [20921](/root/laintas_cli/laintas_cli.py:20921) | output | _cmd_theme / 0 | `Theme changed to {escape(name)}.` |
| [20935](/root/laintas_cli/laintas_cli.py:20935) | output | _cmd_why / 0 | `No matching tool failure is available in this process.` |
| [20939](/root/laintas_cli/laintas_cli.py:20939) | output | _cmd_why / 0 | `No failure #{index + 1}. There are {len(failures)} matching failures.` |
| [20963](/root/laintas_cli/laintas_cli.py:20963) | title/menu/help | _cmd_why / title | `Why the last tool failed` |
| [20978](/root/laintas_cli/laintas_cli.py:20978) | error candidate | _station_terminal_factory | `Terminal did not start.` |
| [20993](/root/laintas_cli/laintas_cli.py:20993) | output | _cmd_station / 0 | `No active manager.` |
| [21002](/root/laintas_cli/laintas_cli.py:21002) | output | _cmd_station / 0 | `Station: {len(agents)} agents, {len(terminals)} terminals` |
| [21004](/root/laintas_cli/laintas_cli.py:21004) | output | _cmd_station / 0 | `{agent.id}: {agent.status} · {agent.deployment or 'not stationed'}` |
| [21028](/root/laintas_cli/laintas_cli.py:21028) | output | _cmd_station / 0 | `Usage: /station <agent-id> [terminal] [--task <work>] or /station auto\|suggest --task <work>` |
| [21035](/root/laintas_cli/laintas_cli.py:21035) | output | _cmd_station / 0 | `Agent '{escape(target)}' not found.` |
| [21040](/root/laintas_cli/laintas_cli.py:21040) | output | _cmd_station / 0 | `Specify a terminal, or use --task for a private task terminal.` |
| [21054](/root/laintas_cli/laintas_cli.py:21054) | output | _cmd_station / 0 | `{decision.action}: {decision.reason}` |
| [21068](/root/laintas_cli/laintas_cli.py:21068) | title/menu/help | _cmd_terminate / title | `Terminate Terminal` |
| [21077](/root/laintas_cli/laintas_cli.py:21077) | output | _cmd_terminate / 0 | `Terminal selection cancelled.` |
| [21079](/root/laintas_cli/laintas_cli.py:21079) | output | _cmd_terminate / 0 | `term0 is owned by this CLI; use /exit to close it.` |
| [21086](/root/laintas_cli/laintas_cli.py:21086) | output | _cmd_terminate / 0 | `Released {name}; its execution CLI remains running.` |
| [21092](/root/laintas_cli/laintas_cli.py:21092) | output | _cmd_terminate / 0 | `Terminated {name} and its child resources; agents ended: {', '.join(terminated_agents)}` |
| [21096](/root/laintas_cli/laintas_cli.py:21096) | output | _cmd_terminate / 0 | `Terminated {name} and its child resources` |
| [21098](/root/laintas_cli/laintas_cli.py:21098) | output | _cmd_terminate / 0 | `Terminal '{name}' not found.` |
| [21112](/root/laintas_cli/laintas_cli.py:21112) | title/menu/help | _cmd_send / title | `Send Command to Terminal` |
| [21120](/root/laintas_cli/laintas_cli.py:21120) | status/prompt | _cmd_send | `Command: ` |
| [21127](/root/laintas_cli/laintas_cli.py:21127) | output | _cmd_send / 0 | `Usage: /send <name> [--wait <seconds>] <command>` |
| [21132](/root/laintas_cli/laintas_cli.py:21132) | output | _cmd_send / 0 | `--wait expects a number between 0 and 30 seconds.` |
| [21137](/root/laintas_cli/laintas_cli.py:21137) | output | _cmd_send / 0 | `Usage: /send <name> <command>` |
| [21141](/root/laintas_cli/laintas_cli.py:21141) | output | _cmd_send / 0 | `Terminal '{name}' not found.` |
| [21143](/root/laintas_cli/laintas_cli.py:21143) | output | _cmd_send / 0 | `Assign work through /station {name}/primary --task <work>. This linked terminal has no local shell input stream.` |
| [21146](/root/laintas_cli/laintas_cli.py:21146) | output | _cmd_send / 0 | `Terminal '{name}' has no active session.` |
| [21150](/root/laintas_cli/laintas_cli.py:21150) | output | _cmd_send / 0 | `{denial}` |
| [21156](/root/laintas_cli/laintas_cli.py:21156) | output | _cmd_send / 0 | `Sent to {name}: {cmd[:80]}` |
| [21159](/root/laintas_cli/laintas_cli.py:21159) | output | _cmd_send / 0 | `Waiting up to {wait_seconds:g}s for new output…` |
| [21168](/root/laintas_cli/laintas_cli.py:21168) | resolved output candidate | _cmd_send | `{preview}` |
| [21168](/root/laintas_cli/laintas_cli.py:21168) | resolved output candidate | _cmd_send | `{preview} ⏎ … {len(output) - 2000} earlier new chars omitted` |
| [21168](/root/laintas_cli/laintas_cli.py:21168) | title/menu/help | _cmd_send / title | `{name} new output` |
| [21170](/root/laintas_cli/laintas_cli.py:21170) | output | _cmd_send / 0 | `Sent asynchronously; use /term to inspect later output.` |
| [21172](/root/laintas_cli/laintas_cli.py:21172) | output | _cmd_send / 0 | `No new output arrived within the wait window.` |
| [21180](/root/laintas_cli/laintas_cli.py:21180) | output | _cmd_hire / 0 | `This terminal is controlled; hire from its controller or /term release first.` |
| [21186](/root/laintas_cli/laintas_cli.py:21186) | output | _cmd_hire / 0 | `Employee names may contain only letters, numbers, dot, underscore, and hyphen (max 64).` |
| [21190](/root/laintas_cli/laintas_cli.py:21190) | output | _cmd_hire / 0 | `Agent '{hire_name}' already exists.` |
| [21199](/root/laintas_cli/laintas_cli.py:21199) | output | _cmd_hire / 0 | `A newly hired agent cannot be deployed directly into the current terminal. Omit --terminal or choose a different terminal.` |
| [21208](/root/laintas_cli/laintas_cli.py:21208) | status/prompt | _cmd_hire | `Fetching available models… {symbols.BULLET} Esc/Ctrl+C cancel` |
| [21213](/root/laintas_cli/laintas_cli.py:21213) | output | _cmd_hire / 0 | `Hiring cancelled.` |
| [21216](/root/laintas_cli/laintas_cli.py:21216) | output | _cmd_hire / 0 | `Failed to fetch models: {exc}` |
| [21221](/root/laintas_cli/laintas_cli.py:21221) | output | _cmd_hire / 0 | `Interactive model selection is unavailable; use /hire <name> --model <model-id>.` |
| [21226](/root/laintas_cli/laintas_cli.py:21226) | output | _cmd_hire / 0 | `Hiring cancelled.` |
| [21234](/root/laintas_cli/laintas_cli.py:21234) | output | _cmd_hire / 0 | `Model '{base_model}' is not in the backend's current available model list.` |
| [21249](/root/laintas_cli/laintas_cli.py:21249) | output | _cmd_hire / 0 | `{exc}` |
| [21269](/root/laintas_cli/laintas_cli.py:21269) | output | _cmd_hire / 0 | `Could not deploy agent '{agent_info.id}' to terminal '{requested_terminal}'; it must be live and unoccupied.` |
| [21276](/root/laintas_cli/laintas_cli.py:21276) | title/menu/help | _cmd_hire / title | `Hired employee: {agent_info.name} → {deployment_text}` |
| [21281](/root/laintas_cli/laintas_cli.py:21281) | output | _cmd_hire / 0 | `Agent is deployed to {requested_terminal}. Start work with /station {agent_info.id} --task "..."` |
| [21285](/root/laintas_cli/laintas_cli.py:21285) | output | _cmd_hire / 0 | `Agent is undeployed. Start temporary isolated work with /station {agent_info.id} --task "...", or deploy explicitly with /station {agent_info.id} <terminal>.` |
| [21311](/root/laintas_cli/laintas_cli.py:21311) | error candidate | _resolve_agent_id_or_name | `Multiple agents match name '{token}': {', '.join((a.id for a in name_matches))}` |
| [21355](/root/laintas_cli/laintas_cli.py:21355) | output | _cmd_agent / 0 | `No agents registered.` |
| [21361](/root/laintas_cli/laintas_cli.py:21361) | output | _cmd_agent / 0 | `Current agent: {cur_label}` |
| [21363](/root/laintas_cli/laintas_cli.py:21363) | output | _cmd_agent / 0 | `No current agent.` |
| [21364](/root/laintas_cli/laintas_cli.py:21364) | output | _cmd_agent / 0 | `Switchable agents:` |
| [21378](/root/laintas_cli/laintas_cli.py:21378) | output | _cmd_agent / 0 | `  /agent {a.id}  {label}{suffix}` |
| [21380](/root/laintas_cli/laintas_cli.py:21380) | output | _cmd_agent / 0 | `  (no other agents)` |
| [21382](/root/laintas_cli/laintas_cli.py:21382) | output | _cmd_agent / 0 | `Use /agent <id-or-name> to switch. Use /agents for the full-screen view.` |
| [21390](/root/laintas_cli/laintas_cli.py:21390) | output | _cmd_agent / 0 | `{exc}` |
| [21393](/root/laintas_cli/laintas_cli.py:21393) | output | _cmd_agent / 0 | `No agent matches '{target}'.` |
| [21397](/root/laintas_cli/laintas_cli.py:21397) | output | _cmd_agent / 0 | `Available: {', '.join((a.id for a in agents))}` |
| [21404](/root/laintas_cli/laintas_cli.py:21404) | output | _cmd_agent / 0 | `Already focused on {label}.` |
| [21416](/root/laintas_cli/laintas_cli.py:21416) | output | _cmd_agent / 0 | `{escape(agent.id)} is working. Use /agents to view progress or send an update; switch after it finishes.` |
| [21424](/root/laintas_cli/laintas_cli.py:21424) | output | _cmd_agent / 0 | `Could not switch sessions: {escape(str(exc))}` |
| [21429](/root/laintas_cli/laintas_cli.py:21429) | output | _cmd_agent / 0 | `Could not switch to agent '{agent.id}'.` |
| [21441](/root/laintas_cli/laintas_cli.py:21441) | output | _cmd_agent / 0 | `Switched to agent {label}.` |
| [21448](/root/laintas_cli/laintas_cli.py:21448) | output | _cmd_agent / 0 | `Terminal ownership unchanged. This agent is stationed at '{escape(_terminal_display_name(term))}' (role={role}).` |
| [21452](/root/laintas_cli/laintas_cli.py:21452) | output | _cmd_agent / 0 | `Terminal ownership unchanged. This agent is undeployed (role={role}); direct commands still run in {escape(_terminal_display_name('term0'))}.` |
| [21466](/root/laintas_cli/laintas_cli.py:21466) | output | _cmd_agents_plain / 0 | `No agents.` |
| [21514](/root/laintas_cli/laintas_cli.py:21514) | output | _cmd_agents_plain / 0 | `── Primary ──` |
| [21517](/root/laintas_cli/laintas_cli.py:21517) | output | _cmd_agents_plain / 0 | `  {a.id}{np}{st_s}{inb}{ses}{marker}` |
| [21521](/root/laintas_cli/laintas_cli.py:21521) | output | _cmd_agents_plain / 0 | `── Pool ({idle_count} available) ──` |
| [21524](/root/laintas_cli/laintas_cli.py:21524) | output | _cmd_agents_plain / 0 | `  {a.id}{np}{st_s}{inb}{ses}{marker}` |
| [21526](/root/laintas_cli/laintas_cli.py:21526) | output | _cmd_agents_plain / 0 | `── Deployed ({len(buckets['deployed'])}) ──` |
| [21531](/root/laintas_cli/laintas_cli.py:21531) | output | _cmd_agents_plain / 0 | `  {a.id}{np} → {home} (parent={parent_term}){st_s}{inb}{ses}{marker}` |
| [21533](/root/laintas_cli/laintas_cli.py:21533) | output | _cmd_agents_plain / 0 | `── Subagents ({len(buckets['subagent'])}) ──` |
| [21537](/root/laintas_cli/laintas_cli.py:21537) | output | _cmd_agents_plain / 0 | `  {a.id}{np} (depth={a.depth}, parent={parent}){st_s}{inb}{marker}` |
| [21539](/root/laintas_cli/laintas_cli.py:21539) | output | _cmd_agents_plain / 0 | `── Other ──` |
| [21542](/root/laintas_cli/laintas_cli.py:21542) | output | _cmd_agents_plain / 0 | `  {a.id}{np}{st_s}{inb}{ses}{marker}` |
| [21555](/root/laintas_cli/laintas_cli.py:21555) | output | _cmd_agents_plain / 0 | `Agent '{escape(reference)}' not found or ambiguous. Use /agents --plain to list IDs.` |
| [21560](/root/laintas_cli/laintas_cli.py:21560) | title/menu/help | _cmd_agents_plain / title | `Employee: {agent.name}` |
| [21562](/root/laintas_cli/laintas_cli.py:21562) | output | _cmd_agents_plain / 0 | `Usage: /agents [tree\|agent-id\|--plain]` |
| [21583](/root/laintas_cli/laintas_cli.py:21583) | output | _submit_primary_runtime_task / 0 | `user_message` |
| [21586](/root/laintas_cli/laintas_cli.py:21586) | output | _submit_primary_runtime_task / 0 | `agent_started` |
| [21629](/root/laintas_cli/laintas_cli.py:21629) | output | _submit_primary_runtime_task.worker / 0 | `agent_aborted` |
| [21629](/root/laintas_cli/laintas_cli.py:21629) | output | _submit_primary_runtime_task.worker / 0 | `agent_error` |
| [21629](/root/laintas_cli/laintas_cli.py:21629) | output | _submit_primary_runtime_task.worker / 0 | `agent_done` |
| [21641](/root/laintas_cli/laintas_cli.py:21641) | output | _submit_primary_runtime_task.worker / 0 | `agent_aborted` |
| [21641](/root/laintas_cli/laintas_cli.py:21641) | output | _submit_primary_runtime_task.worker / 0 | `agent_error` |
| [21654](/root/laintas_cli/laintas_cli.py:21654) | output | _submit_primary_runtime_task / 0 | `agent_error` |
| [21672](/root/laintas_cli/laintas_cli.py:21672) | status/prompt | _attach_primary_runtime_view | `Thinking… {symbols.BULLET} primary runtime` |
| [21679](/root/laintas_cli/laintas_cli.py:21679) | status/prompt | _attach_primary_runtime_view | `Interrupting primary runtime…` |
| [21688](/root/laintas_cli/laintas_cli.py:21688) | output | _attach_primary_runtime_view / 0 | `{escape(agent.error)}` |
| [21790](/root/laintas_cli/laintas_cli.py:21790) | output | _open_agents_view / 0 | `/agents view is already open.` |
| [21851](/root/laintas_cli/laintas_cli.py:21851) | output | _open_agents_view._view / 0 | `Agents Mode failed: {type(exc).__name__}: {escape(str(exc))}` |
| [21853](/root/laintas_cli/laintas_cli.py:21853) | output | _open_agents_view._view / 0 | `Falling back to /agents --plain.` |
| [21866](/root/laintas_cli/laintas_cli.py:21866) | output | _open_agents_view / 0 | `Agents Mode failed: {type(exc).__name__}: {exc}` |
| [21867](/root/laintas_cli/laintas_cli.py:21867) | output | _open_agents_view / 0 | `Falling back to /agents --plain.` |
| [21879](/root/laintas_cli/laintas_cli.py:21879) | output | _cmd_agents / 0 | `Usage: /agents [tree\|agent-id\|--plain]` |
| [21901](/root/laintas_cli/laintas_cli.py:21901) | output | _cmd_agents / 0 | `Agent '{escape(requested)}' is not available in {escape(terminal_name)}.` |
| [21915](/root/laintas_cli/laintas_cli.py:21915) | output | _cmd_spawn / 0 | `Usage: /spawn [name:] <task...>` |
| [21924](/root/laintas_cli/laintas_cli.py:21924) | output | _cmd_spawn / 0 | `No current agent. /hire one first.` |
| [21933](/root/laintas_cli/laintas_cli.py:21933) | output | _cmd_spawn / 0 | `Spawn failed (parent={parent.id})` |
| [21935](/root/laintas_cli/laintas_cli.py:21935) | output | _cmd_spawn / 0 | `Spawned {child_id} (parent={parent.id}, task={task[:60]})` |
| [21945](/root/laintas_cli/laintas_cli.py:21945) | title/menu/help | _cmd_tell / title | `Send Message to Agent` |
| [21954](/root/laintas_cli/laintas_cli.py:21954) | status/prompt | _cmd_tell | `Message: ` |
| [21958](/root/laintas_cli/laintas_cli.py:21958) | output | _cmd_tell / 0 | `Agent message cancelled.` |
| [21977](/root/laintas_cli/laintas_cli.py:21977) | output | _cmd_tell / 0 | `→ {target_id}: {raw[:120]}` |
| [21979](/root/laintas_cli/laintas_cli.py:21979) | output | _cmd_tell / 0 | `Agent '{target_id}' not found or inbox full.` |
| [21992](/root/laintas_cli/laintas_cli.py:21992) | title/menu/help | _cmd_abort / title | `Abort Agent` |
| [22000](/root/laintas_cli/laintas_cli.py:22000) | output | _cmd_abort / 0 | `Agent selection cancelled.` |
| [22003](/root/laintas_cli/laintas_cli.py:22003) | output | _cmd_abort / 0 | `Abort signaled to {target_id}` |
| [22005](/root/laintas_cli/laintas_cli.py:22005) | output | _cmd_abort / 0 | `Agent '{target_id}' not found.` |
| [22013](/root/laintas_cli/laintas_cli.py:22013) | output | _cmd_tools / 0 | `No tools registered.` |
| [22016](/root/laintas_cli/laintas_cli.py:22016) | output | _cmd_tools / 0 | `{src}` |
| [22018](/root/laintas_cli/laintas_cli.py:22018) | output | _cmd_tools / 0 | `  {t.name} — {t.description}` |
| [22095](/root/laintas_cli/laintas_cli.py:22095) | title/menu/help | _cmd_tool / title | `Run Tool` |
| [22109](/root/laintas_cli/laintas_cli.py:22109) | output | _cmd_tool / 0 | `Tool selection cancelled.` |
| [22153](/root/laintas_cli/laintas_cli.py:22153) | output | _cmd_skill / 0 | `No skills in {skills_mod.SKILLS_DIR}` |
| [22154](/root/laintas_cli/laintas_cli.py:22154) | output | _cmd_skill / 0 | `Create one with: /skill new <name>` |
| [22160](/root/laintas_cli/laintas_cli.py:22160) | output | _cmd_skill / 0 | `{name} ({meta.dir_path})` |
| [22162](/root/laintas_cli/laintas_cli.py:22162) | output | _cmd_skill / 0 | `  {meta.description}` |
| [22164](/root/laintas_cli/laintas_cli.py:22164) | output | _cmd_skill / 0 | `  {t.name} — {t.description}` |
| [22166](/root/laintas_cli/laintas_cli.py:22166) | output | _cmd_skill / 0 | `  (standby/documentation-only)` |
| [22173](/root/laintas_cli/laintas_cli.py:22173) | title/menu/help | _cmd_skill / title | `{sub.title()} Skill` |
| [22180](/root/laintas_cli/laintas_cli.py:22180) | output | _cmd_skill / 0 | `Skill selection cancelled.` |
| [22184](/root/laintas_cli/laintas_cli.py:22184) | output | _cmd_skill / 0 | `Unknown skill: {skill_name}` |
| [22188](/root/laintas_cli/laintas_cli.py:22188) | output | _cmd_skill / 0 | `Documentation-only skills do not require executable trust.` |
| [22192](/root/laintas_cli/laintas_cli.py:22192) | output | _cmd_skill / 0 | `Skill trust revoked.` |
| [22192](/root/laintas_cli/laintas_cli.py:22192) | output | _cmd_skill / 0 | `Skill was not trusted.` |
| [22198](/root/laintas_cli/laintas_cli.py:22198) | output | _cmd_skill / 0 | `{manifest_error}` |
| [22218](/root/laintas_cli/laintas_cli.py:22218) | output | _cmd_skill / 0 | `Trusted {skill_name} at {trusted['sha256'][:16]}…` |
| [22221](/root/laintas_cli/laintas_cli.py:22221) | output | _cmd_skill / 0 | `Skill not trusted.` |
| [22227](/root/laintas_cli/laintas_cli.py:22227) | output | _cmd_skill / 0 | `No skills are loaded.` |
| [22229](/root/laintas_cli/laintas_cli.py:22229) | output | _cmd_skill / 0 | `[{'green' if ok else 'red'}]{msg}[/{'green' if ok else 'red'}]` |
| [22239](/root/laintas_cli/laintas_cli.py:22239) | title/menu/help | _cmd_skill / title | `{sub.title()} Skill` |
| [22246](/root/laintas_cli/laintas_cli.py:22246) | output | _cmd_skill / 0 | `Skill selection cancelled.` |
| [22250](/root/laintas_cli/laintas_cli.py:22250) | output | _cmd_skill / 0 | `[{'green' if ok else 'red'}]{msg}[/{'green' if ok else 'red'}]` |
| [22255](/root/laintas_cli/laintas_cli.py:22255) | output | _cmd_skill / 0 | `{msg}` |
| [22257](/root/laintas_cli/laintas_cli.py:22257) | output | _cmd_skill / 0 | `No skills in {skills_mod.SKILLS_DIR}` |
| [22260](/root/laintas_cli/laintas_cli.py:22260) | output | _cmd_skill / 0 | `Usage: /skill new <name>` |
| [22264](/root/laintas_cli/laintas_cli.py:22264) | output | _cmd_skill / 0 | `{msg}` |
| [22266](/root/laintas_cli/laintas_cli.py:22266) | output | _cmd_skill / 0 | `Edit skill.py and extension.json, then run /skill trust <name> and /skill load <name>` |
| [22270](/root/laintas_cli/laintas_cli.py:22270) | output | _cmd_skill / 0 | `Usage: /skill [manager\|list\|trust <name>\|revoke <name>\|load <name>\|unload <name>\|unload all\|reload\|new <name>\|dir]` |
| [22277](/root/laintas_cli/laintas_cli.py:22277) | output | _cmd_mcp / 0 | `mcp SDK not installed: {_get_mcp_mod().MCP_IMPORT_ERROR}` |
| [22278](/root/laintas_cli/laintas_cli.py:22278) | output | _cmd_mcp / 0 | `Install with:  pip install mcp` |
| [22301](/root/laintas_cli/laintas_cli.py:22301) | output | _cmd_mcp / 0 | `No servers in {_get_mcp_mod().CONFIG_PATH}` |
| [22302](/root/laintas_cli/laintas_cli.py:22302) | output | _cmd_mcp / 0 | `Create one with: /mcp init` |
| [22314](/root/laintas_cli/laintas_cli.py:22314) | output | _cmd_mcp / 0 | `  {name} {status} {cmd_str}` |
| [22316](/root/laintas_cli/laintas_cli.py:22316) | output | _cmd_mcp / 0 | `    {srv.last_error}` |
| [22322](/root/laintas_cli/laintas_cli.py:22322) | output | _cmd_mcp / 0 | `MCP server selection cancelled.` |
| [22326](/root/laintas_cli/laintas_cli.py:22326) | output | _cmd_mcp / 0 | `Unknown MCP server: {server_name}` |
| [22330](/root/laintas_cli/laintas_cli.py:22330) | output | _cmd_mcp / 0 | `MCP trust revoked.` |
| [22330](/root/laintas_cli/laintas_cli.py:22330) | output | _cmd_mcp / 0 | `MCP server was not trusted.` |
| [22353](/root/laintas_cli/laintas_cli.py:22353) | output | _cmd_mcp / 0 | `Trusted MCP server {server_name} at {trusted['sha256'][:16]}…` |
| [22356](/root/laintas_cli/laintas_cli.py:22356) | output | _cmd_mcp / 0 | `MCP server not trusted.` |
| [22362](/root/laintas_cli/laintas_cli.py:22362) | output | _cmd_mcp / 0 | `MCP server selection cancelled.` |
| [22367](/root/laintas_cli/laintas_cli.py:22367) | output | _cmd_mcp / 0 | `No tools for mcp:{srv_name} (not connected?)` |
| [22370](/root/laintas_cli/laintas_cli.py:22370) | output | _cmd_mcp / 0 | `  {t.name} — {t.description}` |
| [22378](/root/laintas_cli/laintas_cli.py:22378) | output | _cmd_mcp / 0 | `MCP server selection cancelled.` |
| [22382](/root/laintas_cli/laintas_cli.py:22382) | output | _cmd_mcp / 0 | `{server_name}: {msg}` |
| [22390](/root/laintas_cli/laintas_cli.py:22390) | output | _cmd_mcp / 0 | `MCP server selection cancelled.` |
| [22394](/root/laintas_cli/laintas_cli.py:22394) | output | _cmd_mcp / 0 | `{server_name}: {msg}` |
| [22399](/root/laintas_cli/laintas_cli.py:22399) | output | _cmd_mcp / 0 | `{m}` |
| [22402](/root/laintas_cli/laintas_cli.py:22402) | output | _cmd_mcp / 0 | `{n}: {m}` |
| [22406](/root/laintas_cli/laintas_cli.py:22406) | output | _cmd_mcp / 0 | `{msg}` |
| [22408](/root/laintas_cli/laintas_cli.py:22408) | output | _cmd_mcp / 0 | `Edit the file, enable a server, then /mcp reload` |
| [22412](/root/laintas_cli/laintas_cli.py:22412) | output | _cmd_mcp / 0 | `Usage: /mcp {list\|trust <n>\|revoke <n>\|connect <n>\|disconnect <n>\|reload\|tools <n>\|init\|config}` |
| [22421](/root/laintas_cli/laintas_cli.py:22421) | output | _disconnect_from_helpwo / 0 | `This CLI isn't connected to Helpwo.` |
| [22431](/root/laintas_cli/laintas_cli.py:22431) | output | _disconnect_from_helpwo / 0 | `Runtime environment {name} withdrawn from Helpwo (its terminal and files are no longer reachable there). Run /helpwo to connect again.` |
| [22440](/root/laintas_cli/laintas_cli.py:22440) | output | _disconnect_from_helpwo / 0 | `Sub-terminal {name} withdrawn from Helpwo.` |
| [22466](/root/laintas_cli/laintas_cli.py:22466) | output | _parse_helpwo_flags / 0 | `Unexpected argument. Usage: /helpwo [stop]` |
| [22527](/root/laintas_cli/laintas_cli.py:22527) | output | _report_app_runtime / 0 | `{escape(label)} could not start in sub-terminal {escape(app)}: {escape(str(runtime.get('message') or 'unknown error'))}` |
| [22529](/root/laintas_cli/laintas_cli.py:22529) | output | _report_app_runtime / 0 | `/t to look at the sub-terminal; running the command again replaces it.` |
| [22533](/root/laintas_cli/laintas_cli.py:22533) | output | _report_app_runtime / 0 | `Sub-terminal {escape(app)} exited before {escape(label)} was ready{f' (exit code {code})' if code not in (None, -1) else ''}.` |
| [22538](/root/laintas_cli/laintas_cli.py:22538) | title/menu/help | _report_app_runtime / title | `Last output of the sub-terminal` |
| [22541](/root/laintas_cli/laintas_cli.py:22541) | output | _report_app_runtime / 0 | `{escape(label)} is not ready after waiting; /t to look at sub-terminal {escape(app)}.` |
| [22565](/root/laintas_cli/laintas_cli.py:22565) | output | _launch_app_subterminal / 0 | `A terminal named {escape(app)} already exists and is not {escape(label)}'s. /terminate {escape(app)} first.` |
| [22578](/root/laintas_cli/laintas_cli.py:22578) | output | _launch_app_subterminal / 0 | `{escape(label)} is still starting in sub-terminal {escape(app)}.` |
| [22591](/root/laintas_cli/laintas_cli.py:22591) | output | _launch_app_subterminal / 0 | `{escape(str(exc))}` |
| [22595](/root/laintas_cli/laintas_cli.py:22595) | output | _launch_app_subterminal / 0 | `Starting {escape(label)} in sub-terminal {escape(app)} with its own agent{' (login, data and conversation persist for this folder)' if persistent else ''}…` |
| [22659](/root/laintas_cli/laintas_cli.py:22659) | error candidate | _spawn_app_terminal | `could not start sub-terminal '{name}'` |
| [22666](/root/laintas_cli/laintas_cli.py:22666) | error candidate | _spawn_app_terminal | `could not register sub-terminal '{name}': {exc}` |
| [22714](/root/laintas_cli/laintas_cli.py:22714) | error candidate | _app_session_open | `session limit reached ({manifest.max_sessions})` |
| [22737](/root/laintas_cli/laintas_cli.py:22737) | output | _app_session_open / 0 | `Session opened for user {escape(user)} (sub-terminal {escape(name)}).` |
| [22745](/root/laintas_cli/laintas_cli.py:22745) | error candidate | _app_session_open | `session did not start: {runtime.get('message') or runtime.get('status')}` |
| [22755](/root/laintas_cli/laintas_cli.py:22755) | output | _app_session_close / 0 | `Session closed for user {escape(user)}.` |
| [22857](/root/laintas_cli/laintas_cli.py:22857) | message candidate | _bootstrap_hosted_app / message | `{type(exc).__name__}: {exc}` |
| [22861](/root/laintas_cli/laintas_cli.py:22861) | output | _bootstrap_hosted_app / 0 | `Could not report startup to the main terminal: {exc}` |
| [22887](/root/laintas_cli/laintas_cli.py:22887) | message candidate | _app_start_in_process / message | `no manifest named '{name}'` |
| [22892](/root/laintas_cli/laintas_cli.py:22892) | message candidate | _app_start_in_process / message | `'{name}' is not trusted (or changed since); /app trust {name}` |
| [22900](/root/laintas_cli/laintas_cli.py:22900) | message candidate | _app_start_in_process / message | `application setup failed: {exc}` |
| [22924](/root/laintas_cli/laintas_cli.py:22924) | resolved output candidate | _app_start_in_process | `Hosting application {escape(manifest.name)} ⏎ Project URL: {escape(manifest.app_url) if manifest.app_url else 'not configured (set app_url in the manifest)'} ⏎ Bridge API: {bridge_url}  agent: {agent_id} ⏎ Bridge login: {bridge_open_url} ⏎ POST /api/agents/<id>/send (chat, exec, approval-response, terminal operations; session-open/session-close/session-list with payload.user), GET /api/agents/<id>/updates. Header 'Authorization: token <token>'. Sessions: up to {manifest.max_sessions}, tools {', '.join(manifest.session_tools) or '(none)'}, approvals {'auto' if manifest.auto_approve else 'interactive'}.` |
| [22935](/root/laintas_cli/laintas_cli.py:22935) | title/menu/help | _app_start_in_process / title | `App` |
| [22958](/root/laintas_cli/laintas_cli.py:22958) | message candidate | _app_start_in_process / message | `could not start '{manifest.command}': {exc}` |
| [22959](/root/laintas_cli/laintas_cli.py:22959) | output | _app_start_in_process / 0 | `Started: {escape(manifest.command)} (pid {proc.pid}); output → {log_path}` |
| [22979](/root/laintas_cli/laintas_cli.py:22979) | message candidate | _app_session_start_in_process / message | `invalid session user` |
| [22990](/root/laintas_cli/laintas_cli.py:22990) | message candidate | _app_session_start_in_process / message | `no primary agent in the session` |
| [22994](/root/laintas_cli/laintas_cli.py:22994) | message candidate | _app_session_start_in_process / message | `session setup failed: {exc}` |
| [23019](/root/laintas_cli/laintas_cli.py:23019) | output | _app_session_start_in_process._close_when_idle / 0 | `Idle for {idle_minutes} min — closing session.` |
| [23026](/root/laintas_cli/laintas_cli.py:23026) | resolved output candidate | _app_session_start_in_process | `Session of {escape(name)} for user {escape(user)} ⏎ Tools: {', '.join(allowed) if allowed is not None else 'all'} ⏎ Approvals: {agent_registry.app_mode['approval']}` |
| [23031](/root/laintas_cli/laintas_cli.py:23031) | title/menu/help | _app_session_start_in_process / title | `App session` |
| [23049](/root/laintas_cli/laintas_cli.py:23049) | output | _cmd_helpwo / 0 | `Helpwo sub-terminal closed (its agent and local API with it).` |
| [23054](/root/laintas_cli/laintas_cli.py:23054) | output | _cmd_helpwo / 0 | `Helpwo local API stopped.` |
| [23066](/root/laintas_cli/laintas_cli.py:23066) | output | _cmd_helpwo / 0 | `Helpwo is not running and this CLI isn't connected.` |
| [23098](/root/laintas_cli/laintas_cli.py:23098) | output | _cmd_app / 0 | `No applications registered. Add a manifest to {app_host.user_manifest_dir()} or ./.laintas/apps/.` |
| [23100](/root/laintas_cli/laintas_cli.py:23100) | output | _cmd_app / 0 | `  {"name": "myapp", "description": "…", "command": "node server.js", "prompt": "…", "persistence": "none"}` |
| [23118](/root/laintas_cli/laintas_cli.py:23118) | output | _cmd_app / 0 | `Skipped {escape(problem)}` |
| [23122](/root/laintas_cli/laintas_cli.py:23122) | output | _cmd_app / 0 | `Usage: /app [list\|start <name>\|stop <name>\|trust <name>\|revoke <name>]` |
| [23126](/root/laintas_cli/laintas_cli.py:23126) | output | _cmd_app / 0 | `Usage: /app {sub} <name>` |
| [23129](/root/laintas_cli/laintas_cli.py:23129) | output | _cmd_app / 0 | `Helpwo has its own command: /helpwo` |
| [23134](/root/laintas_cli/laintas_cli.py:23134) | output | _cmd_app / 0 | `Application {escape(name)} stopped (sub-terminal, agent and its process).` |
| [23137](/root/laintas_cli/laintas_cli.py:23137) | output | _cmd_app / 0 | `'{escape(name)}' is not running.` |
| [23141](/root/laintas_cli/laintas_cli.py:23141) | output | _cmd_app / 0 | `Trust for {escape(name)} revoked.` |
| [23143](/root/laintas_cli/laintas_cli.py:23143) | output | _cmd_app / 0 | `'{escape(name)}' was not trusted.` |
| [23148](/root/laintas_cli/laintas_cli.py:23148) | output | _cmd_app / 0 | `No application named '{escape(name)}'. /app list` |
| [23152](/root/laintas_cli/laintas_cli.py:23152) | resolved output candidate | _cmd_app | `{escape(manifest.name)} ({manifest.scope}: {escape(manifest.source)}) ⏎ {escape(manifest.description)} ⏎  ⏎ Command: {escape(manifest.command) or '(none)'} ⏎ Persistence: {manifest.persistence} ⏎ Per-user sessions: up to {manifest.max_sessions}, tools {escape(', '.join(manifest.session_tools) or '(none)')}, approvals {'AUTO' if manifest.auto_approve else 'interactive'}, idle close {manifest.session_idle_minutes or 'never'} min ⏎  ⏎ Configuration: ⏎ {escape(json.dumps(manifest.raw, ensure_ascii=False, indent=2))} ⏎  ⏎ Trust grants this application's token access to the normal file, shell, terminal and agent bridge APIs. App and session agents run as your OS user and use your billing account. Trust covers start/stop until the manifest changes.` |
| [23166](/root/laintas_cli/laintas_cli.py:23166) | title/menu/help | _cmd_app / title | `Trust application` |
| [23168](/root/laintas_cli/laintas_cli.py:23168) | status/prompt | _cmd_app | `Trust it? [y/N] ` |
| [23173](/root/laintas_cli/laintas_cli.py:23173) | output | _cmd_app / 0 | `Trusted. /app start {escape(name)}` |
| [23175](/root/laintas_cli/laintas_cli.py:23175) | output | _cmd_app / 0 | `Not trusted.` |
| [23180](/root/laintas_cli/laintas_cli.py:23180) | output | _cmd_app / 0 | `'{escape(name)}' is not trusted, or its manifest changed since it was. Review it with /app trust {escape(name)}.` |
| [23196](/root/laintas_cli/laintas_cli.py:23196) | output | _cmd_term / 0 | `This terminal: {service.transport.ref}` |
| [23198](/root/laintas_cli/laintas_cli.py:23198) | output | _cmd_term / 0 | `{peer['ref']} · account {peer.get('user_id') or 'local'} · pid {peer['pid']}` |
| [23202](/root/laintas_cli/laintas_cli.py:23202) | output | _cmd_term / 0 | `Invitation sent: {invitation}. Accept in the execution terminal: /term accept {invitation}` |
| [23205](/root/laintas_cli/laintas_cli.py:23205) | output | _cmd_term / 0 | `Adopted as {result['alias']}; assign work from the controller.` |
| [23208](/root/laintas_cli/laintas_cli.py:23208) | output | _cmd_term / 0 | `Relation released; the execution CLI keeps its account and workspace.` |
| [23210](/root/laintas_cli/laintas_cli.py:23210) | output | _cmd_term / 0 | `Usage: /term peers \| adopt <id> --name <name> \| accept <invite> \| release [name]` |
| [23218](/root/laintas_cli/laintas_cli.py:23218) | output | _cmd_term / 0 | `Release the adopted terminal before changing its alias.` |
| [23224](/root/laintas_cli/laintas_cli.py:23224) | output | _cmd_term / 0 | `Usage: /term rename <old> <new>` |
| [23226](/root/laintas_cli/laintas_cli.py:23226) | output | _cmd_term / 0 | `Terminal names may contain only letters, numbers, dot, underscore, and hyphen.` |
| [23228](/root/laintas_cli/laintas_cli.py:23228) | output | _cmd_term / 0 | `The primary terminal term0 cannot be renamed.` |
| [23231](/root/laintas_cli/laintas_cli.py:23231) | output | _cmd_term / 0 | `Terminal renamed: {parts[2]} → {parts[3]}` |
| [23235](/root/laintas_cli/laintas_cli.py:23235) | output | _cmd_term / 0 | `Could not rename '{parts[2]}': source missing or target already exists.` |
| [23241](/root/laintas_cli/laintas_cli.py:23241) | output | _cmd_term / 0 | `Invalid terminal name. Use letters, numbers, dot, underscore, or hyphen; term0 is reserved.` |
| [23250](/root/laintas_cli/laintas_cli.py:23250) | output | _cmd_term / 0 | `Terminal '{name}' already exists. /t to view, /terminate {name} to remove.` |
| [23284](/root/laintas_cli/laintas_cli.py:23284) | error candidate | _cmd_term | `Terminal did not start` |
| [23301](/root/laintas_cli/laintas_cli.py:23301) | output | _cmd_term / 0 | `Could not create terminal '{name}': {exc}` |
| [23304](/root/laintas_cli/laintas_cli.py:23304) | output | _cmd_term / 0 | `Created sub-terminal {name} with its own CLI agent.` |
| [23306](/root/laintas_cli/laintas_cli.py:23306) | output | _cmd_term / 0 | `Created local shell {name}. Agent pairing is unavailable in this CLI.` |
| [23308](/root/laintas_cli/laintas_cli.py:23308) | output | _cmd_term / 0 | `Usage: /term [name\|rename <old> <new>]` |
| [23313](/root/laintas_cli/laintas_cli.py:23313) | output | _cmd_term / 0 | `Terminal ID: {service.transport.ref}` |
| [23315](/root/laintas_cli/laintas_cli.py:23315) | output | _cmd_term / 0 | `Controller: {service.parent['parent']} · alias {service.parent['alias']}` |
| [23318](/root/laintas_cli/laintas_cli.py:23318) | output | _cmd_term / 0 | `Pending: /term accept {invitation['id']}` |
| [23328](/root/laintas_cli/laintas_cli.py:23328) | output | _cmd_reload / 0 | `Usage: /reload` |
| [23340](/root/laintas_cli/laintas_cli.py:23340) | output | _cmd_undo / 0 | `No checkpoints for this repository (not a git repo, or none taken yet).` |
| [23343](/root/laintas_cli/laintas_cli.py:23343) | output | _cmd_undo / 0 | `Checkpoints ({len(cps)}, newest last):` |
| [23346](/root/laintas_cli/laintas_cli.py:23346) | output | _cmd_undo / 0 | `  {c['sha'][:10]}  {c.get('label', '') or '(no label)'}  {ago}` |
| [23347](/root/laintas_cli/laintas_cli.py:23347) | output | _cmd_undo / 0 | `Run /undo to choose a checkpoint, or /undo <sha> directly.` |
| [23352](/root/laintas_cli/laintas_cli.py:23352) | output | _cmd_undo / 0 | `Checkpoint saved: {cp['sha'][:10]} ({label})` |
| [23354](/root/laintas_cli/laintas_cli.py:23354) | output | _cmd_undo / 0 | `Could not snapshot (not a git repository?).` |
| [23361](/root/laintas_cli/laintas_cli.py:23361) | output | _cmd_undo / 0 | `An auto-snapshot is still being created in the background. Wait a moment and try again, or specify a sha directly.` |
| [23369](/root/laintas_cli/laintas_cli.py:23369) | title/menu/help | _cmd_undo / title | `Restore Checkpoint` |
| [23378](/root/laintas_cli/laintas_cli.py:23378) | output | _cmd_undo / 0 | `Checkpoint selection cancelled.` |
| [23391](/root/laintas_cli/laintas_cli.py:23391) | output | _cmd_undo / 0 | `Checkpoint restore cancelled.` |
| [23394](/root/laintas_cli/laintas_cli.py:23394) | output | _cmd_undo / 0 | `{msg}` |
| [23396](/root/laintas_cli/laintas_cli.py:23396) | output | _cmd_undo / 0 | `Files created since the checkpoint were kept. A pre-undo checkpoint was saved (undo the undo with /undo).` |
| [23430](/root/laintas_cli/laintas_cli.py:23430) | label/render | _config_table | `Key` |
| [23431](/root/laintas_cli/laintas_cli.py:23431) | label/render | _config_table | `Value` |
| [23432](/root/laintas_cli/laintas_cli.py:23432) | label/render | _config_table | `Type` |
| [23433](/root/laintas_cli/laintas_cli.py:23433) | label/render | _config_table | `Source` |
| [23434](/root/laintas_cli/laintas_cli.py:23434) | label/render | _config_table | `Description` |
| [23464](/root/laintas_cli/laintas_cli.py:23464) | output | _cmd_config_import / 0 | `Usage: /config import <file>` |
| [23476](/root/laintas_cli/laintas_cli.py:23476) | output | _cmd_config_import / 0 | `Cannot read {escape(str(path))}: {escape(exc.strerror or str(exc))}` |
| [23479](/root/laintas_cli/laintas_cli.py:23479) | output | _cmd_config_import / 0 | `Nothing changed — {escape(str(exc))}` |
| [23490](/root/laintas_cli/laintas_cli.py:23490) | output | _cmd_config_import / 0 | `Imported {len(keys)} setting(s) from {escape(str(path))}:` |
| [23492](/root/laintas_cli/laintas_cli.py:23492) | output | _cmd_config_import / 0 | `  {escape(_config_display_key(key))} = {get_runtime_config(key)!r}` |
| [23500](/root/laintas_cli/laintas_cli.py:23500) | output | _cmd_config_export / 0 | `Usage: /config export <file>` |
| [23511](/root/laintas_cli/laintas_cli.py:23511) | output | _cmd_config_export / 0 | `Cannot write {escape(str(path))}: {escape(exc.strerror or str(exc))}` |
| [23513](/root/laintas_cli/laintas_cli.py:23513) | output | _cmd_config_export / 0 | `Exported {len(values)} changed setting(s) to {escape(str(path.resolve()))}` |
| [23522](/root/laintas_cli/laintas_cli.py:23522) | output | _cmd_config_budget / 0 | `Context budget reset to the shipped defaults ({len(keys)} levels).` |
| [23531](/root/laintas_cli/laintas_cli.py:23531) | output | _cmd_config_budget / 0 | `Unknown budget level: {escape(' '.join(words))}` |
| [23532](/root/laintas_cli/laintas_cli.py:23532) | output | _cmd_config_budget / 0 | `Run /config budget to list every level.` |
| [23536](/root/laintas_cli/laintas_cli.py:23536) | output | _cmd_config_budget / 0 | `Set with /config {shown} <level> share <0-1>; /prop budget shows what was compressed.` |
| [23542](/root/laintas_cli/laintas_cli.py:23542) | resolved output candidate | _cmd_config_budget | `Value: {meta['value']!r} ⏎ Default: {meta['default']!r} ⏎ Type: {meta['type']} ⏎ Source: {'override' if meta['overridden'] else 'default'} ⏎  ⏎ {meta['description']}` |
| [23553](/root/laintas_cli/laintas_cli.py:23553) | output | _cmd_config_budget / 0 | `{exc}` |
| [23557](/root/laintas_cli/laintas_cli.py:23557) | output | _cmd_config_budget / 0 | `{shown} = {stored!r}` |
| [23570](/root/laintas_cli/laintas_cli.py:23570) | output | _cmd_config / 0 | `budget … — {len(budget_keys)} context-budget levels{f' ({changed} changed)' if changed else ''}: /config budget` |
| [23572](/root/laintas_cli/laintas_cli.py:23572) | output | _cmd_config / 0 | `Set with /config <key> <value>; restore with /config reset.` |
| [23573](/root/laintas_cli/laintas_cli.py:23573) | output | _cmd_config / 0 | `Narrow the list with a prefix, e.g. /config search` |
| [23582](/root/laintas_cli/laintas_cli.py:23582) | output | _cmd_config / 0 | `Runtime config reset to defaults.` |
| [23599](/root/laintas_cli/laintas_cli.py:23599) | output | _cmd_config / 0 | `{len(matches)} of {len(described)} keys. Set with /config <key> <value>.` |
| [23602](/root/laintas_cli/laintas_cli.py:23602) | output | _cmd_config / 0 | `Unknown config key: {key}` |
| [23603](/root/laintas_cli/laintas_cli.py:23603) | output | _cmd_config / 0 | `Run /config to list valid keys.` |
| [23605](/root/laintas_cli/laintas_cli.py:23605) | resolved output candidate | _cmd_config | `Value: {meta['value']!r} ⏎ Default: {meta['default']!r} ⏎ Type: {meta['type']} ⏎ Source: {'override' if meta['overridden'] else 'default'} ⏎  ⏎ {meta['description']}` |
| [23618](/root/laintas_cli/laintas_cli.py:23618) | output | _cmd_config / 0 | `Unknown config key: {key}` |
| [23619](/root/laintas_cli/laintas_cli.py:23619) | output | _cmd_config / 0 | `Run /config to list valid keys.` |
| [23627](/root/laintas_cli/laintas_cli.py:23627) | output | _cmd_config / 0 | `{key} = {value!r} ({type(value).__name__})` |
| [23629](/root/laintas_cli/laintas_cli.py:23629) | output | _cmd_config / 0 | `{e}` |
| [23630](/root/laintas_cli/laintas_cli.py:23630) | output | _cmd_config / 0 | `Run /config {key} to inspect the expected type.` |
| [23632](/root/laintas_cli/laintas_cli.py:23632) | output | _cmd_config / 0 | `Usage: /config [key [value]] \| /config reset` |
| [23649](/root/laintas_cli/laintas_cli.py:23649) | output | _web_status / 0 | `{err}` |
| [23655](/root/laintas_cli/laintas_cli.py:23655) | title/menu/help | _web_status / title | `Search engines` |
| [23656](/root/laintas_cli/laintas_cli.py:23656) | label/render | _web_status | `#` |
| [23657](/root/laintas_cli/laintas_cli.py:23657) | label/render | _web_status | `Engine` |
| [23658](/root/laintas_cli/laintas_cli.py:23658) | label/render | _web_status | `Cost` |
| [23659](/root/laintas_cli/laintas_cli.py:23659) | label/render | _web_status | `Usable` |
| [23660](/root/laintas_cli/laintas_cli.py:23660) | label/render | _web_status | `Notes` |
| [23676](/root/laintas_cli/laintas_cli.py:23676) | output | _web_status / 0 | `{warning}` |
| [23734](/root/laintas_cli/laintas_cli.py:23734) | title/menu/help | _web_status / title | `Web` |
| [23740](/root/laintas_cli/laintas_cli.py:23740) | output | _web_engines / 0 | `{err}` |
| [23745](/root/laintas_cli/laintas_cli.py:23745) | output | _web_engines / 0 | `Wrote a starter engine file to {path}` |
| [23746](/root/laintas_cli/laintas_cli.py:23746) | output | _web_engines / 0 | `Edit it, then /web engines to see the result.` |
| [23748](/root/laintas_cli/laintas_cli.py:23748) | output | _web_engines / 0 | `{path} already exists — edit it in place.` |
| [23751](/root/laintas_cli/laintas_cli.py:23751) | output | _web_engines / 0 | `Usage: /web engines [init]` |
| [23756](/root/laintas_cli/laintas_cli.py:23756) | title/menu/help | _web_engines / title | `Engine registry` |
| [23757](/root/laintas_cli/laintas_cli.py:23757) | label/render | _web_engines | `Engine` |
| [23758](/root/laintas_cli/laintas_cli.py:23758) | label/render | _web_engines | `Kind` |
| [23759](/root/laintas_cli/laintas_cli.py:23759) | label/render | _web_engines | `Cost` |
| [23760](/root/laintas_cli/laintas_cli.py:23760) | label/render | _web_engines | `Usable` |
| [23761](/root/laintas_cli/laintas_cli.py:23761) | label/render | _web_engines | `Description` |
| [23770](/root/laintas_cli/laintas_cli.py:23770) | output | _web_engines / 0 | `{message}` |
| [23771](/root/laintas_cli/laintas_cli.py:23771) | output | _web_engines / 0 | `Add your own JSON engines with /web engines init.` |
| [23783](/root/laintas_cli/laintas_cli.py:23783) | output | _web_test / 0 | `{err}` |
| [23790](/root/laintas_cli/laintas_cli.py:23790) | output | _web_test / 0 | `Unknown engine: {requested}` |
| [23791](/root/laintas_cli/laintas_cli.py:23791) | output | _web_test / 0 | `Known: {', '.join(sorted(entries))}` |
| [23802](/root/laintas_cli/laintas_cli.py:23802) | output | _web_test / 0 | `Query: {probe!r}  (use /web try <query> to test your own)` |
| [23805](/root/laintas_cli/laintas_cli.py:23805) | label/render | _web_test | `Engine` |
| [23806](/root/laintas_cli/laintas_cli.py:23806) | label/render | _web_test | `Result` |
| [23807](/root/laintas_cli/laintas_cli.py:23807) | label/render | _web_test | `Hits` |
| [23808](/root/laintas_cli/laintas_cli.py:23808) | label/render | _web_test | `Snippets` |
| [23809](/root/laintas_cli/laintas_cli.py:23809) | label/render | _web_test | `Time` |
| [23810](/root/laintas_cli/laintas_cli.py:23810) | label/render | _web_test | `Detail` |
| [23838](/root/laintas_cli/laintas_cli.py:23838) | output | _web_try / 0 | `{err}` |
| [23842](/root/laintas_cli/laintas_cli.py:23842) | output | _web_try / 0 | `Usage: /web try <query>` |
| [23848](/root/laintas_cli/laintas_cli.py:23848) | output | _web_try / 0 | `{out.get('error')}` |
| [23851](/root/laintas_cli/laintas_cli.py:23851) | output | _web_try / 0 | `{out.get('engine')} answered in {elapsed:.1f}s (cost: {out.get('cost', 'free')})` |
| [23854](/root/laintas_cli/laintas_cli.py:23854) | output | _web_try / 0 | `  {skipped.get('engine')}: {skipped.get('message', '')[:80]}` |
| [23856](/root/laintas_cli/laintas_cli.py:23856) | output | _web_try / 0 | ` ⏎ {item.get('title', '')[:90]}` |
| [23857](/root/laintas_cli/laintas_cli.py:23857) | output | _web_try / 0 | `  {item.get('url', '')[:100]}` |
| [23859](/root/laintas_cli/laintas_cli.py:23859) | output | _web_try / 0 | `  {item['snippet'][:160]}` |
| [23866](/root/laintas_cli/laintas_cli.py:23866) | output | _web_cookies / 0 | `cookie_store module unavailable` |
| [23873](/root/laintas_cli/laintas_cli.py:23873) | output | _web_cookies / 0 | `Cleared {removed} cookie(s) for {target}.` |
| [23879](/root/laintas_cli/laintas_cli.py:23879) | output | _web_cookies / 0 | `Usage: /web cookies [clear [domain]]` |
| [23882](/root/laintas_cli/laintas_cli.py:23882) | output | _web_cookies / 0 | `The cookie jar is off (/config search_cookie_enabled true).` |
| [23887](/root/laintas_cli/laintas_cli.py:23887) | output | _web_cookies / 0 | `No cookies stored.` |
| [23888](/root/laintas_cli/laintas_cli.py:23888) | output | _web_cookies / 0 | `Only anti-bot clearance is kept automatically; a sign-in must be saved with /identity capture.` |
| [23892](/root/laintas_cli/laintas_cli.py:23892) | title/menu/help | _web_cookies / title | `Stored clearance cookies` |
| [23893](/root/laintas_cli/laintas_cli.py:23893) | label/render | _web_cookies | `Domain` |
| [23894](/root/laintas_cli/laintas_cli.py:23894) | label/render | _web_cookies | `Cookies` |
| [23898](/root/laintas_cli/laintas_cli.py:23898) | output | _web_cookies / 0 | `Current exit: {stats['egress']} {symbols.BULLET} {stats['usable']} of {stats['total']} usable here.` |
| [23901](/root/laintas_cli/laintas_cli.py:23901) | output | _web_cookies / 0 | `Values are never displayed. Clear with /web cookies clear [domain].` |
| [23927](/root/laintas_cli/laintas_cli.py:23927) | output | _cmd_web / 0 | `Usage: /web [status\|engines [init]\|test [engine]\|try <query>\|cookies [clear [domain]]]` |
| [23936](/root/laintas_cli/laintas_cli.py:23936) | output | _cmd_identity / 0 | `identity_store module unavailable` |
| [23944](/root/laintas_cli/laintas_cli.py:23944) | output | _cmd_identity / 0 | `No saved logins.` |
| [23945](/root/laintas_cli/laintas_cli.py:23945) | output | _cmd_identity / 0 | `Sign in through the live-view browser, then /identity capture <name>.` |
| [23948](/root/laintas_cli/laintas_cli.py:23948) | title/menu/help | _cmd_identity / title | `Saved logins` |
| [23949](/root/laintas_cli/laintas_cli.py:23949) | label/render | _cmd_identity | `Name` |
| [23950](/root/laintas_cli/laintas_cli.py:23950) | label/render | _cmd_identity | `Domains` |
| [23951](/root/laintas_cli/laintas_cli.py:23951) | label/render | _cmd_identity | `Cookies` |
| [23952](/root/laintas_cli/laintas_cli.py:23952) | label/render | _cmd_identity | `Storage` |
| [23953](/root/laintas_cli/laintas_cli.py:23953) | label/render | _cmd_identity | `Exit` |
| [23954](/root/laintas_cli/laintas_cli.py:23954) | label/render | _cmd_identity | `Last check` |
| [23970](/root/laintas_cli/laintas_cli.py:23970) | output | _cmd_identity / 0 | `Use is disabled — /config identity_enabled true` |
| [23971](/root/laintas_cli/laintas_cli.py:23971) | output | _cmd_identity / 0 | `Values are never displayed.` |
| [23976](/root/laintas_cli/laintas_cli.py:23976) | output | _cmd_identity / 0 | `Usage: /identity check <name>` |
| [23980](/root/laintas_cli/laintas_cli.py:23980) | output | _cmd_identity / 0 | `{out.get('error')}` |
| [23982](/root/laintas_cli/laintas_cli.py:23982) | output | _cmd_identity / 0 | `{name}: still signed in` |
| [23984](/root/laintas_cli/laintas_cli.py:23984) | output | _cmd_identity / 0 | `{name}: signed out — {out.get('detail', '')}` |
| [23985](/root/laintas_cli/laintas_cli.py:23985) | output | _cmd_identity / 0 | `Sign in again in the live view, then /identity capture {name}` |
| [23991](/root/laintas_cli/laintas_cli.py:23991) | output | _cmd_identity / 0 | `Usage: /identity capture <name> [domain,domain]` |
| [23997](/root/laintas_cli/laintas_cli.py:23997) | output | _cmd_identity / 0 | `{out.get('error')}` |
| [24000](/root/laintas_cli/laintas_cli.py:24000) | output | _cmd_identity / 0 | `Saved '{record['name']}'` |
| [24001](/root/laintas_cli/laintas_cli.py:24001) | output | _cmd_identity / 0 | `  domains : {', '.join(record['domains']) or '(none)'}` |
| [24002](/root/laintas_cli/laintas_cli.py:24002) | output | _cmd_identity / 0 | `  cookies : {record['cookies']}, storage origins {record['origins']}` |
| [24003](/root/laintas_cli/laintas_cli.py:24003) | output | _cmd_identity / 0 | `  exit    : {record['egress'] or '(not recorded)'}` |
| [24008](/root/laintas_cli/laintas_cli.py:24008) | output | _cmd_identity / 0 | `Usage: /identity delete <name>` |
| [24010](/root/laintas_cli/laintas_cli.py:24010) | output | _cmd_identity / 0 | `Deleted saved login '{name}'.` |
| [24012](/root/laintas_cli/laintas_cli.py:24012) | output | _cmd_identity / 0 | `No saved login named '{name}'.` |
| [24015](/root/laintas_cli/laintas_cli.py:24015) | output | _cmd_identity / 0 | `Usage: /identity [list\|check <name>\|capture <name> [domains]\|delete <name>]` |
| [24023](/root/laintas_cli/laintas_cli.py:24023) | output | _cmd_max / 0 | `MAX mode — all limits lifted (applies to every agent):` |
| [24025](/root/laintas_cli/laintas_cli.py:24025) | output | _cmd_max / 0 | `  {k} = {v}` |
| [24026](/root/laintas_cli/laintas_cli.py:24026) | output | _cmd_max / 0 | `Note: max_tokens may be capped lower by the provider. Revert with /config reset.` |
| [24050](/root/laintas_cli/laintas_cli.py:24050) | output | _cmd_compact / 0 | `Usage: /compact [status\|--force]` |
| [24058](/root/laintas_cli/laintas_cli.py:24058) | output | _cmd_compact / 0 | `No current session context to compact.` |
| [24066](/root/laintas_cli/laintas_cli.py:24066) | output | _cmd_compact / 0 | `Context  {_fmt_tokens(info['tokens'])} tokens {symbols.BULLET} {info['messages']} messages {symbols.BULLET} {ratio:.0f}% of {_fmt_tokens(info['usable'])} usable {symbols.BULLET} summarized` |
| [24066](/root/laintas_cli/laintas_cli.py:24066) | output | _cmd_compact / 0 | `Context  {_fmt_tokens(info['tokens'])} tokens {symbols.BULLET} {info['messages']} messages {symbols.BULLET} {ratio:.0f}% of {_fmt_tokens(info['usable'])} usable` |
| [24071](/root/laintas_cli/laintas_cli.py:24071) | output | _cmd_compact / 0 | `Automatic and background compaction are disabled by context policy.` |
| [24076](/root/laintas_cli/laintas_cli.py:24076) | output | _cmd_compact / 0 | `{background}; foreground at {_fmt_tokens(info['auto_at'])}; target {_fmt_tokens(info['target_tokens'])} tokens; background task: {info['background_status']}.` |
| [24081](/root/laintas_cli/laintas_cli.py:24081) | output | _cmd_compact / 0 | `Model window {_fmt_tokens(info['provider_window'])}; compaction fires at ~{info['auto_at_share'] * 100:.0f}% of it (/config budget thread, /prop budget).` |
| [24103](/root/laintas_cli/laintas_cli.py:24103) | output | _cmd_compact / 0 | `Context compaction cancelled.` |
| [24106](/root/laintas_cli/laintas_cli.py:24106) | output | _cmd_compact / 0 | `Context compaction failed: {result.get('error', 'unknown error')}` |
| [24110](/root/laintas_cli/laintas_cli.py:24110) | output | _cmd_compact / 0 | `Context unchanged: {result.get('reason', 'nothing to compact')} ({_fmt_tokens(result['tokens'])} tokens).` |
| [24135](/root/laintas_cli/laintas_cli.py:24135) | output | _cmd_compact / 0 | `Context compacted: {_fmt_tokens(result['tokens'])} → {_fmt_tokens(result['after_tokens'])} tokens {symbols.BULLET} {result['messages']} → {result['after_messages']} messages` |
| [24166](/root/laintas_cli/laintas_cli.py:24166) | output | _cmd_continue / 0 | `No previous agent loop to continue.` |
| [24170](/root/laintas_cli/laintas_cli.py:24170) | output | _cmd_continue / 0 | `The current session has no pending continuation.` |
| [24181](/root/laintas_cli/laintas_cli.py:24181) | output | _cmd_continue / 0 | `Continuing current session...` |
| [24340](/root/laintas_cli/laintas_cli.py:24340) | output | _told_browse_history_legacy._show_detail / 0 | `── prompt ──` |
| [24342](/root/laintas_cli/laintas_cli.py:24342) | output | _told_browse_history_legacy._show_detail / 0 | `{escape(str(text))}` |
| [24346](/root/laintas_cli/laintas_cli.py:24346) | output | _told_browse_history_legacy._show_detail / 0 | `── reply ──` |
| [24349](/root/laintas_cli/laintas_cli.py:24349) | output | _told_browse_history_legacy._show_detail / 0 | `(empty reply)` |
| [24353](/root/laintas_cli/laintas_cli.py:24353) | output | _told_browse_history_legacy._show_detail / 0 | `(no reply recorded)` |
| [24355](/root/laintas_cli/laintas_cli.py:24355) | output | _told_browse_history_legacy._show_detail / 0 | `journal records prompts only — no reply stored` |
| [24356](/root/laintas_cli/laintas_cli.py:24356) | output | _told_browse_history_legacy._show_detail / 0 | `{symbols.ARROW_U}{symbols.ARROW_D}/wheel/PgUp/PgDn scroll  q/Esc/Enter back` |
| [24433](/root/laintas_cli/laintas_cli.py:24433) | output | _told_browse_history_legacy / 0 | `Nothing in the {src_label} yet.` |
| [24439](/root/laintas_cli/laintas_cli.py:24439) | title/menu/help | _told_browse_history_legacy / title | `History — {src_label} ({len(items)})` |
| [24553](/root/laintas_cli/laintas_cli.py:24553) | output | _told_browse_history / 0 | `No conversations in the current session yet. Use /told log to inspect the project prompt journal.` |
| [24645](/root/laintas_cli/laintas_cli.py:24645) | title/menu/help | _told_browse_history._load_detail / title | `Conversation` |
| [24649](/root/laintas_cli/laintas_cli.py:24649) | title/menu/help | _told_browse_history / title | `Conversation History` |
| [24678](/root/laintas_cli/laintas_cli.py:24678) | output | _cmd_told / 0 | `Agent '{escape(reference)}' is not available in this terminal.` |
| [24694](/root/laintas_cli/laintas_cli.py:24694) | resolved output candidate | _cmd_told | `Usage: /told [agent-id [reply [N]\|all]\|N\|all\|reply [N]\|log [N]]` |
| [24722](/root/laintas_cli/laintas_cli.py:24722) | output | _cmd_told / 0 | `You last asked:` |
| [24723](/root/laintas_cli/laintas_cli.py:24723) | output | _cmd_told / 0 | `  {escape(strip_ansi(_user_msgs[-1]))}` |
| [24727](/root/laintas_cli/laintas_cli.py:24727) | output | _cmd_told / 0 | `You last asked:` |
| [24728](/root/laintas_cli/laintas_cli.py:24728) | output | _cmd_told / 0 | `  {escape(strip_ansi(_fallback))}` |
| [24730](/root/laintas_cli/laintas_cli.py:24730) | output | _cmd_told / 0 | `You haven't asked anything yet this session.` |
| [24731](/root/laintas_cli/laintas_cli.py:24731) | output | _cmd_told / 0 | `Tip: /told log shows prompts from the on-disk journal (survives /new and restarts).` |
| [24742](/root/laintas_cli/laintas_cli.py:24742) | output | _cmd_told / 0 | `No conversation history for {escape(scoped_agent_name)}.` |
| [24746](/root/laintas_cli/laintas_cli.py:24746) | output | _cmd_told / 0 | `── {escape(scoped_agent_name)} {symbols.BULLET} complete conversation ({len(replayable)} events) ──` |
| [24751](/root/laintas_cli/laintas_cli.py:24751) | output | _cmd_told / 0 | `No user messages in this session yet.` |
| [24752](/root/laintas_cli/laintas_cli.py:24752) | output | _cmd_told / 0 | `Tip: /told log reads the durable per-cwd journal.` |
| [24754](/root/laintas_cli/laintas_cli.py:24754) | output | _cmd_told / 0 | `All your messages ({len(_user_msgs)}):` |
| [24756](/root/laintas_cli/laintas_cli.py:24756) | output | _cmd_told / 0 | `   {escape(strip_ansi(msg))}` |
| [24780](/root/laintas_cli/laintas_cli.py:24780) | output | _cmd_told / 0 | `No conversation turns to replay.` |
| [24781](/root/laintas_cli/laintas_cli.py:24781) | output | _cmd_told / 0 | `Tip: /told log reads the durable per-cwd journal.` |
| [24787](/root/laintas_cli/laintas_cli.py:24787) | output | _cmd_told / 0 | `── {label} ──` |
| [24789](/root/laintas_cli/laintas_cli.py:24789) | output | _cmd_told / 0 | `You:        {escape(strip_ansi(u))}` |
| [24790](/root/laintas_cli/laintas_cli.py:24790) | output | _cmd_told / 0 | `Assistant:   {escape(strip_ansi(a))}` |
| [24797](/root/laintas_cli/laintas_cli.py:24797) | output | _cmd_told / 0 | `The durable prompt journal is project-wide; use /told log [N] without an Agent.` |
| [24819](/root/laintas_cli/laintas_cli.py:24819) | output | _cmd_told / 0 | `No prompts recorded in the journal yet.` |
| [24820](/root/laintas_cli/laintas_cli.py:24820) | output | _cmd_told / 0 | `Journal: {escape(str(_log_path))}` |
| [24823](/root/laintas_cli/laintas_cli.py:24823) | output | _cmd_told / 0 | `Recent prompts (from journal):` |
| [24834](/root/laintas_cli/laintas_cli.py:24834) | output | _cmd_told / 0 | `  {tstr}  {escape(strip_ansi(text))}` |
| [24836](/root/laintas_cli/laintas_cli.py:24836) | output | _cmd_told / 0 | `  {escape(strip_ansi(text))}` |
| [24842](/root/laintas_cli/laintas_cli.py:24842) | output | _cmd_told / 0 | `Unknown subcommand: {escape(sub)}` |
| [24843](/root/laintas_cli/laintas_cli.py:24843) | resolved output candidate | _cmd_told | `Usage: /told [agent-id [reply [N]\|all]\|N\|all\|reply [N]\|log [N]]` |
| [24846](/root/laintas_cli/laintas_cli.py:24846) | output | _cmd_told / 0 | `N must be a positive integer.` |
| [24849](/root/laintas_cli/laintas_cli.py:24849) | output | _cmd_told / 0 | `No user messages in this session yet.` |
| [24850](/root/laintas_cli/laintas_cli.py:24850) | output | _cmd_told / 0 | `Tip: /told log reads the durable per-cwd journal.` |
| [24854](/root/laintas_cli/laintas_cli.py:24854) | output | _cmd_told / 0 | `{label}:` |
| [24856](/root/laintas_cli/laintas_cli.py:24856) | output | _cmd_told / 0 | `   {escape(strip_ansi(msg))}` |
| [24863](/root/laintas_cli/laintas_cli.py:24863) | label/render | _workflow_event_line | `{symbol} ` |
| [24877](/root/laintas_cli/laintas_cli.py:24877) | label/render | _workflow_result | `{symbol} ` |
| [24878](/root/laintas_cli/laintas_cli.py:24878) | label/render | _workflow_result | `{kind} {status}` |
| [24911](/root/laintas_cli/laintas_cli.py:24911) | output | _cmd_hwo._hwo_progress / 0 | `HWO {event.get('runId')} started` |
| [24932](/root/laintas_cli/laintas_cli.py:24932) | output | _cmd_hwo / 0 | `hwo view: no such file: {path}` |
| [24939](/root/laintas_cli/laintas_cli.py:24939) | output | _cmd_hwo / 0 | `hwo view failed: {type(exc).__name__}: {exc}` |
| [24945](/root/laintas_cli/laintas_cli.py:24945) | output | _cmd_hwo / 0 | `hwo: {err}` |
| [24957](/root/laintas_cli/laintas_cli.py:24957) | output | _cmd_hwo / 0 | `Usage: /hwo {sub} <file.hwo>` |
| [25014](/root/laintas_cli/laintas_cli.py:25014) | output | _cmd_hwg / 0 | `resume outputs must be a JSON object; ignoring.` |
| [25016](/root/laintas_cli/laintas_cli.py:25016) | output | _cmd_hwg / 0 | `Could not parse resume outputs JSON; ignoring.` |
| [25075](/root/laintas_cli/laintas_cli.py:25075) | output | _cmd_version / 0 | `Usage: /update [--force]  \|  /update check` |
| [25108](/root/laintas_cli/laintas_cli.py:25108) | output | _cmd_extensions / 0 | `No extensions installed.` |
| [25113](/root/laintas_cli/laintas_cli.py:25113) | output | _cmd_extensions / 0 | `  {trust_marker} {entry['name']} v{entry['version']}  {entry['scope']}  {entry.get('source', '')}  {entry['description']}` |
| [25117](/root/laintas_cli/laintas_cli.py:25117) | output | _cmd_extensions / 0 | ` ⏎ ✓ = trusted  ! = untrusted` |
| [25125](/root/laintas_cli/laintas_cli.py:25125) | output | _cmd_extensions / 0 | `Usage: /extensions available [all\|official\|community]` |
| [25131](/root/laintas_cli/laintas_cli.py:25131) | output | _cmd_extensions / 0 | `Usage: /extensions search <keyword>` |
| [25138](/root/laintas_cli/laintas_cli.py:25138) | output | _cmd_extensions / 0 | `Could not load the extension marketplace: {escape(str(exc))}` |
| [25144](/root/laintas_cli/laintas_cli.py:25144) | output | _cmd_extensions / 0 | `{escape(message)}` |
| [25153](/root/laintas_cli/laintas_cli.py:25153) | output | _cmd_extensions / 0 | ` ⏎ {heading}` |
| [25159](/root/laintas_cli/laintas_cli.py:25159) | output | _cmd_extensions / 0 | `  {identifier}  {name}  v{version}` |
| [25161](/root/laintas_cli/laintas_cli.py:25161) | output | _cmd_extensions / 0 | `    {description}` |
| [25162](/root/laintas_cli/laintas_cli.py:25162) | output | _cmd_extensions / 0 | `    /extensions install {identifier}` |
| [25165](/root/laintas_cli/laintas_cli.py:25165) | output | _cmd_extensions / 0 | ` ⏎ Community extensions are user-published and unreviewed. Installation performs a fresh source scan and still requires approval.` |
| [25171](/root/laintas_cli/laintas_cli.py:25171) | output | _cmd_extensions / 0 | `Usage: /extensions install <path\|url\|.lext> [--global] [--force]` |
| [25177](/root/laintas_cli/laintas_cli.py:25177) | output | _cmd_extensions / 0 | `Usage: /extensions install <path\|url\|.lext> [--global] [--force]` |
| [25181](/root/laintas_cli/laintas_cli.py:25181) | output | _cmd_extensions / 0 | `{result.message}` |
| [25183](/root/laintas_cli/laintas_cli.py:25183) | output | _cmd_extensions / 0 | `{result.message}` |
| [25188](/root/laintas_cli/laintas_cli.py:25188) | output | _cmd_extensions / 0 | `Usage: /extensions remove <name>` |
| [25192](/root/laintas_cli/laintas_cli.py:25192) | output | _cmd_extensions / 0 | `Removed {rest[0]}.` |
| [25194](/root/laintas_cli/laintas_cli.py:25194) | output | _cmd_extensions / 0 | `No extension named {rest[0]!r} was found.` |
| [25199](/root/laintas_cli/laintas_cli.py:25199) | output | _cmd_extensions / 0 | `Usage: /extensions publish <name>` |
| [25207](/root/laintas_cli/laintas_cli.py:25207) | output | _cmd_extensions / 0 | `Published {item.get('id')} v{item.get('version')}.` |
| [25209](/root/laintas_cli/laintas_cli.py:25209) | output | _cmd_extensions / 0 | `Community extensions are unreviewed and receive a fresh AI source review before every installation.` |
| [25212](/root/laintas_cli/laintas_cli.py:25212) | output | _cmd_extensions / 0 | `{exc}` |
| [25217](/root/laintas_cli/laintas_cli.py:25217) | output | _cmd_extensions / 0 | `Usage: /extensions trust <name>` |
| [25227](/root/laintas_cli/laintas_cli.py:25227) | output | _cmd_extensions / 0 | `Extension {escape(target)!r} is not installed.` |
| [25230](/root/laintas_cli/laintas_cli.py:25230) | output | _cmd_extensions / 0 | `{escape(facts['name'])} {escape(facts['version'])} — {escape(facts['publisher'])}` |
| [25230](/root/laintas_cli/laintas_cli.py:25230) | output | _cmd_extensions / 0 | `{escape(facts['name'])} {escape(facts['version'])}` |
| [25234](/root/laintas_cli/laintas_cli.py:25234) | output | _cmd_extensions / 0 | `  {escape(facts['description'])}` |
| [25235](/root/laintas_cli/laintas_cli.py:25235) | output | _cmd_extensions / 0 | `  path      {escape(facts['path'])} ({facts['files']} files)` |
| [25237](/root/laintas_cli/laintas_cli.py:25237) | output | _cmd_extensions / 0 | `  tools     {escape(facts['tool_namespace'])}* (schemas the model is offered)` |
| [25240](/root/laintas_cli/laintas_cli.py:25240) | output | _cmd_extensions / 0 | `  skill     {escape(skill['name'])} (instructions injected into the model's context)` |
| [25244](/root/laintas_cli/laintas_cli.py:25244) | output | _cmd_extensions / 0 | `            {escape(skill['description'][:110])}` |
| [25246](/root/laintas_cli/laintas_cli.py:25246) | output | _cmd_extensions / 0 | `  declares  nothing` |
| [25249](/root/laintas_cli/laintas_cli.py:25249) | output | _cmd_extensions / 0 | `An extension runs inside this process with your permissions. What it declares is a disclosure, not a sandbox.` |
| [25255](/root/laintas_cli/laintas_cli.py:25255) | output | _cmd_extensions / 0 | `Not trusted.` |
| [25258](/root/laintas_cli/laintas_cli.py:25258) | output | _cmd_extensions / 0 | `{msg}` |
| [25263](/root/laintas_cli/laintas_cli.py:25263) | output | _cmd_extensions / 0 | `Usage: /extensions untrust <name>` |
| [25266](/root/laintas_cli/laintas_cli.py:25266) | output | _cmd_extensions / 0 | `{msg}` |
| [25271](/root/laintas_cli/laintas_cli.py:25271) | output | _cmd_extensions / 0 | `Usage: /extensions info <name>` |
| [25275](/root/laintas_cli/laintas_cli.py:25275) | output | _cmd_extensions / 0 | `Extension {rest[0]!r} is not installed.` |
| [25280](/root/laintas_cli/laintas_cli.py:25280) | output | _cmd_extensions / 0 | `{manifest.get('name')} v{manifest.get('version')} ⏎   description : {manifest.get('description', '')} ⏎   directory   : {info['directory']} ⏎   integrity   : {info['integrity'][:16]} ⏎   trusted     : {'yes' if trust.get('trusted') else 'no'} ({trust.get('reason', '')}) ⏎   files       : {', '.join(info['files'])}` |
| [25291](/root/laintas_cli/laintas_cli.py:25291) | output | _cmd_extensions / 0 | `Usage: /extensions create <name> [--desc "description"]` |
| [25296](/root/laintas_cli/laintas_cli.py:25296) | output | _cmd_extensions / 0 | `Usage: /extensions create <name> [--desc "description"]` |
| [25301](/root/laintas_cli/laintas_cli.py:25301) | output | _cmd_extensions / 0 | `Created extension scaffold at {target}` |
| [25302](/root/laintas_cli/laintas_cli.py:25302) | output | _cmd_extensions / 0 | `Edit main.py, then: /extensions install {target}` |
| [25304](/root/laintas_cli/laintas_cli.py:25304) | output | _cmd_extensions / 0 | `{exc}` |
| [25309](/root/laintas_cli/laintas_cli.py:25309) | output | _cmd_extensions / 0 | `Usage: /extensions pack <name> [--output <path>]` |
| [25314](/root/laintas_cli/laintas_cli.py:25314) | output | _cmd_extensions / 0 | `Usage: /extensions pack <name> [--output <path>]` |
| [25319](/root/laintas_cli/laintas_cli.py:25319) | output | _cmd_extensions / 0 | `Packed {name} -> {result_path}` |
| [25321](/root/laintas_cli/laintas_cli.py:25321) | output | _cmd_extensions / 0 | `{exc}` |
| [25324](/root/laintas_cli/laintas_cli.py:25324) | output | _cmd_extensions / 0 | `Usage: /extensions [list\|available\|search\|install\|publish\|remove\|trust\|untrust\|info\|create\|pack]` |
| [25336](/root/laintas_cli/laintas_cli.py:25336) | output | _print_message / 0 | `  {position}. {flag}{escape(item.title)}` |
| [25344](/root/laintas_cli/laintas_cli.py:25344) | output | _print_message / 0 | `     {escape(line)}` |
| [25346](/root/laintas_cli/laintas_cli.py:25346) | output | _print_message / 0 | `     {escape(line)}` |
| [25361](/root/laintas_cli/laintas_cli.py:25361) | output | _print_messages / 0 | `  {startup_mail.LOGO_MARK}  No messages.` |
| [25369](/root/laintas_cli/laintas_cli.py:25369) | output | _print_messages / 0 | `  {startup_mail.LOGO_MARK}  {header}` |
| [25376](/root/laintas_cli/laintas_cli.py:25376) | output | _print_messages / 0 | `  Read one with /messages read <n> {symbols.BULLET} mark them all seen with /messages seen` |
| [25436](/root/laintas_cli/laintas_cli.py:25436) | title/menu/help | _build_mailbox_browser.dismiss / message | `Nothing selected.` |
| [25441](/root/laintas_cli/laintas_cli.py:25441) | title/menu/help | _build_mailbox_browser.dismiss / message | `Press x again to dismiss {item.title}` |
| [25446](/root/laintas_cli/laintas_cli.py:25446) | title/menu/help | _build_mailbox_browser.dismiss / message | `Dismissed {item.title}` |
| [25446](/root/laintas_cli/laintas_cli.py:25446) | title/menu/help | _build_mailbox_browser.dismiss / message | `Already gone.` |
| [25453](/root/laintas_cli/laintas_cli.py:25453) | title/menu/help | _build_mailbox_browser.mark_all / message | `Marked {changed} read` |
| [25453](/root/laintas_cli/laintas_cli.py:25453) | title/menu/help | _build_mailbox_browser.mark_all / message | `Nothing was unread` |
| [25462](/root/laintas_cli/laintas_cli.py:25462) | title/menu/help | _build_mailbox_browser.clear_all / message | `Press c again to clear every message` |
| [25467](/root/laintas_cli/laintas_cli.py:25467) | title/menu/help | _build_mailbox_browser.clear_all / message | `Cleared` |
| [25470](/root/laintas_cli/laintas_cli.py:25470) | title/menu/help | _build_mailbox_browser / title | `Messages` |
| [25550](/root/laintas_cli/laintas_cli.py:25550) | error candidate | _cmd_messages | `Usage: /messages read <n\|key>` |
| [25553](/root/laintas_cli/laintas_cli.py:25553) | output | _cmd_messages / 0 | `No message {escape(argument)}.` |
| [25563](/root/laintas_cli/laintas_cli.py:25563) | output | _cmd_messages / 0 | `Marked {changed} message(s) read.` |
| [25563](/root/laintas_cli/laintas_cli.py:25563) | output | _cmd_messages / 0 | `Nothing was unread.` |
| [25569](/root/laintas_cli/laintas_cli.py:25569) | error candidate | _cmd_messages | `Usage: /messages dismiss <n\|key>` |
| [25572](/root/laintas_cli/laintas_cli.py:25572) | output | _cmd_messages / 0 | `No message {escape(argument)}.` |
| [25576](/root/laintas_cli/laintas_cli.py:25576) | output | _cmd_messages / 0 | `Dismissed {escape(title)}.` |
| [25581](/root/laintas_cli/laintas_cli.py:25581) | output | _cmd_messages / 0 | `No messages.` |
| [25584](/root/laintas_cli/laintas_cli.py:25584) | output | _cmd_messages / 0 | `Cleared {len(items)} message(s).` |
| [25588](/root/laintas_cli/laintas_cli.py:25588) | error candidate | _cmd_messages | `Usage: /messages [list\|read <n>\|seen\|dismiss <n>\|clear]` |
| [25605](/root/laintas_cli/laintas_cli.py:25605) | output | _show_mailbox / 0 | `Message browser unavailable: {type(exc).__name__}: {exc}` |
| [25846](/root/laintas_cli/laintas_cli.py:25846) | output | _handle_meta_command_impl / 0 | `Extension command {action} error: {type(exc).__name__}: {exc}` |
| [25883](/root/laintas_cli/laintas_cli.py:25883) | output | _handle_meta_command_impl / 0 | `.laintas/commands.py error: {e}` |
| [25885](/root/laintas_cli/laintas_cli.py:25885) | output | _handle_meta_command_impl / 0 | `Unknown command: {action}` |
| [25886](/root/laintas_cli/laintas_cli.py:25886) | output | _handle_meta_command_impl / 0 | `Type /help for available commands.` |
| [25905](/root/laintas_cli/laintas_cli.py:25905) | output | handle_meta_command / 0 | `{escape(str(exc))}` |
| [25907](/root/laintas_cli/laintas_cli.py:25907) | output | handle_meta_command / 0 | `Command cancelled.` |
| [25912](/root/laintas_cli/laintas_cli.py:25912) | output | handle_meta_command / 0 | `Password vault unavailable. No diagnostic contents were recorded.` |
| [25915](/root/laintas_cli/laintas_cli.py:25915) | output | handle_meta_command / 0 | `/model add failed ({escape(type(exc).__name__)}); the command line was not recorded because it holds a key.` |
| [25923](/root/laintas_cli/laintas_cli.py:25923) | output | handle_meta_command / 0 | `{action or '/'} failed: {type(exc).__name__}: {exc}` |
| [25936](/root/laintas_cli/laintas_cli.py:25936) | output | handle_meta_command / 0 | `The CLI is still running. Use /help {action} to check usage, or /debug to inspect recent activity.` |
| [25957](/root/laintas_cli/laintas_cli.py:25957) | title/menu/help | show_command_palette / title | `Commands — type to filter` |
| [25995](/root/laintas_cli/laintas_cli.py:25995) | output | show_help / 0 | `Unknown command: {escape(command)}` |
| [25996](/root/laintas_cli/laintas_cli.py:25996) | output | show_help / 0 | `Run /help to list available commands.` |
| [26002](/root/laintas_cli/laintas_cli.py:26002) | resolved output candidate | show_help | `{escape(usage)} ⏎  ⏎ {escape(spec.description)}{details} ⏎  ⏎ Aliases: {escape(aliases)}` |
| [26027](/root/laintas_cli/laintas_cli.py:26027) | output | show_help._print_group / 0 | `  {title}` |
| [26120](/root/laintas_cli/laintas_cli.py:26120) | display constant candidate | <module> / SCOUT_PROFILE_TITLE | `Code Specialist` |
| [26122](/root/laintas_cli/laintas_cli.py:26122) | display constant candidate | <module> / SCOUT_PROFILE_DESCRIPTION | `Works on existing codebases: reads before changing, changes in reviewable steps, verifies what it changed` |
| [26150](/root/laintas_cli/laintas_cli.py:26150) | display constant candidate | <module> / FOREMAN_PROFILE_TITLE | `Work Divider` |
| [26152](/root/laintas_cli/laintas_cli.py:26152) | display constant candidate | <module> / FOREMAN_PROFILE_DESCRIPTION | `Given work that may divide: decides whether it actually does, runs the parts that can run at once, and answers for the joined result` |
| [26174](/root/laintas_cli/laintas_cli.py:26174) | display constant candidate | <module> / _LOGO_LINES | ` ╷    ╭─╮  ┬  ╷ ╷ ┌┬┐  ╭─╮  ╭─╮` |
| [26175](/root/laintas_cli/laintas_cli.py:26175) | display constant candidate | <module> / _LOGO_LINES | ` │    ├─┤  │  │╲│  │   ├─┤  ╰─╮` |
| [26176](/root/laintas_cli/laintas_cli.py:26176) | display constant candidate | <module> / _LOGO_LINES | ` ╰──  ╵ ╵  ┴  ╵ ╵  ┴   ╵ ╵  ╰─╯` |
| [26194](/root/laintas_cli/laintas_cli.py:26194) | output | show_banner / 0 | `{line}` |
| [26196](/root/laintas_cli/laintas_cli.py:26196) | output | show_banner / 0 | `  cli v{__version__}  {symbols.BULLET}  {agent_name}` |
| [26266](/root/laintas_cli/laintas_cli.py:26266) | output | show_banner / 0 | `  {key.rjust(label_width)}  │ {value}` |
| [26275](/root/laintas_cli/laintas_cli.py:26275) | startup/notice | show_banner | `How input is routed` |
| [26276](/root/laintas_cli/laintas_cli.py:26276) | startup/notice | show_banner | `PATH commands run directly {symbols.BULLET} plain text → AI` |
| [26282](/root/laintas_cli/laintas_cli.py:26282) | startup/notice | show_banner | `Training-data sharing is off by default` |
| [26283](/root/laintas_cli/laintas_cli.py:26283) | startup/notice | show_banner | `No interaction leaves this machine for training unless you opt in.` |
| [26287](/root/laintas_cli/laintas_cli.py:26287) | startup/notice | show_banner | `Status slots are clickable with the mouse turned on` |
| [26288](/root/laintas_cli/laintas_cli.py:26288) | startup/notice | show_banner | `Click a slot on the prompt's right side to select it (same as Alt+1..9), then scroll or press up/down to change it and Enter to apply; clicking the L> mark opens this list. Mouse reporting takes over drag-to-select in scrollback, so it stays off until you ask for it.` |
| [26302](/root/laintas_cli/laintas_cli.py:26302) | startup/notice | show_banner | `Hold Shift to select text with the mouse` |
| [26303](/root/laintas_cli/laintas_cli.py:26303) | startup/notice | show_banner | `Status slots are clickable while mouse reporting is on, and in exchange the terminal hands clicks to this CLI instead of selecting text. Shift plus drag selects as usual — that works in Windows Terminal, and in every other terminal that supports mouse reporting.` |
| [26529](/root/laintas_cli/laintas_cli.py:26529) | output | _simple_prompt / 0 | `{cwd} ⏎ $ ` |
| [26532](/root/laintas_cli/laintas_cli.py:26532) | error candidate | _simple_prompt | `terminal input closed` |
| [26540](/root/laintas_cli/laintas_cli.py:26540) | error candidate | _simple_prompt | `terminal input closed` |
| [26707](/root/laintas_cli/laintas_cli.py:26707) | output | _queue_supplementary / 0 | `Supplementary instruction queue is full.` |
| [26711](/root/laintas_cli/laintas_cli.py:26711) | output | _queue_supplementary / 0 | `↳ Queued instruction{f' from {escape(source)}' if source != 'local' else ''}: {escape(agent_loop_crop_for_ui(line, 80))}` |
| [26902](/root/laintas_cli/laintas_cli.py:26902) | output | _bg_reader_cbreak_mode / 0 | ` ⏎ Esc is unavailable for this turn: another prompt is holding the terminal. Press Ctrl+C twice quickly to force exit.` |
| [26938](/root/laintas_cli/laintas_cli.py:26938) | resolved output candidate | _bg_reader_cbreak_mode | ` ⏎ Esc received - stopping. A tool already running finishes first; press Ctrl+C twice quickly to force exit now.` |
| [27197](/root/laintas_cli/laintas_cli.py:27197) | output | _flush_deferred_notices / 0 | `Auto-approve active (*). Use /mode to change.` |
| [27204](/root/laintas_cli/laintas_cli.py:27204) | output | _flush_deferred_notices / 0 | `{notice.replace('[', chr(92) + '[')}[/dim]` |
| [27262](/root/laintas_cli/laintas_cli.py:27262) | output | _arrow_approval_prompt / 0 | `  {escape(_action)}  {escape(_target)}` |
| [27268](/root/laintas_cli/laintas_cli.py:27268) | output | _arrow_approval_prompt / 0 | `  {_line_markup(ln)}` |
| [27420](/root/laintas_cli/laintas_cli.py:27420) | output | _compact_parallel_approval_prompt / 0 | `  ⚠ {escape(_action)} {escape(_agent_label)}: {escape(_cmd_disp)}  ({_opts})` |
| [27429](/root/laintas_cli/laintas_cli.py:27429) | output | _compact_parallel_approval_prompt / 0 | `    …` |
| [27431](/root/laintas_cli/laintas_cli.py:27431) | output | _compact_parallel_approval_prompt / 0 | `    {escape(_ln[:160])}` |
| [27443](/root/laintas_cli/laintas_cli.py:27443) | output | _compact_parallel_approval_prompt / 0 | `  ↳ denied — could not ask you: the terminal is held by {escape(_holder)}` |
| [27448](/root/laintas_cli/laintas_cli.py:27448) | output | _compact_parallel_approval_prompt / 0 | `  ↳ {choice or 'no'}` |
| [27448](/root/laintas_cli/laintas_cli.py:27448) | output | _compact_parallel_approval_prompt / 0 | `  ↳ always (auto-approved for this session)` |
| [27492](/root/laintas_cli/laintas_cli.py:27492) | output | _blocking_approval_prompt / 0 | `Approval required but no interactive TTY available — denying.` |
| [27553](/root/laintas_cli/laintas_cli.py:27553) | output | _prompt_for_approval._single_key_approval / 0 | `  {_esc(_action)}  {_esc(body_lines[0]) if body_lines else ''}` |
| [27556](/root/laintas_cli/laintas_cli.py:27556) | output | _prompt_for_approval._single_key_approval / 0 | `  {_esc(_ln)}` |
| [27557](/root/laintas_cli/laintas_cli.py:27557) | output | _prompt_for_approval._single_key_approval / 0 | `  {question} (y/a/n)` |
| [27557](/root/laintas_cli/laintas_cli.py:27557) | output | _prompt_for_approval._single_key_approval / 0 | `  {question} (y/n)` |
| [27566](/root/laintas_cli/laintas_cli.py:27566) | output | _prompt_for_approval._single_key_approval / 0 | `  ↳ denied — could not ask you: the terminal is held by {escape(_key_choice.split(':', 1)[1])}` |
| [27645](/root/laintas_cli/laintas_cli.py:27645) | output | request_command_approval / 0 | `↳ ˋ{command}ˋ auto-approved for this session.` |
| [27711](/root/laintas_cli/laintas_cli.py:27711) | output | request_file_write_approval / 0 | `↳ Writes to {path} auto-approved for this session.` |
| [27738](/root/laintas_cli/laintas_cli.py:27738) | output | _show_plan_approval_menu / 0 | `A plan is ready for review — press Esc to leave the /agents view and approve it in the main CLI.` |
| [27747](/root/laintas_cli/laintas_cli.py:27747) | output | _show_plan_approval_menu / 0 | `{symbols.OK} Revision {approved['revision']} approved. Executing exact SHA {approved['content_sha'][:12]}…` |
| [27820](/root/laintas_cli/laintas_cli.py:27820) | output | _maybe_step_prefill / 0 | `STEP paused. Press Enter to run the next step (input pre-filled with /continue).` |
| [27859](/root/laintas_cli/laintas_cli.py:27859) | output | _run_agent_loop_with_interrupt / 0 | `{queue_detail if queued else detail}` |
| [27894](/root/laintas_cli/laintas_cli.py:27894) | output | _run_agent_loop_with_interrupt._soft_interrupt / 0 | ` ⏎ Force exit.` |
| [27902](/root/laintas_cli/laintas_cli.py:27902) | output | _run_agent_loop_with_interrupt._soft_interrupt / 0 | ` ⏎ Ctrl+C ignored during AI run. Press Esc to interrupt (takes effect within a moment, including while the model is thinking; a tool already running finishes first), or press Ctrl+C twice quickly to force exit.` |
| [27945](/root/laintas_cli/laintas_cli.py:27945) | output | _run_agent_loop_with_interrupt / 0 | `[auto-pilot] overridden by ! prefix - single agent mode` |
| [27973](/root/laintas_cli/laintas_cli.py:27973) | output | _run_agent_loop_with_interrupt / 0 | `[auto-pilot] {strategy} {symbols.BULLET} {len(subtasks)} subtasks{src_label}` |
| [27975](/root/laintas_cli/laintas_cli.py:27975) | output | _run_agent_loop_with_interrupt / 0 | `[auto-pilot] {strategy}` |
| [27991](/root/laintas_cli/laintas_cli.py:27991) | output | _run_agent_loop_with_interrupt / 0 | `[auto-pilot] auto-executing {len(subtasks)} subtasks ({mode})` |
| [28065](/root/laintas_cli/laintas_cli.py:28065) | output | _run_agent_loop_with_interrupt / 0 | `{symbols.FAIL} Agent runtime error: {escape(run_error)} ⏎ The session was preserved. Use /continue to retry from the latest checkpoint.` |
| [28094](/root/laintas_cli/laintas_cli.py:28094) | output | _run_agent_loop_with_interrupt / 0 | `Stopped {_stopped} sub-agent{'s' if _stopped != 1 else ''} started by this turn.` |
| [28288](/root/laintas_cli/laintas_cli.py:28288) | title/menu/help | main / description | `Laintas CLI — Autonomous AI agent` |
| [28291](/root/laintas_cli/laintas_cli.py:28291) | title/menu/help | main / help | `Set agent name (shows in Helpwo AGNETS)` |
| [28293](/root/laintas_cli/laintas_cli.py:28293) | title/menu/help | main / help | `Use a saved account alias, email or userId` |
| [28297](/root/laintas_cli/laintas_cli.py:28297) | title/menu/help | main / help | `Custom backend URL (enters external/unmetered mode; Laintas credentials are stripped)` |
| [28301](/root/laintas_cli/laintas_cli.py:28301) | title/menu/help | main / help | `Serve the Agent Client Protocol (ACP) over stdio — lets ACP editors (Zed & friends) drive this CLI. Optional: pip install agent-client-protocol` |
| [28305](/root/laintas_cli/laintas_cli.py:28305) | title/menu/help | main / help | `Execute a single task non-interactively and exit` |
| [28307](/root/laintas_cli/laintas_cli.py:28307) | title/menu/help | main / help | `Persist/resume --execute context under this logical session id` |
| [28309](/root/laintas_cli/laintas_cli.py:28309) | title/menu/help | main / help | `Nesting depth (0=user terminal, 1+=sub-agent)` |
| [28311](/root/laintas_cli/laintas_cli.py:28311) | title/menu/help | main / help | `Use plain input() instead of prompt_toolkit` |
| [28313](/root/laintas_cli/laintas_cli.py:28313) | title/menu/help | main / help | `Start as a remote executor only — register, heartbeat, and poll HelpwoAI for tasks. No local REPL.` |
| [28316](/root/laintas_cli/laintas_cli.py:28316) | title/menu/help | main / help | `Resume the saved conversation for this directory on startup` |
| [28318](/root/laintas_cli/laintas_cli.py:28318) | title/menu/help | main / help | `Alias for --resume (session continuation)` |
| [28320](/root/laintas_cli/laintas_cli.py:28320) | title/menu/help | main / help | `Pre-assigned agent id (used by sub-terminals)` |
| [28322](/root/laintas_cli/laintas_cli.py:28322) | title/menu/help | main / help | `Pre-assigned agent display name (used by sub-terminals)` |
| [28325](/root/laintas_cli/laintas_cli.py:28325) | title/menu/help | main / help | `Role for the pre-assigned agent (used by sub-terminals)` |
| [28327](/root/laintas_cli/laintas_cli.py:28327) | title/menu/help | main / help | `Name of the terminal this sub-process represents` |
| [28329](/root/laintas_cli/laintas_cli.py:28329) | title/menu/help | main / help | `Name of the parent terminal that spawned this sub-process` |
| [28331](/root/laintas_cli/laintas_cli.py:28331) | title/menu/help | main / help | `Agent id of the parent process that spawned this sub-process` |
| [28333](/root/laintas_cli/laintas_cli.py:28333) | title/menu/help | main / help | `Helpwo backend agent id of the primary CLI that owns this sub-terminal` |
| [28335](/root/laintas_cli/laintas_cli.py:28335) | title/menu/help | main / help | `Hand this sub-terminal over to Helpwo at startup (internal; used by term-new)` |
| [28432](/root/laintas_cli/laintas_cli.py:28432) | output | main / 0 | `Migrating to new directory layout (~/.laintas/)...` |
| [28456](/root/laintas_cli/laintas_cli.py:28456) | output | main / 0 | `Reclaimed {len(_reaped['reaped'])} orphaned sub-agent worktree(s); any uncommitted work is saved as a patch under .laintas/worktrees/.reaped/.` |
| [28462](/root/laintas_cli/laintas_cli.py:28462) | output | main / 0 | `Worktree sweep incomplete: {_reaped['error']}` |
| [28465](/root/laintas_cli/laintas_cli.py:28465) | output | main / 0 | `Worktree sweep skipped: {_exc}` |
| [28476](/root/laintas_cli/laintas_cli.py:28476) | output | main / 0 | `Your prompt template differs from this version's default. Run /reload to sync (this deletes .laintas/ overrides).` |
| [28519](/root/laintas_cli/laintas_cli.py:28519) | output | main / 0 | `Backend mode: {_active_backend.kind} ({_active_backend.base_url}) — external/unmetered; Laintas credentials are not sent.` |
| [28560](/root/laintas_cli/laintas_cli.py:28560) | output | main._run_extension_task / 0 | ` ⏎ ({escape(source)}) {escape(str(text))}` |
| [28569](/root/laintas_cli/laintas_cli.py:28569) | message candidate | main._run_extension_task / error | `still running after {int(EXTENSION_TASK_TIMEOUT)}s` |
| [28592](/root/laintas_cli/laintas_cli.py:28592) | output | main / 0 | `Extension {_ext_name}: {_ext_message}` |
| [28606](/root/laintas_cli/laintas_cli.py:28606) | output | main / 0 | `Organisation layer: {_org_message}` |
| [28609](/root/laintas_cli/laintas_cli.py:28609) | output | main / 0 | `Organisation layer: {_org_exc}` |
| [28620](/root/laintas_cli/laintas_cli.py:28620) | startup/notice | main | `Extension {_ext_name} did not load` |
| [28624](/root/laintas_cli/laintas_cli.py:28624) | startup/notice | main | `Extension runtime failed to start` |
| [28631](/root/laintas_cli/laintas_cli.py:28631) | output | main / 0 | `Authentication required for --execute mode. Use /login first.` |
| [28653](/root/laintas_cli/laintas_cli.py:28653) | startup/notice | main | `Update available: v{_remote_ver}` |
| [28654](/root/laintas_cli/laintas_cli.py:28654) | startup/notice | main | `This session is running v{__version__}. This install is a git checkout; update it with ˋgit pullˋ (/v update will not touch it).` |
| [28654](/root/laintas_cli/laintas_cli.py:28654) | startup/notice | main | `This session is running v{__version__}.` |
| [28702](/root/laintas_cli/laintas_cli.py:28702) | startup/notice | main | `Session store warning` |
| [28712](/root/laintas_cli/laintas_cli.py:28712) | output | main / 0 | `Could not reset persisted session context: {exc}` |
| [28732](/root/laintas_cli/laintas_cli.py:28732) | startup/notice | main | `A previous session in this directory can be resumed` |
| [28733](/root/laintas_cli/laintas_cli.py:28733) | startup/notice | main | `{_summary['turn_count']} turn(s), {_format_time_ago(_summary['timestamp'])}.` |
| [28757](/root/laintas_cli/laintas_cli.py:28757) | startup/notice | main._close_stale_admission | `A previous run was interrupted` |
| [28758](/root/laintas_cli/laintas_cli.py:28758) | startup/notice | main._close_stale_admission | `It was working on: {_text[:160]}` |
| [28758](/root/laintas_cli/laintas_cli.py:28758) | startup/notice | main._close_stale_admission | `Its recovery journal entry has been closed.` |
| [28781](/root/laintas_cli/laintas_cli.py:28781) | output | main / 0 | `Resumed saved session.` |
| [28784](/root/laintas_cli/laintas_cli.py:28784) | output | main / 0 | `No saved session for this directory.` |
| [28835](/root/laintas_cli/laintas_cli.py:28835) | startup/notice | main | `Not connected to Helpwo` |
| [28836](/root/laintas_cli/laintas_cli.py:28836) | startup/notice | main | `Start the local Helpwo companion with /helpwo.` |
| [28840](/root/laintas_cli/laintas_cli.py:28840) | startup/notice | main | `This sub-terminal isn't linked to Helpwo yet.` |
| [28858](/root/laintas_cli/laintas_cli.py:28858) | output | main / 0 | `Application state unreadable: {_app_state_exc}` |
| [28947](/root/laintas_cli/laintas_cli.py:28947) | startup/notice | main | `'{DEFAULT_SUB_AGENT_NAME}' and '{DECOMPOSER_SUB_AGENT_NAME}' are registered and undeployed` |
| [28949](/root/laintas_cli/laintas_cli.py:28949) | startup/notice | main | `{DEFAULT_SUB_AGENT_NAME} works one job carefully. {DECOMPOSER_SUB_AGENT_NAME} is the one to give work that may divide: it is asked whether the request splits into parts that can run at once, and builds a workflow graph when it does. Neither owns a terminal or does anything until you address it.` |
| [28960](/root/laintas_cli/laintas_cli.py:28960) | startup/notice | main | `Could not register the default sub-agents` |
| [29043](/root/laintas_cli/laintas_cli.py:29043) | output | main._trigger_wake_cb / 0 | `trigger-wake: could not start assignment for '{agent_id}'` |
| [29047](/root/laintas_cli/laintas_cli.py:29047) | output | main._trigger_wake_cb / 0 | `trigger-wake error for '{agent_id}': {_exc}` |
| [29097](/root/laintas_cli/laintas_cli.py:29097) | output | main / 0 | `skill {n}: {m}` |
| [29099](/root/laintas_cli/laintas_cli.py:29099) | output | main / 0 | `skill loader error: {_e}` |
| [29106](/root/laintas_cli/laintas_cli.py:29106) | output | main / 0 | `mcp config present but SDK missing: {_get_mcp_mod().MCP_IMPORT_ERROR}` |
| [29107](/root/laintas_cli/laintas_cli.py:29107) | output | main / 0 | `Install with: pip install mcp` |
| [29112](/root/laintas_cli/laintas_cli.py:29112) | status/prompt | main | `Connecting MCP servers…` |
| [29120](/root/laintas_cli/laintas_cli.py:29120) | output | main / 0 | `mcp {n}: {m}` |
| [29122](/root/laintas_cli/laintas_cli.py:29122) | output | main / 0 | `mcp connect error: {_e}` |
| [29149](/root/laintas_cli/laintas_cli.py:29149) | output | main.shutdown / 0 | ` ⏎ Forced exit.` |
| [29188](/root/laintas_cli/laintas_cli.py:29188) | output | main.shutdown / 0 | ` ⏎ Foreground Agent interrupt requested. Press Ctrl+C again to exit.` |
| [29212](/root/laintas_cli/laintas_cli.py:29212) | output | main.shutdown / 0 | ` ⏎ Shutting down...` |
| [29282](/root/laintas_cli/laintas_cli.py:29282) | error candidate | main | `could not bind agent '{local_agent_id}' to local term0` |
| [29284](/root/laintas_cli/laintas_cli.py:29284) | output | main / 0 | `term0 bash session init failed: {_e}` |
| [29293](/root/laintas_cli/laintas_cli.py:29293) | output | main / 0 | `Terminal linking unavailable: {_link_error}` |
| [29302](/root/laintas_cli/laintas_cli.py:29302) | output | main / 0 | `--monitor-only requires authentication. Run /login first.` |
| [29305](/root/laintas_cli/laintas_cli.py:29305) | output | main / 0 | `--monitor-only requires successful agent registration. Check backend URL and credentials.` |
| [29308](/root/laintas_cli/laintas_cli.py:29308) | resolved output candidate | main | `Monitor-only mode active ⏎ Agent: {agent_registry.agent_name} ({agent_registry.agent_id}) ⏎ Listening for remote exec/query/delegate requests… ⏎ Ctrl+C to exit.` |
| [29313](/root/laintas_cli/laintas_cli.py:29313) | title/menu/help | main / title | `laintas-cli monitor` |
| [29378](/root/laintas_cli/laintas_cli.py:29378) | output | main / 0 | `Recovered from a stale event-loop flag; continuing.` |
| [29441](/root/laintas_cli/laintas_cli.py:29441) | output | main / 0 | `Logged out. Goodbye!` |
| [29465](/root/laintas_cli/laintas_cli.py:29465) | output | main / 0 | `Invalid /resume argument: {_resume_arg}` |
| [29466](/root/laintas_cli/laintas_cli.py:29466) | output | main / 0 | `Usage: /resume [N\|all\|latest]  (N = messages to echo, default 20, 0 = silent)` |
| [29474](/root/laintas_cli/laintas_cli.py:29474) | output | main / 0 | `No saved session to resume in this directory.` |
| [29494](/root/laintas_cli/laintas_cli.py:29494) | output | main / 0 | `Switched to saved session.` |
| [29518](/root/laintas_cli/laintas_cli.py:29518) | output | main / 0 | `Usage: /fork [name] — quote a name that contains spaces` |
| [29533](/root/laintas_cli/laintas_cli.py:29533) | output | main / 0 | `Invalid fork name. Use a non-empty name (max 64 chars).` |
| [29550](/root/laintas_cli/laintas_cli.py:29550) | output | main / 0 | `Fork already exists at path: {' › '.join(_new_lineage)}` |
| [29552](/root/laintas_cli/laintas_cli.py:29552) | output | main / 0 | `Choose a different name or delete the existing fork first.` |
| [29565](/root/laintas_cli/laintas_cli.py:29565) | output | main / 0 | `Forked "{_fork_name}" {symbols.BULLET} {_n} turn(s) {_ago} {symbols.BULLET} Resume with /resume` |
| [29569](/root/laintas_cli/laintas_cli.py:29569) | output | main / 0 | `Could not save fork (no conversation to snapshot).` |
| [29637](/root/laintas_cli/laintas_cli.py:29637) | output | main / 0 | `Started a new branched session {_branch_path} {symbols.BULLET} {_n} turn(s) inherited {symbols.BULLET} Previous context saved for /resume` |
| [29652](/root/laintas_cli/laintas_cli.py:29652) | output | main / 0 | `Usage: {escape(_new_parts[0].lower())} — takes no arguments` |
| [29670](/root/laintas_cli/laintas_cli.py:29670) | output | main / 0 | `New session cancelled.` |
| [29678](/root/laintas_cli/laintas_cli.py:29678) | output | main / 0 | `Could not detach the previous session context: {exc}` |
| [29697](/root/laintas_cli/laintas_cli.py:29697) | output | main / 0 | `Started a new session.` |
| [29724](/root/laintas_cli/laintas_cli.py:29724) | output | main / 0 | `Saved resume checkpoint: {_format_time_ago(_checkpoint.get('timestamp', 0))} {symbols.BULLET} {_checkpoint.get('title', 'Untitled session')}` |
| [29745](/root/laintas_cli/laintas_cli.py:29745) | output | main / 0 | `/private is only available from the local owner terminal.` |
| [29751](/root/laintas_cli/laintas_cli.py:29751) | output | main / 0 | `/password can only be opened from the local terminal; remote and injected entry is disabled by design.` |
| [29818](/root/laintas_cli/laintas_cli.py:29818) | output | main / 0 | `{_queue_detail if _queued else 'Could not queue input.'}` |
| [29821](/root/laintas_cli/laintas_cli.py:29821) | output | main / 0 | `user_message` |
| [29832](/root/laintas_cli/laintas_cli.py:29832) | output | main / 0 | `> {user_input}` |
| [29939](/root/laintas_cli/laintas_cli.py:29939) | output | main / 0 | `PLAN task set from this message.` |
| [29966](/root/laintas_cli/laintas_cli.py:29966) | output | main / 0 | ` ⏎ $ {escape(user_input)}` |
| [29975](/root/laintas_cli/laintas_cli.py:29975) | output | main / 0 | `{_command_denial}` |
| [30120](/root/laintas_cli/laintas_cli.py:30120) | output | main._shell_answer / 0 | ` ⏎ {escape(prompt)} (input hidden; press Enter when done, Esc stops the command)` |
| [30120](/root/laintas_cli/laintas_cli.py:30120) | output | main._shell_answer / 0 | ` ⏎ {escape(prompt)} (type an answer and press Enter; Esc stops the command)` |
| [30178](/root/laintas_cli/laintas_cli.py:30178) | output | main / 0 | `{_note}` |
| [30183](/root/laintas_cli/laintas_cli.py:30183) | output | main / 0 | `Interactive terminal programs can't run inside the /agents view — press Esc to return to the main CLI first.` |
| [30241](/root/laintas_cli/laintas_cli.py:30241) | output | main / 0 | `Not authenticated. Use /login first.` |
| [30282](/root/laintas_cli/laintas_cli.py:30282) | output | main / 0 | `Re-running task in ACT mode...` |
| [30347](/root/laintas_cli/laintas_cli.py:30347) | output | <module> / 0 | ` ⏎ Cancelled. Returning to the terminal.` |

### laintas_cli_download/public/install.ps1

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [18](/root/laintas_cli/laintas_cli_download/public/install.ps1:18) | installer source | <bootstrap> | `function Write-LaintasText([string]$Text, [ConsoleColor]$Color, [bool]$Plain) {` |
| [19](/root/laintas_cli/laintas_cli_download/public/install.ps1:19) | installer source | <bootstrap> | `if ($Plain) { Write-Host -NoNewline $Text }` |
| [28](/root/laintas_cli/laintas_cli_download/public/install.ps1:28) | installer source | <bootstrap> | `Write-Host -NoNewline ("$esc[38;2;" + $rgb + 'm' + $Text + "$esc[0m")` |
| [29](/root/laintas_cli/laintas_cli_download/public/install.ps1:29) | installer source | <bootstrap> | `} else { Write-Host -NoNewline $Text -ForegroundColor $Color }` |
| [38](/root/laintas_cli/laintas_cli_download/public/install.ps1:38) | installer source | <bootstrap> | `Write-Host -NoNewline ("ˋr" + (' ' * ($State.Width - 1)) + "ˋr")` |
| [44](/root/laintas_cli/laintas_cli_download/public/install.ps1:44) | installer source | <bootstrap> | `Write-LaintasText "  $mark $word" $color $State.Plain` |
| [47](/root/laintas_cli/laintas_cli_download/public/install.ps1:47) | installer source | <bootstrap> | `Write-LaintasText $summary Gray $State.Plain` |
| [48](/root/laintas_cli/laintas_cli_download/public/install.ps1:48) | installer source | <bootstrap> | `Write-Host ''` |
| [77](/root/laintas_cli/laintas_cli_download/public/install.ps1:77) | installer source | <bootstrap> | `Write-LaintasText ('  ' + $rail) Green $State.Plain` |
| [79](/root/laintas_cli/laintas_cli_download/public/install.ps1:79) | installer source | <bootstrap> | `Write-LaintasText ([string][char]0x2578) Green $State.Plain` |
| [80](/root/laintas_cli/laintas_cli_download/public/install.ps1:80) | installer source | <bootstrap> | `Write-LaintasText (([string][char]0x2500) * ($railWidth - $filled - 1)) DarkGray $State.Plain` |
| [87](/root/laintas_cli/laintas_cli_download/public/install.ps1:87) | installer source | <bootstrap> | `Write-LaintasText ('  ' + (([string][char]0x2500) * $position)) DarkGray $State.Plain` |
| [88](/root/laintas_cli/laintas_cli_download/public/install.ps1:88) | installer source | <bootstrap> | `Write-LaintasText (([string][char]0x2501) * $pulse) Green $State.Plain` |
| [89](/root/laintas_cli/laintas_cli_download/public/install.ps1:89) | installer source | <bootstrap> | `Write-LaintasText (([string][char]0x2500) * ($railWidth - $pulse - $position)) DarkGray $State.Plain` |
| [94](/root/laintas_cli/laintas_cli_download/public/install.ps1:94) | installer source | <bootstrap> | `Write-LaintasText $row Gray $State.Plain` |
| [108](/root/laintas_cli/laintas_cli_download/public/install.ps1:108) | installer source | <bootstrap> | `Write-LaintasText ('  ' + [char]0x2193 + ' ') Green $state.Plain` |
| [109](/root/laintas_cli/laintas_cli_download/public/install.ps1:109) | installer source | <bootstrap> | `Write-LaintasText 'LaintasSetup.exe' Gray $state.Plain` |
| [110](/root/laintas_cli/laintas_cli_download/public/install.ps1:110) | installer source | <bootstrap> | `Write-Host ''` |
| [141](/root/laintas_cli/laintas_cli_download/public/install.ps1:141) | installer source | <bootstrap> | `if ($state.Bytes -ne $ExpectedSize) { throw 'The installer download is incomplete.' }` |
| [145](/root/laintas_cli/laintas_cli_download/public/install.ps1:145) | installer source | <bootstrap> | `throw` |
| [163](/root/laintas_cli/laintas_cli_download/public/install.ps1:163) | installer source | <bootstrap> | `throw 'The Windows release metadata is invalid. No installer was run.'` |
| [169](/root/laintas_cli/laintas_cli_download/public/install.ps1:169) | installer source | <bootstrap> | `Write-Host "Laintas Windows v$version"` |
| [173](/root/laintas_cli/laintas_cli_download/public/install.ps1:173) | installer source | <bootstrap> | `throw 'The installer failed its size or SHA-256 check. No installer was run.'` |
| [175](/root/laintas_cli/laintas_cli_download/public/install.ps1:175) | installer source | <bootstrap> | `Write-Host 'Installer verified. Opening the installation wizard...'` |
| [177](/root/laintas_cli/laintas_cli_download/public/install.ps1:177) | installer source | <bootstrap> | `if ($process.ExitCode -ne 0) { throw "The installer exited with code $($process.ExitCode)." }` |
| [178](/root/laintas_cli/laintas_cli_download/public/install.ps1:178) | installer source | <bootstrap> | `Write-Host 'Installation completed. Start Laintas Windows from the Start menu.'` |

### laintas_cli_download/public/install.sh

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [49](/root/laintas_cli/laintas_cli_download/public/install.sh:49) | installer source | <bootstrap> | `printf '  %s↓ %s%s%s\n' "$green" "$muted" "$display_label" "$reset" >&2` |
| [64](/root/laintas_cli/laintas_cli_download/public/install.sh:64) | installer source | <bootstrap> | `END { printf "%.0f", n }` |
| [92](/root/laintas_cli/laintas_cli_download/public/install.sh:92) | installer source | <bootstrap> | `for(i=0;i<rail;i++) printf "%s%s", (i<=filled ? green : track), (i<filled ? "━" : (i==filled ? "╸" : "─"))` |
| [96](/root/laintas_cli/laintas_cli_download/public/install.sh:96) | installer source | <bootstrap> | `for(i=0;i<rail;i++) printf "%s%s", (i>=position && i<position+pulse ? green : track), (i>=position && i<position+pulse ? "━" : "─")` |
| [99](/root/laintas_cli/laintas_cli_download/public/install.sh:99) | installer source | <bootstrap> | `printf "%s  %s%s%s%s%s", reset, bold, substr(metrics,1,4), reset, muted, substr(metrics,5,room>4 ? room-4 : 0)` |
| [100](/root/laintas_cli/laintas_cli_download/public/install.sh:100) | installer source | <bootstrap> | `printf "%s", reset` |
| [102](/root/laintas_cli/laintas_cli_download/public/install.sh:102) | installer source | <bootstrap> | `printf '\r\033[2K  %s' "$line" >&2` |
| [112](/root/laintas_cli/laintas_cli_download/public/install.sh:112) | installer source | <bootstrap> | `if [ "$interactive" -eq 1 ]; then printf '\r\033[2K' >&2; fi` |
| [114](/root/laintas_cli/laintas_cli_download/public/install.sh:114) | installer source | <bootstrap> | `printf '  %s✗ Failed%s\n' "$red" "$reset" >&2` |
| [122](/root/laintas_cli/laintas_cli_download/public/install.sh:122) | installer source | <bootstrap> | `printf "%s · %02d:%02d", amount, int(s/60), s%60` |
| [125](/root/laintas_cli/laintas_cli_download/public/install.sh:125) | installer source | <bootstrap> | `printf '  %s✓ 100%%%s\n' "$green" "$reset" >&2` |
| [128](/root/laintas_cli/laintas_cli_download/public/install.sh:128) | installer source | <bootstrap> | `printf '  %s✓ 100%%%s  %s%s%s\n' "$green" "$reset" "$muted" "$line" "$reset" >&2` |
| [131](/root/laintas_cli/laintas_cli_download/public/install.sh:131) | installer source | <bootstrap> | `echo ""` |
| [132](/root/laintas_cli/laintas_cli_download/public/install.sh:132) | installer source | <bootstrap> | `echo "── Laintas CLI Installer ─────────────────────────────────────"` |
| [144](/root/laintas_cli/laintas_cli_download/public/install.sh:144) | installer source | <bootstrap> | `echo "Unsupported OS: $UNAME_S"` |
| [145](/root/laintas_cli/laintas_cli_download/public/install.sh:145) | installer source | <bootstrap> | `echo "Supported platforms: Linux, macOS and 64-bit Windows"` |
| [155](/root/laintas_cli/laintas_cli_download/public/install.sh:155) | installer source | <bootstrap> | `echo "Unsupported Linux architecture: $(uname -m)"` |
| [156](/root/laintas_cli/laintas_cli_download/public/install.sh:156) | installer source | <bootstrap> | `echo "Supported architectures: x86_64/amd64 and aarch64/arm64"` |
| [160](/root/laintas_cli/laintas_cli_download/public/install.sh:160) | installer source | <bootstrap> | `echo "  Detected: Linux $ARCH"` |
| [168](/root/laintas_cli/laintas_cli_download/public/install.sh:168) | installer source | <bootstrap> | `echo "  Extracting package…"` |
| [171](/root/laintas_cli/laintas_cli_download/public/install.sh:171) | installer source | <bootstrap> | `echo "  Installing to /usr/local/bin…"` |
| [179](/root/laintas_cli/laintas_cli_download/public/install.sh:179) | installer source | <bootstrap> | `echo "Invalid package: install.sh was not found after extraction"` |
| [183](/root/laintas_cli/laintas_cli_download/public/install.sh:183) | installer source | <bootstrap> | `printf '\n  Done! Run ˋlaintas-cliˋ to start.\n\n'` |
| [188](/root/laintas_cli/laintas_cli_download/public/install.sh:188) | installer source | <bootstrap> | `*) echo "Unsupported Mac architecture: $(uname -m)"; exit 1 ;;` |
| [190](/root/laintas_cli/laintas_cli_download/public/install.sh:190) | installer source | <bootstrap> | `echo "  Detected: macOS $ARCH"` |
| [196](/root/laintas_cli/laintas_cli_download/public/install.sh:196) | installer source | <bootstrap> | `*[!a-zA-Z0-9._-]*) echo "Invalid release tag"; exit 1 ;;` |
| [208](/root/laintas_cli/laintas_cli_download/public/install.sh:208) | installer source | <bootstrap> | `echo "Checksum verification failed for $ASSET" >&2` |
| [213](/root/laintas_cli/laintas_cli_download/public/install.sh:213) | installer source | <bootstrap> | `printf '\n  Done! Run ˋlaintas-cliˋ to start.\n\n'` |
| [215](/root/laintas_cli/laintas_cli_download/public/install.sh:215) | installer source | <bootstrap> | `echo "  Detected: Windows amd64"` |
| [217](/root/laintas_cli/laintas_cli_download/public/install.sh:217) | installer source | <bootstrap> | `echo "PowerShell is required to install Laintas CLI for Windows."` |
| [224](/root/laintas_cli/laintas_cli_download/public/install.sh:224) | installer source | <bootstrap> | `echo "── ────────────────────────────────────────────────────────────"` |

### local_runtime.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [254](/root/laintas_cli/local_runtime.py:254) | message candidate | run_exec / error | `missing reqId/cmd` |
| [270](/root/laintas_cli/local_runtime.py:270) | message candidate | run_exec / error | `invalid cwd: {body.get('cwd') or '(root)'}` |
| [277](/root/laintas_cli/local_runtime.py:277) | message candidate | run_exec / error | `too many commands are already running` |
| [286](/root/laintas_cli/local_runtime.py:286) | message candidate | run_exec / error | `Blocked by policy: {decision.reason}` |
| [307](/root/laintas_cli/local_runtime.py:307) | message candidate | run_exec / error | `No answer from the user — still unapproved, not refused; send it again if still needed` |
| [312](/root/laintas_cli/local_runtime.py:312) | message candidate | run_exec / error | `User denied: {cmd[:100]}` |
| [316](/root/laintas_cli/local_runtime.py:316) | message candidate | run_exec / error | `aborted` |
| [360](/root/laintas_cli/local_runtime.py:360) | message candidate | _stream_process / error | `aborted` |
| [370](/root/laintas_cli/local_runtime.py:370) | message candidate | _stream_process / error | `no output for {timeout}s; command stopped.{_buffered_pipeline_hint(cmd)}` |
| [406](/root/laintas_cli/local_runtime.py:406) | message candidate | _stream_process / error | `aborted` |
| [734](/root/laintas_cli/local_runtime.py:734) | message candidate | proxy_request / error | `bad port` |
| [752](/root/laintas_cli/local_runtime.py:752) | message candidate | proxy_request / error | `response too large to tunnel` |
| [758](/root/laintas_cli/local_runtime.py:758) | message candidate | proxy_request / error | `cannot reach 127.0.0.1:{port}: {e}` |

### mcp_client.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [153](/root/laintas_cli/mcp_client.py:153) | error candidate | MCPManager._submit | `MCP loop not started` |
| [402](/root/laintas_cli/mcp_client.py:402) | message candidate | MCPManager._adapt_tool._invoke / error | `server '{srv_name}' is not connected` |
| [409](/root/laintas_cli/mcp_client.py:409) | message candidate | MCPManager._adapt_tool._invoke / error | `timeout after {srv_now.call_timeout()}s` |
| [411](/root/laintas_cli/mcp_client.py:411) | message candidate | MCPManager._adapt_tool._invoke / error | `{type(e).__name__}: {e}` |

### mem_extract.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [247](/root/laintas_cli/mem_extract.py:247) | startup/notice | _warn_if_merge_unavailable | `Memory de-duplication is degraded` |
| [248](/root/laintas_cli/mem_extract.py:248) | startup/notice | _warn_if_merge_unavailable | `The embedding endpoint is unreachable, so new memories can only be matched against existing ones by exact name. Near-duplicates will accumulate until it is back. Last error: {reason}` |
| [248](/root/laintas_cli/mem_extract.py:248) | startup/notice | _warn_if_merge_unavailable | `The embedding endpoint is unreachable, so new memories can only be matched against existing ones by exact name. Near-duplicates will accumulate until it is back.` |
| [273](/root/laintas_cli/mem_extract.py:273) | startup/notice | _enforce_budget_quietly | `Archived {len(archived)} memories over the store budget` |
| [274](/root/laintas_cli/mem_extract.py:274) | startup/notice | _enforce_budget_quietly | `The weakest entries by importance, last use and use count were moved to memory/archive/ (kept, not deleted): {names} …` |
| [274](/root/laintas_cli/mem_extract.py:274) | startup/notice | _enforce_budget_quietly | `The weakest entries by importance, last use and use count were moved to memory/archive/ (kept, not deleted): {names}` |

### mem_signals.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [104](/root/laintas_cli/mem_signals.py:104) | title/menu/help | on_write / text | `{desc} \| {body_prev}` |
| [278](/root/laintas_cli/mem_signals.py:278) | title/menu/help | _main / description | `Build the memory-salience training set (and recall event log) from captured .laintas/mem_signals.jsonl files.` |
| [281](/root/laintas_cli/mem_signals.py:281) | title/menu/help | _main / help | `Dirs/files to scan. Default: $HOME and cwd.` |
| [289](/root/laintas_cli/mem_signals.py:289) | output | _main / 0 | `── memory-salience dataset ──` |
| [290](/root/laintas_cli/mem_signals.py:290) | output | _main / 0 | `scanned files : {rep['files_scanned']}` |
| [291](/root/laintas_cli/mem_signals.py:291) | output | _main / 0 | `rows read     : {rep['rows_read']}  (bad {rep['malformed']})` |
| [292](/root/laintas_cli/mem_signals.py:292) | output | _main / 0 | `events        : write {rep['writes']} · delete {rep['deletes']} · search {rep['searches']}` |
| [293](/root/laintas_cli/mem_signals.py:293) | output | _main / 0 | `salience       : keep {rep['salience_keep']} · drop {rep['salience_drop']} · skipped {rep['salience_skipped']}` |
| [294](/root/laintas_cli/mem_signals.py:294) | output | _main / 0 | `train / val    : {rep['train']} / {rep['val']}` |
| [295](/root/laintas_cli/mem_signals.py:295) | output | _main / 0 | `→ {rep['train_path']}` |
| [296](/root/laintas_cli/mem_signals.py:296) | output | _main / 0 | `→ {rep['val_path']}` |
| [297](/root/laintas_cli/mem_signals.py:297) | output | _main / 0 | `→ {rep['recall_path']}  (raw search events, awaiting relevance labels)` |

### memory_system.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [281](/root/laintas_cli/memory_system.py:281) | error candidate | _memory_path | `memory name must be a safe slug without path separators` |
| [286](/root/laintas_cli/memory_system.py:286) | error candidate | _memory_path | `memory name escapes the memory directory` |
| [513](/root/laintas_cli/memory_system.py:513) | error candidate | _resolve_scope | `scope must be 'user' or 'project'` |

### migrate.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [159](/root/laintas_cli/migrate.py:159) | output | migrate_all / 0 | `  [migrate] {desc}: {old.name} → {new}` |
| [163](/root/laintas_cli/migrate.py:163) | output | migrate_all / 0 | `  [migrate] ERROR {desc}: {outcome}` |
| [168](/root/laintas_cli/migrate.py:168) | output | migrate_all / 0 | `  [migrate] Done — {n} item(s) migrated to {paths.LAINTAS_HOME}` |

### mode_manager.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [416](/root/laintas_cli/mode_manager.py:416) | error candidate | _save_config | `refusing to replace symlinked modes.json` |

### model_route.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [141](/root/laintas_cli/model_route.py:141) | error candidate | normalize_url | `provider URL must be an absolute http(s) URL` |
| [143](/root/laintas_cli/model_route.py:143) | error candidate | normalize_url | `provider URL must not contain credentials, query or fragment` |
| [147](/root/laintas_cli/model_route.py:147) | error candidate | normalize_url | `provider URL must be https (http only for localhost)` |
| [210](/root/laintas_cli/model_route.py:210) | error candidate | save_route | `unknown preset: {preset}` |
| [213](/root/laintas_cli/model_route.py:213) | error candidate | save_route | `key variable must be a plain environment variable name` |
| [234](/root/laintas_cli/model_route.py:234) | error candidate | set_route_model | `unknown model route: {name}` |
| [468](/root/laintas_cli/model_route.py:468) | error candidate | fetch_catalog | `HTTP {response.status_code}: {reason or 'request refused'}` |

### password_vault.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [155](/root/laintas_cli/password_vault.py:155) | error candidate | _clean_description | `description must be text` |
| [158](/root/laintas_cli/password_vault.py:158) | error candidate | _clean_description | `description must not be empty` |
| [160](/root/laintas_cli/password_vault.py:160) | error candidate | _clean_description | `description longer than {_DESCRIPTION_MAX} characters` |
| [162](/root/laintas_cli/password_vault.py:162) | error candidate | _clean_description | `description must be a single line` |
| [169](/root/laintas_cli/password_vault.py:169) | error candidate | _clean_origins | `origins must be a non-empty list of exact HTTPS origins` |
| [175](/root/laintas_cli/password_vault.py:175) | error candidate | _clean_origins | `not an exact HTTPS origin: {origin!r} (no http, wildcards, paths, userinfo, or subdomain expansion)` |
| [186](/root/laintas_cli/password_vault.py:186) | error candidate | _validate_origins_list | `entry origins must be a list` |
| [188](/root/laintas_cli/password_vault.py:188) | error candidate | _validate_origins_list | `entry origins must be a non-empty list` |
| [192](/root/laintas_cli/password_vault.py:192) | error candidate | _validate_origins_list | `entry origin is not canonical: {origin!r}` |
| [194](/root/laintas_cli/password_vault.py:194) | error candidate | _validate_origins_list | `duplicate entry origin` |
| [200](/root/laintas_cli/password_vault.py:200) | error candidate | _validate_kdf | `vault header: kdf block missing` |
| [202](/root/laintas_cli/password_vault.py:202) | error candidate | _validate_kdf | `vault header: unexpected kdf fields` |
| [204](/root/laintas_cli/password_vault.py:204) | error candidate | _validate_kdf | `vault header: unsupported kdf algorithm` |
| [207](/root/laintas_cli/password_vault.py:207) | error candidate | _validate_kdf | `vault header: non-integer kdf parameter` |
| [209](/root/laintas_cli/password_vault.py:209) | error candidate | _validate_kdf | `vault header: kdf n out of accepted range` |
| [211](/root/laintas_cli/password_vault.py:211) | error candidate | _validate_kdf | `vault header: kdf r/p out of accepted range` |
| [213](/root/laintas_cli/password_vault.py:213) | error candidate | _validate_kdf | `vault header: unsupported key length` |
| [219](/root/laintas_cli/password_vault.py:219) | error candidate | _validate_kdf | `vault header: kdf resource limit exceeded` |
| [223](/root/laintas_cli/password_vault.py:223) | error candidate | _validate_kdf | `vault header: salt is not valid base64` |
| [225](/root/laintas_cli/password_vault.py:225) | error candidate | _validate_kdf | `vault header: salt too short` |
| [242](/root/laintas_cli/password_vault.py:242) | error candidate | _derive_key | `vault key derivation failed` |
| [248](/root/laintas_cli/password_vault.py:248) | error candidate | _split_vault_file | `vault file: bad magic (not a password vault or wrong format)` |
| [252](/root/laintas_cli/password_vault.py:252) | error candidate | _split_vault_file | `vault file: header is corrupt` |
| [258](/root/laintas_cli/password_vault.py:258) | error candidate | _split_vault_file | `vault file: header is not valid JSON` |
| [262](/root/laintas_cli/password_vault.py:262) | error candidate | _split_vault_file | `vault file: payload is truncated` |
| [277](/root/laintas_cli/password_vault.py:277) | error candidate | _decrypt_blob | `vault decryption failed (wrong passphrase or tampered file)` |
| [281](/root/laintas_cli/password_vault.py:281) | error candidate | _decrypt_blob | `vault payload is not valid JSON` |
| [295](/root/laintas_cli/password_vault.py:295) | error candidate | _validate_entry | `vault payload: bad entry id` |
| [297](/root/laintas_cli/password_vault.py:297) | error candidate | _validate_entry | `vault payload: entry must be an object` |
| [301](/root/laintas_cli/password_vault.py:301) | error candidate | _validate_entry | `vault payload: unexpected entry fields` |
| [304](/root/laintas_cli/password_vault.py:304) | error candidate | _validate_entry | `vault payload: unexpected entry fields` |
| [306](/root/laintas_cli/password_vault.py:306) | error candidate | _validate_entry | `vault payload: unknown entry kind` |
| [308](/root/laintas_cli/password_vault.py:308) | error candidate | _validate_entry | `vault payload: non-canonical description` |
| [313](/root/laintas_cli/password_vault.py:313) | error candidate | _validate_entry | `vault payload: bad entry field type` |
| [317](/root/laintas_cli/password_vault.py:317) | error candidate | _validate_entry | `vault payload: bad entry field type` |
| [319](/root/laintas_cli/password_vault.py:319) | error candidate | _validate_entry | `vault payload: empty secret value` |
| [345](/root/laintas_cli/password_vault.py:345) | error candidate | _migrate_blob | `vault payload: not an object` |
| [348](/root/laintas_cli/password_vault.py:348) | error candidate | _migrate_blob | `vault payload: unexpected fields` |
| [350](/root/laintas_cli/password_vault.py:350) | error candidate | _migrate_blob | `vault payload: entries must be an object` |
| [355](/root/laintas_cli/password_vault.py:355) | error candidate | _migrate_blob | `vault payload: unsupported format` |
| [357](/root/laintas_cli/password_vault.py:357) | error candidate | _migrate_blob | `vault payload: unexpected fields` |
| [360](/root/laintas_cli/password_vault.py:360) | error candidate | _migrate_blob | `vault payload: entries must be an object` |
| [364](/root/laintas_cli/password_vault.py:364) | error candidate | _migrate_blob | `vault payload: un-migratable legacy entry {eid}` |
| [372](/root/laintas_cli/password_vault.py:372) | error candidate | _validate_blob | `vault payload: not an object` |
| [374](/root/laintas_cli/password_vault.py:374) | error candidate | _validate_blob | `vault payload: unsupported format` |
| [376](/root/laintas_cli/password_vault.py:376) | error candidate | _validate_blob | `vault payload: unexpected fields` |
| [379](/root/laintas_cli/password_vault.py:379) | error candidate | _validate_blob | `vault payload: entries must be an object` |
| [387](/root/laintas_cli/password_vault.py:387) | error candidate | _validate_cache | `metadata cache: unsupported format` |
| [389](/root/laintas_cli/password_vault.py:389) | error candidate | _validate_cache | `metadata cache: unexpected fields` |
| [392](/root/laintas_cli/password_vault.py:392) | error candidate | _validate_cache | `metadata cache: entries must be a list` |
| [396](/root/laintas_cli/password_vault.py:396) | error candidate | _validate_cache | `metadata cache: entry must be an object` |
| [399](/root/laintas_cli/password_vault.py:399) | error candidate | _validate_cache | `metadata cache: unexpected entry fields` |
| [401](/root/laintas_cli/password_vault.py:401) | error candidate | _validate_cache | `metadata cache: bad entry id` |
| [403](/root/laintas_cli/password_vault.py:403) | error candidate | _validate_cache | `metadata cache: duplicate entry id` |
| [406](/root/laintas_cli/password_vault.py:406) | error candidate | _validate_cache | `metadata cache: unknown entry kind` |
| [408](/root/laintas_cli/password_vault.py:408) | error candidate | _validate_cache | `metadata cache: non-canonical description` |
| [413](/root/laintas_cli/password_vault.py:413) | error candidate | _validate_cache | `metadata cache: bad timestamp` |
| [433](/root/laintas_cli/password_vault.py:433) | error candidate | _write_all | `short write` |
| [553](/root/laintas_cli/password_vault.py:553) | error candidate | PasswordVault._require_unlocked | `vault is locked` |
| [563](/root/laintas_cli/password_vault.py:563) | error candidate | PasswordVault._read_raw | `no vault exists yet` |
| [571](/root/laintas_cli/password_vault.py:571) | error candidate | PasswordVault._decrypt_current | `vault changed; unlock again` |
| [606](/root/laintas_cli/password_vault.py:606) | error candidate | PasswordVault.create | `passphrase must not be empty` |
| [611](/root/laintas_cli/password_vault.py:611) | error candidate | PasswordVault.create | `a vault already exists` |
| [629](/root/laintas_cli/password_vault.py:629) | error candidate | PasswordVault.unlock | `no vault exists yet` |
| [659](/root/laintas_cli/password_vault.py:659) | error candidate | PasswordVault.change_passphrase | `passphrase must not be empty` |
| [709](/root/laintas_cli/password_vault.py:709) | error candidate | PasswordVault.add_entry | `login entries need both username and password` |
| [716](/root/laintas_cli/password_vault.py:716) | error candidate | PasswordVault.add_entry | `secret entries need a non-empty secret value` |
| [721](/root/laintas_cli/password_vault.py:721) | error candidate | PasswordVault.add_entry | `unknown entry kind: {kind!r}` |
| [771](/root/laintas_cli/password_vault.py:771) | error candidate | PasswordVault.update_entry.apply | `login entries need at least one origin` |
| [774](/root/laintas_cli/password_vault.py:774) | error candidate | PasswordVault.update_entry.apply | `only login entries have a username` |
| [778](/root/laintas_cli/password_vault.py:778) | error candidate | PasswordVault.update_entry.apply | `only login entries have a password` |
| [782](/root/laintas_cli/password_vault.py:782) | error candidate | PasswordVault.update_entry.apply | `only secret entries have a secret value` |
| [784](/root/laintas_cli/password_vault.py:784) | error candidate | PasswordVault.update_entry.apply | `secret value must not be empty` |
| [819](/root/laintas_cli/password_vault.py:819) | error candidate | PasswordVault.list_entries | `no vault exists yet` |
| [822](/root/laintas_cli/password_vault.py:822) | error candidate | PasswordVault.list_entries | `no vault exists yet` |
| [831](/root/laintas_cli/password_vault.py:831) | error candidate | PasswordVault.list_entries | `metadata cache unavailable; unlock to read entries` |
| [836](/root/laintas_cli/password_vault.py:836) | error candidate | PasswordVault.list_entries | `metadata cache is missing; unlock the vault to rebuild it` |
| [840](/root/laintas_cli/password_vault.py:840) | error candidate | PasswordVault.list_entries | `metadata cache is not valid JSON` |
| [856](/root/laintas_cli/password_vault.py:856) | error candidate | PasswordVault.get_secret | `no such entry` |

### password_vault_ui.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [97](/root/laintas_cli/password_vault_ui.py:97) | error candidate | _private_terminal | `secure terminal input unavailable` |
| [103](/root/laintas_cli/password_vault_ui.py:103) | error candidate | _private_terminal | `secure terminal input unavailable; no fallback allowed` |
| [142](/root/laintas_cli/password_vault_ui.py:142) | status/prompt | _secret | `Hidden> ` |
| [169](/root/laintas_cli/password_vault_ui.py:169) | resolved output candidate | handle_command | `/password takes no arguments — secrets must never be typed as command text. Everything happens inside the local vault UI.` |
| [172](/root/laintas_cli/password_vault_ui.py:172) | output | handle_command / 0 | `The password vault needs the local interactive terminal (/dev/tty). Remote or mirrored entry is disabled by design.` |
| [179](/root/laintas_cli/password_vault_ui.py:179) | output | handle_command / 0 | `Vault unavailable; secure input or storage failed. Vault locked.` |
| [199](/root/laintas_cli/password_vault_ui.py:199) | title/menu/help | _confirm | `Yes` |
| [199](/root/laintas_cli/password_vault_ui.py:199) | title/menu/help | _confirm | `No` |
| [218](/root/laintas_cli/password_vault_ui.py:218) | output | _choose_entry / 0 | `  no entries yet — add one first (a).` |
| [233](/root/laintas_cli/password_vault_ui.py:233) | output | _choose_entry / 0 | `  {index}. {entry['id']}  }]  {entry['description']}  ` |
| [244](/root/laintas_cli/password_vault_ui.py:244) | output | _choose_entry / 0 | `  no such entry.` |
| [253](/root/laintas_cli/password_vault_ui.py:253) | output | _lock_flow / 0 | `  vault locked.` |
| [308](/root/laintas_cli/password_vault_ui.py:308) | title/menu/help | _make_browser / title | `Password vault` |
| [374](/root/laintas_cli/password_vault_ui.py:374) | output | _run_session / 0 | `Vault closed — locked.` |
| [378](/root/laintas_cli/password_vault_ui.py:378) | output | _create_flow / 0 | `No password vault exists yet for this account.` |
| [379](/root/laintas_cli/password_vault_ui.py:379) | status/prompt | _create_flow | `Create a password vault now?` |
| [380](/root/laintas_cli/password_vault_ui.py:380) | output | _create_flow / 0 | `Cancelled — no vault was created.` |
| [385](/root/laintas_cli/password_vault_ui.py:385) | output | _create_flow / 0 | `Cancelled — no vault was created.` |
| [388](/root/laintas_cli/password_vault_ui.py:388) | status/prompt | _create_flow | `That passphrase is shorter than 8 characters. Use it anyway?` |
| [394](/root/laintas_cli/password_vault_ui.py:394) | output | _create_flow / 0 | `  Passphrases do not match — try again.` |
| [399](/root/laintas_cli/password_vault_ui.py:399) | output | _create_flow / 0 | `  A vault appeared meanwhile — opening the existing one.` |
| [401](/root/laintas_cli/password_vault_ui.py:401) | output | _create_flow / 0 | `  Vault created and unlocked.` |
| [415](/root/laintas_cli/password_vault_ui.py:415) | title/menu/help | _add_flow | `login` |
| [415](/root/laintas_cli/password_vault_ui.py:415) | title/menu/help | _add_flow | `username + password for a site` |
| [416](/root/laintas_cli/password_vault_ui.py:416) | title/menu/help | _add_flow | `secret` |
| [416](/root/laintas_cli/password_vault_ui.py:416) | title/menu/help | _add_flow | `a single key/token, any format` |
| [417](/root/laintas_cli/password_vault_ui.py:417) | title/menu/help | _add_flow / title | `Entry kind` |
| [432](/root/laintas_cli/password_vault_ui.py:432) | output | _add_flow / 0 | `  cancelled.` |
| [443](/root/laintas_cli/password_vault_ui.py:443) | output | _description_input / 0 | `  Use a single line of 1–200 characters.` |
| [458](/root/laintas_cli/password_vault_ui.py:458) | output | _origins_input / 0 | `  Enter an HTTPS site origin, without a path, query or wildcard.` |
| [471](/root/laintas_cli/password_vault_ui.py:471) | output | _password_input / 0 | `  Passwords do not match — try again.` |
| [480](/root/laintas_cli/password_vault_ui.py:480) | output | _add_login_flow / 0 | `  cancelled.` |
| [487](/root/laintas_cli/password_vault_ui.py:487) | output | _add_login_flow / 0 | `  cancelled.` |
| [494](/root/laintas_cli/password_vault_ui.py:494) | output | _add_login_flow / 0 | `  not stored: {exc}` |
| [496](/root/laintas_cli/password_vault_ui.py:496) | output | _add_login_flow / 0 | `  added {entry_id} (login): {description}` |
| [497](/root/laintas_cli/password_vault_ui.py:497) | output | _add_login_flow / 0 | `  (description and origins are visible to the AI; username/password/notes are not)` |
| [509](/root/laintas_cli/password_vault_ui.py:509) | output | _add_secret_flow / 0 | `  cancelled — secret value is required.` |
| [513](/root/laintas_cli/password_vault_ui.py:513) | output | _add_secret_flow / 0 | `  cancelled.` |
| [521](/root/laintas_cli/password_vault_ui.py:521) | output | _add_secret_flow / 0 | `  not stored: {exc}` |
| [523](/root/laintas_cli/password_vault_ui.py:523) | output | _add_secret_flow / 0 | `  added {entry_id} (secret): {description}` |
| [524](/root/laintas_cli/password_vault_ui.py:524) | output | _add_secret_flow / 0 | `  (description and any origins are visible to the AI; the secret value and notes are not)` |
| [532](/root/laintas_cli/password_vault_ui.py:532) | output | _list_flow / 0 | `  no entries yet — (a)dd one.` |
| [536](/root/laintas_cli/password_vault_ui.py:536) | output | _list_flow / 0 | `  {entry['id']}  }]  {entry['description']}  ` |
| [570](/root/laintas_cli/password_vault_ui.py:570) | status/prompt | _view_flow | `Show the secret on this local screen?` |
| [596](/root/laintas_cli/password_vault_ui.py:596) | title/menu/help | _view_flow / title | `Private entry` |
| [642](/root/laintas_cli/password_vault_ui.py:642) | title/menu/help | _edit_flow / title | `Edit {current['description']}` |
| [647](/root/laintas_cli/password_vault_ui.py:647) | output | _edit_flow / 0 | `  {index}. {row[0]} — {row[1]}` |
| [671](/root/laintas_cli/password_vault_ui.py:671) | output | _edit_flow / 0 | `  Secret must not be empty; field unchanged.` |
| [685](/root/laintas_cli/password_vault_ui.py:685) | status/prompt | _delete_flow | `Delete {entry_id} permanently?` |
| [686](/root/laintas_cli/password_vault_ui.py:686) | output | _delete_flow / 0 | `  not deleted.` |
| [691](/root/laintas_cli/password_vault_ui.py:691) | output | _delete_flow / 0 | `  deleted {entry_id}.` |
| [694](/root/laintas_cli/password_vault_ui.py:694) | output | _delete_flow / 0 | `  entry disappeared — not deleted.` |
| [704](/root/laintas_cli/password_vault_ui.py:704) | output | _unlock_flow / 0 | `  already unlocked.` |
| [711](/root/laintas_cli/password_vault_ui.py:711) | output | _unlock_flow / 0 | `  vault unlocked.` |
| [713](/root/laintas_cli/password_vault_ui.py:713) | output | _unlock_flow / 0 | `  unlock failed — wrong passphrase or tampered vault.` |
| [721](/root/laintas_cli/password_vault_ui.py:721) | output | _passphrase_flow / 0 | `  cancelled.` |
| [724](/root/laintas_cli/password_vault_ui.py:724) | status/prompt | _passphrase_flow | `Shorter than 8 characters. Use it anyway?` |
| [725](/root/laintas_cli/password_vault_ui.py:725) | output | _passphrase_flow / 0 | `  cancelled.` |
| [729](/root/laintas_cli/password_vault_ui.py:729) | output | _passphrase_flow / 0 | `  passphrases do not match — unchanged.` |
| [734](/root/laintas_cli/password_vault_ui.py:734) | output | _passphrase_flow / 0 | `  passphrase changed; vault re-encrypted with a fresh salt.` |

### patch_adapter/adapter.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [187](/root/laintas_cli/patch_adapter/adapter.py:187) | error candidate | apply_edit | `oldString and newString are identical — no change.` |
| [189](/root/laintas_cli/patch_adapter/adapter.py:189) | error candidate | apply_edit | `oldString cannot be empty; use a write for a full rewrite.` |

### paths.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [204](/root/laintas_cli/paths.py:204) | error candidate | require_account_selected | `Account paths are not initialized. Call paths.configure_account(user_id) before importing runtime stores, or configure_account('') for an anonymous backend.` |
| [214](/root/laintas_cli/paths.py:214) | error candidate | account_path | `Not an account path: {name}` |
| [244](/root/laintas_cli/paths.py:244) | error candidate | assert_account_write | `User-data destination is outside the selected account profile` |
| [255](/root/laintas_cli/paths.py:255) | error candidate | configure_account | `Runtime stores already belong to another profile; restart to change accounts` |
| [346](/root/laintas_cli/paths.py:346) | output | _home_owner_ok / 0 | `[paths] WARNING: LAINTAS_HOME resolves to {resolved} (uid={rstat.st_uid}), not owned by you (uid={os.getuid()}); refusing to use it.` |
| [365](/root/laintas_cli/paths.py:365) | error candidate | ensure_home | `LAINTAS_HOME ({LAINTAS_HOME}) resolves to a directory not owned by the current user; refusing to write secrets there. Set LAINTAS_HOME to a safe path or fix ownership.` |

### peer_coordination.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [607](/root/laintas_cli/peer_coordination.py:607) | message candidate | _acquire_session_lease / error | `empty session id` |
| [688](/root/laintas_cli/peer_coordination.py:688) | message candidate | _acquire_session_lease / error | `lease takeover race` |

### policy.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [220](/root/laintas_cli/policy.py:220) | error candidate | register_policy_contributor | `apply_fn must be callable` |

### precheck.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [361](/root/laintas_cli/precheck.py:361) | title/menu/help | _main / description | `Build the tool-precheck training set from captured .laintas/precheck_samples.jsonl files.` |
| [365](/root/laintas_cli/precheck.py:365) | title/menu/help | _main / help | `Dirs (or sample files) to scan recursively. Default: $HOME and the current directory.` |
| [368](/root/laintas_cli/precheck.py:368) | title/menu/help | _main / help | `Output directory (default: ./precheck_dataset)` |
| [370](/root/laintas_cli/precheck.py:370) | title/menu/help | _main / help | `Validation split fraction (default: 0.1)` |
| [376](/root/laintas_cli/precheck.py:376) | output | _main / 0 | `── tool-precheck dataset ──` |
| [377](/root/laintas_cli/precheck.py:377) | output | _main / 0 | `scanned files : {report['files_scanned']}` |
| [378](/root/laintas_cli/precheck.py:378) | output | _main / 0 | `rows read     : {report['rows_read']}  (dup {report['duplicates']}, bad {report['malformed']})` |
| [380](/root/laintas_cli/precheck.py:380) | output | _main / 0 | `unique        : {report['unique']}` |
| [381](/root/laintas_cli/precheck.py:381) | output | _main / 0 | `train / val   : {report['train']} / {report['val']}` |
| [382](/root/laintas_cli/precheck.py:382) | output | _main / 0 | `class distribution:` |
| [387](/root/laintas_cli/precheck.py:387) | output | _main / 0 | `  {label:<14} {n:>6}  {bar}` |
| [388](/root/laintas_cli/precheck.py:388) | output | _main / 0 | `→ {report['train_path']}` |
| [389](/root/laintas_cli/precheck.py:389) | output | _main / 0 | `→ {report['val_path']}` |

### private_files.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [42](/root/laintas_cli/private_files.py:42) | error candidate | rules | `Private file settings unavailable; access denied.` |
| [44](/root/laintas_cli/private_files.py:44) | error candidate | rules | `Invalid private file settings; access denied.` |
| [49](/root/laintas_cli/private_files.py:49) | error candidate | rules | `Invalid private file rule; access denied.` |
| [85](/root/laintas_cli/private_files.py:85) | error candidate | check | `Access denied by private file permissions.` |
| [92](/root/laintas_cli/private_files.py:92) | error candidate | check | `Recursive operation contains private files; access denied.` |
| [172](/root/laintas_cli/private_files.py:172) | output | handle_command / 0 | `{shlex.quote(r['path'])}  {', '.join(kinds)}` |
| [174](/root/laintas_cli/private_files.py:174) | output | handle_command / 0 | `No private file rules. {usage}` |
| [177](/root/laintas_cli/private_files.py:177) | output | handle_command / 0 | `Private rule removed.` |
| [180](/root/laintas_cli/private_files.py:180) | output | handle_command / 0 | `Private rule saved. Native AI execution is disabled while rules exist.` |
| [182](/root/laintas_cli/private_files.py:182) | resolved output candidate | handle_command | `/private [list \| set <path> hidden\|no-read\|no-write [...] \| remove <path>]` |

### prompt_budget.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [170](/root/laintas_cli/prompt_budget.py:170) | error candidate | validate | `{key} must be greater than 0` |
| [175](/root/laintas_cli/prompt_budget.py:175) | error candidate | validate | `{key} must be greater than 0` |
| [180](/root/laintas_cli/prompt_budget.py:180) | error candidate | validate | `{key} expects {', '.join(SHRINK_CHOICES)}` |
| [185](/root/laintas_cli/prompt_budget.py:185) | error candidate | validate | `{key} must be 0 or greater` |
| [189](/root/laintas_cli/prompt_budget.py:189) | error candidate | validate | `{key} must be between 0 and 1` |
| [191](/root/laintas_cli/prompt_budget.py:191) | error candidate | validate | `{key} must be greater than 0` |

### prompt_lab.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [392](/root/laintas_cli/prompt_lab.py:392) | error candidate | draft_patch | `Prompt Lab branch not found: {branch_id}` |
| [431](/root/laintas_cli/prompt_lab.py:431) | error candidate | compile_patch | `invalid patch id` |
| [693](/root/laintas_cli/prompt_lab.py:693) | error candidate | record_test_result | `Patch not found: {patch_id}` |
| [716](/root/laintas_cli/prompt_lab.py:716) | error candidate | build_diagnosis_task | `Branch not found: {branch_id}` |

### prompt_opt.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [213](/root/laintas_cli/prompt_opt.py:213) | title/menu/help | capture_feedback / description | `Prompt optimization feedback: {description[:80]}` |
| [265](/root/laintas_cli/prompt_opt.py:265) | title/menu/help | capture_structured_failure / description | `Failure: {entry['category']} — {task[:60]}` |
| [862](/root/laintas_cli/prompt_opt.py:862) | error candidate | draft_skill_patch | `Invalid skill path: use a relative file inside the registered skill directory` |

### prop_ui.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [18](/root/laintas_cli/prop_ui.py:18) | error candidate | parse_target | `Usage: /prop [sys] [N]` |
| [21](/root/laintas_cli/prop_ui.py:21) | error candidate | parse_target | `N must be a positive integer (1 is newest)` |
| [192](/root/laintas_cli/prop_ui.py:192) | title/menu/help | open_browser / title | `{title} · newest #{newest_index} · {call_count:,} calls · latest/final call shown by default` |
| [245](/root/laintas_cli/prop_ui.py:245) | error candidate | _parse_budget | `Usage: /prop budget [N] [output [path]]` |
| [247](/root/laintas_cli/prop_ui.py:247) | error candidate | _parse_budget | `N must be a positive integer (1 is newest)` |
| [281](/root/laintas_cli/prop_ui.py:281) | title/menu/help | open_budget_browser / title | `Context Budget · newest #{newest_index} · {compressed} block(s) compressed · edit with /config budget · /prop budget {newest_index} output for the tuning page` |

### rag_signals.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [258](/root/laintas_cli/rag_signals.py:258) | title/menu/help | _main / description | `Build the retrieval-rerank training set (query → positive + negatives) from captured .laintas/rag_signals.jsonl files.` |
| [261](/root/laintas_cli/rag_signals.py:261) | title/menu/help | _main / help | `Dirs/files to scan. Default: $HOME and cwd.` |
| [269](/root/laintas_cli/rag_signals.py:269) | output | _main / 0 | `── retrieval-rerank dataset ──` |
| [270](/root/laintas_cli/rag_signals.py:270) | output | _main / 0 | `scanned files : {rep['files_scanned']}` |
| [271](/root/laintas_cli/rag_signals.py:271) | output | _main / 0 | `rows read     : {rep['rows_read']}  (bad {rep['malformed']})` |
| [272](/root/laintas_cli/rag_signals.py:272) | output | _main / 0 | `events        : search {rep['searches']} · select {rep['selects']} · matched {rep['matched_pairs']}` |
| [273](/root/laintas_cli/rag_signals.py:273) | output | _main / 0 | `train / val   : {rep['train']} / {rep['val']}` |
| [274](/root/laintas_cli/rag_signals.py:274) | output | _main / 0 | `→ {rep['train_path']}` |
| [275](/root/laintas_cli/rag_signals.py:275) | output | _main / 0 | `→ {rep['val_path']}` |

### redactor.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [358](/root/laintas_cli/redactor.py:358) | title/menu/help | _main / description | `Build the secret/PII redaction (NER) training set from captured .laintas/redact_samples.jsonl files.` |
| [361](/root/laintas_cli/redactor.py:361) | title/menu/help | _main / help | `Dirs/files to scan. Default: $HOME and cwd.` |
| [369](/root/laintas_cli/redactor.py:369) | output | _main / 0 | `── secret/PII redaction dataset ──` |
| [370](/root/laintas_cli/redactor.py:370) | output | _main / 0 | `scanned files : {rep['files_scanned']}` |
| [371](/root/laintas_cli/redactor.py:371) | output | _main / 0 | `rows read     : {rep['rows_read']}  (dup {rep['duplicates']}, bad {rep['malformed']})` |
| [372](/root/laintas_cli/redactor.py:372) | output | _main / 0 | `unique        : {rep['unique']}` |
| [373](/root/laintas_cli/redactor.py:373) | output | _main / 0 | `train / val   : {rep['train']} / {rep['val']}` |
| [374](/root/laintas_cli/redactor.py:374) | output | _main / 0 | `span type counts:` |
| [379](/root/laintas_cli/redactor.py:379) | output | _main / 0 | `  {t:<12} {n:>6}  {bar}` |
| [380](/root/laintas_cli/redactor.py:380) | output | _main / 0 | `→ {rep['train_path']}` |
| [381](/root/laintas_cli/redactor.py:381) | output | _main / 0 | `→ {rep['val_path']}` |

### release.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [54](/root/laintas_cli/release.py:54) | error candidate | parse_checksums | `SHA256SUMS.txt is not valid UTF-8` |
| [61](/root/laintas_cli/release.py:61) | error candidate | parse_checksums | `SHA256SUMS.txt contains a malformed line` |
| [68](/root/laintas_cli/release.py:68) | error candidate | parse_checksums | `SHA256SUMS.txt contains an invalid entry` |
| [80](/root/laintas_cli/release.py:80) | error candidate | verify_sha256 | `{label} checksum verification failed.` |
| [94](/root/laintas_cli/release.py:94) | error candidate | verify_ed25519 | `cryptography is required for enterprise release verification` |
| [102](/root/laintas_cli/release.py:102) | error candidate | verify_ed25519 | `{label} signature verification failed.` |
| [119](/root/laintas_cli/release.py:119) | error candidate | platform_id | `Not available for {platform.system()} {platform.machine()}` |
| [172](/root/laintas_cli/release.py:172) | error candidate | extract_archive | `Unsafe archive path: {name}` |
| [181](/root/laintas_cli/release.py:181) | error candidate | extract_archive | `Cannot extract {name}: not a regular file` |

### resource_ui.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [195](/root/laintas_cli/resource_ui.py:195) | message candidate | <module> / warning | `#e3b341` |
| [196](/root/laintas_cli/resource_ui.py:196) | message candidate | <module> / error | `#f85149 bold` |
| [660](/root/laintas_cli/resource_ui.py:660) | title/menu/help | ResourceBrowser._execute_action / message | `{action.label} failed: {type(exc).__name__}: {exc}` |

### retask.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [157](/root/laintas_cli/retask.py:157) | error candidate | tokenize | `unterminated quote in check: {line}` |
| [174](/root/laintas_cli/retask.py:174) | error candidate | parse_check | `empty check` |
| [178](/root/laintas_cli/retask.py:178) | error candidate | parse_check | `unknown check {kind!r}; use one of {', '.join(CHECK_KINDS)}` |
| [182](/root/laintas_cli/retask.py:182) | error candidate | parse_check | `check {kind} takes {_CHECK_ARITY[kind]} argument(s), got {len(args)}: {line}` |
| [188](/root/laintas_cli/retask.py:188) | error candidate | parse_check | `min_length needs a non-negative number: {line}` |
| [193](/root/laintas_cli/retask.py:193) | error candidate | parse_check | `invalid pattern in check: {exc}` |
| [244](/root/laintas_cli/retask.py:244) | error candidate | parse | `not a retask file: the first line must be '{VERSION_LINE}'` |
| [259](/root/laintas_cli/retask.py:259) | error candidate | parse | `line {idx + 1}: expected a task header '## [ ] id title'` |
| [262](/root/laintas_cli/retask.py:262) | error candidate | parse | `line {idx + 1}: unknown mark ` |
| [306](/root/laintas_cli/retask.py:306) | error candidate | validate | `a retask needs a title` |
| [308](/root/laintas_cli/retask.py:308) | error candidate | validate | `a retask needs at least one task` |
| [312](/root/laintas_cli/retask.py:312) | error candidate | validate | `task id {task.id!r} must be 1-32 letters, digits, _ or -` |
| [314](/root/laintas_cli/retask.py:314) | error candidate | validate | `duplicate task id {task.id}` |
| [317](/root/laintas_cli/retask.py:317) | error candidate | validate | `task {task.id} needs a title` |
| [319](/root/laintas_cli/retask.py:319) | error candidate | validate | `task {task.id} has unknown status {task.status!r}` |
| [324](/root/laintas_cli/retask.py:324) | error candidate | validate | `task {task.id}: a note must be one line` |
| [328](/root/laintas_cli/retask.py:328) | error candidate | validate | `task {task.id} is after unknown task {dep}` |
| [330](/root/laintas_cli/retask.py:330) | error candidate | validate | `task {task.id} cannot be after itself` |
| [342](/root/laintas_cli/retask.py:342) | error candidate | _reject_cycles.visit | `tasks wait on each other: {' -> '.join(trail + [node])}` |
| [356](/root/laintas_cli/retask.py:356) | error candidate | load | `cannot read {path}: {exc}` |
| [449](/root/laintas_cli/retask.py:449) | error candidate | _resolve_inside | `{rel} is outside the workspace` |
| [513](/root/laintas_cli/retask.py:513) | message candidate | set_status / error | `no task {task_id}; tasks are {ids}` |
| [515](/root/laintas_cli/retask.py:515) | message candidate | set_status / error | `unknown status {status!r}; use one of {', '.join(STATUSES)}` |
| [521](/root/laintas_cli/retask.py:521) | message candidate | set_status / error | `{task.id} waits on {', '.join(waiting)}` |
| [529](/root/laintas_cli/retask.py:529) | message candidate | set_status / error | `checks failed: {'; '.join(gaps)}` |
| [533](/root/laintas_cli/retask.py:533) | message candidate | set_status / error | `{task.id} has a review check ({'; '.join((describe_check(c) for c in reviews))}); pass a note naming the evidence you looked at` |
| [555](/root/laintas_cli/retask.py:555) | error candidate | task_from_input | `each task must be an object` |

### retask_view.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [483](/root/laintas_cli/retask_view.py:483) | output | open_viewer / 0 | `retask view: {exc}` |

### runtime_host.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [18](/root/laintas_cli/runtime_host.py:18) | error candidate | bind | `runtime host must not be None` |
| [27](/root/laintas_cli/runtime_host.py:27) | error candidate | current | `agent runtime host has not been bound` |

### session_store.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [371](/root/laintas_cli/session_store.py:371) | error candidate | save_session | `Cannot save another account's task` |

### shared_storage.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [75](/root/laintas_cli/shared_storage.py:75) | error candidate | clean_remote_path | `A remote path is required` |
| [79](/root/laintas_cli/shared_storage.py:79) | error candidate | clean_remote_path | `Invalid remote path: {raw!r}` |
| [112](/root/laintas_cli/shared_storage.py:112) | error candidate | SharedStorage._call | `Could not reach {self._base}: {exc}` |
| [114](/root/laintas_cli/shared_storage.py:114) | error candidate | SharedStorage._call | `Not signed in — run /login first.` |
| [120](/root/laintas_cli/shared_storage.py:120) | error candidate | SharedStorage._call | `Non-JSON reply from {path}` |
| [158](/root/laintas_cli/shared_storage.py:158) | error candidate | SharedStorage.push_file | `Cannot read {local_path}: {exc}` |
| [175](/root/laintas_cli/shared_storage.py:175) | error candidate | SharedStorage.push_file | `Upload of {local_path} failed: {exc}` |
| [178](/root/laintas_cli/shared_storage.py:178) | error candidate | SharedStorage.push_file | `Upload of {local_path} rejected by storage (HTTP {resp.status_code})` |
| [201](/root/laintas_cli/shared_storage.py:201) | error candidate | SharedStorage.pull_file | `Download of {remote} failed (HTTP {resp.status_code})` |
| [211](/root/laintas_cli/shared_storage.py:211) | error candidate | SharedStorage.pull_file | `Download of {remote} failed: {exc}` |
| [271](/root/laintas_cli/shared_storage.py:271) | error candidate | walk_local | `{root} holds more than {WALK_LOCAL_MAX_FILES} files. Share a specific folder inside it instead.` |

### skills.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [287](/root/laintas_cli/skills.py:287) | error candidate | sync_gateway_skills | `managed skill target is a symlink` |
| [292](/root/laintas_cli/skills.py:292) | error candidate | sync_gateway_skills | `managed skill file is a symlink` |
| [315](/root/laintas_cli/skills.py:315) | error candidate | sync_gateway_skills | `managed skill temporary file is a symlink` |
| [812](/root/laintas_cli/skills.py:812) | error candidate | _load_skill_full | `no get_tools()` |
| [816](/root/laintas_cli/skills.py:816) | error candidate | _load_skill_full | `get_tools() must return a list or tuple` |
| [822](/root/laintas_cli/skills.py:822) | error candidate | _load_skill_full | `get_tools() returned a non-Tool value` |
| [828](/root/laintas_cli/skills.py:828) | error candidate | _load_skill_full | `invalid tool name: {t.name!r}` |
| [830](/root/laintas_cli/skills.py:830) | error candidate | _load_skill_full | `tool name is already registered: {t.name}` |
| [839](/root/laintas_cli/skills.py:839) | error candidate | _load_skill_full | `{name}: tool {t.name} requests undeclared capabilities: {sorted(effective_caps - declared_capabilities)}` |
| [850](/root/laintas_cli/skills.py:850) | error candidate | _load_skill_full | `failed to register tool: {t.name}` |
| [1037](/root/laintas_cli/skills.py:1037) | output | get_activated_skills_context / 0 | `skill.used` |

### station_service.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [272](/root/laintas_cli/station_service.py:272) | error candidate | StationService.deploy | `Terminal did not start.` |
| [279](/root/laintas_cli/station_service.py:279) | error candidate | StationService.deploy | `Agent or manager changed during deployment.` |
| [285](/root/laintas_cli/station_service.py:285) | error candidate | StationService.deploy | `Deployment is no longer valid.` |
| [291](/root/laintas_cli/station_service.py:291) | error candidate | StationService.deploy | `Terminal is occupied or deployment is no longer valid.` |

### station_ui.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [29](/root/laintas_cli/station_ui.py:29) | title/menu/help | show_station.load_items.emit / subtitle | `{a.role} · {a.deployment or 'not stationed'} · {a.task[:100]}` |
| [46](/root/laintas_cli/station_ui.py:46) | title/menu/help | show_station.load_items / subtitle | `Parent: {t.parent or 'root'} · Owner: {t.owner or 'none'}` |
| [52](/root/laintas_cli/station_ui.py:52) | title/menu/help | show_station.load_items / subtitle | `Not stationed · task terminal: {a.task_terminal or 'none'}` |
| [93](/root/laintas_cli/station_ui.py:93) | title/menu/help | show_station.toggle / message | `View: {mode[0]}` |
| [104](/root/laintas_cli/station_ui.py:104) | title/menu/help | show_station.deploy_selected / message | `Select an agent to deploy.` |
| [111](/root/laintas_cli/station_ui.py:111) | title/menu/help | show_station.undeploy_selected / message | `Select an agent to unstation.` |
| [117](/root/laintas_cli/station_ui.py:117) | title/menu/help | show_station.command / message | `Cancelled.` |
| [124](/root/laintas_cli/station_ui.py:124) | title/menu/help | show_station.command / message | `The primary terminal belongs to the CLI; use /exit.` |
| [132](/root/laintas_cli/station_ui.py:132) | title/menu/help | show_station.command / message | `Closes terminals {', '.join(affected)}; affected agents: {', '.join(people) or 'none'}. Enter close confirm to proceed.` |
| [140](/root/laintas_cli/station_ui.py:140) | title/menu/help | show_station.command / message | `Enter a task after the command.` |
| [142](/root/laintas_cli/station_ui.py:142) | title/menu/help | show_station.command / message | `Select an employee first.` |
| [147](/root/laintas_cli/station_ui.py:147) | title/menu/help | show_station.command / message | `{decision.action}: {decision.reason}` |
| [150](/root/laintas_cli/station_ui.py:150) | title/menu/help | show_station.command / message | `Select an agent first.` |
| [154](/root/laintas_cli/station_ui.py:154) | title/menu/help | show_station.command / message | `Usage: bind <terminal>` |
| [161](/root/laintas_cli/station_ui.py:161) | title/menu/help | show_station.command / message | `Use task, auto, suggest, bind, cancel or unstation.` |
| [165](/root/laintas_cli/station_ui.py:165) | title/menu/help | show_station / title | `Station` |

### stuck_signals.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [185](/root/laintas_cli/stuck_signals.py:185) | title/menu/help | _main / description | `Build the stuck-detection training set from the durable event log (.laintas/events.jsonl). No capture hook needed.` |
| [188](/root/laintas_cli/stuck_signals.py:188) | title/menu/help | _main / help | `Dirs/files to scan. Default: $HOME and cwd.` |
| [196](/root/laintas_cli/stuck_signals.py:196) | output | _main / 0 | `── stuck-detection dataset ──` |
| [197](/root/laintas_cli/stuck_signals.py:197) | output | _main / 0 | `scanned files    : {rep['files_scanned']}` |
| [198](/root/laintas_cli/stuck_signals.py:198) | output | _main / 0 | `events read      : {rep['rows_read']}  (bad {rep['malformed']})` |
| [199](/root/laintas_cli/stuck_signals.py:199) | output | _main / 0 | `runs w/ outcome  : {rep['runs_with_outcome']}` |
| [200](/root/laintas_cli/stuck_signals.py:200) | output | _main / 0 | `labeled          : ok {rep['ok']} · stuck {rep['stuck']} · skipped {rep['skipped']}` |
| [201](/root/laintas_cli/stuck_signals.py:201) | output | _main / 0 | `train / val      : {rep['train']} / {rep['val']}` |
| [202](/root/laintas_cli/stuck_signals.py:202) | output | _main / 0 | `→ {rep['train_path']}` |
| [203](/root/laintas_cli/stuck_signals.py:203) | output | _main / 0 | `→ {rep['val_path']}` |

### task_handoff.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [41](/root/laintas_cli/task_handoff.py:41) | error candidate | validate | `Invalid task checkpoint; upgrade laintas-cli or create it again` |
| [43](/root/laintas_cli/task_handoff.py:43) | error candidate | validate | `Task checkpoint has no valid conversation` |
| [45](/root/laintas_cli/task_handoff.py:45) | error candidate | validate | `Invalid task plan` |
| [49](/root/laintas_cli/task_handoff.py:49) | error candidate | validate | `Invalid checkpoint state: {key}` |
| [52](/root/laintas_cli/task_handoff.py:52) | error candidate | validate | `Invalid checkpoint state: {key}` |
| [54](/root/laintas_cli/task_handoff.py:54) | error candidate | validate | `Invalid checkpoint terminal history` |
| [58](/root/laintas_cli/task_handoff.py:58) | error candidate | validate | `Invalid checkpoint message thread` |
| [60](/root/laintas_cli/task_handoff.py:60) | error candidate | validate | `Task checkpoint is too large; /compact before creating the handoff` |
| [66](/root/laintas_cli/task_handoff.py:66) | error candidate | capture | `Sign in before handing off a task` |
| [86](/root/laintas_cli/task_handoff.py:86) | error candidate | continuation | `Sign in before accepting a task` |
| [89](/root/laintas_cli/task_handoff.py:89) | error candidate | continuation | `This task is addressed to another account` |

### terminal_link.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [39](/root/laintas_cli/terminal_link.py:39) | error candidate | valid_name | `Use a terminal name of 1-48 letters, digits, dot, underscore or hyphen; term0 is reserved` |
| [45](/root/laintas_cli/terminal_link.py:45) | error candidate | valid_ref | `Use the exact local:… or remote:… terminal ID from /term peers` |
| [65](/root/laintas_cli/terminal_link.py:65) | error candidate | check_terminal_creation | `This terminal is adopted. Create execution terminals from its controller; a third terminal level is not allowed` |
| [70](/root/laintas_cli/terminal_link.py:70) | error candidate | get_service | `Terminal linking is not running in this CLI` |
| [84](/root/laintas_cli/terminal_link.py:84) | error candidate | _json | `Terminal request or result is too large` |
| [104](/root/laintas_cli/terminal_link.py:104) | error candidate | LocalTransport.record | `Not a local terminal` |
| [107](/root/laintas_cli/terminal_link.py:107) | error candidate | LocalTransport.record | `Unsafe terminal endpoint` |
| [111](/root/laintas_cli/terminal_link.py:111) | error candidate | LocalTransport.record | `Unsafe terminal endpoint permissions` |
| [116](/root/laintas_cli/terminal_link.py:116) | error candidate | LocalTransport.record | `Terminal is offline` |
| [119](/root/laintas_cli/terminal_link.py:119) | error candidate | LocalTransport.record | `Terminal endpoint is unavailable` |
| [135](/root/laintas_cli/terminal_link.py:135) | error candidate | LocalTransport.source | `Connect this terminal with /helpwo and update its kernel to use remote terminal linking` |
| [142](/root/laintas_cli/terminal_link.py:142) | error candidate | LocalTransport.call | `Unknown terminal operation` |
| [145](/root/laintas_cli/terminal_link.py:145) | error candidate | LocalTransport.call | `Remote terminal transport is unavailable` |
| [159](/root/laintas_cli/terminal_link.py:159) | error candidate | LocalTransport.call | `Terminal response too large` |
| [162](/root/laintas_cli/terminal_link.py:162) | error candidate | LocalTransport.call | `Terminal did not answer; retry using the same request identity` |
| [179](/root/laintas_cli/terminal_link.py:179) | error candidate | LocalTransport.start.Handler.do_POST | `Unauthorized terminal request` |
| [182](/root/laintas_cli/terminal_link.py:182) | error candidate | LocalTransport.start.Handler.do_POST | `Invalid terminal request size` |
| [189](/root/laintas_cli/terminal_link.py:189) | error candidate | LocalTransport.start.Handler.do_POST | `Invalid terminal source` |
| [191](/root/laintas_cli/terminal_link.py:191) | error candidate | LocalTransport.start.Handler.do_POST | `Expired terminal request` |
| [234](/root/laintas_cli/terminal_link.py:234) | error candidate | LocalTransport.publish | `Unsafe terminal endpoint directory` |
| [323](/root/laintas_cli/terminal_link.py:323) | error candidate | Service.adopt | `An adopted terminal cannot adopt another terminal` |
| [325](/root/laintas_cli/terminal_link.py:325) | error candidate | Service.adopt | `Terminal name is already in use` |
| [327](/root/laintas_cli/terminal_link.py:327) | error candidate | Service.adopt | `Terminal relation capacity is full` |
| [329](/root/laintas_cli/terminal_link.py:329) | error candidate | Service.adopt | `Manager is unavailable` |
| [332](/root/laintas_cli/terminal_link.py:332) | error candidate | Service.adopt | `A terminal cannot adopt itself` |
| [350](/root/laintas_cli/terminal_link.py:350) | error candidate | Service.prepare_created | `Terminal name is unavailable or this terminal is controlled` |
| [352](/root/laintas_cli/terminal_link.py:352) | error candidate | Service.prepare_created | `Manager unavailable or terminal capacity full` |
| [365](/root/laintas_cli/terminal_link.py:365) | error candidate | Service.accept | `Unknown or expired invitation` |
| [367](/root/laintas_cli/terminal_link.py:367) | error candidate | Service.accept | `This terminal already has a controller` |
| [369](/root/laintas_cli/terminal_link.py:369) | error candidate | Service.accept | `A terminal with execution children cannot be adopted (two levels only)` |
| [371](/root/laintas_cli/terminal_link.py:371) | error candidate | Service.accept | `Finish active work before accepting a controller` |
| [381](/root/laintas_cli/terminal_link.py:381) | error candidate | Service.accept | `Invitation was released during acceptance` |
| [387](/root/laintas_cli/terminal_link.py:387) | message candidate | Service.dispatch / error | `Account switch is in progress` |
| [392](/root/laintas_cli/terminal_link.py:392) | error candidate | Service.dispatch | `Invalid terminal operation` |
| [395](/root/laintas_cli/terminal_link.py:395) | error candidate | Service.dispatch | `Remote terminal linking requires the same account` |
| [398](/root/laintas_cli/terminal_link.py:398) | error candidate | Service.dispatch | `Terminal is shutting down` |
| [410](/root/laintas_cli/terminal_link.py:410) | error candidate | Service._dispatch | `Terminal is not an independent leaf` |
| [413](/root/laintas_cli/terminal_link.py:413) | error candidate | Service._dispatch | `Invalid invitation` |
| [417](/root/laintas_cli/terminal_link.py:417) | error candidate | Service._dispatch | `Invitation expired or inbox full` |
| [422](/root/laintas_cli/terminal_link.py:422) | error candidate | Service._dispatch | `Invitation identity conflict` |
| [434](/root/laintas_cli/terminal_link.py:434) | error candidate | Service._dispatch | `Invitation is no longer valid` |
| [437](/root/laintas_cli/terminal_link.py:437) | error candidate | Service._dispatch | `Controller changed before acceptance` |
| [439](/root/laintas_cli/terminal_link.py:439) | error candidate | Service._dispatch | `Created terminal is still starting` |
| [442](/root/laintas_cli/terminal_link.py:442) | error candidate | Service._dispatch | `Invalid Agent inventory` |
| [491](/root/laintas_cli/terminal_link.py:491) | error candidate | Service._dispatch | `Invalid job identity` |
| [496](/root/laintas_cli/terminal_link.py:496) | error candidate | Service._dispatch | `Job identity conflicts with another task` |
| [499](/root/laintas_cli/terminal_link.py:499) | error candidate | Service._dispatch | `Task request history is full; start a new runtime` |
| [510](/root/laintas_cli/terminal_link.py:510) | error candidate | Service._dispatch | `Unknown operation` |
| [514](/root/laintas_cli/terminal_link.py:514) | error candidate | Service._check_child | `Invalid child identity or relation token` |
| [521](/root/laintas_cli/terminal_link.py:521) | error candidate | Service._check_parent | `Controller relation is invalid or has been released` |
| [527](/root/laintas_cli/terminal_link.py:527) | error candidate | Service.assign | `Agent is not owned by this controller` |
| [531](/root/laintas_cli/terminal_link.py:531) | error candidate | Service.assign | `Terminal request history is full` |
| [536](/root/laintas_cli/terminal_link.py:536) | error candidate | Service.assign | `Request identity conflicts with another task` |
| [540](/root/laintas_cli/terminal_link.py:540) | error candidate | Service.assign | `Relation was released during task admission` |
| [548](/root/laintas_cli/terminal_link.py:548) | error candidate | Service.deploy | `Remote Agents can only be stationed in their original execution terminal` |
| [576](/root/laintas_cli/terminal_link.py:576) | error candidate | Service.release | `This terminal has no controller` |
| [585](/root/laintas_cli/terminal_link.py:585) | error candidate | Service.release | `No adopted terminal with this name` |

### terminal_link_runtime.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [106](/root/laintas_cli/terminal_link_runtime.py:106) | error candidate | Adapter._import_child_locked | `Execution terminal must export exactly one primary` |
| [111](/root/laintas_cli/terminal_link_runtime.py:111) | error candidate | Adapter._import_child_locked | `Invalid or duplicate exported Agent ID` |
| [114](/root/laintas_cli/terminal_link_runtime.py:114) | error candidate | Adapter._import_child_locked | `Agent proxy name is already in use` |
| [116](/root/laintas_cli/terminal_link_runtime.py:116) | error candidate | Adapter._import_child_locked | `Invalid exported tool policy` |
| [119](/root/laintas_cli/terminal_link_runtime.py:119) | error candidate | Adapter._import_child_locked | `Terminal name is already in use` |
| [218](/root/laintas_cli/terminal_link_runtime.py:218) | output | Adapter.sync / 0 | `agent_done` |
| [218](/root/laintas_cli/terminal_link_runtime.py:218) | output | Adapter.sync / 0 | `agent_error` |
| [233](/root/laintas_cli/terminal_link_runtime.py:233) | error candidate | Adapter.start_job | `Unknown exported Agent or empty task` |
| [235](/root/laintas_cli/terminal_link_runtime.py:235) | error candidate | Adapter.start_job | `Invalid task tool scope` |
| [238](/root/laintas_cli/terminal_link_runtime.py:238) | error candidate | Adapter.start_job | `Agent is unavailable` |
| [244](/root/laintas_cli/terminal_link_runtime.py:244) | error candidate | Adapter.start_job | `Agent already has a delegated task` |
| [286](/root/laintas_cli/terminal_link_runtime.py:286) | error candidate | Adapter.start_job | `Agent already has active work` |
| [340](/root/laintas_cli/terminal_link_runtime.py:340) | error candidate | Adapter.deploy | `Unknown exported Agent` |
| [368](/root/laintas_cli/terminal_link_runtime.py:368) | error candidate | start_service | `Created-terminal identity mismatch` |

### terminal_preferences.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [238](/root/laintas_cli/terminal_preferences.py:238) | error candidate | set_ui_preference | `UI preference is not persistent: {key}` |

### tools.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [470](/root/laintas_cli/tools.py:470) | message candidate | ToolRegistry.invoke / error | `tool '{name}' not found` |
| [474](/root/laintas_cli/tools.py:474) | message candidate | ToolRegistry.invoke / error | `tool '{name}' is from an untrusted extension` |
| [507](/root/laintas_cli/tools.py:507) | message candidate | ToolRegistry.invoke / error | `{type(e).__name__}: {e}` |
| [805](/root/laintas_cli/tools.py:805) | message candidate | _bi_mem_read / error | `memory_system module not available` |
| [808](/root/laintas_cli/tools.py:808) | message candidate | _bi_mem_read / error | `missing 'name'` |
| [811](/root/laintas_cli/tools.py:811) | message candidate | _bi_mem_read / error | `memory '{name}' not found in the current scope` |
| [823](/root/laintas_cli/tools.py:823) | message candidate | _bi_mem_save / error | `memory_system module not available` |
| [831](/root/laintas_cli/tools.py:831) | message candidate | _bi_mem_save / error | `missing 'name' or 'body'` |
| [854](/root/laintas_cli/tools.py:854) | message candidate | _bi_mem_delete / error | `memory_system module not available` |
| [857](/root/laintas_cli/tools.py:857) | message candidate | _bi_mem_delete / error | `missing 'name'` |
| [864](/root/laintas_cli/tools.py:864) | message candidate | _bi_mem_list / error | `memory_system module not available` |
| [1038](/root/laintas_cli/tools.py:1038) | message candidate | _bi_skill_forget / error | `missing 'name'` |
| [1041](/root/laintas_cli/tools.py:1041) | message candidate | _bi_skill_forget / error | `a reason is required — a retirement with no recorded reason cannot be reviewed later` |
| [1061](/root/laintas_cli/tools.py:1061) | message candidate | _bi_tool_search / error | `missing 'query'` |
| [1091](/root/laintas_cli/tools.py:1091) | message candidate | _bi_skill_load / error | `missing 'name'` |
| [1260](/root/laintas_cli/tools.py:1260) | message candidate | _read_already_visible / error | `lines {start}-{end} of {abs_path} are already in your context above (you hold {shown}); re-reading them returns the same bytes. Use what you have, read a range you do not hold, or grep for what you are looking for.` |
| [1338](/root/laintas_cli/tools.py:1338) | message candidate | _bi_fs_read / error | `missing 'path'` |
| [1507](/root/laintas_cli/tools.py:1507) | message candidate | _bi_fs_read / error | `offset {offset} is past end of file ({total_lines} lines)` |
| [1609](/root/laintas_cli/tools.py:1609) | message candidate | _check_file_write_policy / error | `Write policy failed closed: {exc}` |
| [1612](/root/laintas_cli/tools.py:1612) | message candidate | _check_file_write_policy / error | `Blocked by policy: {decision.reason}` |
| [1617](/root/laintas_cli/tools.py:1617) | message candidate | _check_file_write_policy / error | `Write requires approval but no approval channel is available` |
| [1624](/root/laintas_cli/tools.py:1624) | message candidate | _check_file_write_policy / error | `User denied write: {decision.reason}` |
| [1643](/root/laintas_cli/tools.py:1643) | message candidate | _check_file_delete_policy / error | `Delete policy failed: {exc}` |
| [1647](/root/laintas_cli/tools.py:1647) | message candidate | _check_file_delete_policy / error | `Blocked by policy: {decision.reason}` |
| [1654](/root/laintas_cli/tools.py:1654) | message candidate | _check_file_delete_policy / error | `Deletion requires approval but no approval channel is available` |
| [1662](/root/laintas_cli/tools.py:1662) | message candidate | _check_file_delete_policy / error | `User denied deletion: {decision.reason}` |
| [1746](/root/laintas_cli/tools.py:1746) | message candidate | _bi_fs_delete / error | `missing 'path'` |
| [1750](/root/laintas_cli/tools.py:1750) | message candidate | _bi_fs_delete / error | `Path does not exist: {abs_path}` |
| [1758](/root/laintas_cli/tools.py:1758) | message candidate | _bi_fs_delete / error | `Directory is not empty; set recursive=true to delete it` |
| [1770](/root/laintas_cli/tools.py:1770) | message candidate | _bi_fs_delete / error | `Delete target changed before execution: {error}` |
| [1774](/root/laintas_cli/tools.py:1774) | message candidate | _bi_fs_delete / error | `Delete target changed while awaiting approval; review again` |
| [1819](/root/laintas_cli/tools.py:1819) | message candidate | _bi_fs_write / error | `missing 'path'` |
| [1851](/root/laintas_cli/tools.py:1851) | message candidate | _bi_fs_write / error | `Blocked by cross-instance coordination: {_stale}` |
| [1878](/root/laintas_cli/tools.py:1878) | message candidate | _bi_fs_write / error | `could not create parent directory: {e}` |
| [1981](/root/laintas_cli/tools.py:1981) | message candidate | _bi_fs_ls / error | `offset and limit must be integers` |
| [2019](/root/laintas_cli/tools.py:2019) | message candidate | _bi_task_create / error | `task_manager module not available` |
| [2023](/root/laintas_cli/tools.py:2023) | message candidate | _bi_task_create / error | `missing 'subject'` |
| [2076](/root/laintas_cli/tools.py:2076) | message candidate | _bi_task_update / error | `task_manager module not available` |
| [2081](/root/laintas_cli/tools.py:2081) | message candidate | _bi_task_update / error | `missing 'id'{'. ' + hint if hint else ''}` |
| [2106](/root/laintas_cli/tools.py:2106) | message candidate | _bi_task_list / error | `task_manager module not available` |
| [2124](/root/laintas_cli/tools.py:2124) | message candidate | _bi_task_get / error | `task_manager module not available` |
| [2129](/root/laintas_cli/tools.py:2129) | message candidate | _bi_task_get / error | `missing 'id'{'. ' + hint if hint else ''}` |
| [2137](/root/laintas_cli/tools.py:2137) | message candidate | _bi_task_get / error | `Task '{task_id}' not found{'. ' + hint if hint else ''}` |
| [2143](/root/laintas_cli/tools.py:2143) | message candidate | _bi_plan_read / error | `plan_mode module not available` |
| [2147](/root/laintas_cli/tools.py:2147) | message candidate | _bi_plan_read / error | `No plan available` |
| [2153](/root/laintas_cli/tools.py:2153) | message candidate | _bi_plan_update / error | `plan_mode module not available` |
| [2156](/root/laintas_cli/tools.py:2156) | message candidate | _bi_plan_update / error | `missing 'content'` |
| [2159](/root/laintas_cli/tools.py:2159) | message candidate | _bi_plan_update / error | `No active plan to update` |
| [2176](/root/laintas_cli/tools.py:2176) | message candidate | _bi_snapshot_create / error | `checkpoint failed: {exc}` |
| [2179](/root/laintas_cli/tools.py:2179) | message candidate | _bi_snapshot_create / error | `no checkpoint taken (not a git repository, or git failed)` |
| [2192](/root/laintas_cli/tools.py:2192) | message candidate | _bi_snapshot_list / error | `could not read checkpoints: {exc}` |
| [2221](/root/laintas_cli/tools.py:2221) | message candidate | _bi_snapshot_restore / error | `restoring a checkpoint requires approval but no approval channel is available` |
| [2231](/root/laintas_cli/tools.py:2231) | message candidate | _bi_snapshot_restore / error | `user denied the checkpoint restore` |
| [2237](/root/laintas_cli/tools.py:2237) | message candidate | _bi_snapshot_restore / error | `restore failed: {exc}` |
| [2244](/root/laintas_cli/tools.py:2244) | message candidate | _bi_plan_list / error | `plan_mode module not available` |
| [2252](/root/laintas_cli/tools.py:2252) | message candidate | _bi_plan_submit / error | `plan_mode module not available` |
| [2256](/root/laintas_cli/tools.py:2256) | message candidate | _bi_plan_submit / error | `Plan could not be submitted. Ensure it is substantial and has an active revision.` |
| [2274](/root/laintas_cli/tools.py:2274) | message candidate | _bi_prompt_feedback / error | `prompt_opt module not available` |
| [2277](/root/laintas_cli/tools.py:2277) | message candidate | _bi_prompt_feedback / error | `missing 'description'` |
| [2299](/root/laintas_cli/tools.py:2299) | message candidate | _bi_prompt_draft / error | `prompt_opt module not available` |
| [2304](/root/laintas_cli/tools.py:2304) | message candidate | _bi_prompt_draft / error | `missing 'patch' and 'rationale'` |
| [2306](/root/laintas_cli/tools.py:2306) | message candidate | _bi_prompt_draft / error | `missing 'feedback_id'` |
| [2315](/root/laintas_cli/tools.py:2315) | message candidate | _bi_prompt_review / error | `prompt_opt module not available` |
| [2319](/root/laintas_cli/tools.py:2319) | message candidate | _bi_prompt_review / error | `No candidate found` |
| [2326](/root/laintas_cli/tools.py:2326) | message candidate | _bi_prompt_apply / error | `Applying prompt candidates requires explicit user approval. Ask the user to run /prompt apply [id].` |
| [2333](/root/laintas_cli/tools.py:2333) | message candidate | _bi_prompt_discard / error | `Discarding prompt candidates requires explicit user approval. Ask the user to run /prompt discard [id].` |
| [2340](/root/laintas_cli/tools.py:2340) | message candidate | _bi_prompt_structured_feedback / error | `prompt_opt module not available` |
| [2350](/root/laintas_cli/tools.py:2350) | message candidate | _bi_prompt_structured_feedback / error | `missing 'task' and 'actual' (at least one required)` |
| [2352](/root/laintas_cli/tools.py:2352) | message candidate | _bi_prompt_structured_feedback / error | `invalid category '{fields['category']}'. Valid: {_prompt_opt_mod.FAILURE_CATEGORIES}` |
| [2373](/root/laintas_cli/tools.py:2373) | message candidate | _bi_prompt_skill_patch / error | `prompt_opt module not available` |
| [2383](/root/laintas_cli/tools.py:2383) | message candidate | _bi_prompt_skill_patch / error | `missing 'skill_name'` |
| [2385](/root/laintas_cli/tools.py:2385) | message candidate | _bi_prompt_skill_patch / error | `missing 'feedback_id'` |
| [2387](/root/laintas_cli/tools.py:2387) | message candidate | _bi_prompt_skill_patch / error | `missing 'patch' (required for append mode)` |
| [2389](/root/laintas_cli/tools.py:2389) | message candidate | _bi_prompt_skill_patch / error | `missing 'old_string' or 'new_string' (required for replace mode)` |
| [2391](/root/laintas_cli/tools.py:2391) | message candidate | _bi_prompt_skill_patch / error | `invalid mode '{mode}'. Use 'append' or 'replace'.` |
| [2406](/root/laintas_cli/tools.py:2406) | message candidate | _bi_prompt_skill_apply / error | `Applying skill patches requires explicit user approval. Ask the user to run /prompt skill apply <id>.` |
| [2413](/root/laintas_cli/tools.py:2413) | message candidate | _bi_prompt_skill_discard / error | `Discarding skill patches requires explicit user approval. Ask the user to run /prompt skill discard <id>.` |
| [2420](/root/laintas_cli/tools.py:2420) | message candidate | _bi_prompt_lab_draft / error | `prompt_lab module not available` |
| [2448](/root/laintas_cli/tools.py:2448) | message candidate | _bi_evolve_lab_draft / error | `evolution_lab module not available` |
| [2491](/root/laintas_cli/tools.py:2491) | message candidate | _bi_fs_edit / error | `missing 'path'` |
| [2493](/root/laintas_cli/tools.py:2493) | message candidate | _bi_fs_edit / error | `old_string and new_string are identical` |
| [2518](/root/laintas_cli/tools.py:2518) | message candidate | _bi_fs_edit / error | `old_string not found (tried exact, whitespace- and indentation-tolerant matching).{_edit_anchor_hint(content, old)}` |
| [2532](/root/laintas_cli/tools.py:2532) | message candidate | _bi_fs_edit / error | `Blocked by cross-instance coordination: {_stale}` |
| [2560](/root/laintas_cli/tools.py:2560) | message candidate | _bi_fs_edit / error | `old_string appears {count} times (not unique). Use replace_all: true or add more context to make it unique.` |
| [2579](/root/laintas_cli/tools.py:2579) | message candidate | _bi_fs_edit / error | `Blocked by cross-instance coordination: {_stale}` |
| [2681](/root/laintas_cli/tools.py:2681) | message candidate | _bi_fs_multi_edit / error | `missing 'path'` |
| [2683](/root/laintas_cli/tools.py:2683) | message candidate | _bi_fs_multi_edit / error | `'edits' must be a non-empty array` |
| [2699](/root/laintas_cli/tools.py:2699) | message candidate | _bi_fs_multi_edit / error | `edit #{i + 1} is not an object` |
| [2703](/root/laintas_cli/tools.py:2703) | message candidate | _bi_fs_multi_edit / error | `edit #{i + 1}: old_string equals new_string` |
| [2705](/root/laintas_cli/tools.py:2705) | message candidate | _bi_fs_multi_edit / error | `edit #{i + 1}: old_string is empty` |
| [2764](/root/laintas_cli/tools.py:2764) | message candidate | _bi_fs_multi_edit / error | `edit #{i + 1}: {reason}{detail}` |
| [2782](/root/laintas_cli/tools.py:2782) | message candidate | _bi_fs_multi_edit / error | `Blocked by cross-instance coordination: {_stale}` |
| [2836](/root/laintas_cli/tools.py:2836) | message candidate | _bi_fs_diff / error | `git diff failed: {e}` |
| [2849](/root/laintas_cli/tools.py:2849) | message candidate | _bi_fs_diff / error | `missing 'a'` |
| [2851](/root/laintas_cli/tools.py:2851) | message candidate | _bi_fs_diff / error | `provide either 'b' or 'b_text'` |
| [2859](/root/laintas_cli/tools.py:2859) | message candidate | _bi_fs_diff / error | `read a: {e}` |
| [2867](/root/laintas_cli/tools.py:2867) | message candidate | _bi_fs_diff / error | `read b: {e}` |
| [3062](/root/laintas_cli/tools.py:3062) | message candidate | _bi_fs_grep / error | `offset and max_results must be integers` |
| [3080](/root/laintas_cli/tools.py:3080) | message candidate | _bi_fs_grep / error | `missing 'pattern'` |
| [3086](/root/laintas_cli/tools.py:3086) | message candidate | _bi_fs_grep / error | `Invalid regex: {e}` |
| [3092](/root/laintas_cli/tools.py:3092) | message candidate | _bi_fs_grep / error | `Path not found: {abs_path}` |
| [3311](/root/laintas_cli/tools.py:3311) | message candidate | _bi_fs_glob / error | `offset and max_results must be integers` |
| [3445](/root/laintas_cli/tools.py:3445) | message candidate | _bi_web_search / error | `web_search module not available` |
| [3449](/root/laintas_cli/tools.py:3449) | message candidate | _bi_web_search / error | `missing 'query'` |
| [3479](/root/laintas_cli/tools.py:3479) | message candidate | _bi_identity_list / error | `identity_store not available` |
| [3490](/root/laintas_cli/tools.py:3490) | message candidate | _bi_identity_check / error | `web_search module not available` |
| [3493](/root/laintas_cli/tools.py:3493) | message candidate | _bi_identity_check / error | `missing 'name'` |
| [3538](/root/laintas_cli/tools.py:3538) | message candidate | _bi_media_generate_image / error | `missing 'prompt'` |
| [3543](/root/laintas_cli/tools.py:3543) | message candidate | _bi_media_generate_image / error | `missing dependency: {exc}` |
| [3553](/root/laintas_cli/tools.py:3553) | message candidate | _bi_media_generate_image / error | `could not reach backend: {exc}` |
| [3557](/root/laintas_cli/tools.py:3557) | message candidate | _bi_media_generate_image / error | `backend returned no JSON (HTTP {resp.status_code})` |
| [3564](/root/laintas_cli/tools.py:3564) | message candidate | _bi_media_generate_image / error | `no image in response` |
| [3582](/root/laintas_cli/tools.py:3582) | message candidate | _bi_media_generate_video / error | `missing 'prompt'` |
| [3587](/root/laintas_cli/tools.py:3587) | message candidate | _bi_media_generate_video / error | `missing dependency: {exc}` |
| [3598](/root/laintas_cli/tools.py:3598) | message candidate | _bi_media_generate_video / error | `could not reach backend: {exc}` |
| [3602](/root/laintas_cli/tools.py:3602) | message candidate | _bi_media_generate_video / error | `backend returned no JSON (HTTP {resp.status_code})` |
| [3608](/root/laintas_cli/tools.py:3608) | message candidate | _bi_media_generate_video / error | `backend returned no task id` |
| [3627](/root/laintas_cli/tools.py:3627) | message candidate | _bi_media_generate_video / error | `generation failed upstream: {status.get('error')}` |
| [3628](/root/laintas_cli/tools.py:3628) | message candidate | _bi_media_generate_video / error | `still rendering after {_VIDEO_POLL_TIMEOUT_S}s; task {task_id} is still running server-side` |
| [3641](/root/laintas_cli/tools.py:3641) | message candidate | _bi_web_fetch / error | `web_search module not available` |
| [3645](/root/laintas_cli/tools.py:3645) | message candidate | _bi_web_fetch / error | `missing 'url'` |
| [3713](/root/laintas_cli/tools.py:3713) | message candidate | _bi_agent_spawn / error | `spawn not available in this context` |
| [3716](/root/laintas_cli/tools.py:3716) | message candidate | _bi_agent_spawn / error | `no agent_id in context` |
| [3747](/root/laintas_cli/tools.py:3747) | message candidate | _bi_agent_spawn / error | `parallel spawn failed (parent '{parent_id}' not found)` |
| [3770](/root/laintas_cli/tools.py:3770) | message candidate | _bi_agent_spawn / error | `missing 'task' (or 'tasks' for parallel mode)` |
| [3774](/root/laintas_cli/tools.py:3774) | message candidate | _bi_agent_spawn / error | `spawn not available in this context` |
| [3777](/root/laintas_cli/tools.py:3777) | message candidate | _bi_agent_spawn / error | `no agent_id in context` |
| [3782](/root/laintas_cli/tools.py:3782) | message candidate | _bi_agent_spawn / error | `contract is not checkable: {exc}` |
| [3789](/root/laintas_cli/tools.py:3789) | message candidate | _bi_agent_spawn / error | `spawn failed (parent '{parent_id}' not found)` |
| [3865](/root/laintas_cli/tools.py:3865) | message candidate | _bi_spawn / error | `missing 'goal'` |
| [3869](/root/laintas_cli/tools.py:3869) | message candidate | _bi_spawn / error | `no current agent` |
| [3874](/root/laintas_cli/tools.py:3874) | message candidate | _bi_spawn / error | `contract is not checkable: {exc}` |
| [3881](/root/laintas_cli/tools.py:3881) | message candidate | _bi_spawn / error | `Cannot spawn child agent.` |
| [3913](/root/laintas_cli/tools.py:3913) | message candidate | _bi_spawn / error | `spawn interrupted: {goal[:80]}` |
| [3917](/root/laintas_cli/tools.py:3917) | message candidate | _bi_spawn / error | `spawn stopped: no progress for {int(_al.AGENT_STALL_SECONDS)}s on: {goal[:80]}` |
| [4188](/root/laintas_cli/tools.py:4188) | message candidate | _bi_spawn_parallel / error | `spawn_parallel requires at least one task` |
| [4190](/root/laintas_cli/tools.py:4190) | message candidate | _bi_spawn_parallel / error | `spawn_parallel: maximum 6 tasks` |
| [4193](/root/laintas_cli/tools.py:4193) | message candidate | _bi_spawn_parallel / error | `no current agent` |
| [4213](/root/laintas_cli/tools.py:4213) | message candidate | _bi_spawn_parallel / error | `every parallel task requires a goal` |
| [4241](/root/laintas_cli/tools.py:4241) | message candidate | _bi_spawn_parallel / error | `only {len(child_ids)}/{len(normalized)} child agents could be spawned (depth or concurrency limit); the started ones were stopped` |
| [4246](/root/laintas_cli/tools.py:4246) | output | _bi_spawn_parallel / 0 | `  ▾ {len(child_ids)} parallel agents launched` |
| [4259](/root/laintas_cli/tools.py:4259) | output | _bi_spawn_parallel / 0 | `    {_esc_markup(_cid)}  {_goal}` |
| [4585](/root/laintas_cli/tools.py:4585) | message candidate | _bi_spawn_chain / error | `spawn_chain requires at least 2 steps` |
| [4587](/root/laintas_cli/tools.py:4587) | message candidate | _bi_spawn_chain / error | `spawn_chain: maximum 6 steps` |
| [4590](/root/laintas_cli/tools.py:4590) | message candidate | _bi_spawn_chain / error | `no current agent` |
| [4604](/root/laintas_cli/tools.py:4604) | message candidate | _bi_spawn_chain / error | `chain step {index + 1} requires a goal` |
| [4690](/root/laintas_cli/tools.py:4690) | message candidate | _bi_await_spawns / error | `No agent_ids specified and no current agent` |
| [4759](/root/laintas_cli/tools.py:4759) | message candidate | _bi_hwo / error | `missing 'path'` |
| [4794](/root/laintas_cli/tools.py:4794) | message candidate | _bi_hwg / error | `missing 'path'` |
| [4796](/root/laintas_cli/tools.py:4796) | message candidate | _bi_hwg / error | `missing 'run_id'` |
| [4824](/root/laintas_cli/tools.py:4824) | message candidate | _bi_hwg / error | `unsupported HWG action: {action}` |
| [4859](/root/laintas_cli/tools.py:4859) | message candidate | _bi_hwo_agent_send / error | `missing 'to'` |
| [4861](/root/laintas_cli/tools.py:4861) | message candidate | _bi_hwo_agent_send / error | `missing 'message'` |
| [4864](/root/laintas_cli/tools.py:4864) | message candidate | _bi_hwo_agent_send / error | `no current agent` |
| [4866](/root/laintas_cli/tools.py:4866) | message candidate | _bi_hwo_agent_send / error | `no such agent '{to}' — check the [TEAM MANIFEST] for valid names` |
| [4871](/root/laintas_cli/tools.py:4871) | message candidate | _bi_hwo_agent_send / error | `'{to}' is not on a tree edge from '{caller_id}': only your parent and your own children are reachable. A parallel member cannot message another parallel member - declare the value in out(...) and let the parent scope pass it on.` |
| [4879](/root/laintas_cli/tools.py:4879) | message candidate | _bi_hwo_agent_send / error | `mailbox for '{to}' is full` |
| [4897](/root/laintas_cli/tools.py:4897) | message candidate | _bi_hwo_agent_receive / error | `no current agent` |
| [4934](/root/laintas_cli/tools.py:4934) | message candidate | _bi_hwo_agent_receive / error | `agent_receive gave up after {_elapsed:.0f}s waiting for {who} (no message, and the sender is no longer making progress).` |
| [4942](/root/laintas_cli/tools.py:4942) | message candidate | _bi_hwo_agent_return / error | `missing 'value'` |
| [4944](/root/laintas_cli/tools.py:4944) | message candidate | _bi_hwo_agent_return / error | `agent_return is only meaningful inside an HWO workflow` |
| [4947](/root/laintas_cli/tools.py:4947) | message candidate | _bi_hwo_agent_return / error | `current agent not found in registry` |
| [4975](/root/laintas_cli/tools.py:4975) | message candidate | _bi_branch_status / error | `no current agent` |
| [4980](/root/laintas_cli/tools.py:4980) | message candidate | _bi_branch_status / error | `no such branch '{branch_id}'` |
| [4983](/root/laintas_cli/tools.py:4983) | message candidate | _bi_branch_status / error | `branch '{branch_id}' belongs to another agent` |
| [5002](/root/laintas_cli/tools.py:5002) | message candidate | _bi_agent_ask_parent / error | `missing 'question'` |
| [5004](/root/laintas_cli/tools.py:5004) | message candidate | _bi_agent_ask_parent / error | `no current agent` |
| [5033](/root/laintas_cli/tools.py:5033) | message candidate | _bi_agent_answer / error | `missing 'agent_id'` |
| [5035](/root/laintas_cli/tools.py:5035) | message candidate | _bi_agent_answer / error | `missing 'decision'` |
| [5037](/root/laintas_cli/tools.py:5037) | message candidate | _bi_agent_answer / error | `no current agent` |
| [5051](/root/laintas_cli/tools.py:5051) | message candidate | _bi_agent_tell / error | `missing 'agent_id'` |
| [5054](/root/laintas_cli/tools.py:5054) | message candidate | _bi_agent_tell / error | `missing 'message'` |
| [5056](/root/laintas_cli/tools.py:5056) | message candidate | _bi_agent_tell / error | `send_to_agent not available` |
| [5059](/root/laintas_cli/tools.py:5059) | message candidate | _bi_agent_tell / error | `no current agent` |
| [5064](/root/laintas_cli/tools.py:5064) | message candidate | _bi_agent_tell / error | `agent '{target_id}' is outside the caller's communication scope (same terminal or direct terminal/agent neighbor only)` |
| [5076](/root/laintas_cli/tools.py:5076) | message candidate | _bi_agent_tell / error | `agent '{target_id}' not found or inbox full` |
| [5124](/root/laintas_cli/tools.py:5124) | message candidate | _bi_agent_station / error | `no current agent to station` |
| [5136](/root/laintas_cli/tools.py:5136) | message candidate | _bi_agent_station / error | `agent has an active assignment and cannot move` |
| [5146](/root/laintas_cli/tools.py:5146) | message candidate | _bi_agent_station / error | `failed to start terminal '{name}'` |
| [5154](/root/laintas_cli/tools.py:5154) | message candidate | _bi_agent_station / error | `could not register terminal '{name}': {exc}` |
| [5159](/root/laintas_cli/tools.py:5159) | message candidate | _bi_agent_station / error | `could not deploy agent to '{name}'` |
| [5167](/root/laintas_cli/tools.py:5167) | message candidate | _bi_agent_abort / error | `missing 'agent_id'` |
| [5169](/root/laintas_cli/tools.py:5169) | message candidate | _bi_agent_abort / error | `abort not available` |
| [5172](/root/laintas_cli/tools.py:5172) | message candidate | _bi_agent_abort / error | `agent '{target_id}' not found` |
| [5180](/root/laintas_cli/tools.py:5180) | message candidate | _bi_agent_wait / error | `missing 'agent_id'` |
| [5188](/root/laintas_cli/tools.py:5188) | message candidate | _bi_agent_wait / error | `wait not available` |
| [5212](/root/laintas_cli/tools.py:5212) | message candidate | _bi_agent_wait / error | `wait interrupted: agent '{target_id}'` |
| [5218](/root/laintas_cli/tools.py:5218) | message candidate | _bi_agent_wait / error | `agent '{target_id}' not found, or {_why}` |
| [5226](/root/laintas_cli/tools.py:5226) | message candidate | _bi_agent_hire / error | `hire not available` |
| [5233](/root/laintas_cli/tools.py:5233) | message candidate | _bi_agent_hire / error | `agent '{name}' already exists` |
| [5239](/root/laintas_cli/tools.py:5239) | message candidate | _bi_agent_hire / error | `unknown employee profile '{role_name}'.{hint}` |
| [5243](/root/laintas_cli/tools.py:5243) | message candidate | _bi_agent_hire / error | `tools must be an array of tool names` |
| [5251](/root/laintas_cli/tools.py:5251) | message candidate | _bi_agent_hire / error | `unknown tools: {', '.join(unknown)}.{hint}` |
| [5258](/root/laintas_cli/tools.py:5258) | title/menu/help | _bi_agent_hire / title | `General Agent` |
| [5259](/root/laintas_cli/tools.py:5259) | title/menu/help | _bi_agent_hire / description | `General-purpose autonomous employee` |
| [5278](/root/laintas_cli/tools.py:5278) | message candidate | _bi_agent_hire / error | `a newly hired agent cannot be deployed directly into the caller's current terminal; omit terminal or choose another live, unoccupied terminal` |
| [5291](/root/laintas_cli/tools.py:5291) | message candidate | _bi_agent_hire / error | `could not fetch the current model list: {exc}` |
| [5297](/root/laintas_cli/tools.py:5297) | message candidate | _bi_agent_hire / error | `model '{requested_model}' is not in the backend's current available model list` |
| [5307](/root/laintas_cli/tools.py:5307) | message candidate | _bi_agent_hire / error | `model '{requested_model}' belongs to provider '{verified_provider}', not '{requested_provider}'` |
| [5337](/root/laintas_cli/tools.py:5337) | message candidate | _bi_agent_hire / error | `could not deploy agent '{info.id}' to '{requested_terminal}'; it must be live and unoccupied` |
| [5370](/root/laintas_cli/tools.py:5370) | message candidate | _bi_agent_list / error | `no current agent` |
| [5373](/root/laintas_cli/tools.py:5373) | message candidate | _bi_agent_list / error | `current agent not found in registry` |
| [5388](/root/laintas_cli/tools.py:5388) | message candidate | _bi_agent_rename / error | `missing 'name'` |
| [5390](/root/laintas_cli/tools.py:5390) | message candidate | _bi_agent_rename / error | `rename not available` |
| [5394](/root/laintas_cli/tools.py:5394) | message candidate | _bi_agent_rename / error | `no current agent to rename` |
| [5398](/root/laintas_cli/tools.py:5398) | message candidate | _bi_agent_rename / error | `the primary agent cannot be renamed` |
| [5401](/root/laintas_cli/tools.py:5401) | message candidate | _bi_agent_rename / error | `rename failed` |
| [5412](/root/laintas_cli/tools.py:5412) | message candidate | _bi_agent_remove / error | `missing 'agent_id'` |
| [5414](/root/laintas_cli/tools.py:5414) | message candidate | _bi_agent_remove / error | `remove not available` |
| [5419](/root/laintas_cli/tools.py:5419) | message candidate | _bi_agent_remove / error | `the primary agent cannot be removed` |
| [5423](/root/laintas_cli/tools.py:5423) | message candidate | _bi_agent_remove / error | `agent '{target_id}' not found` |
| [5427](/root/laintas_cli/tools.py:5427) | message candidate | _bi_agent_remove / error | `caller agent not found` |
| [5435](/root/laintas_cli/tools.py:5435) | message candidate | _bi_agent_remove / error | `you can only remove your own child agents or yourself` |
| [5450](/root/laintas_cli/tools.py:5450) | message candidate | _bi_agent_remove / error | `remove not supported by this runtime` |
| [5452](/root/laintas_cli/tools.py:5452) | message candidate | _bi_agent_remove / error | `remove failed: {exc}` |
| [5475](/root/laintas_cli/tools.py:5475) | message candidate | _bi_account_usage / error | `'days' must be a number` |
| [5483](/root/laintas_cli/tools.py:5483) | message candidate | _bi_account_usage / error | `local usage unavailable: {exc}` |
| [5501](/root/laintas_cli/tools.py:5501) | message candidate | _bi_account_usage / error | `missing dependency: {exc}` |
| [5552](/root/laintas_cli/tools.py:5552) | message candidate | _bi_policy_check / error | `missing dependency: {exc}` |
| [5559](/root/laintas_cli/tools.py:5559) | message candidate | _bi_policy_check / error | `give one of 'command', 'write_path' or 'delete_path'` |
| [5589](/root/laintas_cli/tools.py:5589) | message candidate | _bi_policy_check / error | `policy check failed: {exc}` |
| [5602](/root/laintas_cli/tools.py:5602) | message candidate | _bi_work_status / error | `missing dependency: {exc}` |
| [5644](/root/laintas_cli/tools.py:5644) | message candidate | _bi_diag_tool_failures / error | `'limit' must be a number` |
| [5687](/root/laintas_cli/tools.py:5687) | message candidate | _storage_call / error | `missing dependency: {exc}` |
| [5689](/root/laintas_cli/tools.py:5689) | message candidate | _storage_call / error | `storage unavailable: {exc}` |
| [5734](/root/laintas_cli/tools.py:5734) | message candidate | _bi_storage_get / error | `missing 'path' — the remote file to download` |
| [5766](/root/laintas_cli/tools.py:5766) | message candidate | _bi_storage_put / error | `missing 'local_path' — the file to upload` |
| [5770](/root/laintas_cli/tools.py:5770) | message candidate | _bi_storage_put / error | `not a file: {local}` |
| [5785](/root/laintas_cli/tools.py:5785) | message candidate | _bi_storage_mkdir / error | `missing 'path'` |
| [5799](/root/laintas_cli/tools.py:5799) | message candidate | _bi_storage_move / error | `both 'from' and 'to' are required` |
| [5819](/root/laintas_cli/tools.py:5819) | message candidate | _bi_storage_delete / error | `missing 'path'` |
| [5827](/root/laintas_cli/tools.py:5827) | message candidate | _bi_storage_delete.work / error | `Deleting from shared storage requires approval but no approval channel is available` |
| [5837](/root/laintas_cli/tools.py:5837) | message candidate | _bi_storage_delete.work / error | `User denied deletion of {clean}` |
| [5861](/root/laintas_cli/tools.py:5861) | message candidate | _bi_file_push / error | `cannot resolve backend: {backend_url}` |
| [5865](/root/laintas_cli/tools.py:5865) | message candidate | _bi_file_push / error | `missing 'paths' — list of local file paths` |
| [5869](/root/laintas_cli/tools.py:5869) | message candidate | _bi_file_push / error | `'paths' must be a list of file paths` |
| [5873](/root/laintas_cli/tools.py:5873) | message candidate | _bi_file_push / error | `missing 'target_agent_id' — the Helpwo agent to notify` |
| [5937](/root/laintas_cli/tools.py:5937) | message candidate | _bi_file_push / error | `no files uploaded` |
| [5974](/root/laintas_cli/tools.py:5974) | message candidate | _bi_terminal_send / error | `missing 'name'` |
| [5976](/root/laintas_cli/tools.py:5976) | message candidate | _bi_terminal_send / error | `missing 'input' (or legacy 'command')` |
| [5978](/root/laintas_cli/tools.py:5978) | message candidate | _bi_terminal_send / error | `mode must be 'line' or 'raw'` |
| [5980](/root/laintas_cli/tools.py:5980) | message candidate | _bi_terminal_send / error | `terminal access not available` |
| [5983](/root/laintas_cli/tools.py:5983) | message candidate | _bi_terminal_send / error | `terminal '{target}' not found` |
| [5985](/root/laintas_cli/tools.py:5985) | message candidate | _bi_terminal_send / error | `terminal '{target}' is dead` |
| [6022](/root/laintas_cli/tools.py:6022) | message candidate | _bi_terminal_read / error | `missing 'name'` |
| [6024](/root/laintas_cli/tools.py:6024) | message candidate | _bi_terminal_read / error | `terminal access not available` |
| [6027](/root/laintas_cli/tools.py:6027) | message candidate | _bi_terminal_read / error | `terminal '{target}' not found` |
| [6029](/root/laintas_cli/tools.py:6029) | message candidate | _bi_terminal_read / error | `terminal '{target}' has no session` |
| [6051](/root/laintas_cli/tools.py:6051) | message candidate | _bi_terminal_read / error | `cursor and max_chars must be integers` |
| [6150](/root/laintas_cli/tools.py:6150) | message candidate | _bi_terminal_scroll / error | `missing 'name'` |
| [6152](/root/laintas_cli/tools.py:6152) | message candidate | _bi_terminal_scroll / error | `terminal access not available` |
| [6155](/root/laintas_cli/tools.py:6155) | message candidate | _bi_terminal_scroll / error | `terminal '{target}' not found` |
| [6157](/root/laintas_cli/tools.py:6157) | message candidate | _bi_terminal_scroll / error | `terminal '{target}' has no session` |
| [6210](/root/laintas_cli/tools.py:6210) | message candidate | _bi_terminal_scroll / error | `unknown scroll action '{action}' (use up/down/live/top)` |
| [6236](/root/laintas_cli/tools.py:6236) | message candidate | _bi_terminal_wait / error | `missing 'name'` |
| [6238](/root/laintas_cli/tools.py:6238) | message candidate | _bi_terminal_wait / error | `terminal access not available` |
| [6241](/root/laintas_cli/tools.py:6241) | message candidate | _bi_terminal_wait / error | `terminal '{target}' not found` |
| [6246](/root/laintas_cli/tools.py:6246) | message candidate | _bi_terminal_wait / error | `timeout and poll_interval must be numbers` |
| [6248](/root/laintas_cli/tools.py:6248) | message candidate | _bi_terminal_wait / error | `timeout must not be negative` |
| [6250](/root/laintas_cli/tools.py:6250) | message candidate | _bi_terminal_wait / error | `poll_interval must be between 0.05 and 2 seconds` |
| [6264](/root/laintas_cli/tools.py:6264) | message candidate | _bi_terminal_wait / error | `failed while waiting for '{target}': {exc}` |
| [6289](/root/laintas_cli/tools.py:6289) | message candidate | _bi_terminal_terminate / error | `missing 'name'` |
| [6291](/root/laintas_cli/tools.py:6291) | message candidate | _bi_terminal_terminate / error | `term0 is owned by the current CLI; exit the CLI to close it` |
| [6293](/root/laintas_cli/tools.py:6293) | message candidate | _bi_terminal_terminate / error | `terminate not available` |
| [6297](/root/laintas_cli/tools.py:6297) | message candidate | _bi_terminal_terminate / error | `terminal '{target}' not found` |
| [6362](/root/laintas_cli/tools.py:6362) | message candidate | _bi_app_manifest_get / error | `missing 'name'` |
| [6366](/root/laintas_cli/tools.py:6366) | message candidate | _bi_app_manifest_get / error | `no application named '{name}'` |
| [6376](/root/laintas_cli/tools.py:6376) | message candidate | _bi_app_trust_request / error | `missing 'name'` |
| [6381](/root/laintas_cli/tools.py:6381) | message candidate | _bi_app_trust_request / error | `no application named '{name}'` |
| [6393](/root/laintas_cli/tools.py:6393) | message candidate | _bi_app_trust_request / error | `trusting requires user approval but no approval channel is available` |
| [6395](/root/laintas_cli/tools.py:6395) | message candidate | _bi_app_trust_request / error | `user denied the trust request` |
| [6405](/root/laintas_cli/tools.py:6405) | message candidate | _bi_app_start / error | `missing 'name'` |
| [6409](/root/laintas_cli/tools.py:6409) | message candidate | _bi_app_start / error | `no application named '{name}'` |
| [6423](/root/laintas_cli/tools.py:6423) | message candidate | _bi_app_start / error | `could not start '{name}' in a sub-terminal` |
| [6432](/root/laintas_cli/tools.py:6432) | message candidate | _bi_app_stop / error | `missing 'name'` |
| [6434](/root/laintas_cli/tools.py:6434) | message candidate | _bi_app_stop / error | `'{name}' is not running` |
| [6438](/root/laintas_cli/tools.py:6438) | message candidate | _bi_app_stop / error | `could not stop '{name}'` |
| [6447](/root/laintas_cli/tools.py:6447) | message candidate | _bi_app_manifest_put / error | `scope must be project or user` |
| [6466](/root/laintas_cli/tools.py:6466) | message candidate | _bi_app_trust_revoke / error | `invalid application name` |
| [6475](/root/laintas_cli/tools.py:6475) | message candidate | _bi_terminal_create / error | `missing 'name'` |
| [6477](/root/laintas_cli/tools.py:6477) | message candidate | _bi_terminal_create / error | `terminal creation not available` |
| [6491](/root/laintas_cli/tools.py:6491) | message candidate | _bi_terminal_create / error | `terminal '{name}' already exists` |
| [6509](/root/laintas_cli/tools.py:6509) | message candidate | _bi_terminal_create / error | `failed to start terminal '{name}'` |
| [6517](/root/laintas_cli/tools.py:6517) | message candidate | _bi_terminal_create / error | `could not register terminal '{name}': {exc}` |
| [6525](/root/laintas_cli/tools.py:6525) | message candidate | _bi_terminal_list / error | `terminal listing not available` |
| [6563](/root/laintas_cli/tools.py:6563) | message candidate | _bi_terminal_exec / error | `missing 'name'` |
| [6565](/root/laintas_cli/tools.py:6565) | message candidate | _bi_terminal_exec / error | `missing 'command'` |
| [6568](/root/laintas_cli/tools.py:6568) | message candidate | _bi_terminal_exec / error | `terminal creation not available` |
| [6572](/root/laintas_cli/tools.py:6572) | message candidate | _bi_terminal_exec / error | `terminal '{name}' already exists and is alive; terminate it first` |
| [6593](/root/laintas_cli/tools.py:6593) | message candidate | _bi_terminal_exec / error | `could not register terminal '{name}': {exc}` |
| [6629](/root/laintas_cli/tools.py:6629) | message candidate | _bi_terminal_watch / error | `missing 'name'` |
| [6631](/root/laintas_cli/tools.py:6631) | message candidate | _bi_terminal_watch / error | `trigger control not available` |
| [6635](/root/laintas_cli/tools.py:6635) | message candidate | _bi_terminal_watch / error | `terminal '{name}' not found` |
| [6645](/root/laintas_cli/tools.py:6645) | message candidate | _bi_terminal_watch / error | `terminal '{name}' not found` |
| [6668](/root/laintas_cli/tools.py:6668) | message candidate | _bi_session_start / error | `missing 'command'` |
| [6671](/root/laintas_cli/tools.py:6671) | message candidate | _bi_session_start / error | `temporary PTY sessions are unavailable` |
| [6678](/root/laintas_cli/tools.py:6678) | message candidate | _bi_session_start / error | `an interactive session is already active; close it first` |
| [6690](/root/laintas_cli/tools.py:6690) | message candidate | _bi_session_start / error | `timeout must be an integer` |
| [6704](/root/laintas_cli/tools.py:6704) | message candidate | _bi_session_start / error | `failed to start interactive session: {exc}` |
| [6722](/root/laintas_cli/tools.py:6722) | message candidate | _bi_session_read / error | `no active interactive session` |
| [6727](/root/laintas_cli/tools.py:6727) | message candidate | _bi_session_read / error | `wait and tail_lines must be numbers` |
| [6779](/root/laintas_cli/tools.py:6779) | message candidate | _bi_session_keys / error | `missing 'keys'` |
| [6783](/root/laintas_cli/tools.py:6783) | message candidate | _bi_session_keys / error | `mode must be 'raw' or 'line'` |
| [6785](/root/laintas_cli/tools.py:6785) | message candidate | _bi_session_keys / error | `no active session — call session.start first` |
| [6802](/root/laintas_cli/tools.py:6802) | message candidate | _bi_sleep / error | `seconds must be a number` |
| [6804](/root/laintas_cli/tools.py:6804) | message candidate | _bi_sleep / error | `seconds must be between 0 and 300` |
| [6995](/root/laintas_cli/tools.py:6995) | message candidate | _bi_task_complete / error | `you still have delegated work running: ⏎ {_detail} ⏎ Decide before finishing: await_spawns(batch_id=...) to collect what you need, agent_abort on the children whose results you no longer want, or call task_complete again to finish and drop them. Finishing now would leave them running against your task with nobody reading the results.` |
| [7020](/root/laintas_cli/tools.py:7020) | message candidate | _bi_task_complete / error | `this task was spawned under a contract: call task_complete again with outputs={...} carrying {', '.join((o['name'] for o in _contract.get('outputs') or ))}. {agent_contract.render(_contract)}` |
| [7057](/root/laintas_cli/tools.py:7057) | message candidate | _bi_task_complete / error | `Task completion blocked: current agent tree still has open TASK items: {details}. Update them before task_complete.` |
| [7069](/root/laintas_cli/tools.py:7069) | message candidate | _bi_task_complete / error | `Task completion blocked: workflow phase '{wf.current.name}' is still active. Complete the phase with workflow_phase_complete, or obtain the required user approval for a gated phase.` |
| [7078](/root/laintas_cli/tools.py:7078) | message candidate | _bi_task_complete / error | `Could not verify workflow completion state safely: {exc}` |
| [7089](/root/laintas_cli/tools.py:7089) | message candidate | _bi_task_complete / error | `Completion blocked: required durable completion hook(s) have not been satisfied for this task: ⏎ {details} ⏎ Complete each obligation, then call rule_mark_satisfied for its id before task_complete.` |
| [7151](/root/laintas_cli/tools.py:7151) | message candidate | _bi_rule_cancel / error | `durable rule not found` |
| [7160](/root/laintas_cli/tools.py:7160) | message candidate | _bi_rule_mark_satisfied / error | `active durable rule not found` |
| [7162](/root/laintas_cli/tools.py:7162) | message candidate | _bi_rule_mark_satisfied / error | `no task state is available to record rule satisfaction` |
| [7175](/root/laintas_cli/tools.py:7175) | message candidate | _bi_workflow_phase_complete / error | `no active workflow phase` |
| [7179](/root/laintas_cli/tools.py:7179) | message candidate | _bi_workflow_phase_complete / error | `this workflow phase requires explicit user confirmation; use the workflow approval UI` |
| [7413](/root/laintas_cli/tools.py:7413) | error candidate | _replace_persistent_shell | `replacement shell exited during startup` |
| [7564](/root/laintas_cli/tools.py:7564) | message candidate | _exec_in_deployed_shell / error | `The terminal is running a command the user detached from ({_detached.command if _detached else 'a command'}); it is not free until that finishes or the user stops it.` |
| [7589](/root/laintas_cli/tools.py:7589) | message candidate | _exec_in_deployed_shell / error | `Terminal is stuck: a previous command is still running or an interactive program is holding the shell. Interrupt it in the terminal, or wait and retry.` |
| [7622](/root/laintas_cli/tools.py:7622) | message candidate | _exec_in_deployed_shell / error | `Command aborted` |
| [7684](/root/laintas_cli/tools.py:7684) | message candidate | _exec_in_deployed_shell / error | `Deployment terminal exited` |
| [7701](/root/laintas_cli/tools.py:7701) | message candidate | _exec_in_deployed_shell / error | `Command is waiting for interactive input: {_q} (waiting on stdin). Rerun with non-interactive flags (e.g. DEBIAN_FRONTEND=noninteractive, yes \| ..., --yes), or feed the answer via stdin.` |
| [7722](/root/laintas_cli/tools.py:7722) | message candidate | _exec_in_deployed_shell / error | `Command produced no output for {_idle_for}s (ran {_ran_for}s){hint}.{buffered_pipeline_hint(command)}` |
| [8060](/root/laintas_cli/tools.py:8060) | message candidate | _bi_shell_exec_raw / error | `missing 'command'` |
| [8069](/root/laintas_cli/tools.py:8069) | message candidate | _bi_shell_exec_raw / error | `missing shell command after 'shell.exec'` |
| [8094](/root/laintas_cli/tools.py:8094) | message candidate | _bi_shell_exec_raw / error | `Deployment terminal is not running` |
| [8131](/root/laintas_cli/tools.py:8131) | message candidate | _bi_shell_exec_raw / error | `A private interactive session is already active; close it before using shell.exec.` |
| [8141](/root/laintas_cli/tools.py:8141) | message candidate | _bi_shell_exec_raw / error | `temporary PTY sessions are unavailable` |
| [8157](/root/laintas_cli/tools.py:8157) | error candidate | _bi_shell_exec_raw | `temporary shell exited during startup` |
| [8164](/root/laintas_cli/tools.py:8164) | message candidate | _bi_shell_exec_raw / error | `failed to start temporary terminal: {exc}` |
| [8236](/root/laintas_cli/tools.py:8236) | message candidate | _bi_shell_exec_raw / error | `Command aborted` |
| [8241](/root/laintas_cli/tools.py:8241) | message candidate | _bi_shell_exec_raw / error | `Command produced no output for {_idle}s and was stopped: {command[:120]}.{buffered_pipeline_hint(command)}` |
| [8251](/root/laintas_cli/tools.py:8251) | message candidate | _bi_shell_exec_raw / error | `{type(e).__name__}: {e}` |
| [8267](/root/laintas_cli/tools.py:8267) | message candidate | _bi_browser_debug_open / error | `browser_session module not available` |
| [8291](/root/laintas_cli/tools.py:8291) | message candidate | _bi_browser_debug_open / error | `start failed: {e}` |
| [8329](/root/laintas_cli/tools.py:8329) | message candidate | _bi_browser_debug_close / error | `browser_session module not available` |
| [8333](/root/laintas_cli/tools.py:8333) | message candidate | _bi_browser_debug_close / error | `missing 'name'` |
| [8343](/root/laintas_cli/tools.py:8343) | message candidate | _bi_browser_debug_list / error | `browser_session module not available` |
| [8431](/root/laintas_cli/tools.py:8431) | message candidate | _browser_check_action / error | `blocked by policy: {decision.reason}` |
| [8462](/root/laintas_cli/tools.py:8462) | message candidate | _browser_check_action / error | `action requires approval but no approval callback is available: {cmd}` |
| [8465](/root/laintas_cli/tools.py:8465) | message candidate | _browser_check_action / error | `action not approved: {cmd}` |
| [8591](/root/laintas_cli/tools.py:8591) | error candidate | _check_tool_interrupt | `interrupted by user` |
| [8716](/root/laintas_cli/tools.py:8716) | message candidate | _bi_image_describe / error | `image.describe: {e}` |
| [8718](/root/laintas_cli/tools.py:8718) | message candidate | _bi_image_describe / error | `image.describe: {type(e).__name__}: {e}` |
| [8738](/root/laintas_cli/tools.py:8738) | message candidate | _bi_image_to_text / error | `image.to_text: {e}` |
| [8740](/root/laintas_cli/tools.py:8740) | message candidate | _bi_image_to_text / error | `image.to_text: {type(e).__name__}: {e}` |
| [8818](/root/laintas_cli/tools.py:8818) | message candidate | _bi_retask_create / error | `{os.path.relpath(path, root)} already exists; change it with retask.update or pick another path` |
| [8854](/root/laintas_cli/tools.py:8854) | message candidate | _bi_retask_update / error | `no task {task_id}; tasks are {', '.join((t.id for t in doc.tasks))}` |
| [8878](/root/laintas_cli/tools.py:8878) | message candidate | _bi_retask_update / error | `pass the task id to change` |
| [8922](/root/laintas_cli/tools.py:8922) | message candidate | _bi_browser_open / error | `browser_session module not available` |
| [8942](/root/laintas_cli/tools.py:8942) | message candidate | _bi_browser_open / error | `start failed: {e}` |
| [8974](/root/laintas_cli/tools.py:8974) | message candidate | _bi_browser_close / error | `browser_session module not available` |
| [8980](/root/laintas_cli/tools.py:8980) | message candidate | _bi_browser_close / error | `no active browser session` |
| [8996](/root/laintas_cli/tools.py:8996) | message candidate | _bi_browser_navigate / error | `missing 'url'` |
| [9043](/root/laintas_cli/tools.py:9043) | message candidate | _bi_browser_navigate / error | `{type(e).__name__}: {e}` |
| [9063](/root/laintas_cli/tools.py:9063) | message candidate | _bi_browser_click._job / error | `element not found: {selector}` |
| [9071](/root/laintas_cli/tools.py:9071) | message candidate | _bi_browser_click / error | `{type(e).__name__}: {e}` |
| [9087](/root/laintas_cli/tools.py:9087) | message candidate | _bi_browser_type / error | `missing 'text'` |
| [9096](/root/laintas_cli/tools.py:9096) | message candidate | _bi_browser_type._job / error | `element not found: {selector}` |
| [9109](/root/laintas_cli/tools.py:9109) | message candidate | _bi_browser_type / error | `{type(e).__name__}: {e}` |
| [9130](/root/laintas_cli/tools.py:9130) | message candidate | _bi_browser_screenshot / error | `{type(e).__name__}: {e}` |
| [9140](/root/laintas_cli/tools.py:9140) | message candidate | _bi_browser_query / error | `missing 'selector'` |
| [9174](/root/laintas_cli/tools.py:9174) | message candidate | _bi_browser_query / error | `{type(e).__name__}: {e}` |
| [9223](/root/laintas_cli/tools.py:9223) | message candidate | _bi_browser_snapshot / error | `{type(e).__name__}: {e}` |
| [9239](/root/laintas_cli/tools.py:9239) | message candidate | _bi_browser_scroll._job / error | `element not found: {selector}` |
| [9248](/root/laintas_cli/tools.py:9248) | message candidate | _bi_browser_scroll / error | `{type(e).__name__}: {e}` |
| [9261](/root/laintas_cli/tools.py:9261) | message candidate | _bi_browser_evaluate / error | `missing 'script'` |
| [9268](/root/laintas_cli/tools.py:9268) | message candidate | _bi_browser_evaluate / error | `{type(e).__name__}: {e}` |
| [9281](/root/laintas_cli/tools.py:9281) | message candidate | _bi_browser_press_key / error | `missing 'key'` |
| [9292](/root/laintas_cli/tools.py:9292) | message candidate | _bi_browser_press_key / error | `{type(e).__name__}: {e}` |
| [9306](/root/laintas_cli/tools.py:9306) | message candidate | _bi_browser_get_url / error | `{type(e).__name__}: {e}` |
| [9320](/root/laintas_cli/tools.py:9320) | message candidate | _bi_browser_get_title / error | `{type(e).__name__}: {e}` |
| [9330](/root/laintas_cli/tools.py:9330) | message candidate | _bi_browser_wait_for / error | `missing 'selector'` |
| [9358](/root/laintas_cli/tools.py:9358) | message candidate | _bi_browser_wait_for._job / error | `element not found: {selector}` |
| [9362](/root/laintas_cli/tools.py:9362) | message candidate | _bi_browser_wait_for / error | `{type(e).__name__}: {e}` |
| [9379](/root/laintas_cli/tools.py:9379) | message candidate | _bi_browser_select / error | `missing 'value' or 'label'` |
| [9385](/root/laintas_cli/tools.py:9385) | message candidate | _bi_browser_select._job / error | `select element not found: {selector}` |
| [9396](/root/laintas_cli/tools.py:9396) | message candidate | _bi_browser_select / error | `{type(e).__name__}: {e}` |
| [9414](/root/laintas_cli/tools.py:9414) | message candidate | _bi_browser_go_back / error | `{type(e).__name__}: {e}` |
| [9432](/root/laintas_cli/tools.py:9432) | message candidate | _bi_browser_go_forward / error | `{type(e).__name__}: {e}` |
| [9523](/root/laintas_cli/tools.py:9523) | message candidate | _bi_browser_expect / error | `unknown expect parameter(s): {', '.join(_unknown)}. Valid conditions: text, state, count, url_contains, title_contains (with selector or ref).` |
| [9540](/root/laintas_cli/tools.py:9540) | message candidate | _bi_browser_expect._job / error | `expect needs selector + a condition (text/state/count), or url_contains/title_contains` |
| [9559](/root/laintas_cli/tools.py:9559) | message candidate | _bi_browser_expect / error | `{type(e).__name__}: {e}` |
| [9591](/root/laintas_cli/tools.py:9591) | message candidate | _bi_browser_test_flow / error | `test_flow needs a non-empty 'steps' array` |
| [9621](/root/laintas_cli/tools.py:9621) | message candidate | _bi_browser_test_flow / error | `{type(e).__name__}: {e}` |
| [9676](/root/laintas_cli/tools.py:9676) | message candidate | _contract_call / error | `contract file error: {e}` |
| [9778](/root/laintas_cli/tools.py:9778) | message candidate | _bi_password_list / error | `no password vault exists yet — create one with /password` |
| [9781](/root/laintas_cli/tools.py:9781) | message candidate | _bi_password_list / error | `vault metadata unavailable; repair or unlock locally` |
| [9798](/root/laintas_cli/tools.py:9798) | message candidate | _bi_password_fill / error | `protected autofill requires a trusted credential broker; none is deployed in this session. The vault stays manageable locally via /password, but credentials are never handed to model-controlled browser tools.` |
| [9809](/root/laintas_cli/tools.py:9809) | title/menu/help | register_builtin_tools / description | `Summarise the shared API contract with the frontend: how many operations sit in each state (proposed/agreed/implemented/verified/drift).` |
| [9809](/root/laintas_cli/tools.py:9809) | tool/help | register_builtin_tools / description | `Summarise the shared API contract with the frontend: how many operations sit in each state (proposed/agreed/implemented/verified/drift).` |
| [9816](/root/laintas_cli/tools.py:9816) | title/menu/help | register_builtin_tools / description | `Read the shared API contract. Omit both arguments for the whole surface; pass ˋoperationˋ for one endpoint, or ˋstateˋ to list only the endpoints in that state. Prefer a filter — reading the entire contract when you need four endpoints wastes the context you will need for the code.` |
| [9816](/root/laintas_cli/tools.py:9816) | tool/help | register_builtin_tools / description | `Read the shared API contract. Omit both arguments for the whole surface; pass ˋoperationˋ for one endpoint, or ˋstateˋ to list only the endpoints in that state. Prefer a filter — reading the entire contract when you need four endpoints wastes the context you will need for the code.` |
| [9830](/root/laintas_cli/tools.py:9830) | title/menu/help | register_builtin_tools / description | `Declare what an endpoint should look like, as an OpenAPI 3.1 operation object. Use this when you need an interface the other side has not agreed to yet, and re-use it to counter-offer a change: re-proposing an agreed operation moves it back to ˋproposedˋ so the other side has to see the change instead of inheriting it.` |
| [9830](/root/laintas_cli/tools.py:9830) | tool/help | register_builtin_tools / description | `Declare what an endpoint should look like, as an OpenAPI 3.1 operation object. Use this when you need an interface the other side has not agreed to yet, and re-use it to counter-offer a change: re-proposing an agreed operation moves it back to ˋproposedˋ so the other side has to see the change instead of inheriting it.` |
| [9846](/root/laintas_cli/tools.py:9846) | title/menu/help | register_builtin_tools / description | `Accept a proposed endpoint shape. After this it is a commitment the other side is entitled to build against, so read the definition before agreeing rather than after.` |
| [9846](/root/laintas_cli/tools.py:9846) | tool/help | register_builtin_tools / description | `Accept a proposed endpoint shape. After this it is a commitment the other side is entitled to build against, so read the definition before agreeing rather than after.` |
| [9857](/root/laintas_cli/tools.py:9857) | title/menu/help | register_builtin_tools / description | `Record that an agreed endpoint is now built, naming the files that build it. Those files' hash is what later drift checks compare against, so name the ones that actually implement the behaviour — not the whole directory, and not a file you merely touched.` |
| [9857](/root/laintas_cli/tools.py:9857) | tool/help | register_builtin_tools / description | `Record that an agreed endpoint is now built, naming the files that build it. Those files' hash is what later drift checks compare against, so name the ones that actually implement the behaviour — not the whole directory, and not a file you merely touched.` |
| [9873](/root/laintas_cli/tools.py:9873) | title/menu/help | register_builtin_tools / description | `Make the real request and check the real response against the declared schema. This is the only step that is evidence rather than a claim — ˋimplementedˋ is you saying you built it, ˋverifiedˋ is it answering correctly. Omit ˋoperationˋ to verify everything implemented.` |
| [9873](/root/laintas_cli/tools.py:9873) | tool/help | register_builtin_tools / description | `Make the real request and check the real response against the declared schema. This is the only step that is evidence rather than a claim — ˋimplementedˋ is you saying you built it, ˋverifiedˋ is it answering correctly. Omit ˋoperationˋ to verify everything implemented.` |
| [9886](/root/laintas_cli/tools.py:9886) | title/menu/help | register_builtin_tools / description | `Find endpoints whose contract and code have parted ways — the declared shape changed after it was agreed, or an implementing file changed after it was declared done. Read-only unless ˋmarkˋ is true.` |
| [9886](/root/laintas_cli/tools.py:9886) | tool/help | register_builtin_tools / description | `Find endpoints whose contract and code have parted ways — the declared shape changed after it was agreed, or an implementing file changed after it was declared done. Read-only unless ˋmarkˋ is true.` |
| [9897](/root/laintas_cli/tools.py:9897) | title/menu/help | register_builtin_tools / description | `A sample response synthesised from an endpoint's declared schema, so work can proceed against a proposed endpoint before it exists.` |
| [9897](/root/laintas_cli/tools.py:9897) | tool/help | register_builtin_tools / description | `A sample response synthesised from an endpoint's declared schema, so work can proceed against a proposed endpoint before it exists.` |
| [9907](/root/laintas_cli/tools.py:9907) | title/menu/help | register_builtin_tools / description | `List hosted applications registered by manifests in ~/.laintas/apps/ or ./.laintas/apps/, with trust and running state. Broken manifests are reported under 'problems'. Read-only.` |
| [9907](/root/laintas_cli/tools.py:9907) | tool/help | register_builtin_tools / description | `List hosted applications registered by manifests in ~/.laintas/apps/ or ./.laintas/apps/, with trust and running state. Broken manifests are reported under 'problems'. Read-only.` |
| [9916](/root/laintas_cli/tools.py:9916) | title/menu/help | register_builtin_tools / description | `Read one hosted application's manifest (name, command, prompt, persistence, bridge port, app_url, session tools) with its trust and running state. Use it to review a manifest before requesting trust. Read-only.` |
| [9916](/root/laintas_cli/tools.py:9916) | tool/help | register_builtin_tools / description | `Read one hosted application's manifest (name, command, prompt, persistence, bridge port, app_url, session tools) with its trust and running state. Use it to review a manifest before requesting trust. Read-only.` |
| [9927](/root/laintas_cli/tools.py:9927) | title/menu/help | register_builtin_tools / description | `Create or update a hosted app manifest. Validates and atomically writes to project (default) or user scope. Supports agent, agents, terminals, session_tools, auto_approve and app_url (project HTTP(S) address). Call app.start to trust and launch; restart a running app to apply changes.` |
| [9927](/root/laintas_cli/tools.py:9927) | tool/help | register_builtin_tools / description | `Create or update a hosted app manifest. Validates and atomically writes to project (default) or user scope. Supports agent, agents, terminals, session_tools, auto_approve and app_url (project HTTP(S) address). Call app.start to trust and launch; restart a running app to apply changes.` |
| [9940](/root/laintas_cli/tools.py:9940) | title/menu/help | register_builtin_tools / description | `Revoke a hosted application's saved trust. Does not stop a running app; use app.stop first if needed.` |
| [9940](/root/laintas_cli/tools.py:9940) | tool/help | register_builtin_tools / description | `Revoke a hosted application's saved trust. Does not stop a running app; use app.stop first if needed.` |
| [9949](/root/laintas_cli/tools.py:9949) | title/menu/help | register_builtin_tools / description | `Request the user's approval to trust a hosted application. Shows what the application's session agents may run. Nothing is recorded unless the user approves; re-run after the manifest changes, since trust follows the manifest digest.` |
| [9949](/root/laintas_cli/tools.py:9949) | tool/help | register_builtin_tools / description | `Request the user's approval to trust a hosted application. Shows what the application's session agents may run. Nothing is recorded unless the user approves; re-run after the manifest changes, since trust follows the manifest digest.` |
| [9963](/root/laintas_cli/tools.py:9963) | title/menu/help | register_builtin_tools / description | `Start a hosted application with its configured agents and terminals. Requests trust through the normal approval policy if needed; an unchanged trusted manifest needs no additional start approval.` |
| [9963](/root/laintas_cli/tools.py:9963) | tool/help | register_builtin_tools / description | `Start a hosted application with its configured agents and terminals. Requests trust through the normal approval policy if needed; an unchanged trusted manifest needs no additional start approval.` |
| [9974](/root/laintas_cli/tools.py:9974) | title/menu/help | register_builtin_tools / description | `Stop a running hosted application and close its sub-terminal, agent, and process. No extra approval is needed.` |
| [9974](/root/laintas_cli/tools.py:9974) | tool/help | register_builtin_tools / description | `Stop a running hosted application and close its sub-terminal, agent, and process. No extra approval is needed.` |
| [9983](/root/laintas_cli/tools.py:9983) | title/menu/help | register_builtin_tools / description | `Read one persistent memory by name from the current user/project scope.` |
| [9983](/root/laintas_cli/tools.py:9983) | tool/help | register_builtin_tools / description | `Read one persistent memory by name from the current user/project scope.` |
| [9996](/root/laintas_cli/tools.py:9996) | title/menu/help | register_builtin_tools / description | `Save a persistent memory that survives across sessions. Types: user (profile/preferences), feedback (corrections/confirmations), project (goals/deadlines), structure (project architecture/layout facts), reference (external resources). Use this to remember important facts for future conversations.` |
| [9996](/root/laintas_cli/tools.py:9996) | tool/help | register_builtin_tools / description | `Save a persistent memory that survives across sessions. Types: user (profile/preferences), feedback (corrections/confirmations), project (goals/deadlines), structure (project architecture/layout facts), reference (external resources). Use this to remember important facts for future conversations.` |
| [10032](/root/laintas_cli/tools.py:10032) | title/menu/help | register_builtin_tools / description | `Delete a persistent memory entry by name.` |
| [10032](/root/laintas_cli/tools.py:10032) | tool/help | register_builtin_tools / description | `Delete a persistent memory entry by name.` |
| [10045](/root/laintas_cli/tools.py:10045) | title/menu/help | register_builtin_tools / description | `Search persistent memories in the current scope. Results include a body preview and relevance score.` |
| [10045](/root/laintas_cli/tools.py:10045) | tool/help | register_builtin_tools / description | `Search persistent memories in the current scope. Results include a body preview and relevance score.` |
| [10060](/root/laintas_cli/tools.py:10060) | title/menu/help | register_builtin_tools / description | `Record, as a reusable skill, how a task in THIS project was successfully completed: the goal, the steps in order, the commands and files involved, and how the result was verified. Call it after the task is done and verified, so the same kind of task can be done the same way next time. Use mem.save for a FACT about the project. Project scope by default, so it never shows up in other repos. Documentation only: this writes instructions, never code. Saving under an existing name updates it.` |
| [10060](/root/laintas_cli/tools.py:10060) | tool/help | register_builtin_tools / description | `Record, as a reusable skill, how a task in THIS project was successfully completed: the goal, the steps in order, the commands and files involved, and how the result was verified. Call it after the task is done and verified, so the same kind of task can be done the same way next time. Use mem.save for a FACT about the project. Project scope by default, so it never shows up in other repos. Documentation only: this writes instructions, never code. Saving under an existing name updates it.` |
| [10102](/root/laintas_cli/tools.py:10102) | title/menu/help | register_builtin_tools / description | `Retire a skill you or a previous session wrote, once it stopped being true. The file is kept for the record but leaves the catalogue. Retire aggressively: every skill in the catalogue makes choosing the right one harder, so a lesson that no longer holds costs every later task.` |
| [10102](/root/laintas_cli/tools.py:10102) | tool/help | register_builtin_tools / description | `Retire a skill you or a previous session wrote, once it stopped being true. The file is kept for the record but leaves the catalogue. Retire aggressively: every skill in the catalogue makes choosing the right one harder, so a lesson that no longer holds costs every later task.` |
| [10122](/root/laintas_cli/tools.py:10122) | title/menu/help | register_builtin_tools / description | `List the skills written by this agent, with scope, lifecycle status and usefulness counters.` |
| [10122](/root/laintas_cli/tools.py:10122) | tool/help | register_builtin_tools / description | `List the skills written by this agent, with scope, lifecycle status and usefulness counters.` |
| [10131](/root/laintas_cli/tools.py:10131) | title/menu/help | register_builtin_tools / description | `List available skills with short descriptions and loaded status. Use this when deciding whether specialized instructions are available.` |
| [10131](/root/laintas_cli/tools.py:10131) | tool/help | register_builtin_tools / description | `List available skills with short descriptions and loaded status. Use this when deciding whether specialized instructions are available.` |
| [10139](/root/laintas_cli/tools.py:10139) | title/menu/help | register_builtin_tools / description | `Find and request native tools that are not currently visible. Use this instead of shell, source code, or event logs when a task needs a missing capability. Matching schemas are exposed on the next model turn only when runtime policy permits them.` |
| [10139](/root/laintas_cli/tools.py:10139) | tool/help | register_builtin_tools / description | `Find and request native tools that are not currently visible. Use this instead of shell, source code, or event logs when a task needs a missing capability. Matching schemas are exposed on the next model turn only when runtime policy permits them.` |
| [10162](/root/laintas_cli/tools.py:10162) | title/menu/help | register_builtin_tools / description | `Load a named skill's full instructions into subsequent context. Call before starting specialized work when the skill catalog has a relevant skill.` |
| [10162](/root/laintas_cli/tools.py:10162) | tool/help | register_builtin_tools / description | `Load a named skill's full instructions into subsequent context. Call before starting specialized work when the skill catalog has a relevant skill.` |
| [10175](/root/laintas_cli/tools.py:10175) | title/menu/help | register_builtin_tools / description | `Unload a loaded skill: drop its tools and stop injecting its instructions. Call when you are done with that specialized work to free context. Omit 'name' to unload ALL loaded skills at once. Skills stay available and can be re-loaded later.` |
| [10175](/root/laintas_cli/tools.py:10175) | tool/help | register_builtin_tools / description | `Unload a loaded skill: drop its tools and stop injecting its instructions. Call when you are done with that specialized work to free context. Omit 'name' to unload ALL loaded skills at once. Skills stay available and can be re-loaded later.` |
| [10190](/root/laintas_cli/tools.py:10190) | title/menu/help | register_builtin_tools / description | `Read a file as UTF-8 text, as a paged document. ˋ{path}ˋ alone opens page 1 and reports how many pages the file has -- do NOT pass offset/limit to walk a file, use the pages. ˋ{path, page:'next'}ˋ turns the page; the page you leave is REMOVED from your context and replaced by a stub with its line range and an index of what it defined, so one file costs one page however big it is. Pass ˋnoteˋ when turning to keep your own summary of the page you are leaving (write it for someone who cannot see the code, and keep the line numbers that matter); pass ˋpin:trueˋ to hold a page you are about to edit. ˋpageˋ also takes a number, 'prev', 'first', 'last'. Passing offset/limit instead is the targeted window: use it to check specific lines, say around an fs.grep hit. It does not move the page cursor and is never dropped. Output is prefixed with line numbers (ˋN→ˋ, cat -n style) so you can refer to exact lines. Those prefixes are DISPLAY ONLY and are not in the file: strip them before using a line as an fs.edit anchor, or pass line_numbers:false to get the raw text. Their width tracks the window's largest line number, so the same line looks differently indented in different reads. Prefer this over ˋcatˋ for source files.` |
| [10190](/root/laintas_cli/tools.py:10190) | tool/help | register_builtin_tools / description | `Read a file as UTF-8 text, as a paged document. ˋ{path}ˋ alone opens page 1 and reports how many pages the file has -- do NOT pass offset/limit to walk a file, use the pages. ˋ{path, page:'next'}ˋ turns the page; the page you leave is REMOVED from your context and replaced by a stub with its line range and an index of what it defined, so one file costs one page however big it is. Pass ˋnoteˋ when turning to keep your own summary of the page you are leaving (write it for someone who cannot see the code, and keep the line numbers that matter); pass ˋpin:trueˋ to hold a page you are about to edit. ˋpageˋ also takes a number, 'prev', 'first', 'last'. Passing offset/limit instead is the targeted window: use it to check specific lines, say around an fs.grep hit. It does not move the page cursor and is never dropped. Output is prefixed with line numbers (ˋN→ˋ, cat -n style) so you can refer to exact lines. Those prefixes are DISPLAY ONLY and are not in the file: strip them before using a line as an fs.edit anchor, or pass line_numbers:false to get the raw text. Their width tracks the window's largest line number, so the same line looks differently indented in different reads. Prefer this over ˋcatˋ for source files.` |
| [10243](/root/laintas_cli/tools.py:10243) | title/menu/help | register_builtin_tools / description | `Overwrite a file with the provided UTF-8 string content. Missing parent directories of a new file are created automatically (mkdir -p), and the result names any directory it had to create.` |
| [10243](/root/laintas_cli/tools.py:10243) | tool/help | register_builtin_tools / description | `Overwrite a file with the provided UTF-8 string content. Missing parent directories of a new file are created automatically (mkdir -p), and the result names any directory it had to create.` |
| [10259](/root/laintas_cli/tools.py:10259) | title/menu/help | register_builtin_tools / description | `Delete one file, symlink, or directory through the security policy ONLY when the user explicitly requested that exact deletion or approved a plan containing it. Do not use as inferred cleanup or secret handling. Non-empty directories require recursive=true. The target is inspected again after approval and deletion is cancelled if it changed.` |
| [10259](/root/laintas_cli/tools.py:10259) | tool/help | register_builtin_tools / description | `Delete one file, symlink, or directory through the security policy ONLY when the user explicitly requested that exact deletion or approved a plan containing it. Do not use as inferred cleanup or secret handling. Non-empty directories require recursive=true. The target is inspected again after approval and deletion is cancelled if it changed.` |
| [10279](/root/laintas_cli/tools.py:10279) | title/menu/help | register_builtin_tools / description | `List files in a directory (one level). Returns name/type/size. Entries are sorted by name; one call returns at most ˋlimitˋ of them and no more than fits one tool result. When there are more, the result says how many and names the offset that continues the listing.` |
| [10279](/root/laintas_cli/tools.py:10279) | tool/help | register_builtin_tools / description | `List files in a directory (one level). Returns name/type/size. Entries are sorted by name; one call returns at most ˋlimitˋ of them and no more than fits one tool result. When there are more, the result says how many and names the offset that continues the listing.` |
| [10299](/root/laintas_cli/tools.py:10299) | title/menu/help | register_builtin_tools / description | `Return the current epoch time and ISO-8601 timestamp.` |
| [10299](/root/laintas_cli/tools.py:10299) | tool/help | register_builtin_tools / description | `Return the current epoch time and ISO-8601 timestamp.` |
| [10305](/root/laintas_cli/tools.py:10305) | title/menu/help | register_builtin_tools / description | `Exact string replacement in a file. Replace old_string with new_string. Use replace_all:true if the string is not unique. old_string must be the file's RAW bytes: no ˋN→ˋ line-number prefixes from fs.read. Whitespace/indentation drift is tolerated as a fallback, but an exact anchor is what makes the edit predictable. This is the primary tool for editing code — prefer it over fs.write.` |
| [10305](/root/laintas_cli/tools.py:10305) | tool/help | register_builtin_tools / description | `Exact string replacement in a file. Replace old_string with new_string. Use replace_all:true if the string is not unique. old_string must be the file's RAW bytes: no ˋN→ˋ line-number prefixes from fs.read. Whitespace/indentation drift is tolerated as a fallback, but an exact anchor is what makes the edit predictable. This is the primary tool for editing code — prefer it over fs.write.` |
| [10326](/root/laintas_cli/tools.py:10326) | title/menu/help | register_builtin_tools / description | `Apply multiple sequential exact-string edits to one file atomically. Edits run in order against the result of the previous one; if any fails, the file is left untouched and the error names which edit failed and which ones matched — re-send the batch with only that one corrected, do not re-guess the others. Same anchor rules as fs.edit: raw bytes, no ˋN→ˋ prefixes. Use this when one fs.edit call would conflict with another in the same file (e.g. rename + import update).` |
| [10326](/root/laintas_cli/tools.py:10326) | tool/help | register_builtin_tools / description | `Apply multiple sequential exact-string edits to one file atomically. Edits run in order against the result of the previous one; if any fails, the file is left untouched and the error names which edit failed and which ones matched — re-send the batch with only that one corrected, do not re-guess the others. Same anchor rules as fs.edit: raw bytes, no ˋN→ˋ prefixes. Use this when one fs.edit call would conflict with another in the same file (e.g. rename + import update).` |
| [10357](/root/laintas_cli/tools.py:10357) | title/menu/help | register_builtin_tools / description | `Compute a unified diff between two files, or between a file and an inline text payload. Use to preview changes before fs.edit, or to explain what changed between two file revisions. To inspect git working-tree changes for one file, pass {"path":"..."}.` |
| [10357](/root/laintas_cli/tools.py:10357) | tool/help | register_builtin_tools / description | `Compute a unified diff between two files, or between a file and an inline text payload. Use to preview changes before fs.edit, or to explain what changed between two file revisions. To inspect git working-tree changes for one file, pass {"path":"..."}.` |
| [10380](/root/laintas_cli/tools.py:10380) | title/menu/help | register_builtin_tools / description | `Run a shell command in the current agent's execution scope. A deployed agent runs directly in its persistent deployment terminal, so cd, export, aliases, and compound-command shell state persist. An undeployed agent uses an isolated, non-PTY subprocess. Returns output and exit status. It BLOCKS your loop until the command exits, and ˋtimeoutˋ bounds only SILENCE, not total runtime — a command that keeps printing holds the turn indefinitely. For anything expected to take minutes (builds, full test suites, whole-disk searches, servers) run it with terminal.exec instead, which returns immediately, and collect it with terminal.read or terminal.wait. A pipeline whose last stage buffers (ˋcmd \| tailˋ, ˋ\| sortˋ, ˋ\| wc -lˋ) prints NOTHING until the stage before it exits, so its silence is not progress and it gets a proportionally longer budget; for a long one, redirect to a file and read that instead of piping. For REPLs or commands that need keystrokes without a deployment terminal, start an agent-private PTY with session.start instead.` |
| [10380](/root/laintas_cli/tools.py:10380) | tool/help | register_builtin_tools / description | `Run a shell command in the current agent's execution scope. A deployed agent runs directly in its persistent deployment terminal, so cd, export, aliases, and compound-command shell state persist. An undeployed agent uses an isolated, non-PTY subprocess. Returns output and exit status. It BLOCKS your loop until the command exits, and ˋtimeoutˋ bounds only SILENCE, not total runtime — a command that keeps printing holds the turn indefinitely. For anything expected to take minutes (builds, full test suites, whole-disk searches, servers) run it with terminal.exec instead, which returns immediately, and collect it with terminal.read or terminal.wait. A pipeline whose last stage buffers (ˋcmd \| tailˋ, ˋ\| sortˋ, ˋ\| wc -lˋ) prints NOTHING until the stage before it exits, so its silence is not progress and it gets a proportionally longer budget; for a long one, redirect to a file and read that instead of piping. For REPLs or commands that need keystrokes without a deployment terminal, start an agent-private PTY with session.start instead.` |
| [10416](/root/laintas_cli/tools.py:10416) | title/menu/help | register_builtin_tools / description | `Search files for a regex pattern. Returns matching lines with file, line number, and content. Respects include/exclude glob filters.` |
| [10416](/root/laintas_cli/tools.py:10416) | tool/help | register_builtin_tools / description | `Search files for a regex pattern. Returns matching lines with file, line number, and content. Respects include/exclude glob filters.` |
| [10445](/root/laintas_cli/tools.py:10445) | title/menu/help | register_builtin_tools / description | `Find files matching glob pattern(s). Returns paths with type and size. Use for discovering files by name pattern, extension, or directory.` |
| [10445](/root/laintas_cli/tools.py:10445) | tool/help | register_builtin_tools / description | `Find files matching glob pattern(s). Returns paths with type and size. Use for discovering files by name pattern, extension, or directory.` |
| [10465](/root/laintas_cli/tools.py:10465) | title/menu/help | register_builtin_tools / description | `Search the web and return results with title, URL, and snippet. Use this for finding documentation, solutions, or current information.` |
| [10465](/root/laintas_cli/tools.py:10465) | tool/help | register_builtin_tools / description | `Search the web and return results with title, URL, and snippet. Use this for finding documentation, solutions, or current information.` |
| [10496](/root/laintas_cli/tools.py:10496) | title/menu/help | register_builtin_tools / description | `Fetch a URL and extract its readable text. Use for reading documentation pages, API references, or any web content. When a site blocks the plain request, this escalates on its own — browser TLS fingerprint, then a rendered browser page, then an archive snapshot — and the reply says which was used. If it comes back with unlock_pending, a browser is sitting on the challenge page: ask the user to open the live view and clear it, then call this again.` |
| [10496](/root/laintas_cli/tools.py:10496) | tool/help | register_builtin_tools / description | `Fetch a URL and extract its readable text. Use for reading documentation pages, API references, or any web content. When a site blocks the plain request, this escalates on its own — browser TLS fingerprint, then a rendered browser page, then an archive snapshot — and the reply says which was used. If it comes back with unlock_pending, a browser is sitting on the challenge page: ask the user to open the live view and clear it, then call this again.` |
| [10528](/root/laintas_cli/tools.py:10528) | title/menu/help | register_builtin_tools / description | `Generate an image from a text prompt. Returns a URL. Costs more than an ordinary call, so write a specific prompt (subject, style, composition) rather than regenerating repeatedly. Only when the user actually wants a generated picture.` |
| [10528](/root/laintas_cli/tools.py:10528) | tool/help | register_builtin_tools / description | `Generate an image from a text prompt. Returns a URL. Costs more than an ordinary call, so write a specific prompt (subject, style, composition) rather than regenerating repeatedly. Only when the user actually wants a generated picture.` |
| [10547](/root/laintas_cli/tools.py:10547) | title/menu/help | register_builtin_tools / description | `Generate a short video clip from a text prompt, or animate a first-frame image. Describe MOTION, not just the scene. BILLED PER SECOND and far more expensive than an image; one call blocks for 40s to several minutes. Only on an explicit request for a video, never speculatively, and at the shortest duration and lowest resolution that answers the ask.` |
| [10547](/root/laintas_cli/tools.py:10547) | tool/help | register_builtin_tools / description | `Generate a short video clip from a text prompt, or animate a first-frame image. Describe MOTION, not just the scene. BILLED PER SECOND and far more expensive than an image; one call blocks for 40s to several minutes. Only on an explicit request for a video, never speculatively, and at the shortest duration and lowest resolution that answers the ask.` |
| [10575](/root/laintas_cli/tools.py:10575) | title/menu/help | register_builtin_tools / description | `List the saved logins this machine can browse as. Returns names, which domains each covers, and whether it is still fresh — never any cookie or token values.` |
| [10575](/root/laintas_cli/tools.py:10575) | tool/help | register_builtin_tools / description | `List the saved logins this machine can browse as. Returns names, which domains each covers, and whether it is still fresh — never any cookie or token values.` |
| [10583](/root/laintas_cli/tools.py:10583) | title/menu/help | register_builtin_tools / description | `Check whether a saved login is still signed in, before starting work that depends on it. Ask the user to re-authenticate if it reports signed_in false.` |
| [10583](/root/laintas_cli/tools.py:10583) | tool/help | register_builtin_tools / description | `Check whether a saved login is still signed in, before starting work that depends on it. Ask the user to re-authenticate if it reports signed_in false.` |
| [10597](/root/laintas_cli/tools.py:10597) | title/menu/help | register_builtin_tools / description | `Create an executable Step in the active WorkGraph. For medium work, decompose execution into specific steps with clear names. Tasks have status (pending→in_progress→completed), dependencies (blocks/blockedBy), progress (0-100), notes, and metadata. The runtime always scopes them to the calling session and agent.` |
| [10597](/root/laintas_cli/tools.py:10597) | tool/help | register_builtin_tools / description | `Create an executable Step in the active WorkGraph. For medium work, decompose execution into specific steps with clear names. Tasks have status (pending→in_progress→completed), dependencies (blocks/blockedBy), progress (0-100), notes, and metadata. The runtime always scopes them to the calling session and agent.` |
| [10620](/root/laintas_cli/tools.py:10620) | title/menu/help | register_builtin_tools / description | `Update a WorkGraph Step's status, description, dependencies, progress, notes, or metadata. Use addSubtask to create a child task with automatic dependency linking. ˋidˋ is REQUIRED — pass an id from task_list or from the task_create that returned it in THIS session; ids are session-scoped and do not survive into a new one.` |
| [10620](/root/laintas_cli/tools.py:10620) | tool/help | register_builtin_tools / description | `Update a WorkGraph Step's status, description, dependencies, progress, notes, or metadata. Use addSubtask to create a child task with automatic dependency linking. ˋidˋ is REQUIRED — pass an id from task_list or from the task_create that returned it in THIS session; ids are session-scoped and do not survive into a new one.` |
| [10662](/root/laintas_cli/tools.py:10662) | title/menu/help | register_builtin_tools / description | `List tasks, optionally filtered by status. Use available=true to get tasks ready to work on (not blocked).` |
| [10662](/root/laintas_cli/tools.py:10662) | tool/help | register_builtin_tools / description | `List tasks, optionally filtered by status. Use available=true to get tasks ready to work on (not blocked).` |
| [10677](/root/laintas_cli/tools.py:10677) | title/menu/help | register_builtin_tools / description | `Get full details of a single task by ID. ˋidˋ is REQUIRED and must come from task_list or task_create in this session.` |
| [10677](/root/laintas_cli/tools.py:10677) | tool/help | register_builtin_tools / description | `Get full details of a single task by ID. ˋidˋ is REQUIRED and must come from task_list or task_create in this session.` |
| [10692](/root/laintas_cli/tools.py:10692) | title/menu/help | register_builtin_tools / description | `Read the current plan (or a specific plan by name). Returns the full plan document.` |
| [10692](/root/laintas_cli/tools.py:10692) | tool/help | register_builtin_tools / description | `Read the current plan (or a specific plan by name). Returns the full plan document.` |
| [10704](/root/laintas_cli/tools.py:10704) | title/menu/help | register_builtin_tools / description | `Create a new immutable revision of the current plan and refresh its Markdown projection. Any prior approval is invalidated.` |
| [10704](/root/laintas_cli/tools.py:10704) | tool/help | register_builtin_tools / description | `Create a new immutable revision of the current plan and refresh its Markdown projection. Any prior approval is invalidated.` |
| [10717](/root/laintas_cli/tools.py:10717) | title/menu/help | register_builtin_tools / description | `List all saved plans.` |
| [10717](/root/laintas_cli/tools.py:10717) | tool/help | register_builtin_tools / description | `List all saved plans.` |
| [10723](/root/laintas_cli/tools.py:10723) | title/menu/help | register_builtin_tools / description | `Submit the current immutable plan revision for explicit user review. Call only after exploration, risks, concrete steps, and verification criteria are complete. This does not approve or execute the plan.` |
| [10723](/root/laintas_cli/tools.py:10723) | tool/help | register_builtin_tools / description | `Submit the current immutable plan revision for explicit user review. Call only after exploration, risks, concrete steps, and verification criteria are complete. This does not approve or execute the plan.` |
| [10733](/root/laintas_cli/tools.py:10733) | title/menu/help | register_builtin_tools / description | `Record the current working tree as a restorable checkpoint before starting something hard to unpick — a wide refactor, a codemod, a risky migration. Backed by a dangling git commit: it does not touch the index, the stash, or any branch, and it is not a commit (it never enters project history). Covers tracked and untracked files but NOT gitignored ones. No-op outside a git repo. Cheap and non-destructive — prefer taking one over asking whether to.` |
| [10733](/root/laintas_cli/tools.py:10733) | tool/help | register_builtin_tools / description | `Record the current working tree as a restorable checkpoint before starting something hard to unpick — a wide refactor, a codemod, a risky migration. Backed by a dangling git commit: it does not touch the index, the stash, or any branch, and it is not a commit (it never enters project history). Covers tracked and untracked files but NOT gitignored ones. No-op outside a git repo. Cheap and non-destructive — prefer taking one over asking whether to.` |
| [10752](/root/laintas_cli/tools.py:10752) | title/menu/help | register_builtin_tools / description | `List the checkpoints recorded for the current repository (newest last), with their short sha and label. Use before proposing a restore so you can name the right one.` |
| [10752](/root/laintas_cli/tools.py:10752) | tool/help | register_builtin_tools / description | `List the checkpoints recorded for the current repository (newest last), with their short sha and label. Use before proposing a restore so you can name the right one.` |
| [10760](/root/laintas_cli/tools.py:10760) | title/menu/help | register_builtin_tools / description | `Roll the working tree back to a checkpoint. Always asks the user first and cannot be pre-approved in bulk. Restores EVERY tracked file, so it also reverts edits made outside this session — propose it only when your own changes are the problem and the user agrees. Files created since the checkpoint are kept, and the current state is checkpointed first, so the restore is itself reversible.` |
| [10760](/root/laintas_cli/tools.py:10760) | tool/help | register_builtin_tools / description | `Roll the working tree back to a checkpoint. Always asks the user first and cannot be pre-approved in bulk. Restores EVERY tracked file, so it also reverts edits made outside this session — propose it only when your own changes are the problem and the user agrees. Files created since the checkpoint are kept, and the current state is checkpointed first, so the restore is itself reversible.` |
| [10778](/root/laintas_cli/tools.py:10778) | title/menu/help | register_builtin_tools / description | `Capture user feedback about prompt behavior and spawn a background optimizer sub-agent to draft a patch. Use when the user reports a recurring behavioral issue that a prompt change could fix (e.g., 'you rarely reply', 'you don't verify').` |
| [10778](/root/laintas_cli/tools.py:10778) | tool/help | register_builtin_tools / description | `Capture user feedback about prompt behavior and spawn a background optimizer sub-agent to draft a patch. Use when the user reports a recurring behavioral issue that a prompt change could fix (e.g., 'you rarely reply', 'you don't verify').` |
| [10794](/root/laintas_cli/tools.py:10794) | title/menu/help | register_builtin_tools / description | `Write a prompt-optimization candidate to disk. Used by the optimizer sub-agent after diagnosing a cli.prop deficiency. The patch is an additive <prompt_opt_patch> block — do NOT include the wrapper tags in the 'patch' parameter.` |
| [10794](/root/laintas_cli/tools.py:10794) | tool/help | register_builtin_tools / description | `Write a prompt-optimization candidate to disk. Used by the optimizer sub-agent after diagnosing a cli.prop deficiency. The patch is an additive <prompt_opt_patch> block — do NOT include the wrapper tags in the 'patch' parameter.` |
| [10814](/root/laintas_cli/tools.py:10814) | title/menu/help | register_builtin_tools / description | `Read a prompt-optimization candidate (or the active one) to review its patch and rationale before applying.` |
| [10814](/root/laintas_cli/tools.py:10814) | tool/help | register_builtin_tools / description | `Read a prompt-optimization candidate (or the active one) to review its patch and rationale before applying.` |
| [10826](/root/laintas_cli/tools.py:10826) | title/menu/help | register_builtin_tools / description | `Capture a structured failure report (v3 template) and spawn a background optimizer sub-agent. The optimizer triages whether the fix belongs in cli.prop or a skill based on the failure category. Use when the user reports a specific behavioral failure with a clear cause.` |
| [10826](/root/laintas_cli/tools.py:10826) | tool/help | register_builtin_tools / description | `Capture a structured failure report (v3 template) and spawn a background optimizer sub-agent. The optimizer triages whether the fix belongs in cli.prop or a skill based on the failure category. Use when the user reports a specific behavioral failure with a clear cause.` |
| [10853](/root/laintas_cli/tools.py:10853) | title/menu/help | register_builtin_tools / description | `Draft a skill-optimization patch. Used by the optimizer sub-agent after diagnosing that a skill's SKILL.md or skill.py is the root cause of a failure. Two modes: 'append' adds a <skill_opt_patch> block to the file (best for SKILL.md instruction tweaks); 'replace' does an exact string replacement (best for skill.py code fixes or targeted SKILL.md edits). The user reviews and applies.` |
| [10853](/root/laintas_cli/tools.py:10853) | tool/help | register_builtin_tools / description | `Draft a skill-optimization patch. Used by the optimizer sub-agent after diagnosing that a skill's SKILL.md or skill.py is the root cause of a failure. Two modes: 'append' adds a <skill_opt_patch> block to the file (best for SKILL.md instruction tweaks); 'replace' does an exact string replacement (best for skill.py code fixes or targeted SKILL.md edits). The user reviews and applies.` |
| [10885](/root/laintas_cli/tools.py:10885) | title/menu/help | register_builtin_tools / description | `Draft a project-scoped Prompt Lab overlay and regression cases after diagnosing a captured incident. This never activates the patch; only the user can activate it.` |
| [10885](/root/laintas_cli/tools.py:10885) | tool/help | register_builtin_tools / description | `Draft a project-scoped Prompt Lab overlay and regression cases after diagnosing a captured incident. This never activates the patch; only the user can activate it.` |
| [10918](/root/laintas_cli/tools.py:10918) | title/menu/help | register_builtin_tools / description | `Draft a project-scoped feature/extension candidate in Evolution Lab. This writes only a candidate and never activates executable code.` |
| [10918](/root/laintas_cli/tools.py:10918) | tool/help | register_builtin_tools / description | `Draft a project-scoped feature/extension candidate in Evolution Lab. This writes only a candidate and never activates executable code.` |
| [10952](/root/laintas_cli/tools.py:10952) | title/menu/help | register_builtin_tools / description | `NON-BLOCKING. Spawn a disposable in-process child agent for one sub-task and keep working: it runs on its own thread and posts its result to your inbox, which you receive at the start of a later turn. (The tool called ˋspawnˋ is the BLOCKING one.) It has an isolated context and is not a hired employee. The judgement roles (reviewer, silent-failure-hunter, tester) come with a MANDATORY contract: their findings must cite path:line locations that the runtime resolves against the files, and as many real citations as issues they claim. Your own ˋcontractˋ is added to that one, never replaces it. Supports specialized roles (explorer, architect, reviewer, silent-failure-hunter, simplifier, tester) and fire-and-forget parallel spawning via the 'tasks' parameter (check inbox for results). wait=true makes this call BLOCK: it hands the whole batch to spawn_parallel (live status table, max 6 tasks) and holds your loop until every child is finished -- prefer calling spawn_parallel directly when you already know you want to wait.` |
| [10952](/root/laintas_cli/tools.py:10952) | tool/help | register_builtin_tools / description | `NON-BLOCKING. Spawn a disposable in-process child agent for one sub-task and keep working: it runs on its own thread and posts its result to your inbox, which you receive at the start of a later turn. (The tool called ˋspawnˋ is the BLOCKING one.) It has an isolated context and is not a hired employee. The judgement roles (reviewer, silent-failure-hunter, tester) come with a MANDATORY contract: their findings must cite path:line locations that the runtime resolves against the files, and as many real citations as issues they claim. Your own ˋcontractˋ is added to that one, never replaces it. Supports specialized roles (explorer, architect, reviewer, silent-failure-hunter, simplifier, tester) and fire-and-forget parallel spawning via the 'tasks' parameter (check inbox for results). wait=true makes this call BLOCK: it hands the whole batch to spawn_parallel (live status table, max 6 tasks) and holds your loop until every child is finished -- prefer calling spawn_parallel directly when you already know you want to wait.` |
| [11040](/root/laintas_cli/tools.py:11040) | title/menu/help | register_builtin_tools / description | `What the work you delegated is doing: every member of every branch you opened, its outcome or its current activity, elapsed time and tool count. Use it to decide whether to wait, follow up, or stop something — you do not have to block to find out.` |
| [11040](/root/laintas_cli/tools.py:11040) | tool/help | register_builtin_tools / description | `What the work you delegated is doing: every member of every branch you opened, its outcome or its current activity, elapsed time and tool count. Use it to decide whether to wait, follow up, or stop something — you do not have to block to find out.` |
| [11056](/root/laintas_cli/tools.py:11056) | title/menu/help | register_builtin_tools / description | `Ask the agent that spawned you a question you cannot answer yourself, and WAIT for the answer. Use it the moment you hit a wall that is your caller's to remove -- a tool you were not given, a task that contradicts your scope, a choice with consequences outside your task -- instead of working around it for the rest of your budget and reporting the wall at the end. You keep your context while you wait and continue where you left off. If no answer comes, you are released with instructions to proceed on your own judgement.` |
| [11056](/root/laintas_cli/tools.py:11056) | tool/help | register_builtin_tools / description | `Ask the agent that spawned you a question you cannot answer yourself, and WAIT for the answer. Use it the moment you hit a wall that is your caller's to remove -- a tool you were not given, a task that contradicts your scope, a choice with consequences outside your task -- instead of working around it for the rest of your budget and reporting the wall at the end. You keep your context while you wait and continue where you left off. If no answer comes, you are released with instructions to proceed on your own judgement.` |
| [11086](/root/laintas_cli/tools.py:11086) | title/menu/help | register_builtin_tools / description | `Answer a child of yours that is blocked waiting on you (its request arrives in your inbox as child-help). It resumes where it stopped, with your decision, keeping everything it had already worked out.` |
| [11086](/root/laintas_cli/tools.py:11086) | tool/help | register_builtin_tools / description | `Answer a child of yours that is blocked waiting on you (its request arrives in your inbox as child-help). It resumes where it stopped, with your decision, keeping everything it had already worked out.` |
| [11106](/root/laintas_cli/tools.py:11106) | title/menu/help | register_builtin_tools / description | `Send a message to your parent's or one of your own children's inbox. Only tree edges carry messages: a sibling is not reachable, and work that two siblings must agree on belongs to the parent that owns both.` |
| [11106](/root/laintas_cli/tools.py:11106) | tool/help | register_builtin_tools / description | `Send a message to your parent's or one of your own children's inbox. Only tree edges carry messages: a sibling is not reachable, and work that two siblings must agree on belongs to the parent that owns both.` |
| [11123](/root/laintas_cli/tools.py:11123) | title/menu/help | register_builtin_tools / description | `Bind the calling, already-running agent to a named shell terminal. User-created employee assignments start through /station --task.` |
| [11123](/root/laintas_cli/tools.py:11123) | tool/help | register_builtin_tools / description | `Bind the calling, already-running agent to a named shell terminal. User-created employee assignments start through /station --task.` |
| [11135](/root/laintas_cli/tools.py:11135) | title/menu/help | register_builtin_tools / description | `Abort another agent's execution.` |
| [11135](/root/laintas_cli/tools.py:11135) | tool/help | register_builtin_tools / description | `Abort another agent's execution.` |
| [11147](/root/laintas_cli/tools.py:11147) | title/menu/help | register_builtin_tools / description | `BLOCKING. Wait for ONE named agent to finish — the single-agent form of await_spawns. Without a timeout it waits as long as that agent keeps making progress, and gives up only once it goes silent.` |
| [11147](/root/laintas_cli/tools.py:11147) | tool/help | register_builtin_tools / description | `BLOCKING. Wait for ONE named agent to finish — the single-agent form of await_spawns. Without a timeout it waits as long as that agent keeps making progress, and gives up only once it goes silent.` |
| [11167](/root/laintas_cli/tools.py:11167) | title/menu/help | register_builtin_tools / description | `Hire a persistent employee with an independent base model, prompt, and tool policy. It stays undeployed unless a different explicit terminal is supplied; work while undeployed uses a private temporary terminal. This does not start an assignment. For one-off parallel tasks, use spawn_parallel instead — hire is for long-lived employees, spawn_parallel is for temporary task workers.` |
| [11167](/root/laintas_cli/tools.py:11167) | tool/help | register_builtin_tools / description | `Hire a persistent employee with an independent base model, prompt, and tool policy. It stays undeployed unless a different explicit terminal is supplied; work while undeployed uses a private temporary terminal. This does not start an assignment. For one-off parallel tasks, use spawn_parallel instead — hire is for long-lived employees, spawn_parallel is for temporary task workers.` |
| [11192](/root/laintas_cli/tools.py:11192) | title/menu/help | register_builtin_tools / description | `List the agents you can reach: your parent and your own children. Agents elsewhere in the tree (siblings, cousins, anything under another parent) are not listed and cannot be messaged - route through your parent instead.` |
| [11192](/root/laintas_cli/tools.py:11192) | tool/help | register_builtin_tools / description | `List the agents you can reach: your parent and your own children. Agents elsewhere in the tree (siblings, cousins, anything under another parent) are not listed and cannot be messaged - route through your parent instead.` |
| [11202](/root/laintas_cli/tools.py:11202) | title/menu/help | register_builtin_tools / description | `Rename the current agent. Only non-primary agents can rename themselves; the primary agent cannot be renamed.` |
| [11202](/root/laintas_cli/tools.py:11202) | tool/help | register_builtin_tools / description | `Rename the current agent. Only non-primary agents can rename themselves; the primary agent cannot be renamed.` |
| [11214](/root/laintas_cli/tools.py:11214) | title/menu/help | register_builtin_tools / description | `Permanently remove an agent and free its ID for reuse. The primary agent cannot be removed. A parent can remove its direct children; an agent can remove itself.` |
| [11214](/root/laintas_cli/tools.py:11214) | tool/help | register_builtin_tools / description | `Permanently remove an agent and free its ID for reuse. The primary agent cannot be removed. A parent can remove its direct children; an agent can remove itself.` |
| [11229](/root/laintas_cli/tools.py:11229) | title/menu/help | register_builtin_tools / description | `This machine's token accounting plus the Laintas account's balance, quota and subscription. Use it before committing to an expensive plan (a long parallel batch, a large model), and when the user asks what something has cost. The call allowance is one pool shared by every Laintas product, so these figures are not CLI-only.` |
| [11229](/root/laintas_cli/tools.py:11229) | tool/help | register_builtin_tools / description | `This machine's token accounting plus the Laintas account's balance, quota and subscription. Use it before committing to an expensive plan (a long parallel batch, a large model), and when the user asks what something has cost. The call allowance is one pool shared by every Laintas product, so these figures are not CLI-only.` |
| [11250](/root/laintas_cli/tools.py:11250) | title/menu/help | register_builtin_tools / description | `Ask the security policy what it would decide about a shell command, a file write or a delete — allow, needs_approval or deny, and which rule says so. Nothing runs. Use it before a long or destructive plan so you can choose a different approach, or tell the user what they will be asked to approve, instead of discovering the boundary by hitting it. Read-only: rules are the user's, changed with /policy.` |
| [11250](/root/laintas_cli/tools.py:11250) | tool/help | register_builtin_tools / description | `Ask the security policy what it would decide about a shell command, a file write or a delete — allow, needs_approval or deny, and which rule says so. Nothing runs. Use it before a long or destructive plan so you can choose a different approach, or tell the user what they will be asked to approve, instead of discovering the boundary by hitting it. Read-only: rules are the user's, changed with /policy.` |
| [11274](/root/laintas_cli/tools.py:11274) | title/menu/help | register_builtin_tools / description | `The active WorkGraph for this project: the objective it was opened with, its status and plan revision, the workflow phase and how many steps are done. Read this when resuming work, or when a long task needs to check it is still serving the objective the user set. Read-only: which work item is active is the user's choice.` |
| [11274](/root/laintas_cli/tools.py:11274) | tool/help | register_builtin_tools / description | `The active WorkGraph for this project: the objective it was opened with, its status and plan revision, the workflow phase and how many steps are done. Read this when resuming work, or when a long task needs to check it is still serving the objective the user set. Read-only: which work item is active is the user's choice.` |
| [11287](/root/laintas_cli/tools.py:11287) | title/menu/help | register_builtin_tools / description | `Recent tool failures in this CLI process, newest first, with the command, the error and a tail of the output. Your own failures are already in this conversation — reach for this when a CHILD agent or another terminal failed and you need to know what it actually hit. Filter by agent_id, terminal or tool name.` |
| [11287](/root/laintas_cli/tools.py:11287) | tool/help | register_builtin_tools / description | `Recent tool failures in this CLI process, newest first, with the command, the error and a tail of the output. Your own failures are already in this conversation — reach for this when a CHILD agent or another terminal failed and you need to know what it actually hit. Filter by agent_id, terminal or tool name.` |
| [11309](/root/laintas_cli/tools.py:11309) | title/menu/help | register_builtin_tools / description | `How full the account's Laintas Storage is: tier, bytes used, free allowance, overage and the per-file size limit. Check this before uploading anything large — an upload over the allowance is rejected by the gateway, and overage is billed.` |
| [11309](/root/laintas_cli/tools.py:11309) | tool/help | register_builtin_tools / description | `How full the account's Laintas Storage is: tier, bytes used, free allowance, overage and the per-file size limit. Check this before uploading anything large — an upload over the allowance is rejected by the gateway, and overage is billed.` |
| [11320](/root/laintas_cli/tools.py:11320) | title/menu/help | register_builtin_tools / description | `List a folder in Laintas Storage — the same cloud folder Helpwo mounts as "Laintas Storage" and the user manages with /shared. Omit 'path' for the root. This is remote storage, not this machine: use fs.ls for local files.` |
| [11320](/root/laintas_cli/tools.py:11320) | tool/help | register_builtin_tools / description | `List a folder in Laintas Storage — the same cloud folder Helpwo mounts as "Laintas Storage" and the user manages with /shared. Omit 'path' for the root. This is remote storage, not this machine: use fs.ls for local files.` |
| [11337](/root/laintas_cli/tools.py:11337) | title/menu/help | register_builtin_tools / description | `Download one file from Laintas Storage to this machine. Writes to 'local_path' (a directory keeps the remote name), or to the remote file's name in the working directory.` |
| [11337](/root/laintas_cli/tools.py:11337) | tool/help | register_builtin_tools / description | `Download one file from Laintas Storage to this machine. Writes to 'local_path' (a directory keeps the remote name), or to the remote file's name in the working directory.` |
| [11355](/root/laintas_cli/tools.py:11355) | title/menu/help | register_builtin_tools / description | `Upload one local file to Laintas Storage, where the user and their Helpwo sessions can open it. 'path' is the remote destination (defaults to the file's own name). To hand files to a specific Helpwo agent instead, use file_push.` |
| [11355](/root/laintas_cli/tools.py:11355) | tool/help | register_builtin_tools / description | `Upload one local file to Laintas Storage, where the user and their Helpwo sessions can open it. 'path' is the remote destination (defaults to the file's own name). To hand files to a specific Helpwo agent instead, use file_push.` |
| [11373](/root/laintas_cli/tools.py:11373) | title/menu/help | register_builtin_tools / description | `Create a folder in Laintas Storage.` |
| [11373](/root/laintas_cli/tools.py:11373) | tool/help | register_builtin_tools / description | `Create a folder in Laintas Storage.` |
| [11385](/root/laintas_cli/tools.py:11385) | title/menu/help | register_builtin_tools / description | `Move or rename a file or folder inside Laintas Storage.` |
| [11385](/root/laintas_cli/tools.py:11385) | tool/help | register_builtin_tools / description | `Move or rename a file or folder inside Laintas Storage.` |
| [11399](/root/laintas_cli/tools.py:11399) | title/menu/help | register_builtin_tools / description | `Delete a file or folder from Laintas Storage. Always asks the user first, in every policy mode: this store is shared with Helpwo and nothing here is covered by the local checkpoint.` |
| [11399](/root/laintas_cli/tools.py:11399) | tool/help | register_builtin_tools / description | `Delete a file or folder from Laintas Storage. Always asks the user first, in every policy mode: this store is shared with Helpwo and nothing here is covered by the local checkpoint.` |
| [11415](/root/laintas_cli/tools.py:11415) | title/menu/help | register_builtin_tools / description | `Push local files to Laintas shared cloud storage (R2) and notify a Helpwo agent. Files are uploaded via presigned URLs — the gateway never touches the bytes. After upload, a 'file_push' message is sent to the target agent so it can download the files into its workspace.` |
| [11415](/root/laintas_cli/tools.py:11415) | tool/help | register_builtin_tools / description | `Push local files to Laintas shared cloud storage (R2) and notify a Helpwo agent. Files are uploaded via presigned URLs — the gateway never touches the bytes. After upload, a 'file_push' message is sent to the target agent so it can download the files into its workspace.` |
| [11441](/root/laintas_cli/tools.py:11441) | title/menu/help | register_builtin_tools / description | `BLOCKING. Spawn ONE sub-agent and hold your loop until it finishes — you can do nothing else meanwhile. Use it only when the very next thing you do needs that result; otherwise use agent.spawn, which is the non-blocking form of this same mechanism. If the concurrency cap is reached the sub-agent queues and starts when a slot frees. Give COMPLETE instructions in goal: file paths, conventions, constraints.` |
| [11441](/root/laintas_cli/tools.py:11441) | tool/help | register_builtin_tools / description | `BLOCKING. Spawn ONE sub-agent and hold your loop until it finishes — you can do nothing else meanwhile. Use it only when the very next thing you do needs that result; otherwise use agent.spawn, which is the non-blocking form of this same mechanism. If the concurrency cap is reached the sub-agent queues and starts when a slot frees. Give COMPLETE instructions in goal: file paths, conventions, constraints.` |
| [11461](/root/laintas_cli/tools.py:11461) | title/menu/help | register_builtin_tools / description | `NON-BLOCKING by default. Spawn multiple TEMPORARY sub-agents in PARALLEL for one-off tasks: returns batch_id and child_ids immediately while results continue to arrive through your inbox, so continue useful independent work — whichever child finishes first reaches you first, without waiting for the slowest. For persistent long-lived employees, use agent.hire instead. Set wait=true only for a real barrier that requires ALL results before the next action; that BLOCKING mode returns a combined structured report. Max 6 agents per batch. Each member must work on DIFFERENT files — decompose by file boundaries. Agents beyond the concurrency cap queue automatically. In wait=true barrier mode there is no batch time budget; its live supervisor stops an individual child after several minutes without observable progress. The default asynchronous mode does not keep this tool call open to supervise the batch. Still scope each task to a reviewable slice (roughly <=300-400 lines of code, or an equivalently bounded unit): an oversized slice returns a shallower review, not a better one. Inside a child, prefer fs.read/fs.grep for reading code over shell one-liners that slice files — each distinct ad-hoc command re-triggers a policy approval and burns the same budget waiting on it.` |
| [11461](/root/laintas_cli/tools.py:11461) | tool/help | register_builtin_tools / description | `NON-BLOCKING by default. Spawn multiple TEMPORARY sub-agents in PARALLEL for one-off tasks: returns batch_id and child_ids immediately while results continue to arrive through your inbox, so continue useful independent work — whichever child finishes first reaches you first, without waiting for the slowest. For persistent long-lived employees, use agent.hire instead. Set wait=true only for a real barrier that requires ALL results before the next action; that BLOCKING mode returns a combined structured report. Max 6 agents per batch. Each member must work on DIFFERENT files — decompose by file boundaries. Agents beyond the concurrency cap queue automatically. In wait=true barrier mode there is no batch time budget; its live supervisor stops an individual child after several minutes without observable progress. The default asynchronous mode does not keep this tool call open to supervise the batch. Still scope each task to a reviewable slice (roughly <=300-400 lines of code, or an equivalently bounded unit): an oversized slice returns a shallower review, not a better one. Inside a child, prefer fs.read/fs.grep for reading code over shell one-liners that slice files — each distinct ad-hoc command re-triggers a policy approval and burns the same budget waiting on it.` |
| [11532](/root/laintas_cli/tools.py:11532) | title/menu/help | register_builtin_tools / description | `Run a SEQUENTIAL pipeline of sub-agents (2-6 steps) with automatic handoff. Each step receives a handoff document from the previous step. Use for dependent work: analyze → implement → verify. If a step fails the chain aborts. Returns the final step output + per-step summary.` |
| [11532](/root/laintas_cli/tools.py:11532) | tool/help | register_builtin_tools / description | `Run a SEQUENTIAL pipeline of sub-agents (2-6 steps) with automatic handoff. Each step receives a handoff document from the previous step. Use for dependent work: analyze → implement → verify. If a step fails the chain aborts. Returns the final step output + per-step summary.` |
| [11562](/root/laintas_cli/tools.py:11562) | title/menu/help | register_builtin_tools / description | `BLOCKING, and all-or-nothing: it returns only once EVERY selected child has finished, so the ones that finished early sit unused until the slowest one ends. There is no 'first result' mode. Select a returned batch_id or child agent_ids. If both are omitted, waits for ALL children of the current agent — not just the last batch. It gives up after 20 minutes, and on that timeout (or on Esc) every child still running is ABORTED and its work is lost. Use only when the next action truly depends on all of those results; otherwise read them from your inbox as they arrive, and use branch_status to see where the batch stands without blocking.` |
| [11562](/root/laintas_cli/tools.py:11562) | tool/help | register_builtin_tools / description | `BLOCKING, and all-or-nothing: it returns only once EVERY selected child has finished, so the ones that finished early sit unused until the slowest one ends. There is no 'first result' mode. Select a returned batch_id or child agent_ids. If both are omitted, waits for ALL children of the current agent — not just the last batch. It gives up after 20 minutes, and on that timeout (or on Esc) every child still running is ABORTED and its work is lost. Use only when the next action truly depends on all of those results; otherwise read them from your inbox as they arrive, and use branch_status to see where the batch stands without blocking.` |
| [11615](/root/laintas_cli/tools.py:11615) | title/menu/help | register_builtin_tools / description | `Compile, run, inspect, resume, or cancel a durable HWG graph. Use HWG to connect HWO stages with conditional routing, bounded cycles, retries, manual gates, and resumable checkpoints. Load the hwo-workflows skill before authoring or changing .hwg files.` |
| [11615](/root/laintas_cli/tools.py:11615) | tool/help | register_builtin_tools / description | `Compile, run, inspect, resume, or cancel a durable HWG graph. Use HWG to connect HWO stages with conditional routing, bounded cycles, retries, manual gates, and resumable checkpoints. Load the hwo-workflows skill before authoring or changing .hwg files.` |
| [11640](/root/laintas_cli/tools.py:11640) | title/menu/help | register_builtin_tools / description | `Send a message to a sibling or parent agent within the current HWO workflow, addressed by name (see your [TEAM MANIFEST]). Cannot reach descendants.` |
| [11640](/root/laintas_cli/tools.py:11640) | tool/help | register_builtin_tools / description | `Send a message to a sibling or parent agent within the current HWO workflow, addressed by name (see your [TEAM MANIFEST]). Cannot reach descendants.` |
| [11656](/root/laintas_cli/tools.py:11656) | title/menu/help | register_builtin_tools / description | `Block until a message arrives from a specific HWO teammate (or anyone, if 'from' is omitted). Default timeout 60s, max 300s.` |
| [11656](/root/laintas_cli/tools.py:11656) | tool/help | register_builtin_tools / description | `Block until a message arrives from a specific HWO teammate (or anyone, if 'from' is omitted). Default timeout 60s, max 300s.` |
| [11671](/root/laintas_cli/tools.py:11671) | title/menu/help | register_builtin_tools / description | `Submit declared HWO output values for the current agent. This stores variables for later #agent.output references; it does not terminate the agent.` |
| [11671](/root/laintas_cli/tools.py:11671) | tool/help | register_builtin_tools / description | `Submit declared HWO output values for the current agent. This stores variables for later #agent.output references; it does not terminate the agent.` |
| [11689](/root/laintas_cli/tools.py:11689) | title/menu/help | register_builtin_tools / description | `Send interactive input/keystrokes to a named terminal and return only newly observed output. This reports delivery, not command completion, and has no process exit code. Use shell.exec for one-shot commands.` |
| [11689](/root/laintas_cli/tools.py:11689) | tool/help | register_builtin_tools / description | `Send interactive input/keystrokes to a named terminal and return only newly observed output. This reports delivery, not command completion, and has no process exit code. Use shell.exec for one-shot commands.` |
| [11708](/root/laintas_cli/tools.py:11708) | title/menu/help | register_builtin_tools / description | `Terminate a named sub-terminal, recursively ending its child terminals and every agent deployed under that terminal subtree.` |
| [11708](/root/laintas_cli/tools.py:11708) | tool/help | register_builtin_tools / description | `Terminate a named sub-terminal, recursively ending its child terminals and every agent deployed under that terminal subtree.` |
| [11723](/root/laintas_cli/tools.py:11723) | title/menu/help | register_builtin_tools / description | `Read only output added since this agent's previous read/send cursor. Returns running/completed state and a real process exit code once known. Completed terminal.exec jobs remain readable for 10 minutes. When more than max_chars arrived at once you get the TAIL and a ˋresume_cursorˋ: pass it back as ˋcursorˋ to read the skipped middle, which is still buffered.` |
| [11723](/root/laintas_cli/tools.py:11723) | tool/help | register_builtin_tools / description | `Read only output added since this agent's previous read/send cursor. Returns running/completed state and a real process exit code once known. Completed terminal.exec jobs remain readable for 10 minutes. When more than max_chars arrived at once you get the TAIL and a ˋresume_cursorˋ: pass it back as ˋcursorˋ to read the skipped middle, which is still buffered.` |
| [11746](/root/laintas_cli/tools.py:11746) | title/menu/help | register_builtin_tools / description | `Scroll a named terminal's viewport within its scrollable history, like a mouse wheel. ˋaction=upˋ moves the view toward OLDER output (freeze); ˋaction=downˋ moves toward NEWER output; ˋaction=liveˋ pins back to the newest output (default). ˋlinesˋ defaults to the viewport height. After scrolling, the next <sub_terminals> injection shows the chosen window.` |
| [11746](/root/laintas_cli/tools.py:11746) | tool/help | register_builtin_tools / description | `Scroll a named terminal's viewport within its scrollable history, like a mouse wheel. ˋaction=upˋ moves the view toward OLDER output (freeze); ˋaction=downˋ moves toward NEWER output; ˋaction=liveˋ pins back to the newest output (default). ˋlinesˋ defaults to the viewport height. After scrolling, the next <sub_terminals> injection shows the chosen window.` |
| [11773](/root/laintas_cli/tools.py:11773) | title/menu/help | register_builtin_tools / description | `Wait until a terminal.exec background job completes or timeout expires. Returns new output, completion state, and the real exit code when known. Use this instead of sleep followed by terminal.read for finite jobs. Long output is tail-trimmed to max_chars and reports a ˋresume_cursorˋ for the skipped middle, same as terminal.read.` |
| [11773](/root/laintas_cli/tools.py:11773) | tool/help | register_builtin_tools / description | `Wait until a terminal.exec background job completes or timeout expires. Returns new output, completion state, and the real exit code when known. Use this instead of sleep followed by terminal.read for finite jobs. Long output is tail-trimmed to max_chars and reports a ˋresume_cursorˋ for the skipped middle, same as terminal.read.` |
| [11800](/root/laintas_cli/tools.py:11800) | title/menu/help | register_builtin_tools / description | `Create a named managed laintas-cli terminal with NO command running in it yet — for stationing an agent, or for sending keystrokes later. To start a terminal that runs a command, use terminal.exec instead. It remains available for later sends/stationing while its parent terminal is alive. Terminating a parent recursively ends child terminals and their deployed agents; use session.start for a disposable agent-private PTY.` |
| [11800](/root/laintas_cli/tools.py:11800) | tool/help | register_builtin_tools / description | `Create a named managed laintas-cli terminal with NO command running in it yet — for stationing an agent, or for sending keystrokes later. To start a terminal that runs a command, use terminal.exec instead. It remains available for later sends/stationing while its parent terminal is alive. Terminating a parent recursively ends child terminals and their deployed agents; use session.start for a disposable agent-private PTY.` |
| [11819](/root/laintas_cli/tools.py:11819) | title/menu/help | register_builtin_tools / description | `List all named sub-terminals and their statuses.` |
| [11819](/root/laintas_cli/tools.py:11819) | tool/help | register_builtin_tools / description | `List all named sub-terminals and their statuses.` |
| [11826](/root/laintas_cli/tools.py:11826) | title/menu/help | register_builtin_tools / description | `NON-BLOCKING. Run an arbitrary shell command in a background sub-terminal and keep working — the right tool for anything slow, where shell.exec would hold your loop until it exits. Optionally set a trigger regex: any new output line matching the pattern will push a watch.trigger event to the agent's inbox. This call reports started/running, not a successful process exit; use terminal.wait or terminal.read for completion and exit status.` |
| [11826](/root/laintas_cli/tools.py:11826) | tool/help | register_builtin_tools / description | `NON-BLOCKING. Run an arbitrary shell command in a background sub-terminal and keep working — the right tool for anything slow, where shell.exec would hold your loop until it exits. Optionally set a trigger regex: any new output line matching the pattern will push a watch.trigger event to the agent's inbox. This call reports started/running, not a successful process exit; use terminal.wait or terminal.read for completion and exit status.` |
| [11851](/root/laintas_cli/tools.py:11851) | title/menu/help | register_builtin_tools / description | `Set or clear a trigger on an existing sub-terminal. When pattern is non-empty, matching output lines push watch.trigger events to the inbox of every agent listed in agent_ids (defaults to the calling agent). Empty pattern clears the trigger. When the terminal process exits, a terminal.exit event is also delivered.` |
| [11851](/root/laintas_cli/tools.py:11851) | tool/help | register_builtin_tools / description | `Set or clear a trigger on an existing sub-terminal. When pattern is non-empty, matching output lines push watch.trigger events to the inbox of every agent listed in agent_ids (defaults to the calling agent). Empty pattern clears the trigger. When the terminal process exits, a terminal.exit event is also delivered.` |
| [11876](/root/laintas_cli/tools.py:11876) | title/menu/help | register_builtin_tools / description | `Start one agent-private temporary PTY for an INTERACTIVE command — a REPL, an ssh login, anything that asks questions and needs keystrokes back. You may hold only one at a time, it is not a named terminal, and it closes with the agent run. For a non-interactive command that merely takes a long time, terminal.exec is the one to use.` |
| [11876](/root/laintas_cli/tools.py:11876) | tool/help | register_builtin_tools / description | `Start one agent-private temporary PTY for an INTERACTIVE command — a REPL, an ssh login, anything that asks questions and needs keystrokes back. You may hold only one at a time, it is not a named terminal, and it closes with the agent run. For a non-interactive command that merely takes a long time, terminal.exec is the one to use.` |
| [11895](/root/laintas_cli/tools.py:11895) | title/menu/help | register_builtin_tools / description | `Read new output from the current temporary PTY, or return a full tail.` |
| [11895](/root/laintas_cli/tools.py:11895) | tool/help | register_builtin_tools / description | `Read new output from the current temporary PTY, or return a full tail.` |
| [11907](/root/laintas_cli/tools.py:11907) | title/menu/help | register_builtin_tools / description | `Inspect the current agent-private temporary PTY.` |
| [11907](/root/laintas_cli/tools.py:11907) | tool/help | register_builtin_tools / description | `Inspect the current agent-private temporary PTY.` |
| [11913](/root/laintas_cli/tools.py:11913) | title/menu/help | register_builtin_tools / description | `Close the current one-off interactive PTY session and capture its output.` |
| [11913](/root/laintas_cli/tools.py:11913) | tool/help | register_builtin_tools / description | `Close the current one-off interactive PTY session and capture its output.` |
| [11919](/root/laintas_cli/tools.py:11919) | title/menu/help | register_builtin_tools / description | `Send keystrokes to the current interactive PTY session.` |
| [11919](/root/laintas_cli/tools.py:11919) | tool/help | register_builtin_tools / description | `Send keystrokes to the current interactive PTY session.` |
| [11938](/root/laintas_cli/tools.py:11938) | title/menu/help | register_builtin_tools / description | `Sleep for exactly N seconds (e.g., after starting a dev server). Cap: 300s.` |
| [11938](/root/laintas_cli/tools.py:11938) | tool/help | register_builtin_tools / description | `Sleep for exactly N seconds (e.g., after starting a dev server). Cap: 300s.` |
| [11951](/root/laintas_cli/tools.py:11951) | title/menu/help | register_builtin_tools / description | `Persist an explicit long-lived user rule. Use only when the user clearly establishes a recurring or cross-session requirement; never infer durability from a keyword alone.` |
| [11951](/root/laintas_cli/tools.py:11951) | tool/help | register_builtin_tools / description | `Persist an explicit long-lived user rule. Use only when the user clearly establishes a recurring or cross-session requirement; never infer durability from a keyword alone.` |
| [11969](/root/laintas_cli/tools.py:11969) | title/menu/help | register_builtin_tools / description | `List structured durable user rules for this project.` |
| [11969](/root/laintas_cli/tools.py:11969) | tool/help | register_builtin_tools / description | `List structured durable user rules for this project.` |
| [11977](/root/laintas_cli/tools.py:11977) | title/menu/help | register_builtin_tools / description | `Cancel a durable rule only when the user explicitly withdraws or replaces it.` |
| [11977](/root/laintas_cli/tools.py:11977) | tool/help | register_builtin_tools / description | `Cancel a durable rule only when the user explicitly withdraws or replaces it.` |
| [11986](/root/laintas_cli/tools.py:11986) | title/menu/help | register_builtin_tools / description | `After actually fulfilling a before_task_completion durable rule, mark that rule satisfied for the current task.` |
| [11986](/root/laintas_cli/tools.py:11986) | tool/help | register_builtin_tools / description | `After actually fulfilling a before_task_completion durable rule, mark that rule satisfied for the current task.` |
| [11997](/root/laintas_cli/tools.py:11997) | title/menu/help | register_builtin_tools / description | `Explicitly complete the current non-gated workflow phase after its requirements are satisfied. This is distinct from task_complete.` |
| [11997](/root/laintas_cli/tools.py:11997) | tool/help | register_builtin_tools / description | `Explicitly complete the current non-gated workflow phase after its requirements are satisfied. This is distinct from task_complete.` |
| [12008](/root/laintas_cli/tools.py:12008) | title/menu/help | register_builtin_tools / description | `Affirmatively signal the user's task is fully finished. Call this — and ONLY this — when there is no remaining work and no further tool call is needed. In autonomous/execute mode this is the only way to end normally: simply not calling a tool does NOT mean done. Put your final result summary in ˋsummaryˋ.` |
| [12008](/root/laintas_cli/tools.py:12008) | tool/help | register_builtin_tools / description | `Affirmatively signal the user's task is fully finished. Call this — and ONLY this — when there is no remaining work and no further tool call is needed. In autonomous/execute mode this is the only way to end normally: simply not calling a tool does NOT mean done. Put your final result summary in ˋsummaryˋ.` |
| [12034](/root/laintas_cli/tools.py:12034) | title/menu/help | register_builtin_tools / description | `Hand work to the PERSON as a checklist they can see (/retask, Alt+R) — only for steps only they can do: signing in to a dashboard, creating API keys, paying, answering an exercise. Never for work you can do yourself. Each task: one action, a text description saying where to go, what to do and which file the result goes in (no images or videos — this is a terminal), and checks that decide done. Load the retask skill first.` |
| [12034](/root/laintas_cli/tools.py:12034) | tool/help | register_builtin_tools / description | `Hand work to the PERSON as a checklist they can see (/retask, Alt+R) — only for steps only they can do: signing in to a dashboard, creating API keys, paying, answering an exercise. Never for work you can do yourself. Each task: one action, a text description saying where to go, what to do and which file the result goes in (no images or videos — this is a terminal), and checks that decide done. Load the retask skill first.` |
| [12071](/root/laintas_cli/tools.py:12071) | title/menu/help | register_builtin_tools / description | `Change the person's checklist. status=done runs the task's checks against the workspace first: a failing check makes the task ˋrejectedˋ with the gaps noted (returned as ok=false) — tell the person what is missing. A task with a review check needs a note naming the evidence you looked at. Also edits title/description/checks/after, appends tasks, or removes one. Statuses: todo, doing, submitted, done, rejected, skipped.` |
| [12071](/root/laintas_cli/tools.py:12071) | tool/help | register_builtin_tools / description | `Change the person's checklist. status=done runs the task's checks against the workspace first: a failing check makes the task ˋrejectedˋ with the gaps noted (returned as ok=false) — tell the person what is missing. A task with a review check needs a note naming the evidence you looked at. Also edits title/description/checks/after, appends tasks, or removes one. Statuses: todo, doing, submitted, done, rejected, skipped.` |
| [12100](/root/laintas_cli/tools.py:12100) | title/menu/help | register_builtin_tools / description | `Read the person's checklist in full (every description, check and note). The per-turn <retask> block only carries the current task; read this before rewriting tasks.` |
| [12100](/root/laintas_cli/tools.py:12100) | tool/help | register_builtin_tools / description | `Read the person's checklist in full (every description, check and note). The per-turn <retask> block only carries the current task; read this before rewriting tasks.` |
| [12115](/root/laintas_cli/tools.py:12115) | title/menu/help | register_builtin_tools / description | `Look at an image file and answer a question about it. Use for screenshots, mockups, diagrams, photos — anything where you need to know what it LOOKS like: is the layout broken, what does this chart show, why is this page blank. Costs a call on a vision model, so ask a specific question rather than requesting a general description twice. To read a document word for word, use image.to_text instead.` |
| [12115](/root/laintas_cli/tools.py:12115) | tool/help | register_builtin_tools / description | `Look at an image file and answer a question about it. Use for screenshots, mockups, diagrams, photos — anything where you need to know what it LOOKS like: is the layout broken, what does this chart show, why is this page blank. Costs a call on a vision model, so ask a specific question rather than requesting a general description twice. To read a document word for word, use image.to_text instead.` |
| [12135](/root/laintas_cli/tools.py:12135) | title/menu/help | register_builtin_tools / description | `Reproduce a document or image as text, page by page, keeping headings and tables. Use for scans, photographed pages, receipts, image-only PDFs — anything where a summary would lose the content. Accepts .pdf as well as image files. To ask what a picture LOOKS like, use image.describe instead.` |
| [12135](/root/laintas_cli/tools.py:12135) | tool/help | register_builtin_tools / description | `Reproduce a document or image as text, page by page, keeping headings and tables. Use for scans, photographed pages, receipts, image-only PDFs — anything where a summary would lose the content. Accepts .pdf as well as image files. To ask what a picture LOOKS like, use image.describe instead.` |
| [12154](/root/laintas_cli/tools.py:12154) | title/menu/help | register_builtin_tools / description | `[DEBUG/P1] Open a headless-browser live-view session. Spawns Xvfb+Chrome+x11vnc on the host and bridges to the backend /vnc relay. Returns CDP endpoint, VNC port, and display number. The WS relay shows 'retrying' until the backend deploys /vnc — that's expected; Chrome+CDP+VNC are up regardless.` |
| [12154](/root/laintas_cli/tools.py:12154) | tool/help | register_builtin_tools / description | `[DEBUG/P1] Open a headless-browser live-view session. Spawns Xvfb+Chrome+x11vnc on the host and bridges to the backend /vnc relay. Returns CDP endpoint, VNC port, and display number. The WS relay shows 'retrying' until the backend deploys /vnc — that's expected; Chrome+CDP+VNC are up regardless.` |
| [12176](/root/laintas_cli/tools.py:12176) | title/menu/help | register_builtin_tools / description | `[DEBUG/P1] Close a headless-browser session by name.` |
| [12176](/root/laintas_cli/tools.py:12176) | tool/help | register_builtin_tools / description | `[DEBUG/P1] Close a headless-browser session by name.` |
| [12188](/root/laintas_cli/tools.py:12188) | title/menu/help | register_builtin_tools / description | `[DEBUG/P1] List active browser sessions and their status.` |
| [12188](/root/laintas_cli/tools.py:12188) | tool/help | register_builtin_tools / description | `[DEBUG/P1] List active browser sessions and their status.` |
| [12196](/root/laintas_cli/tools.py:12196) | title/menu/help | register_builtin_tools / description | `Open a new headless-browser session with live-view relay. Spawns Xvfb+Chrome+x11vnc and bridges to the backend /vnc. Returns session name, CDP endpoint, and VNC port. Use browser.navigate/click/type/etc. to interact afterwards.` |
| [12196](/root/laintas_cli/tools.py:12196) | tool/help | register_builtin_tools / description | `Open a new headless-browser session with live-view relay. Spawns Xvfb+Chrome+x11vnc and bridges to the backend /vnc. Returns session name, CDP endpoint, and VNC port. Use browser.navigate/click/type/etc. to interact afterwards.` |
| [12216](/root/laintas_cli/tools.py:12216) | title/menu/help | register_builtin_tools / description | `Close a browser session and tear down its Chrome/Xvfb/x11vnc stack.` |
| [12216](/root/laintas_cli/tools.py:12216) | tool/help | register_builtin_tools / description | `Close a browser session and tear down its Chrome/Xvfb/x11vnc stack.` |
| [12228](/root/laintas_cli/tools.py:12228) | title/menu/help | register_builtin_tools / description | `Navigate the browser to a URL. Returns the page title.` |
| [12228](/root/laintas_cli/tools.py:12228) | tool/help | register_builtin_tools / description | `Navigate the browser to a URL. Returns the page title.` |
| [12244](/root/laintas_cli/tools.py:12244) | title/menu/help | register_builtin_tools / description | `Click an element identified by CSS selector or by ref from browser.snapshot.` |
| [12244](/root/laintas_cli/tools.py:12244) | tool/help | register_builtin_tools / description | `Click an element identified by CSS selector or by ref from browser.snapshot.` |
| [12258](/root/laintas_cli/tools.py:12258) | title/menu/help | register_builtin_tools / description | `Type text into an input element identified by CSS selector or by ref from browser.snapshot. Optionally clears the field first (default: yes).` |
| [12258](/root/laintas_cli/tools.py:12258) | tool/help | register_builtin_tools / description | `Type text into an input element identified by CSS selector or by ref from browser.snapshot. Optionally clears the field first (default: yes).` |
| [12277](/root/laintas_cli/tools.py:12277) | title/menu/help | register_builtin_tools / description | `Take a screenshot and save to a file. Returns the file path. The AI cannot see the image — use browser.snapshot for text content.` |
| [12277](/root/laintas_cli/tools.py:12277) | tool/help | register_builtin_tools / description | `Take a screenshot and save to a file. Returns the file path. The AI cannot see the image — use browser.snapshot for text content.` |
| [12291](/root/laintas_cli/tools.py:12291) | title/menu/help | register_builtin_tools / description | `Query DOM elements by CSS selector. Returns tag, text, and key attributes (href, src, value, placeholder, etc.) for each match. Use this to understand page structure before clicking/typing.` |
| [12291](/root/laintas_cli/tools.py:12291) | tool/help | register_builtin_tools / description | `Query DOM elements by CSS selector. Returns tag, text, and key attributes (href, src, value, placeholder, etc.) for each match. Use this to understand page structure before clicking/typing.` |
| [12308](/root/laintas_cli/tools.py:12308) | title/menu/help | register_builtin_tools / description | `Return a text snapshot of the page: URL, title, visible body text, and numbered refs for interactive elements. Use refs with browser.click/type/select.` |
| [12308](/root/laintas_cli/tools.py:12308) | tool/help | register_builtin_tools / description | `Return a text snapshot of the page: URL, title, visible body text, and numbered refs for interactive elements. Use refs with browser.click/type/select.` |
| [12323](/root/laintas_cli/tools.py:12323) | title/menu/help | register_builtin_tools / description | `Scroll the page by (x, y) pixels or scroll an element into view by selector.` |
| [12323](/root/laintas_cli/tools.py:12323) | tool/help | register_builtin_tools / description | `Scroll the page by (x, y) pixels or scroll an element into view by selector.` |
| [12337](/root/laintas_cli/tools.py:12337) | title/menu/help | register_builtin_tools / description | `Evaluate a JavaScript expression on the page and return the result. Use for custom DOM queries or actions not covered by other browser.* tools.` |
| [12337](/root/laintas_cli/tools.py:12337) | tool/help | register_builtin_tools / description | `Evaluate a JavaScript expression on the page and return the result. Use for custom DOM queries or actions not covered by other browser.* tools.` |
| [12351](/root/laintas_cli/tools.py:12351) | title/menu/help | register_builtin_tools / description | `Press a keyboard key (e.g., Enter, Tab, Escape, ArrowDown, Control+c).` |
| [12351](/root/laintas_cli/tools.py:12351) | tool/help | register_builtin_tools / description | `Press a keyboard key (e.g., Enter, Tab, Escape, ArrowDown, Control+c).` |
| [12364](/root/laintas_cli/tools.py:12364) | title/menu/help | register_builtin_tools / description | `Get the current page URL.` |
| [12364](/root/laintas_cli/tools.py:12364) | tool/help | register_builtin_tools / description | `Get the current page URL.` |
| [12375](/root/laintas_cli/tools.py:12375) | title/menu/help | register_builtin_tools / description | `Get the current page title.` |
| [12375](/root/laintas_cli/tools.py:12375) | tool/help | register_builtin_tools / description | `Get the current page title.` |
| [12386](/root/laintas_cli/tools.py:12386) | title/menu/help | register_builtin_tools / description | `Wait for an element to reach a state (visible/hidden/attached/detached). Useful for waiting for dynamic content to load.` |
| [12386](/root/laintas_cli/tools.py:12386) | tool/help | register_builtin_tools / description | `Wait for an element to reach a state (visible/hidden/attached/detached). Useful for waiting for dynamic content to load.` |
| [12403](/root/laintas_cli/tools.py:12403) | title/menu/help | register_builtin_tools / description | `Select an option in a <select> dropdown by value or label, using CSS selector or ref.` |
| [12403](/root/laintas_cli/tools.py:12403) | tool/help | register_builtin_tools / description | `Select an option in a <select> dropdown by value or label, using CSS selector or ref.` |
| [12418](/root/laintas_cli/tools.py:12418) | title/menu/help | register_builtin_tools / description | `Navigate back in browser history.` |
| [12418](/root/laintas_cli/tools.py:12418) | tool/help | register_builtin_tools / description | `Navigate back in browser history.` |
| [12429](/root/laintas_cli/tools.py:12429) | title/menu/help | register_builtin_tools / description | `Navigate forward in browser history.` |
| [12429](/root/laintas_cli/tools.py:12429) | tool/help | register_builtin_tools / description | `Navigate forward in browser history.` |
| [12440](/root/laintas_cli/tools.py:12440) | title/menu/help | register_builtin_tools / description | `Runtime-error digest for testing: uncaught JS exceptions + console.error messages + failed/4xx-5xx network requests captured since the page loaded. Returns clean=true when none. Call after navigating/interacting to verify the page didn't break.` |
| [12440](/root/laintas_cli/tools.py:12440) | tool/help | register_builtin_tools / description | `Runtime-error digest for testing: uncaught JS exceptions + console.error messages + failed/4xx-5xx network requests captured since the page loaded. Returns clean=true when none. Call after navigating/interacting to verify the page didn't break.` |
| [12451](/root/laintas_cli/tools.py:12451) | title/menu/help | register_builtin_tools / description | `Console messages captured from the page. Optional level filter (error/warning/log/info/debug); omit or 'all' for everything.` |
| [12451](/root/laintas_cli/tools.py:12451) | tool/help | register_builtin_tools / description | `Console messages captured from the page. Optional level filter (error/warning/log/info/debug); omit or 'all' for everything.` |
| [12461](/root/laintas_cli/tools.py:12461) | title/menu/help | register_builtin_tools / description | `Assert a condition about the page for testing. Returns pass=true/false. Use selector + one of: text (element text contains), state (visible\|hidden), count (number of matches); or page-level url_contains / title_contains. With selector and no condition, asserts the element exists.` |
| [12461](/root/laintas_cli/tools.py:12461) | tool/help | register_builtin_tools / description | `Assert a condition about the page for testing. Returns pass=true/false. Use selector + one of: text (element text contains), state (visible\|hidden), count (number of matches); or page-level url_contains / title_contains. With selector and no condition, asserts the element exists.` |
| [12479](/root/laintas_cli/tools.py:12479) | title/menu/help | register_builtin_tools / description | `Run a multi-step website test and get a pass/fail report. Give 'steps': an ordered array of {action, ...params} where action is one of navigate\|click\|type\|select\|press_key\|scroll\|wait_for\|evaluate\|expect (same params as the matching browser.* tool; use 'expect' steps for assertions). Stops at the first failed step, auto-captures a screenshot, and (unless check_errors=false) fails the flow on captured runtime errors. Returns pass=true/false, failed_at, per-step results, and the screenshot path.` |
| [12479](/root/laintas_cli/tools.py:12479) | tool/help | register_builtin_tools / description | `Run a multi-step website test and get a pass/fail report. Give 'steps': an ordered array of {action, ...params} where action is one of navigate\|click\|type\|select\|press_key\|scroll\|wait_for\|evaluate\|expect (same params as the matching browser.* tool; use 'expect' steps for assertions). Stops at the first failed step, auto-captures a screenshot, and (unless check_errors=false) fails the flow on captured runtime errors. Returns pass=true/false, failed_at, per-step results, and the screenshot path.` |
| [12504](/root/laintas_cli/tools.py:12504) | title/menu/help | register_builtin_tools / description | `List password-vault entries: opaque id, kind (login or secret — secret entries are a single key/token in any format), user-written description, and approved HTTPS origins only. Usernames, passwords, secret values, and notes are encrypted and never returned. Works while the vault is locked.` |
| [12504](/root/laintas_cli/tools.py:12504) | tool/help | register_builtin_tools / description | `List password-vault entries: opaque id, kind (login or secret — secret entries are a single key/token in any format), user-written description, and approved HTTPS origins only. Usernames, passwords, secret values, and notes are encrypted and never returned. Works while the vault is locked.` |
| [12516](/root/laintas_cli/tools.py:12516) | title/menu/help | register_builtin_tools / description | `Fill a vault credential into a protected browser login form. Requires a broker-issued protected tab handle and never accepts scripts, arbitrary destinations, or secret values. Returns a fixed status: filled, locked, origin_mismatch, unsupported_form, or broker_unavailable (no trusted broker deployed — fail closed, no fallback to the ordinary browser).` |
| [12516](/root/laintas_cli/tools.py:12516) | tool/help | register_builtin_tools / description | `Fill a vault credential into a protected browser login form. Requires a broker-issued protected tab handle and never accepts scripts, arbitrary destinations, or secret values. Returns a fixed status: filled, locked, origin_mismatch, unsupported_form, or broker_unavailable (no trusted broker deployed — fail closed, no fallback to the ordinary browser).` |

### transcript_view.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [562](/root/laintas_cli/transcript_view.py:562) | label/render | TranscriptRenderer.command_block | `  {line}` |
| [562](/root/laintas_cli/transcript_view.py:562) | output | TranscriptRenderer.command_block / 0 | `  {line}` |
| [632](/root/laintas_cli/transcript_view.py:632) | output | TranscriptRenderer._after_row / 0 | `    {escape(line)}` |

### trust_store.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [75](/root/laintas_cli/trust_store.py:75) | error candidate | _current_hashes | `refusing executable symlink: {path}` |
| [152](/root/laintas_cli/trust_store.py:152) | error candidate | trust_executable_file | `refusing non-file or symbolic link: {path}` |

### updater.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [158](/root/laintas_cli/updater.py:158) | output | _log_no_write_permission / 0 | `No write permission for {directory}.` |
| [159](/root/laintas_cli/updater.py:159) | output | _log_no_write_permission / 0 | `Re-run with sudo to replace the binary in place.` |
| [281](/root/laintas_cli/updater.py:281) | error candidate | _release_checksums | `SHA256SUMS.txt is not valid UTF-8` |
| [288](/root/laintas_cli/updater.py:288) | error candidate | _release_checksums | `SHA256SUMS.txt contains a malformed line` |
| [295](/root/laintas_cli/updater.py:295) | error candidate | _release_checksums | `SHA256SUMS.txt contains an invalid entry` |
| [311](/root/laintas_cli/updater.py:311) | error candidate | fetch_manifest | `manifest.json is missing required keys (version, files)` |
| [314](/root/laintas_cli/updater.py:314) | error candidate | fetch_manifest | `manifest.json has no files` |
| [318](/root/laintas_cli/updater.py:318) | error candidate | fetch_manifest | `manifest entry '{name}' is missing a valid sha256` |
| [402](/root/laintas_cli/updater.py:402) | output | apply_source_update / 0 | `{base} is a git checkout — refusing to overwrite it. Update it with ˋgit pullˋ.` |
| [413](/root/laintas_cli/updater.py:413) | title/menu/help | apply_source_update / label | `src_manifest.zip` |
| [416](/root/laintas_cli/updater.py:416) | output | apply_source_update / 0 | `Could not download source bundle: {e}` |
| [429](/root/laintas_cli/updater.py:429) | output | apply_source_update / 0 | `Source bundle contains an unsafe path: {entry!r}. Aborting.` |
| [434](/root/laintas_cli/updater.py:434) | output | apply_source_update / 0 | `Source bundle is not a valid zip. Aborting.` |
| [442](/root/laintas_cli/updater.py:442) | output | apply_source_update / 0 | `{name} is missing from the source bundle. Aborting.` |
| [445](/root/laintas_cli/updater.py:445) | output | apply_source_update / 0 | `{name} has no sha256 in manifest. Aborting.` |
| [448](/root/laintas_cli/updater.py:448) | output | apply_source_update / 0 | `Checksum mismatch for {name}. Aborting.` |
| [475](/root/laintas_cli/updater.py:475) | output | apply_source_update / 0 | `Write failed ({e}); rolling back…` |
| [489](/root/laintas_cli/updater.py:489) | output | apply_source_update / 0 | `Your replaced files are in {backup_dir}` |
| [505](/root/laintas_cli/updater.py:505) | output | apply_source_update / 0 | `This update added Python dependencies that are not installed in this environment: {', '.join(missing)}.` |
| [508](/root/laintas_cli/updater.py:508) | output | apply_source_update / 0 | `Install them with: pip install {' '.join(missing)}` |
| [569](/root/laintas_cli/updater.py:569) | output | apply_frozen_update / 0 | `Unsupported architecture: {machine or 'unknown'}` |
| [578](/root/laintas_cli/updater.py:578) | title/menu/help | apply_frozen_update / label | `SHA256SUMS.txt` |
| [583](/root/laintas_cli/updater.py:583) | output | apply_frozen_update / 0 | `SHA256SUMS.txt does not cover {asset}. Aborting.` |
| [587](/root/laintas_cli/updater.py:587) | output | apply_frozen_update / 0 | `Could not download or parse release checksums: {exc}` |
| [592](/root/laintas_cli/updater.py:592) | output | apply_frozen_update / 0 | `Checksum mismatch for {asset}; the download may be corrupted. Aborting.` |
| [624](/root/laintas_cli/updater.py:624) | output | apply_frozen_update / 0 | `Unexpected tarball layout; aborting.` |
| [644](/root/laintas_cli/updater.py:644) | output | apply_frozen_update / 0 | `Could not replace {target} through sudo: {exc}` |
| [649](/root/laintas_cli/updater.py:649) | output | apply_frozen_update / 0 | `Could not replace {target} through sudo{': ' + detail if detail else ''}.` |
| [653](/root/laintas_cli/updater.py:653) | output | apply_frozen_update / 0 | `Updated executable is missing after replacing {target}.` |
| [687](/root/laintas_cli/updater.py:687) | error candidate | apply_frozen_update | `updated executable is missing after atomic replace: {target}` |
| [750](/root/laintas_cli/updater.py:750) | error candidate | fetch_helpwo_manifest | `{HELPWO_MANIFEST_ASSET} is missing required keys (version, files)` |
| [753](/root/laintas_cli/updater.py:753) | error candidate | fetch_helpwo_manifest | `{HELPWO_MANIFEST_ASSET} has no files` |
| [755](/root/laintas_cli/updater.py:755) | error candidate | fetch_helpwo_manifest | `{HELPWO_MANIFEST_ASSET} has no index.html` |
| [758](/root/laintas_cli/updater.py:758) | error candidate | fetch_helpwo_manifest | `unsafe path in {HELPWO_MANIFEST_ASSET}: {name!r}` |
| [761](/root/laintas_cli/updater.py:761) | error candidate | fetch_helpwo_manifest | `manifest entry '{name}' is missing a valid sha256` |
| [809](/root/laintas_cli/updater.py:809) | output | install_helpwo_dist / 0 | `Downloading Helpwo UI {version} ({len(changed)} file(s) differ)…` |

### usage_tracker.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [81](/root/laintas_cli/usage_tracker.py:81) | output | record / 0 | `model.usage` |

### vision.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [110](/root/laintas_cli/vision.py:110) | error candidate | _read_image | `a file path is required` |
| [112](/root/laintas_cli/vision.py:112) | error candidate | _read_image | `no such file: {path}` |
| [116](/root/laintas_cli/vision.py:116) | error candidate | _read_image | `{ext or 'this file'} is not an image format this reads ({', '.join(sorted(IMAGE_EXTENSIONS))}). For a PDF, use image.to_text, which handles documents directly.` |
| [122](/root/laintas_cli/vision.py:122) | error candidate | _read_image | `image is {size / 1000000.0:.1f} MB, over the {MAX_FILE_BYTES / 1000000.0:.0f} MB limit — resize it first` |
| [158](/root/laintas_cli/vision.py:158) | error candidate | _fit | `Pillow is required to prepare images (pip install Pillow)` |
| [163](/root/laintas_cli/vision.py:163) | error candidate | _fit | `could not decode the image ({type(e).__name__}: {e})` |
| [225](/root/laintas_cli/vision.py:225) | error candidate | describe_image | `no backend callable was provided` |
| [238](/root/laintas_cli/vision.py:238) | error candidate | describe_image | `the vision endpoint could not read this image (the file itself was read and decoded fine): {type(e).__name__}: {e}` |
| [243](/root/laintas_cli/vision.py:243) | error candidate | describe_image | `no enabled vision model could read this image (the file itself was read and decoded fine). Configure the image-understanding model order in System -> API Keys. Gateway response: {detail}` |
| [248](/root/laintas_cli/vision.py:248) | error candidate | describe_image | `the vision endpoint returned an empty response` |
| [269](/root/laintas_cli/vision.py:269) | error candidate | image_to_text | `no such file: {path}` |
| [273](/root/laintas_cli/vision.py:273) | error candidate | image_to_text | `PDF is {size / 1000000.0:.1f} MB, over the {MAX_FILE_BYTES / 1000000.0:.0f} MB limit` |
| [286](/root/laintas_cli/vision.py:286) | error candidate | image_to_text | `the encoded document is {len(data_url) / 1000000.0:.1f} M characters, over the {OCR_MAX_DATA_URL_CHARS / 1000000.0:.0f} M inline limit — split it or reduce its resolution first` |
| [297](/root/laintas_cli/vision.py:297) | error candidate | image_to_text | `no backend callable was provided` |
| [305](/root/laintas_cli/vision.py:305) | error candidate | image_to_text | `no OCR model is available — add one in the admin page under System -> API Keys, or ask an administrator to switch one back on` |
| [319](/root/laintas_cli/vision.py:319) | error candidate | image_to_text | `OCR returned no text. If this is a photograph rather than a document, image.describe is the tool that reads it.` |

### web_search.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [1667](/root/laintas_cli/web_search.py:1667) | message candidate | search / error | `missing 'query'` |
| [1688](/root/laintas_cli/web_search.py:1688) | message candidate | search / error | `no usable search engine: {detail}` |
| [1708](/root/laintas_cli/web_search.py:1708) | message candidate | search / error | `unavailable` |
| [1715](/root/laintas_cli/web_search.py:1715) | message candidate | search / message | `{name} skipped (recently failed, in cooldown)` |
| [1720](/root/laintas_cli/web_search.py:1720) | error candidate | search | `interrupted by user` |
| [1751](/root/laintas_cli/web_search.py:1751) | message candidate | search / error | `All search engines failed; {' \| '.join(error_parts)}` |
| [1826](/root/laintas_cli/web_search.py:1826) | error candidate | _guard_url | `a URL is required` |
| [1829](/root/laintas_cli/web_search.py:1829) | error candidate | _guard_url | `only http(s) URLs may be fetched (got '{parts.scheme or 'no scheme'}')` |
| [1831](/root/laintas_cli/web_search.py:1831) | error candidate | _guard_url | `URLs containing embedded credentials are not allowed` |
| [1834](/root/laintas_cli/web_search.py:1834) | error candidate | _guard_url | `not a valid URL: {raw!r}` |
| [1836](/root/laintas_cli/web_search.py:1836) | error candidate | _guard_url | `refusing to fetch {host!r}: private host name` |
| [1846](/root/laintas_cli/web_search.py:1846) | error candidate | _guard_url | `refusing to fetch {host}: non-public address (loopback/private/link-local/metadata are blocked)` |
| [1857](/root/laintas_cli/web_search.py:1857) | error candidate | _guard_url | `cannot resolve host {host!r}: {e}` |
| [1862](/root/laintas_cli/web_search.py:1862) | error candidate | _guard_url | `refusing to fetch {host!r}: resolves to non-public address {ip} (loopback/private/link-local/metadata are blocked)` |
| [1937](/root/laintas_cli/web_search.py:1937) | error candidate | _read_capped | `interrupted by user` |
| [2000](/root/laintas_cli/web_search.py:2000) | error candidate | _http_get | `interrupted by user` |
| [2025](/root/laintas_cli/web_search.py:2025) | message candidate | _http_get / error | `request failed ({err.value})` |
| [2032](/root/laintas_cli/web_search.py:2032) | message candidate | _http_get / error | `request failed: {e}` |
| [2039](/root/laintas_cli/web_search.py:2039) | message candidate | _http_get / error | `HTTP {resp.status_code} with no Location header` |
| [2065](/root/laintas_cli/web_search.py:2065) | message candidate | _http_get / error | `too many redirects (more than {_MAX_REDIRECTS})` |
| [2093](/root/laintas_cli/web_search.py:2093) | message candidate | fetch / error | `URL must start with http:// or https://` |
| [2104](/root/laintas_cli/web_search.py:2104) | message candidate | fetch / error | `[identity] {reason}` |
| [2108](/root/laintas_cli/web_search.py:2108) | message candidate | fetch / error | `[identity] identity_store is unavailable in this build` |
| [2114](/root/laintas_cli/web_search.py:2114) | message candidate | fetch / error | `}] {got['error']}` |
| [2122](/root/laintas_cli/web_search.py:2122) | message candidate | fetch / error | `[http_error] HTTP {status}` |
| [2346](/root/laintas_cli/web_search.py:2346) | message candidate | _RenderWorker.submit / error | `browser render timed out after {timeout:.0f}s` |
| [2512](/root/laintas_cli/web_search.py:2512) | message candidate | capture_identity / error | `identity_store is unavailable in this build` |
| [2514](/root/laintas_cli/web_search.py:2514) | message candidate | capture_identity / error | `no browser session is open to capture` |
| [2533](/root/laintas_cli/web_search.py:2533) | message candidate | capture_identity / error | `the browser holds no session to save — sign in first, then capture` |
| [2573](/root/laintas_cli/web_search.py:2573) | message candidate | probe_identity / error | `identity_store is unavailable in this build` |
| [2576](/root/laintas_cli/web_search.py:2576) | message candidate | probe_identity / error | `no identity named '{name}'` |
| [2581](/root/laintas_cli/web_search.py:2581) | message candidate | probe_identity / error | `identity '{name}' has no probe configured — set one so its freshness can be checked before a task depends on it` |
| [2645](/root/laintas_cli/web_search.py:2645) | error candidate | _blocked_result._check | `interrupted by user` |
| [2749](/root/laintas_cli/web_search.py:2749) | message candidate | _blocked_result / error | ` could not get readable content from {final_url}. Tried — {'; '.join(attempts)}` |

### workflow_engine.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [93](/root/laintas_cli/workflow_engine.py:93) | title/menu/help | _feature_dev_phases / description | `Understand what needs to be built` |
| [108](/root/laintas_cli/workflow_engine.py:108) | title/menu/help | _feature_dev_phases / description | `Understand existing codebase patterns` |
| [130](/root/laintas_cli/workflow_engine.py:130) | title/menu/help | _feature_dev_phases / description | `Resolve ambiguities before designing` |
| [144](/root/laintas_cli/workflow_engine.py:144) | title/menu/help | _feature_dev_phases / description | `Design implementation approach` |
| [169](/root/laintas_cli/workflow_engine.py:169) | title/menu/help | _feature_dev_phases / description | `Build the feature` |
| [184](/root/laintas_cli/workflow_engine.py:184) | title/menu/help | _feature_dev_phases / description | `Review code quality` |
| [205](/root/laintas_cli/workflow_engine.py:205) | title/menu/help | _feature_dev_phases / description | `Document what was accomplished` |
| [224](/root/laintas_cli/workflow_engine.py:224) | title/menu/help | _bug_fix_phases / description | `Reproduce and understand the bug` |
| [238](/root/laintas_cli/workflow_engine.py:238) | title/menu/help | _bug_fix_phases / description | `Identify the exact root cause` |
| [256](/root/laintas_cli/workflow_engine.py:256) | title/menu/help | _bug_fix_phases / description | `Implement the fix` |
| [269](/root/laintas_cli/workflow_engine.py:269) | title/menu/help | _bug_fix_phases / description | `Verify the fix works` |
| [287](/root/laintas_cli/workflow_engine.py:287) | title/menu/help | _code_review_phases / description | `Analyze code changes` |
| [306](/root/laintas_cli/workflow_engine.py:306) | title/menu/help | _code_review_phases / description | `Present findings` |
| [319](/root/laintas_cli/workflow_engine.py:319) | title/menu/help | _code_review_phases / description | `Provide improvement suggestions` |
| [525](/root/laintas_cli/workflow_engine.py:525) | error candidate | advance_phase | `Phase '{current.name}' requires explicit user confirmation. Use /workflow approve [summary].` |
| [530](/root/laintas_cli/workflow_engine.py:530) | error candidate | advance_phase | `Phase '{current.name}' advances when the agent signals completion. Use /workflow advance only for a manual override.` |

### workgraph.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [198](/root/laintas_cli/workgraph.py:198) | error candidate | _clean_metadata | `metadata must be an object` |
| [251](/root/laintas_cli/workgraph.py:251) | error candidate | set_active_work | `Work item not found: {work_id}` |
| [253](/root/laintas_cli/workgraph.py:253) | error candidate | set_active_work | `Work item not found in session: {work_id}` |
| [283](/root/laintas_cli/workgraph.py:283) | error candidate | create_work | `objective is required` |
| [378](/root/laintas_cli/workgraph.py:378) | error candidate | add_revision | `plan content is empty` |
| [382](/root/laintas_cli/workgraph.py:382) | error candidate | add_revision | `Work item not found: {work_id}` |
| [416](/root/laintas_cli/workgraph.py:416) | error candidate | submit_plan | `plan has no revision to submit` |
| [428](/root/laintas_cli/workgraph.py:428) | error candidate | submit_plan | `plan is incomplete: replace template placeholders and include context, architecture, implementation steps, risks, and tests` |
| [438](/root/laintas_cli/workgraph.py:438) | error candidate | submit_plan | `plan has no concrete numbered implementation steps` |
| [481](/root/laintas_cli/workgraph.py:481) | error candidate | review_snapshot | `Work item not found: {work_id}` |
| [484](/root/laintas_cli/workgraph.py:484) | error candidate | review_snapshot | `plan revision not found` |
| [496](/root/laintas_cli/workgraph.py:496) | error candidate | approve_plan | `work or revision not found` |
| [498](/root/laintas_cli/workgraph.py:498) | error candidate | approve_plan | `plan is not awaiting review: {work['status']}` |
| [500](/root/laintas_cli/workgraph.py:500) | error candidate | approve_plan | `plan changed after review; review the new revision` |
| [529](/root/laintas_cli/workgraph.py:529) | error candidate | begin_execution | `work is not approved` |
| [531](/root/laintas_cli/workgraph.py:531) | error candidate | begin_execution | `execution revision does not match approval` |
| [536](/root/laintas_cli/workgraph.py:536) | error candidate | begin_execution | `approved plan content changed` |
| [547](/root/laintas_cli/workgraph.py:547) | error candidate | update_work | `invalid work status: {updates['status']}` |
| [559](/root/laintas_cli/workgraph.py:559) | error candidate | update_work | `Work item not found: {work_id}` |
| [609](/root/laintas_cli/workgraph.py:609) | error candidate | create_step | `step subject is required` |
| [617](/root/laintas_cli/workgraph.py:617) | error candidate | create_step | `Work item not found: {work_id}` |
| [620](/root/laintas_cli/workgraph.py:620) | error candidate | create_step | `Parent step not found: {parent_id}` |
| [765](/root/laintas_cli/workgraph.py:765) | error candidate | add_dependency | `a step cannot depend on itself` |
| [771](/root/laintas_cli/workgraph.py:771) | error candidate | add_dependency | `dependency references a missing step` |
| [773](/root/laintas_cli/workgraph.py:773) | error candidate | add_dependency | `dependency would create a cycle` |
| [808](/root/laintas_cli/workgraph.py:808) | error candidate | update_step | `Step not found: {step_id}` |
| [811](/root/laintas_cli/workgraph.py:811) | error candidate | update_step | `invalid step status: {status}` |
| [816](/root/laintas_cli/workgraph.py:816) | error candidate | update_step | `invalid step transition: {row['status']} -> {status}` |
| [820](/root/laintas_cli/workgraph.py:820) | error candidate | update_step | `progress must be an integer` |
| [836](/root/laintas_cli/workgraph.py:836) | error candidate | update_step | `step is blocked by incomplete step(s): {', '.join((str(x[0]) for x in blockers))}` |
| [842](/root/laintas_cli/workgraph.py:842) | error candidate | update_step | `metadata must be an object` |
| [894](/root/laintas_cli/workgraph.py:894) | error candidate | import_session_steps | `Work item not found: {work_id}` |

### worktree_manager.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [152](/root/laintas_cli/worktree_manager.py:152) | error candidate | _changed_vs_head | `git status failed: {err}` |
| [183](/root/laintas_cli/worktree_manager.py:183) | error candidate | create_isolated_worktree | `{base_cwd} is not inside a git repository` |
| [187](/root/laintas_cli/worktree_manager.py:187) | error candidate | create_isolated_worktree | `cannot resolve HEAD: {err}` |
| [198](/root/laintas_cli/worktree_manager.py:198) | error candidate | create_isolated_worktree | `git worktree add failed: {err or out}` |


### version.py

| Line | Category | Context | Template / source expression |
|---|---|---|---|
| [8](/root/laintas_cli/version.py:8) | display constant candidate | __version__ | `1.32.5b2` |
| [9](/root/laintas_cli/version.py:9) | display constant candidate | RELEASE_NAME | `laintas-cli-beta v2` |
| [10](/root/laintas_cli/version.py:10) | display constant candidate | RELEASE_TAG | `laintas-cli-beta-v2` |
