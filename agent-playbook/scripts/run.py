#!/usr/bin/env python3
"""Run checklist: every step of a run ends done (with evidence) or skipped (with a reason), and finish refuses until then.

A run is the checklist of one recipe in playbook/state/recipes.json (one recipe per kind of job: reply, round,
post, news-share, release...), saved as playbook/state/runs/<started>-<recipe>.json. Each step names the flow
or part that does it. Nothing is silently skipped: a skip needs a reason, and finish prints every skip, pace
flag, QC override and QC warning the audit log recorded during the run, for the report.

  run.py recipes                                                    the recipes: id, title, when to use it
  run.py start --recipe reply [--note "owner: skip Reddit today"]   open a run (refused while another is open)
  run.py start --recipe news-share,post                             chain recipes into one run (steps news-share:start...)
                                                                    (--kind is the old name for --recipe)
  run.py status                                                     the open run's steps: todo, done, skipped
  run.py done STEP --note "evidence"                                close a step with what shows it was done
  run.py skip STEP --reason "why"                                   skip a step; "owner: ..." marks the owner's call,
                                                                    and "owner: next run" carries it into the next run of that recipe
  run.py finish                                                     close the run; exit 1 while any step is todo
  run.py abandon --reason "why"                                     close an unfinished run on purpose
  run.py check                                                      for a Stop hook: exit 2 with the open steps
  run.py list [-n 10]                                               recent runs
"""
import datetime as dt
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NEEDS = []
EFFECTS = ["files"]
RUNS = os.path.join(common.STATE_DIR, "runs")
RECIPES = os.path.join(common.STATE_DIR, "recipes.json")
AUDIT_LOG = os.path.join(os.path.dirname(common.STATE_DIR), "audit", "posts.jsonl")


def build_parser():
    ap = common.make_parser(__doc__, __file__, "PROG {recipes,start,status,done,skip,finish,abandon,check,list} ...")
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True  # no command is a usage error (exit 2), as in qc.py and audit.py
    sub.add_parser("recipes")
    s = sub.add_parser("start")
    s.add_argument("--recipe", "--kind", dest="recipe", required=True, help="a recipe id, or several joined by commas")
    s.add_argument("--note", default="")
    sub.add_parser("status")
    d = sub.add_parser("done")
    d.add_argument("step")
    d.add_argument("--note", required=True, help="evidence: what was checked or sent")
    k = sub.add_parser("skip")
    k.add_argument("step")
    k.add_argument("--reason", required=True)
    sub.add_parser("finish")
    a = sub.add_parser("abandon")
    a.add_argument("--reason", required=True)
    sub.add_parser("check")
    li = sub.add_parser("list")
    li.add_argument("-n", type=int, default=10)
    return ap


def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def clean(text):
    return " ".join(str(text or "").split())[:400]


def templates():
    try:
        return json.loads(common.read_text(RECIPES))["recipes"]
    except (ValueError, KeyError):
        common.die("%s is not a valid recipes file" % RECIPES)


def build_steps(ids, recipes):
    """The steps of one recipe, or of several chained (ids prefixed with the recipe: news-share:start)."""
    steps = []
    for rid in ids:
        for s in recipes[rid]["steps"]:
            step = {"id": ("%s:%s" % (rid, s["id"])) if len(ids) > 1 else s["id"], "text": s["text"], "status": "todo"}
            for k in ("flow", "part"):
                if s.get(k):
                    step[k] = s[k]
            steps.append(step)
    return steps


def run_files():
    return sorted(glob.glob(os.path.join(RUNS, "*.json")))


def load(path):
    """A run file, or None (with a warning naming it) when it can't be read or isn't a run: one damaged file
    must not stop every command, the Stop hook's check included."""
    try:
        with open(path, encoding="utf-8") as fh:
            run = json.load(fh)
    except (OSError, ValueError) as e:
        common.warn("skipped %s: %s" % (os.path.basename(path), getattr(e, "strerror", None) or e))
        return None
    if not isinstance(run, dict) or not isinstance(run.get("steps"), list) or not all(
            isinstance(s, dict) and "id" in s and s.get("status") in ("todo", "done", "skipped") for s in run["steps"]):
        common.warn("skipped %s: not a run file" % os.path.basename(path))
        return None
    run.setdefault("kind", run.get("recipe") or "?")
    run.setdefault("started", "")
    run.setdefault("state", "?")
    for s in run["steps"]:
        s.setdefault("text", "")
        if s["status"] == "skipped":
            s.setdefault("reason", "")
    return run


def save(path, run):
    os.makedirs(RUNS, exist_ok=True)
    try:
        common.write_atomic(path, json.dumps(run, ensure_ascii=False, indent=1) + "\n")
    except OSError as e:
        common.die("can't write %s: %s" % (path, e.strerror or e))


def open_run():
    """(path, run) of the open run, or (None, None). Fails closed (exit 2) when the newest run file can't be read:
    only the newest can still be open (start refuses while one is), so a damaged one may be an unfinished checklist
    and must not let check pass or a second run start."""
    for i, path in enumerate(reversed(run_files())):
        run = load(path)
        if run is None:
            if i == 0:
                common.die("the newest run file %s can't be read and may be the open run: repair it, or move it out of "
                           "%s to close it" % (os.path.basename(path), RUNS), common.EXIT_USAGE)
            continue
        if run.get("state") == "open":
            return path, run
    return None, None


def need_open():
    path, run = open_run()
    if not run:
        common.die("no open run: start one with run.py start --recipe <id> (run.py recipes lists them)")
    return path, run


def todo(run):
    return [s for s in run["steps"] if s["status"] == "todo"]


def flags(since):
    """What the report must explain, from `since` on: sends flagged as too soon after the previous one, sends logged
    with a QC override, and sends whose QC receipt carried warnings."""
    out = {"pace": [], "override": [], "warn": []}
    if not os.path.exists(AUDIT_LOG):
        return out
    try:
        start = common.parse_time(since)
    except ValueError:
        start = None
    with open(AUDIT_LOG, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict) or r.get("kind") != "item" or not logged_since(r.get("logged_at"), since, start):
                continue
            who = "%s %s %s" % (r.get("id", "?"), r.get("platform", ""), r.get("account", ""))
            if r.get("pace_flag"):
                out["pace"].append("%s: %s" % (who, r["pace_flag"]))
            if r.get("qc_override"):
                out["override"].append("%s: %s" % (who, r["qc_override"]))
            if r.get("qc_warn"):
                warns = r["qc_warn"] if isinstance(r["qc_warn"], list) else [r["qc_warn"]]
                out["warn"].append("%s: %s" % (who, ", ".join(map(str, warns))))
    return out


def logged_since(logged_at, since, start):
    """True when an audit record's logged_at is at or after the run's start. Compared as times, so a clock
    change during the run (a different UTC offset in the two stamps) can't hide a record."""
    if not isinstance(logged_at, str):
        return False
    if start is not None:
        try:
            return common.parse_time(logged_at) >= start
        except ValueError:
            pass
    return logged_at >= since


def carry_over(ids):
    """Steps the last closed run of the same recipe skipped with a reason starting "owner: next run". They wait for
    that recipe: a run of another recipe in between doesn't use them up. For a chain, each recipe's own leftovers."""
    out = []
    for rid in ids:
        for path in reversed(run_files()):
            run = load(path)
            if not run or run.get("state") not in ("finished", "abandoned"):
                continue
            parts = (run.get("recipe") or run.get("kind") or "").split("+")
            if rid not in parts:
                continue
            prefix = rid + ":" if len(parts) > 1 else ""
            out += [{"id": s["id"], "text": s["text"], "kind": run["kind"], "started": run["started"]}
                    for s in run["steps"] if s["status"] == "skipped" and s["id"].startswith(prefix)
                    and s.get("reason", "").lower().startswith("owner: next run")]
            break
    return out


def show(run):
    print("%s run, started %s%s" % (run["kind"], run["started"], (" (" + run["note"] + ")") if run.get("note") else ""))
    for s in run["steps"]:
        mark = {"todo": "[ ]", "done": "[x]", "skipped": "[-]"}[s["status"]]
        extra = s.get("note") or s.get("reason") or ""
        where = "  (flow %s)" % s["flow"] if s.get("flow") else "  (part %s)" % s["part"] if s.get("part") else ""
        print("%s %-16s %s%s%s" % (mark, s["id"], s["text"], where if s["status"] == "todo" else "", ("\n      " + extra) if extra else ""))
    left = todo(run)
    print("\n%d done, %d skipped, %d todo" % (sum(s["status"] == "done" for s in run["steps"]),
                                             sum(s["status"] == "skipped" for s in run["steps"]), len(left)))


def step_of(run, sid):
    for s in run["steps"]:
        if s["id"] == sid:
            return s
    common.die("no step %r in this run; steps: %s" % (sid, ", ".join(s["id"] for s in run["steps"])))


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    common.safe_stdio()
    if a.cmd == "recipes":
        for rid, r in sorted(templates().items()):
            print("%-14s %s\n%-14s when: %s" % (rid, r.get("title", ""), "", r.get("when", "")))
        print("\npython playbook/pb.py catalog <recipe> shows every flow, part, script and QC filter a recipe uses.")
    elif a.cmd == "start":
        recipes = templates()
        ids = [x.strip() for x in a.recipe.split(",") if x.strip()]
        bad = [x for x in ids if x not in recipes]
        if not ids or bad:
            common.die("unknown recipe %s; recipes: %s (run.py recipes)" % (", ".join(map(repr, bad)) or "''", ", ".join(sorted(recipes))))
        path, run = open_run()
        if run:
            common.die("a %s run is still open (%s): finish or abandon it first" % (run["kind"], os.path.basename(path)))
        name = "+".join(ids)
        run = {"kind": name, "recipe": name, "title": " + ".join(recipes[x].get("title", "") for x in ids), "started": now(),
               "note": clean(a.note), "state": "open", "steps": build_steps(ids, recipes)}
        carried = carry_over(ids)
        if carried:
            run["carried_over"] = carried
        path = os.path.join(RUNS, "%s-%s.json" % (dt.datetime.now().strftime("%Y%m%d-%H%M%S"), name))
        save(path, run)
        show(run)
        if carried:
            print("\nLeft for this run by the last one (skipped with \"owner: next run\"): do these first")
            for c in carried:
                print("- %s (%s run %s): %s" % (c["id"], c["kind"], c["started"][:10], c["text"]))
    elif a.cmd == "status":
        path, run = need_open()
        show(run)
    elif a.cmd in ("done", "skip"):
        path, run = need_open()
        s = step_of(run, a.step)
        text = clean(a.note if a.cmd == "done" else a.reason)
        if not text:
            common.die("a %s needs words" % ("note" if a.cmd == "done" else "reason"), common.EXIT_USAGE)
        s.update({"status": "done" if a.cmd == "done" else "skipped", "at": now()})
        s.pop("note", None)
        s.pop("reason", None)
        s["note" if a.cmd == "done" else "reason"] = text
        save(path, run)
        print("%s %s: %s" % (s["status"], s["id"], text))
    elif a.cmd == "finish":
        path, run = need_open()
        left = todo(run)
        if left:
            show(run)
            common.die("not finished: %d step(s) still todo. Do them, or skip each with a reason." % len(left))
        run.update({"state": "finished", "finished": now()})
        save(path, run)
        skipped = [s for s in run["steps"] if s["status"] == "skipped"]
        print("finished %s run (%s)" % (run["kind"], os.path.basename(path)))
        print("\nSkipped (put these in the report):" if skipped else "\nNothing skipped.")
        for s in skipped:
            who = "owner's call" if s["reason"].lower().startswith("owner") else "agent's call: tell the owner"
            print("- %s: %s (%s)" % (s["id"], s["reason"], who))
        found = flags(run["started"])
        for key, head, none in (("pace", "Pace flags (explain each in the report):", "No pace flags."),
                                ("override", "QC overrides (say why each went out anyway):", "No QC overrides."),
                                ("warn", "QC warnings on what went out:", "No QC warnings.")):
            print("\n" + (head if found[key] else none))
            for f in found[key]:
                print("- " + f)
    elif a.cmd == "abandon":
        path, run = need_open()
        run.update({"state": "abandoned", "finished": now(), "abandon_reason": clean(a.reason)})
        save(path, run)
        print("abandoned %s run: %s; open steps: %s" % (run["kind"], run["abandon_reason"], ", ".join(s["id"] for s in todo(run)) or "none"))
    elif a.cmd == "check":
        path, run = open_run()
        left = todo(run) if run else []
        if left:
            print("agent-playbook: a %s run is open with %d step(s) todo: %s. Do them, skip each with a reason "
                  "(run.py skip STEP --reason ...), or abandon the run with a reason." % (run["kind"], len(left), ", ".join(s["id"] for s in left)),
                  file=sys.stderr)
            return 2
    elif a.cmd == "list":
        for path in run_files()[-a.n:] if a.n > 0 else []:
            run = load(path)
            if not run:
                continue
            print("%-28s %-8s %-10s %d todo, %d skipped" % (os.path.basename(path), run["kind"], run["state"], len(todo(run)),
                                                           sum(s["status"] == "skipped" for s in run["steps"])))
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
