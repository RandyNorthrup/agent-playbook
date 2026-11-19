---
name: agent-playbook
description: Run the owner's repeatable operation from a playbook of step-by-step flows the owner edits on a canvas. The shipped demo covers social work for the owner's own app (reply passes on X, Bluesky and Reddit, original posts from a post bank, release announcements, banners and art), with a QC gate on every draft, an audit log of everything sent, and run checklists. Use when asked to "do a reply run", "do a round", "post something", "share this news", "announce the release", "make a banner", "set up on a new machine", "remember this rule" or "open the playbook canvas".
---

# Agent Playbook

Everything this skill knows is in its playbook: flows an agent follows step by step, which the owner edits on a
canvas. This file only says how to read it. Run every command from this folder.

## Every task

1. Pick the recipe for the job: `python scripts/run.py recipes` (reply, round, post, news-share, release, banner,
   lesson, onboard...). `python playbook/pb.py catalog <recipe>` shows its checklist and every flow, part, script
   and QC filter it uses.
2. Open its checklist: `python scripts/run.py start --recipe <id>` (chain jobs: `--recipe a,b`). Each step names
   the flow or part that does it, and ends done with evidence (`run.py done <step> --note "..."`) or skipped with a
   reason (`run.py skip <step> --reason "..."`). `python scripts/run.py finish` refuses while any step is open;
   its list of skips, pace flags and QC overrides goes into the report.
3. `python playbook/pb.py show <flow>` for each flow a step names, then follow it as written. A `[GOTO]` line ends
   in the id of the next flow to open. `python playbook/pb.py list` shows every flow, by group.
4. Read `ground-rules` every session. Its rules override convenience.
5. A step names its command. Arguments: `python scripts/<name>.py --help`, or `scripts/INDEX.json` and
   `media/INDEX.json` for all of them.
6. Every draft passes `python playbook/qc.py check ...` before it is sent (part `part-qc-check`): BLOCK means fix
   it, ASK means answer with `--ok <id>="evidence"`, WARN goes in the report.
   `python playbook/audit.py gap --platform P --account A --action reply` right before each send: WAIT means wait.
7. Log each post, reply, comment, article and video right after it goes out (flow `audit`).
   `audit.py add` refuses a reply or post whose words have no passing QC receipt.

## Look things up

| Need | Command |
|---|---|
| Which pieces a kind of job uses | `python playbook/pb.py catalog [recipe]` |
| What each kind of piece is for, where a lesson goes | `python playbook/pb.py catalog --pieces` |
| Which QC filters apply, and why | `python playbook/qc.py filters [--platform P --action A --recipe R]`, `qc.py explain <id>` |
| Where something is mentioned | `python playbook/pb.py search <text>` |
| The owner's handles and channels | `python playbook/pb.py accounts [platform]` |
| Content folders and key files | `python playbook/pb.py folders` |
| What the owner changed on the canvas | `python playbook/pb.py edited [YYYY-MM-DD]` |
| What went out today, against the caps | `python playbook/audit.py today` |
| The last posts in one place | `python playbook/audit.py recent --platform P --account A [--place PLACE]` |

## Change the playbook

- A lasting lesson goes into the piece it belongs to, never into agent memory: recipe `lesson` and flow
  `playbook` say which. A mistake a script can catch becomes a QC filter in `playbook/qc/filters.json`, with the
  text that slipped through as its bad example.
- Flows are single jobs that chain (a platform hub, then its reply and post flows). A flow that grows a second job
  gets split: `python playbook/pb.py split <flow> <plan.json> --reason "why"`.
- `python playbook/pb.py check --strict` must exit 0 after any change (it runs the QC selftest and the recipe checks).
- Never edit files in `playbook/store/` by hand: use the canvas or `pb.py put`, which keep a version of every
  change and refuse a stale save. Never overwrite an owner edit without asking.
- The owner opens the canvas with the "Agent Playbook" shortcut or `python playbook/launch.pyw`.

## Layout

| Folder | Holds |
|---|---|
| `playbook/` | The flows, accounts, folders and parts (`store/`), the audit log and QC receipts (`audit/`), the QC filters and lists (`qc/`), the recipes, post bank and groups pacing clock (`state/`), `pb.py`, `audit.py`, `qc.py`, the canvas |
| `scripts/` | Helpers the flows call: standard library only, silent, `--help` on each |
| `media/` | Image and video tools the flows call: `--help` on each, `media/INDEX.json` for all of them; a tool that needs Pillow, numpy or ffmpeg says so and how to install it |
| `templates/` | The carousel and banner pages |
