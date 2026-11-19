#!/usr/bin/env python3
"""Facebook group pacing guard: which groups may take a post now, and record a post just made.

groups.tsv columns (tab-separated, # comments allowed):
  slug  name  mode  min_hours  last_post_utc
mode:
  approval  admins approve each post -> min_hours since that group's own last
            post (default 6). Approval is per group: every group has its own
            admins and queue, so one group never waits on another
  open      posts go live -> min_hours since last post (default 4)
  own       the owner's group -> min_hours (default 1)
  strict    like open, but organic/non-promo posts only
  skip      never post (e.g. an old post is stuck pending)

  pace.py [groups.tsv]                 list groups and whether each is eligible now (READY, wait or skip)
  pace.py [groups.tsv] --mark <slug>   record a post to <slug> at the current time (UTC)
The groups file is the one named first, else --groups FILE, else $AGENT_PLAYBOOK_GROUPS, else
playbook/state/groups.tsv in the skill folder.

A last_post_utc with no time zone is read as UTC. A row with an unknown mode, a bad min_hours or a bad
last_post_utc is listed as "wait" with the reason, and the exit code is 1. --mark rewrites the file in one
step (temp file, then rename), so an interrupted run can't leave it half-written.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = []
EFFECTS = ["files"]
DEFAULTS = {"approval": 6.0, "open": 4.0, "own": 1.0, "strict": 4.0, "skip": 1e9}
WIDTH = 5


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG [GROUPS_TSV] [--groups FILE] [--mark SLUG]")
    ap.add_argument("groups_file", nargs="?", metavar="GROUPS_TSV")
    ap.add_argument("--groups", metavar="FILE", help="the groups file (default: $AGENT_PLAYBOOK_GROUPS, else playbook/state/groups.tsv)")
    ap.add_argument("--mark", metavar="SLUG", help="record a post to this group now")
    return ap


def load(path):
    """[(kind, value)] with every data row padded to 5 columns; comment and blank lines kept as they are."""
    return [(kind, value + [""] * (WIDTH - len(value)) if kind == "row" else value)
            for kind, value in common.tsv_lines(common.read_text(path))]


def dump(rows):
    return "".join(("\t".join(value) if kind == "row" else value) + "\n" for kind, value in rows)


def status(cols, now):
    """(ready, mode, need_hours, hours_since_last or None, problem or "") for one row."""
    mode = cols[2].strip() or "open"
    if mode not in DEFAULTS:
        return False, mode, 0.0, None, "unknown mode %r (use %s)" % (mode, ", ".join(DEFAULTS))
    need = DEFAULTS[mode]
    if cols[3].strip():
        try:
            need = float(cols[3])
        except ValueError:
            return False, mode, 0.0, None, "bad min_hours %r" % cols[3]
    since = None
    if cols[4].strip():
        try:
            since = common.hours_since(cols[4], now)
        except ValueError:
            return False, mode, need, None, "bad last_post_utc %r" % cols[4]
    return mode != "skip" and (since is None or since >= need), mode, need, since, ""


def mark(path, rows, slug, now):
    for kind, cols in rows:
        if kind == "row" and cols[0] == slug:
            cols[4] = now.isoformat(timespec="minutes")
            try:
                common.write_atomic(path, dump(rows))
            except OSError as e:
                common.die("can't write %s: %s" % (path, e.strerror or e))
            print("marked", cols[0], cols[4])
            return 0
    common.die("slug not found in %s: %s" % (path, slug))


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.groups_file and a.groups:
        ap.error("name the groups file once: first, or with --groups")
    path = common.state_file("groups.tsv", a.groups_file or a.groups, "AGENT_PLAYBOOK_GROUPS")
    common.safe_stdio()
    rows = load(path)
    now = common.now_utc()
    if a.mark is not None:
        return mark(path, rows, a.mark, now)
    problems = 0
    for kind, cols in rows:
        if kind != "row":
            continue
        ok, mode, need, since, problem = status(cols, now)
        problems += bool(problem)
        why = "FIX: " + problem if problem else ("" if since is None else f"{since:.1f}h since last")
        if mode == "skip" and not problem:  # never eligible: no hours to show
            print(f"skip  {mode:8} {cols[0]:28} never post  {cols[1]}")
            continue
        print(f"{'READY' if ok else 'wait '} {mode:8} {cols[0]:28} need {need:g}h  {why}  {cols[1]}")
    if problems:
        common.die("%d row%s in %s need fixing (listed as wait)" % (problems, "" if problems == 1 else "s", path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
