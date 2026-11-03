# issues

Laintas Issues in the CLI.

Pick an Issues preset with `/model` and ask: the question is posted to the
Issues board, and its answers keep arriving after the reply that posted it.

- `/issues` shows the answers to your latest question, `/issues list` your
  latest questions, `/issues <id>` one question.
- To pick the answer your conversation continues with, say so in the
  conversation ("pick answer 2"). The model calls `issues.pick` and you
  confirm. Picking moves no money: each answer was already paid from the
  question's pool as it landed. The picked answer becomes the reply.

The extension declares the `account` capability: it calls Laintas as you
through the host, which keeps your session and asks before any change.

Personal tasks stay in the terminal:

- `/issues <id>` shows applicants, their quotes, the squad, progress and delivery evidence.
- `/issues choose <id>` opens an applicant picker; add an application ID to select it directly.
  The host confirms the quote and sharing of the sealed context before starting.
- `/issues accept <id>` shows the checklist and evidence, then asks to confirm settlement.
- `/issues reject <id> <item> <reason>` rejects a specific checklist item and opens the appeal window.
Private chat lives on the Issues site only; this extension has no chat
commands. Applying for tasks and working a run also happen on the Issues
site or in Helpwow - the extension covers the asker's side.
