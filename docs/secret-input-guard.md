# Local credential checks

Chat input is checked on this machine with detect-secrets 1.5.0 format detectors
and application rules for Laintas keys, bearer tokens, JWTs, and English/Chinese
credential assignments. Only the detectors' `analyze_string` API is used: no
online verification, samples, or raw findings are recorded.

Detected input is refused before REPL history, conversation storage, event
publication, or execution. Supplementary input, remote chat, Agents Mode, ACP,
and execute mode also check admission. Model request content has a final check
covering history and tool results. Authentication headers are outside this scan.

The English refusal is:

> Message not sent: possible password or API key detected. Remove it and try again. Use /password for credentials or /model for model keys.

Missing dependencies or detector failures refuse sending with:

> Message not sent: local security check unavailable. Please try again.

Explicit local `/password` and `/model add` management retain their private input
paths and history exclusions. No automatic credential import or filling is added.
Generic hashes, addresses, and email are not treated as credentials just because
they have high entropy. Standalone passwords without identifying context may be
missed; binary image/audio content is not inspected.

Validation: `venv/bin/python -m pytest tests/test_secret_guard.py`.
