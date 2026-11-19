# Agent Playbook

The playbook as a visual, editable canvas: every flow and rule, the QC gates, the accounts registry, the shared folders and a library of reusable parts. Agents read the same files the canvas edits, so a change on the canvas changes what the agent does next run.

## Open it

- **Windows:** the "Agent Playbook" shortcut on the Desktop or in the Start menu. To add it on a new machine: `python playbook/install_shortcut.py` (`--remove` takes it away).
- **Any OS:** `python playbook/launch.pyw` starts the local server (127.0.0.1:47321, this machine only) and opens the page in your browser. Another port: `--port N` or `AGENT_PLAYBOOK_PORT=N`. `--no-browser` only starts the server.
- **View only, no install:** run `python playbook/pb.py export`, then publish `playbook.html` and `store.json` together. Without the local server the page opens read-only.

## Use it

- Drag the board to move, scroll to zoom, **F** to fit. A wide flow opens at a readable zoom from its left edge; **F** again shows the whole flow. Click a node for its details; double-click (or **Enter**) to edit.
- Drag a node's right-hand dot onto another node to connect them. Drop it on empty board to add a connected step.
- **Shift**-drag selects a group. **Ctrl+C / Ctrl+V** copies nodes, across flows too. **Ctrl+D** duplicates, **Ctrl+Z** undoes.
- **Duplicate flow** (the copy icon in the top bar) copies a whole flow, for example to set up a new account or project.
- **Parts are referenced, not copied.** "From the parts library" and **Swap part** make the step follow the part: its title, summary, details and checklist are read from the part, so a rule is written once and edited in one place (**Edit part**). **Detach** copies the text into the step and stops following. **Save as part** turns a step into a part and makes the step follow it.
- **Where this came from:** a node's `origin` and sources are shown collapsed under its details. They are kept on every save and never printed by `pb.py show`.
- **Accounts:** add, duplicate, delete, or place an account on any canvas. Each platform flow shows its accounts in the top bar. Deleting an account turns the steps that showed it into ordinary steps that keep their text.
- **History** lists everything posted (full text, link, status, updates), and every change to the playbook. The clock button on a flow restores an earlier version; a deleted flow can be restored from the Playbook changes list.
- **Deleting needs a reason.** Deleting a step, connection, flow, account, folder or part asks why, and the reason goes into the change history next to what was removed. The server refuses a delete without one, so agents give it too: `pb.py delete <col> <id> --reason "…"`, `pb.py put … --reason "…"` when a new version drops steps or connections, and `audit.py update <id> --status deleted --reason "…"` for posts. Undo and version restores record their own reason.
- **Folders** are shared: define a folder once and attach it to any step. Each one opens in the file manager, copies its path, or opens in VS Code, and lists the flows that use it.
- **Two editors, no lost work.** Every save carries the revision it started from. If an agent changed a flow while it was open on the canvas (or the other way round), the later save is refused: the canvas loads the newer version and says so, and `pb.py put` exits 1 with "changed since revision …". Nothing is overwritten silently, and every replaced version is in history.
- **Server stopped?** A banner says so and keeps your changes; they are sent as soon as the server answers again ("Try again" checks at once). The page warns before you close a tab with unsaved work.

## Files

| Path | What it is |
|---|---|
| `store/flows/*.json` | One file per flow: nodes (kind, title, summary, details, checklist, folders, links, sources, origin, position) and edges |
| `store/accounts/*.json` | The accounts registry (never passwords, codes or emails) |
| `store/folders/*.json` | Shared folders and key files |
| `store/parts/*.json` | Reusable steps; a node points at one with `"part": "<id>"` |
| `store/.trash/` | Anything deleted, kept with a timestamp |
| `audit/posts.jsonl` | Append-only audit log of everything posted, scheduled, edited or deleted (`audit.py`) |
| `history/` | `changes.jsonl` (who changed what, when, why) and the replaced version of every saved document, one per change |
| `lib/` | The engine: the id rule, the schema, the store (lock, revisions, atomic canonical writes), history, lint, the audit log model |
| `pb.py` | Command line for agents: `list`, `show <flow> [--json]`, `search`, `accounts`, `folders`, `parts`, `edited`, `check`, `rev`, `export`, `normalize`, and the versioned writes `put`, `delete`, `versions`, `restore`, `changes` |
| `audit.py` | Command line for the audit log: `add`, `update`, `recent`, `today`, `list`, `show`, `search`, `stats`, `import` |
| `server.py`, `launch.pyw`, `install_shortcut.py` | Local server, launcher, shortcut installer (Python standard library only). The server writes `server.log` |
| `app/playbook.src.html` | Page source; `python app/build_page.py` rebuilds `playbook.html` (`--check` fails when it is stale) |

## Rules the engine enforces

- **Schema.** Every document is checked before it is saved; an unknown key, a bad kind, an over-long field or a NaN is refused with a message that names the file and the field. `python pb.py check` runs the schema and the lint rules over the whole store, prints `file: path: level [rule] message`, and exits 1 on any error (`--strict`: on any warning too; `-w` lists the warnings).
- **Ids** are lower-case words joined by hyphens, at most 64 characters, and never leave their collection folder. The server and `pb.py` use the same rule.
- **One stored shape.** Files are UTF-8 with LF endings, keys in a fixed order, a final newline, on every OS, whoever wrote them. `python pb.py normalize` rewrites a store in that form.
- **Exit codes** for `pb.py` and `audit.py`: 0 done, 1 refused or failed (the reason is on stderr), 2 the command line was wrong.

The server binds to 127.0.0.1 only, checks the Host header, refuses to be framed, and needs a per-run token for every write or "open folder" request, so other websites can't drive it. "Open folder" only shows an absolute local folder or selects a file in the file manager; it never runs anything.
